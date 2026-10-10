"""The security change form Heimdall fills: %LOCALAPPDATA%\\Asgard\\settings\\heimdall.json.

The form file says where the catalog item is and which fields it has. It holds no values for a
particular submission; those live in templates (templates.py). Each field is known by its label,
which is both how Heimdall finds it on the page (unless a "selector" overrides that) and the key
templates use, so a template can't drift from the form without validation noticing.

Standard library only, and no Playwright: a UI can import this module freely.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from string import Template
from typing import Any, Dict, List, Optional, Tuple

from asgard import paths

KINDS = ("text", "select", "reference", "checkbox")
UIS = ("classic", "portal")
PLACEHOLDERS = ("today", "today_us")
EXAMPLE = Path(__file__).resolve().parent.parent / "heimdall.example.json"

_SYS_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_TRUE = ("true", "yes", "y", "1", "on", "checked")
_FALSE = ("false", "no", "n", "0", "off", "unchecked")


class FormError(ValueError):
    """The form file can't be used, or a value doesn't suit a field. The message says what to fix."""


@dataclass(frozen=True)
class Field:
    label: str
    kind: str = "text"
    selector: Optional[str] = None
    required: bool = False
    default: Any = None          # str for text/select/reference, bool for checkbox, None = untouched

    def to_json(self) -> Dict[str, Any]:
        return {"label": self.label, "kind": self.kind, "required": self.required,
                "default": self.default, "selector": self.selector}


@dataclass(frozen=True)
class Form:
    path: Path
    instance_url: str
    catalog_sys_id: str
    ui: str
    ready_label: Optional[str]
    ready_selector: Optional[str]
    attach_selector: Optional[str]
    channel: str
    sign_in_timeout_s: int
    attempts: int
    profile_dir: Path
    fields: Tuple[Field, ...]

    def field(self, label: str) -> Optional[Field]:
        """The field with this label, matched without regard to case or outer spaces."""
        want = _fold(label)
        for f in self.fields:
            if _fold(f.label) == want:
                return f
        return None

    def labels(self) -> List[str]:
        return [f.label for f in self.fields]


def form_path() -> Path:
    return paths.data_dir() / "settings" / "heimdall.json"


def default_profile_dir() -> Path:
    return paths.data_dir() / "heimdall" / "edge-profile"


def load(path: Optional[Path] = None) -> Form:
    path = Path(path) if path else form_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        raise FormError(f"No form file at {path}. Run 'heimdall init' to create one, then edit it.") from None
    except json.JSONDecodeError as exc:
        raise FormError(f"{path} is not valid JSON (line {exc.lineno}): {exc.msg}. Fix it and try again.") from None
    except OSError as exc:
        raise FormError(f"Couldn't read {path} ({exc.strerror}).") from exc
    if not isinstance(raw, dict):
        raise FormError(f"{path} must hold a JSON object. Compare it with {EXAMPLE.name}.")
    return parse(raw, path)


def parse(raw: Dict[str, Any], path: Path) -> Form:
    where = path.name
    base = str(raw.get("instance_url", "")).strip().rstrip("/")
    if not base.startswith("https://"):
        raise FormError(f'{where}: set "instance_url" to the https:// address of the ServiceNow instance.')
    sys_id = str(raw.get("catalog_sys_id", "")).strip().lower()
    if not _SYS_ID_RE.match(sys_id):
        raise FormError(f'{where}: set "catalog_sys_id" to the 32-character sys_id of the catalog item '
                        "(it's in the item's URL after sysparm_id= or sys_id=).")
    ui = raw.get("ui", "classic")
    if ui not in UIS:
        raise FormError(f'{where}: "ui" must be one of: {", ".join(UIS)}.')

    raw_fields = raw.get("fields")
    if not isinstance(raw_fields, list) or not raw_fields:
        raise FormError(f'{where}: "fields" must list the form\'s fields, like '
                        '[{"label": "Short description", "kind": "text"}].')
    fields: List[Field] = []
    seen: Dict[str, int] = {}
    for i, item in enumerate(raw_fields, 1):
        if not isinstance(item, dict):
            raise FormError(f"{where}: field {i} must be an object with a \"label\".")
        label = str(item.get("label", "")).strip()
        if not label:
            raise FormError(f'{where}: field {i} needs a "label" (the text shown next to it on the form).')
        if _fold(label) in seen:
            raise FormError(f'{where}: fields {seen[_fold(label)]} and {i} are both labelled "{label}". '
                            "Labels must be unique; give one a different label and a \"selector\".")
        seen[_fold(label)] = i
        kind = item.get("kind", "text")
        if kind not in KINDS:
            raise FormError(f'{where}: field "{label}": "kind" must be one of: {", ".join(KINDS)}.')
        selector = item.get("selector") or None
        if selector is not None and not isinstance(selector, str):
            raise FormError(f'{where}: field "{label}": "selector" must be text.')
        draft = Field(label=label, kind=kind, selector=selector, required=bool(item.get("required", False)))
        default = item.get("default")
        if default is not None:
            default = coerce(draft, default, context=f"{where}: default for")
        fields.append(Field(label=label, kind=kind, selector=selector, required=draft.required, default=default))

    ready_label = raw.get("ready_label") or None
    ready_selector = raw.get("ready_selector") or None
    if not ready_label and not ready_selector:
        ready_label = fields[0].label   # the first field is as good a "the form is here" sign as any

    def number(key: str, default: int, low: int, high: int) -> int:
        value = raw.get(key, default)
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise FormError(f'{where}: "{key}" must be a whole number from {low} to {high}.')
        return value

    profile = raw.get("profile_dir")
    return Form(
        path=path,
        instance_url=base,
        catalog_sys_id=sys_id,
        ui=ui,
        ready_label=ready_label,
        ready_selector=ready_selector,
        attach_selector=raw.get("attach_selector") or None,
        channel=str(raw.get("channel", "msedge")),
        sign_in_timeout_s=number("sign_in_timeout_s", 300, 10, 3600),
        attempts=number("attempts", 3, 1, 10),
        profile_dir=Path(profile).expanduser() if profile else default_profile_dir(),
        fields=tuple(fields),
    )


def coerce(field: Field, value: Any, context: str = "") -> Any:
    """Turn a value from JSON or the command line into what the field takes, or explain why not."""
    prefix = f'{context} "{field.label}"' if context else f'"{field.label}"'
    if field.kind == "checkbox":
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in _TRUE:
            return True
        if text in _FALSE:
            return False
        raise FormError(f"{prefix} is a checkbox: use true or false, not {value!r}.")
    if isinstance(value, (dict, list)) or isinstance(value, bool):
        raise FormError(f"{prefix} takes text, not {value!r}.")
    text = str(value)
    check_placeholders(text, f"{prefix}")
    return text


def check_placeholders(text: str, where: str) -> None:
    """Refuse $names Heimdall doesn't know, so a typo fails now instead of reaching the form."""
    for m in Template.pattern.finditer(text):
        name = m.group("named") or m.group("braced")
        if m.group("invalid") is not None:
            raise FormError(f"{where}: a lone $ must be written $$.")
        if name and name not in PLACEHOLDERS:
            known = ", ".join("$" + p for p in PLACEHOLDERS)
            raise FormError(f"{where}: ${name} isn't a placeholder Heimdall knows ({known}). "
                            "Write $$ for a literal dollar sign.")


def render(value: Any, values: Dict[str, str]) -> Any:
    return Template(value).substitute(values) if isinstance(value, str) else value


def _fold(label: str) -> str:
    return " ".join(label.split()).casefold()
