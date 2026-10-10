"""What Ysildir teaches an agent: the instructions, the guides, the prompts and the guide resources.

Every lesson lives in a file that ships with the app it describes, and is read when it's asked for,
so Ysildir keeps no copy that could drift:
- the initialize instructions: apps/ysildir/instructions.md, without each line that names a tool
  that is switched off;
- asgard_guide(topic): baldur (apps/baldur/prompts/agent-guide.md), muninn
  (asgard/muninn/agent-guide.md), review (apps/baldur/prompts/review.md), and tools, which is
  written here from the switches;
- the same three guides as resources, asgard://guides/<topic>, while asgard_guide is on;
- the prompts record-estimate, my-day, review-day and what-changed, each registered only when the
  tools it names are on.
"""
import datetime as dt
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, NamedTuple, Optional, Tuple

from asgard import muninn
from asgard.muninn import keys

from . import Refused, parse_day
from .config import ALWAYS_ON, NAMES, SWITCHES, Config
from .models import DATE, Topic

HERE = Path(__file__).resolve().parent
APPS = HERE.parent.parent
INSTRUCTIONS = HERE.parent / "instructions.md"
GUIDES: Dict[str, Path] = {
    "baldur": APPS / "baldur" / "prompts" / "agent-guide.md",
    "muninn": Path(muninn.__file__).resolve().parent / "agent-guide.md",
    "review": APPS / "baldur" / "prompts" / "review.md",
}
GUIDE_TITLES = {"baldur": "Baldur for AI agents: recording estimates, explaining a day",
                "muninn": "Muninn for AI agents: answering questions about the person's work",
                "review": "Baldur's review prompt"}
MAX_INSTRUCTIONS = 2000
_TOOL = re.compile(r"\b(" + "|".join(NAMES) + r")\b")


def version(path: Path) -> str:
    """The version on a guide's first line: <!-- baldur-agent-2 --> gives baldur-agent-2."""
    first = path.read_text(encoding="utf-8").splitlines()[0]
    return first.replace("<!--", "").replace("-->", "").strip()


def named(text: str) -> List[str]:
    """The Ysildir tools a line of text names."""
    return _TOOL.findall(text)


def instructions(config: Config) -> str:
    lines = INSTRUCTIONS.read_text(encoding="utf-8").splitlines()
    if lines and lines[0].startswith("<!--"):
        lines = lines[1:]                                   # the version line is for maintainers
    return "\n".join(line for line in lines if all(config.on(t) for t in named(line))).strip()


def tools_guide(config: Config) -> str:
    out = ["# Ysildir's tools on this computer", "",
           "The person switches each tool on or off, and their ISSO approves what each one sends you. A tool "
           "that's off isn't available to you: tell the person it exists, and don't look for a way around it. To "
           "turn one on, the person runs `ysildir.cmd tools --on NAME`, then restarts the MCP server in their AI "
           "client.", "",
           "| Tool | Switch | Sends you | Without the tool |", "| --- | --- | --- | --- |"]
    out += [f"| `{s.name}` | {'on' if config.on(s.name) else 'off'} | {s.sends} | `{s.fallback}` |"
            if s.fallback.startswith("baldur.cmd") else
            f"| `{s.name}` | {'on' if config.on(s.name) else 'off'} | {s.sends} | {s.fallback} |" for s in SWITCHES]
    if config.problem:
        out += ["", f"The switches file ({config.path.name}) couldn't be used: {config.problem}. Every tool but "
                f"`{ALWAYS_ON}` is off until the person fixes the file or deletes it (`ysildir.cmd tools --list` "
                "shows what's wrong)."]
    if config.unknown:
        out += ["", f"The switches file names tools Ysildir doesn't have, which are ignored: "
                f"{', '.join(config.unknown[:10])}."]
    out += ["", "`baldur_review_pack` and `baldur_submit_review` also need Baldur's AI review on: the person runs "
            "`baldur.cmd setup --set review_mode=metadata` once their ISSO agrees.", "",
            "Never a tool, whatever is switched on: approving, rejecting or changing time, real hours, calibration, "
            "Baldur's settings, keys or repositories, collecting, and posting to Jira. Give the person the command "
            "instead, such as `baldur.cmd approve --date 2026-10-01 --ai 3f2a9c1d` (the id names the figures "
            "they saw).",
            "",
            "When MCP isn't available at all, the command-line column still works, and AI review works through "
            "the clipboard: `baldur.cmd ai pack DATE`, then `baldur.cmd ai review DATE ANSWER.json`."]
    return "\n".join(out) + "\n"


def guide(topic: str, config: Config) -> str:
    if topic == "tools":
        return tools_guide(config)
    path = GUIDES.get(topic)
    if path is None:
        raise Refused(f"There's no {topic} guide; the topics are baldur, muninn, review and tools.")
    return path.read_text(encoding="utf-8")


def guide_tool(config: Config) -> Callable[..., str]:
    """asgard_guide for this server's switches."""
    def asgard_guide(topic: Topic) -> str:
        return guide(topic, config)
    return asgard_guide


# --------------------------------------------------------------------------
# Prompts: the common workflows, as the person's slash commands
# --------------------------------------------------------------------------

def _day_arg(text: Optional[str]) -> str:
    if text and not re.fullmatch(DATE, text.strip()):
        raise Refused(f"{text[:40]!r} isn't a day; give one like 2026-10-01, or leave it out for today.")
    return parse_day(text.strip() if text else None).isoformat()


