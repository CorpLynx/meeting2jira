"""Theme tokens for Asgard's desktop UI: one set of names, overridable per app and per person.

Every colour, font and size the QML uses is a named token (theme.accent, theme.surface, ...).
A token's value is decided in layers, later ones winning:

    built-in light or dark set  <  the app's manifest "theme"  <  your global overrides
                                <  your overrides for that app        (settings\\ui.json)

Any layer may be flat ({"accent": "#5B4B9A"}), which applies in both modes, or split by mode
({"light": {...}, "dark": {...}}), or both (the mode-specific value wins).

Rules it keeps:
- Bad or unknown values warn and are dropped; they never stop the window opening.
- Derived tokens keep text readable whatever accent someone picks: accentText (text on an
  accent background) and accentInk (accent used as text) are chosen or adjusted to reach
  WCAG contrast (4.5:1) against what they sit on. Full accessibility still needs manual
  testing with assistive technology.

Standard library only: settings and tests use it without Qt.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

MODES = ("light", "dark")

LIGHT: Dict[str, str] = {
    "background": "#F3F4F6",
    "surface": "#FFFFFF",
    "surfaceHigh": "#EDEFF2",
    "sidebar": "#FAFAFB",
    "border": "#C4CAD3",
    "borderSoft": "#E2E5E9",
    "text": "#1A1D21",
    "textMuted": "#4A525C",
    "textDim": "#5F6773",
    "accent": "#2B5797",
    "success": "#1E7B3A",
    "warning": "#8A5200",
    "error": "#B3261E",
}

DARK: Dict[str, str] = {
    "background": "#15171A",
    "surface": "#1E2125",
    "surfaceHigh": "#2A2E33",
    "sidebar": "#191B1E",
    "border": "#454B53",
    "borderSoft": "#30343A",
    "text": "#ECEEF1",
    "textMuted": "#B5BCC5",
    "textDim": "#9BA3AD",
    "accent": "#6E9BE0",
    "success": "#5CC27A",
    "warning": "#E0A44A",
    "error": "#F2867E",
}

SIZES: Dict[str, Any] = {
    "fontFamily": "Segoe UI",
    "monoFamily": "Consolas",
    "fontSize": 13,
    "radiusSmall": 6,
    "radiusMedium": 10,
    "radiusLarge": 14,
    "sidebarWidth": 232,
    "spacing": 12,
}

# Allowed range for each number token, so a typo can't make the window unusable.
_NUMBER_RANGES = {
    "fontSize": (9, 24), "radiusSmall": (0, 24), "radiusMedium": (0, 32), "radiusLarge": (0, 40),
    "sidebarWidth": (160, 420), "spacing": (4, 32),
}
_TEXT_TOKENS = ("fontFamily", "monoFamily")
COLOR_TOKENS = tuple(LIGHT)
DERIVED = ("accentSoft", "accentText", "accentInk", "focus", "errorText")
_HEX = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


# ------------------------------------------------------------------ colour maths


def parse_hex(value: str) -> Tuple[int, int, int]:
    text = value.lstrip("#")
    if len(text) == 3:
        text = "".join(c * 2 for c in text)
    return int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)


def to_hex(rgb: Tuple[float, float, float]) -> str:
    return "#" + "".join(f"{max(0, min(255, round(c))):02X}" for c in rgb)


def mix(a: str, b: str, amount: float) -> str:
    """amount of a mixed into b (0 gives b, 1 gives a)."""
    ra, rb = parse_hex(a), parse_hex(b)
    return to_hex(tuple(x * amount + y * (1 - amount) for x, y in zip(ra, rb)))


def luminance(color: str) -> float:
    def channel(c: int) -> float:
        s = c / 255
        return s / 12.92 if s <= 0.03928 else ((s + 0.055) / 1.055) ** 2.4
    r, g, b = (channel(c) for c in parse_hex(color))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: str, b: str) -> float:
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def readable_on(background: str, minimum: float = 4.5) -> str:
    """Near-white or near-black text for a background, whichever reads better."""
    light, dark = "#FFFFFF", "#000000"   # pure black: the pair then always reaches 4.58:1
    return light if contrast(light, background) >= contrast(dark, background) else dark


def ink(color: str, background: str, minimum: float = 4.5) -> str:
    """The colour itself if it reads as text on background, else the nearest shade that does."""
    if contrast(color, background) >= minimum:
        return color
    toward = "#000000" if luminance(background) > 0.5 else "#FFFFFF"
    for step in range(1, 21):
        shade = mix(toward, color, step / 20)
        if contrast(shade, background) >= minimum:
            return shade
    return readable_on(background)


# ------------------------------------------------------------------ overrides


def clean(overrides: Any, where: str) -> Tuple[Dict[str, Dict[str, Any]], List[str]]:
    """Split a layer into {"both": {...}, "light": {...}, "dark": {...}}, dropping bad values."""
    out: Dict[str, Dict[str, Any]] = {"both": {}, "light": {}, "dark": {}}
    warnings: List[str] = []
    if overrides in (None, {}):
        return out, warnings
    if not isinstance(overrides, dict):
        return out, [f"{where}: the theme must be an object like {{\"accent\": \"#5B4B9A\"}}; ignored."]

    def take(bucket: str, items: Dict[str, Any], label: str) -> None:
        for key, value in items.items():
            if key.startswith("_"):
                continue                      # comments, as in every Asgard JSON file
            problem = check_token(key, value)
            if problem:
                warnings.append(f"{label}: {problem}; ignored.")
            else:
                out[bucket][key] = value

    for mode in MODES:
        nested = overrides.get(mode)
        if nested is None:
            continue
        if isinstance(nested, dict):
            take(mode, nested, f"{where} ({mode})")
        else:
            warnings.append(f'{where}: "{mode}" must be an object of tokens; ignored.')
    take("both", {k: v for k, v in overrides.items() if k not in MODES}, where)
    return out, warnings


def check_token(key: str, value: Any) -> Optional[str]:
    if key in COLOR_TOKENS:
        if isinstance(value, str) and _HEX.match(value):
            return None
        return f'"{key}" must be a colour like "#5B4B9A", not {value!r}'
    if key in _NUMBER_RANGES:
        low, high = _NUMBER_RANGES[key]
        if isinstance(value, int) and not isinstance(value, bool) and low <= value <= high:
            return None
        return f'"{key}" must be a whole number from {low} to {high}, not {value!r}'
    if key in _TEXT_TOKENS:
        return None if isinstance(value, str) and value.strip() else f'"{key}" must be a font name'
    if key in DERIVED:
        return f'"{key}" is worked out from "accent" so it stays readable; set "accent" instead'
    return f'"{key}" is not a theme token (tokens: {", ".join(COLOR_TOKENS + tuple(SIZES))})'


def resolve(mode: str, *layers: Any) -> Tuple[Dict[str, Any], List[str]]:
    """The full token set for a mode, applying layers in order. Returns (tokens, warnings)."""
    if mode not in MODES:
        mode = "light"
    tokens: Dict[str, Any] = dict(LIGHT if mode == "light" else DARK)
    tokens.update(SIZES)
    warnings: List[str] = []
    for i, layer in enumerate(layers):
        where = layer[0] if isinstance(layer, tuple) else f"theme layer {i + 1}"
        body = layer[1] if isinstance(layer, tuple) else layer
        split, found = clean(body, where)
        warnings.extend(found)
        tokens.update(split["both"])
        tokens.update(split[mode])
    tokens.update(derive(tokens))
    tokens["mode"] = mode
    return tokens, warnings


def derive(tokens: Dict[str, Any]) -> Dict[str, str]:
    accent, surface = tokens["accent"], tokens["surface"]
    dark = luminance(surface) < 0.5
    return {
        "accentSoft": mix(accent, surface, 0.22 if dark else 0.12),
        "accentText": readable_on(accent),
        "accentInk": ink(accent, surface),
        "focus": ink(accent, tokens["background"], 3.0),
        "errorText": readable_on(tokens["error"]),
    }
