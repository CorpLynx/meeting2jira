"""Saved value combinations for the form: %LOCALAPPDATA%\\Asgard\\settings\\heimdall-templates.json.

A template maps field labels to values, plus an optional attachment and description. It never
describes the form itself, so when the form changes there is one place to fix.

Rules it keeps:
- A template only names fields the form has. Unknown labels fail loudly with the label named;
  a silently blank field would reach the review page looking complete.
- A file that won't parse is never overwritten: every write refuses until it's fixed, so a typo
  can't cost you every saved template.
- Writes go to a temporary file first and replace the old one in one step.
- plan() is the one place values are layered: field default < template < this run's overrides.

Standard library only, and no Playwright: a UI can import this module freely.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from asgard import paths

from .form import Field, Form, FormError, coerce, render

VERSION = 1
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.\-]{0,63}$")


class TemplateError(ValueError):
    """A template is missing, invalid, or can't be saved. The message says what to do."""


@dataclass
class Saved:
    name: str
    values: Dict[str, Any] = field(default_factory=dict)   # field label -> value, as the form spells it
    attachment: Optional[str] = None
    description: str = ""
    updated: str = ""

    def to_json(self) -> Dict[str, Any]:
        return {"description": self.description, "values": dict(self.values),
                "attachment": self.attachment, "updated": self.updated}


@dataclass
class Plan:
    """Exactly what a fill will do. The UI shows this; fill executes it."""
    steps: List[Tuple[Field, Any]]          # value None means "leave the field as it is"
    attachment: Optional[str]
    template: Optional[str]

    def to_json(self) -> Dict[str, Any]:
        return {"template": self.template, "attachment": self.attachment,
                "fields": [{"label": f.label, "kind": f.kind, "value": v, "action": _action(v)}
                           for f, v in self.steps]}


def templates_path() -> Path:
    return paths.data_dir() / "settings" / "heimdall-templates.json"


class Store:
    """The templates file. Load once, change, then save()."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path) if path else templates_path()
        self.items: Dict[str, Saved] = {}
        self.broken: Optional[str] = None
        self._load()

    # -------------------------------------------------- reading

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except FileNotFoundError:
            return
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            self.broken = f"it isn't valid JSON ({exc})"
            return
        except OSError as exc:
            self.broken = f"it can't be read ({exc.strerror})"
            return
        if not isinstance(raw, dict) or not isinstance(raw.get("templates", {}), dict):
            self.broken = 'it must be an object with a "templates" object inside'
            return
        if raw.get("version", VERSION) != VERSION:
            self.broken = f"it is version {raw.get('version')!r}, and this Heimdall reads version {VERSION}"
            return
        for name, body in raw.get("templates", {}).items():
            if not isinstance(body, dict) or not isinstance(body.get("values", {}), dict):
                self.broken = f'template "{name}" must be an object with a "values" object'
                return
            self.items[_key(name)] = Saved(
                name=name,
                values=dict(body.get("values", {})),
                attachment=body.get("attachment") or None,
                description=str(body.get("description") or ""),
                updated=str(body.get("updated") or ""),
            )

    def check_usable(self) -> None:
        if self.broken:
            raise TemplateError(f"{self.path} can't be used: {self.broken}. Fix it, or move it aside to "
                                "start again; Heimdall won't overwrite it.")

    def names(self) -> List[str]:
        return sorted((t.name for t in self.items.values()), key=str.casefold)

    def get(self, name: str) -> Saved:
        self.check_usable()
        found = self.items.get(_key(name))
        if found is None:
            have = ", ".join(self.names()) or "none saved yet"
            raise TemplateError(f'No template named "{name}" (templates: {have}). '
                                "List them with 'heimdall template list'.")
        return found

    # -------------------------------------------------- changing

    def put(self, form: Form, name: str, values: Dict[str, Any], attachment: Optional[str],
            description: str, replace: bool = False) -> Saved:
        """Create a template, or with replace=True overwrite one wholesale."""
        self.check_usable()
        name = check_name(name)
        if _key(name) in self.items and not replace:
            raise TemplateError(f'A template named "{self.items[_key(name)].name}" already exists. '
                                "Change it with 'heimdall template edit', or overwrite it with --replace.")
        saved = Saved(name=name, values=normalize(form, values), attachment=_clean_attachment(attachment),
                      description=description.strip(), updated=_now())
        self.items[_key(name)] = saved
        return saved

    def edit(self, form: Form, name: str, set_values: Dict[str, Any], unset: List[str],
             attachment: Optional[str] = None, clear_attachment: bool = False,
             description: Optional[str] = None, rename: Optional[str] = None) -> Saved:
        """Change part of a template; anything not mentioned stays as it was."""
        current = self.get(name)
        values = normalize(form, current.values)
        values.update(normalize(form, set_values))
        for label in unset:
            f = _known(form, label)
            if f.label not in values:
                raise TemplateError(f'Template "{current.name}" has no value for "{f.label}" to remove.')
            del values[f.label]
        new_name = current.name
        if rename is not None:
            new_name = check_name(rename)
            clash = self.items.get(_key(new_name))
            if clash is not None and clash is not current:
                raise TemplateError(f'A template named "{clash.name}" already exists. Pick another name.')
        saved = Saved(
            name=new_name,
            values=values,
            attachment=None if clear_attachment else (_clean_attachment(attachment) or current.attachment),
            description=current.description if description is None else description.strip(),
            updated=_now(),
        )
        del self.items[_key(current.name)]
        self.items[_key(new_name)] = saved
        return saved

    def delete(self, name: str) -> Saved:
        found = self.get(name)
        del self.items[_key(found.name)]
        return found

    def save(self) -> None:
        self.check_usable()
        body = {"version": VERSION,
                "templates": {t.name: t.to_json() for t in sorted(self.items.values(), key=lambda t: t.name.casefold())}}
        text = json.dumps(body, indent=2, ensure_ascii=False) + "\n"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".heimdall-templates-", suffix=".tmp", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(text)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


# ------------------------------------------------------ checking against the form


def normalize(form: Form, values: Dict[str, Any]) -> Dict[str, Any]:
    """Map labels to the form's spelling and coerce each value; raise on anything unknown."""
    out: Dict[str, Any] = {}
    for label, value in values.items():
        f = _known(form, label)
        if f.label in out:
            raise TemplateError(f'"{f.label}" is given twice ({label!r} matches it too).')
        try:
            out[f.label] = coerce(f, value)
        except FormError as exc:
            raise TemplateError(str(exc)) from None
    return out