def _since_arg(text: Optional[str]) -> str:
    if not text:
        midnight = dt.datetime.combine(dt.date.today(), dt.time()).astimezone()
        return midnight.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    value = text.strip()
    if len(value) > 40 or not re.fullmatch(r"[0-9T:.+\-Z]+", value):
        raise Refused(f"{text[:40]!r} isn't a time or a day; give one like 2026-10-08T04:00:00Z or 2026-10-08.")
    return value


def _steps(config: Config, intro: str, steps: List[str], outro: str = "") -> str:
    """The prompt's text: its numbered steps, without any step that names a tool that's off."""
    kept = [s for s in steps if all(config.on(t) for t in named(s))]
    return "\n".join([intro] + [f"{n}. {s}" for n, s in enumerate(kept, 1)] + ([outro] if outro else []))


class Prompt(NamedTuple):
    name: str
    title: str
    description: str
    needs: Tuple[str, ...]                          # registered only when all of these are on
    make: Callable[[Config], Callable[..., str]]


def _record_estimate(config: Config) -> Callable[..., str]:
    def record_estimate(key: Optional[str] = None) -> str:
        if key:
            try:
                key = keys.normalize_key(key)
            except ValueError as exc:
                raise Refused(str(exc)) from None
        ticket = key or "(leave key out: Baldur knows each commit's ticket from its branch)"
        return _steps(config, "Record in Baldur your estimate of my working time on the change we just finished.", [
            "Get the full SHAs of the commits you made with me for it today (git log --since=midnight --format=%H, "
            "then pick ours).",
            "Estimate my time: reading, prompting you, reviewing and testing; not your running time. Use clock times "
            "only if you saw them, and give a range when you're unsure.",
            f"Call baldur_record_estimate with a report: agent, guide {version(GUIDES['baldur'])}, commits, key "
            f"{ticket}, minutes, minutes_low, confidence and a one-sentence summary.",
            "Tell me the report's id, and that nothing changes until I approve. If Baldur refuses the report, show me "
            "its message."])
    return record_estimate


def _my_day(config: Config) -> Callable[..., str]:
    def my_day(date: Optional[str] = None) -> str:
        day = _day_arg(date)
        return _steps(config, f"Show me how {day} looks in Baldur.", [
            f"Call baldur_day for {day}.",
            "For each ticket, give Baldur's estimate and what I've approved.",
            "If there are AI-assisted figures, say what they change and why, and give me the commands to take them or "
            "keep the estimate. Don't run them.",
            "List the day's flags in plain words."])
    return my_day


def _review_day(config: Config) -> Callable[..., str]:
    def review_day(date: Optional[str] = None) -> str:
        day = _day_arg(date)
        return _steps(config, f"Review Baldur's estimate for {day}.", [
            f"Call baldur_review_pack for {day}.",
            "Follow the prompt it returns exactly, with its pack as the input, and produce only the JSON reply.",
            f"Call baldur_submit_review with date {day} and that reply.",
            "Show me what changed, and the command to take it. Don't run it."])
    return review_day


def _what_changed(config: Config) -> Callable[..., str]:
    def what_changed(since: Optional[str] = None) -> str:
        start = _since_arg(since)
        return _steps(config, f"What changed in Asgard since {start}?", [
            f"Call muninn_what_changed from {start}.",
            "Group it by app and by ticket, newest first.",
            "Use muninn_issue for any ticket you need to name."],
            "Say where each number comes from: Baldur's estimate, an approved figure, or time logged in Jira.")
    return what_changed


PROMPTS = (
    Prompt("record-estimate", "Record my time in Baldur", "Record your estimate of my working time on the change we "
           "just finished, in Baldur.", ("baldur_record_estimate",), _record_estimate),
    Prompt("my-day", "How does my day look?", "Show how a day looks in Baldur: the estimate, what I approved, and "
           "any AI-assisted figures. Takes a day, YYYY-MM-DD (default today).", ("baldur_day",), _my_day),
    Prompt("review-day", "Review a day's estimate", "Review Baldur's estimate for a day with Baldur's review prompt, "
           "and store the checked result. Takes a day, YYYY-MM-DD (default today).",
           ("baldur_review_pack", "baldur_submit_review"), _review_day),
    Prompt("what-changed", "What changed?", "What changed in Asgard since a time. Takes a time with its zone or a "
           "day (default: today's midnight).", ("muninn_what_changed",), _what_changed),
)


def _reader(path: Path) -> Callable[[], str]:
    def read() -> str:
        return path.read_text(encoding="utf-8")
    return read


def register(server: Any, config: Config, wrap: Callable[[Callable[..., str]], Callable[..., str]] = lambda f: f
             ) -> None:
    """Add the guide resources, and the prompts whose tools are on (each through wrap), to an MCPServer."""
    if config.on("asgard_guide"):
        for topic, path in GUIDES.items():
            server.resource(f"asgard://guides/{topic}", name=f"{topic}-guide", title=GUIDE_TITLES[topic],
                            description=f"{GUIDE_TITLES[topic]} ({version(path)}).",
                            mime_type="text/markdown")(_reader(path))
    for p in PROMPTS:
        if all(config.on(t) for t in p.needs):
            server.prompt(name=p.name, title=p.title, description=p.description)(wrap(p.make(config)))
