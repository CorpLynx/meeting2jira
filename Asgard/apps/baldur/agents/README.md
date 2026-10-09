# Baldur for AI coding agents

These files teach an AI coding agent (Kiro first; the same text works for Copilot or Claude Code)
to record its estimate of your working time in Baldur, and stop it from making your decisions.
They're the AI-assisted method's front door: the agent records, Baldur checks, you approve.

| File | What it does |
| --- | --- |
| `../prompts/agent-guide.md` | The full guide (version `baldur-agent-2`): when to record, what "minutes" means, the JSON form, the rules. `baldur.cmd ai guide` prints it; Ysildir serves it as its MCP instructions |
| `kiro-steering.md` | The short version, as a Kiro steering file (`inclusion: auto`: loaded when a change is done or time comes up) |
| `hooks/guard_baldur.py` | PreToolUse guard: blocks `approve`, `reject`, `change`, `actual`, `calibrate --accept`, `setup` changes, key and repository changes, `schedule`, the GitHub token, Ysildir's `tools --on/--off` and `setup`, and anything touching `muninn.db`, another database in Asgard's folder, `baldur.json` or `ysildir.json`. It reads commands quoted inside others (`cmd /c "..."`, `powershell -Command "..."`, `bash -lc "..."`, `Start-Process -ArgumentList`) and a bare `cli.py` run from Baldur's folder, but not the free text of a commit message or a report's summary |
| `hooks/after_commit.py` | PostToolUse reminder: after a `git commit`, one line telling the agent how to record its estimate |

## Installing into a workspace

```
baldur.cmd ai kiro --into C:\src\my-service
```

writes `.kiro/steering/baldur-estimates.md` and `.kiro/hooks/baldur-guard.json` and
`baldur-after-commit.json` into that folder, with this computer's Python and Asgard paths filled
in. It doesn't overwrite files that are already there unless you add `--force`. Open the
workspace in Kiro and check the Agent Steering and Agent Hooks panels list them.

To cover every workspace, copy the steering file into `%USERPROFILE%\.kiro\steering\` if your
Kiro version reads global steering; hooks stay per workspace.

## Reconciling with the steering you already have

Your on-premises guardrails already ask the agent to estimate how long its changes take; they
just don't put the answer anywhere Baldur can see. Keep whatever they say about *how* to
estimate, if it's stricter, and change *where it goes* to `baldur.cmd ai record`. Check three
things against Baldur's guide:

1. **What is estimated.** Baldur records the person's working time on the change that day, the
   only figure that may become logged time. If your steering asks for "how long a developer
   would take without AI", that's a sizing figure: record it somewhere else, not in Baldur.
2. **Ranges.** If your steering produces a range, give the low end as `--low`; Baldur uses it.
3. **One report per change per day**, citing the commits by full SHA.

Then remove any instruction that has the agent log time itself (a Jira worklog, a timesheet
line): with Baldur, the person approves and Odin posts.