def problems(form: Form, saved: Saved) -> List[str]:
    """Why a saved template no longer suits the form (empty when it does)."""
    found: List[str] = []
    for label, value in saved.values.items():
        f = form.field(label)
        if f is None:
            found.append(f'"{label}" is not a field on the form any more')
            continue
        try:
            coerce(f, value)
        except FormError as exc:
            found.append(str(exc))
    return found


def plan(form: Form, saved: Optional[Saved], overrides: Dict[str, Any], attachment: Optional[str],
         clear_attachment: bool, today: Optional[dt.date] = None) -> Plan:
    """Layer default < template < overrides, check required fields and the attachment, fill in $today."""
    if saved is not None:
        bad = problems(form, saved)
        if bad:
            raise TemplateError(f'Template "{saved.name}" doesn\'t match the form: {"; ".join(bad)}. '
                                f"Fix it with 'heimdall template edit \"{saved.name}\"'.")
    chosen: Dict[str, Any] = {f.label: f.default for f in form.fields}
    if saved is not None:
        chosen.update(normalize(form, saved.values))
    chosen.update(normalize(form, overrides))

    missing = [f.label for f in form.fields if f.required and chosen[f.label] in (None, "")]
    if missing:
        raise TemplateError(f"Required fields have no value: {', '.join(missing)}. "
                            "Add them to the template, or give them with --set LABEL=VALUE.")

    subs = placeholder_values(today)
    steps = [(f, render(chosen[f.label], subs)) for f in form.fields]

    path = None if clear_attachment else (_clean_attachment(attachment) or (saved.attachment if saved else None))
    if path:
        p = Path(os.path.expandvars(path)).expanduser()
        if not p.is_file():
            raise TemplateError(f"Attachment not found: {p}. Fix the path, or run with --no-attachment.")
        path = str(p.resolve())
    return Plan(steps=steps, attachment=path, template=saved.name if saved else None)


def placeholder_values(today: Optional[dt.date] = None) -> Dict[str, str]:
    """Values for $today and friends. Muninn data plugs in here later."""
    today = today or dt.date.today()
    return {"today": today.isoformat(), "today_us": today.strftime("%m/%d/%Y")}


def parse_assignments(items: List[str]) -> Dict[str, str]:
    """--set "Label=value" pairs into a dict. Splits on the first '=' so values may contain one."""
    out: Dict[str, str] = {}
    for item in items:
        label, sep, value = item.partition("=")
        if not sep or not label.strip():
            raise TemplateError(f'--set needs LABEL=VALUE, like --set "Short description=Monthly scan"; got {item!r}.')
        out[label.strip()] = value
    return out


def check_name(name: str) -> str:
    name = " ".join(name.split())
    if not _NAME_RE.match(name):
        raise TemplateError(f"Template names are 1 to 64 letters, digits, spaces, '.', '_' or '-', "
                            f"starting with a letter or digit; {name!r} isn't one.")
    return name


def _known(form: Form, label: str) -> Field:
    f = form.field(label)
    if f is None:
        raise TemplateError(f'"{label}" is not a field on the form. Fields: {", ".join(form.labels())}.')
    return f


def _action(value: Any) -> str:
    if value is None:
        return "leave"
    if value is True:
        return "check"
    if value is False:
        return "uncheck"
    return "clear" if value == "" else "set"


def _clean_attachment(path: Optional[str]) -> Optional[str]:
    return path.strip() if path and path.strip() else None


def _key(name: str) -> str:
    return " ".join(name.split()).casefold()


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
