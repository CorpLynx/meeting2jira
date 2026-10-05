"""The Asgard window: a grid of app tiles, in the spirit of the Microsoft My Apps portal.

Click a tile to open its app. Right-click (or the Menu key) for more.
Type to search, arrow keys to move, Enter to open, F5 to reload tiles.
"""
from __future__ import annotations

import math
import os
import sys
import threading
import time
import traceback
import webbrowser
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import __version__, catalog, paths, winutil
from .catalog import COMING_SOON, MISSING, NEEDS_SETUP, READY, App, CatalogError
from .runner import LaunchError, Runner, log_tail

try:
    from . import muninn
except Exception:  # a damaged install still launches apps; About explains
    muninn = None  # type: ignore[assignment]

BADGE_REFRESH_MS = 60_000

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
    from tkinter import font as tkfont
except ImportError:  # main() explains what to do
    tk = None  # type: ignore[assignment]

RUNNING = "running"

LIGHT: Dict[str, Any] = {
    "bg": "#F3F3F3", "card": "#FFFFFF", "card_hover": "#FBFBFB", "card_press": "#F0F0F0",
    "border": "#E0E0E0", "border_hover": "#BDBDBD",
    "text": "#1B1B1B", "text2": "#5C5C5C", "text3": "#6B6B6B",
    "accent": "#005FB8", "field": "#FFFFFF", "field_border": "#CFCFCF",
    "button_hover": "#E6E6E6", "scroll": "#C6C6C6", "scroll_hover": "#A6A6A6",
    "badge_bg": "#005FB8", "badge_fg": "#FFFFFF",
    "pills": {
        RUNNING: ("Running", "#0E6B0E", "#DFF6DD", True),
        NEEDS_SETUP: ("Set up", "#005FB8", "#E3EEF9", False),
        MISSING: ("Not found", "#7A4A00", "#FFF1C2", False),
        COMING_SOON: ("Coming soon", "#5C5C5C", "#EBEBEB", False),
    },
}
DARK: Dict[str, Any] = {
    "bg": "#202020", "card": "#2B2B2B", "card_hover": "#323232", "card_press": "#272727",
    "border": "#383838", "border_hover": "#575757",
    "text": "#FFFFFF", "text2": "#C5C5C5", "text3": "#9A9A9A",
    "accent": "#60CDFF", "field": "#2B2B2B", "field_border": "#4A4A4A",
    "button_hover": "#383838", "scroll": "#4A4A4A", "scroll_hover": "#6A6A6A",
    "badge_bg": "#60CDFF", "badge_fg": "#000000",
    "pills": {
        RUNNING: ("Running", "#6CCB5F", "#1C351B", True),
        NEEDS_SETUP: ("Set up", "#60CDFF", "#17344A", False),
        MISSING: ("Not found", "#FCE100", "#3B3214", False),
        COMING_SOON: ("Coming soon", "#C5C5C5", "#383838", False),
    },
}


# --------------------------------------------------------------------------
# Drawing helpers
# --------------------------------------------------------------------------

def mix(a: str, b: str, t: float) -> str:
    """Blend colour a toward colour b; t=0 gives a, t=1 gives b."""
    ca = [int(a[i:i + 2], 16) for i in (1, 3, 5)]
    cb = [int(b[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * t):02X}" for x, y in zip(ca, cb))


def rounded_rect(canvas: "tk.Canvas", x1: float, y1: float, x2: float, y2: float,
                 r: float, **kw: Any) -> int:
    r = max(0.0, min(r, (x2 - x1) / 2, (y2 - y1) / 2))
    pts = [x1 + r, y1, x1 + r, y1, x2 - r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y1 + r,
           x2, y2 - r, x2, y2 - r, x2, y2, x2 - r, y2, x2 - r, y2, x1 + r, y2, x1 + r, y2,
           x1, y2, x1, y2 - r, x1, y2 - r, x1, y1 + r, x1, y1 + r, x1, y1]
    return canvas.create_polygon(pts, smooth=True, splinesteps=16, **kw)


def fit_lines(font: "tkfont.Font", text: str, width: int, max_lines: int) -> List[str]:
    """Word-wrap text into at most max_lines lines, ending with an ellipsis if cut."""
    words = text.split()
    lines: List[str] = []
    i = 0
    while i < len(words) and len(lines) < max_lines:
        line = words[i]
        i += 1
        while i < len(words) and font.measure(line + " " + words[i]) <= width:
            line += " " + words[i]
            i += 1
        lines.append(line)
    if lines and (i < len(words) or font.measure(lines[-1]) > width):
        last = lines[-1]
        while last and font.measure(last + "…") > width:
            last = last[:-1].rstrip()
        lines[-1] = last + "…"
    return lines


# --------------------------------------------------------------------------
# The window
# --------------------------------------------------------------------------

class LauncherWindow:
    TILE_W, TILE_H, GAP, MARGIN, PAD, ICON = 212, 140, 16, 28, 16, 44

    def __init__(self, root: "tk.Tk", *, dark: Optional[bool] = None,
                 defaults: Optional[Path] = None, local: Optional[Path] = None) -> None:
        self.root = root
        self.dark = winutil.apps_use_dark_theme() if dark is None else dark
        self.p = DARK if self.dark else LIGHT
        self.s = max(1.0, root.winfo_fpixels("1i") / 96.0)
        self.defaults, self.local = defaults, local
        self.apps: List[App] = []
        self.visible: List[App] = []
        self.boxes: List[Tuple[int, int, int, int]] = []
        self.cols = 1
        self.hover: Optional[int] = None
        self.pressed: Optional[int] = None
        self.focus_index = 0
        self.keyboard = False
        self.canvas_focused = False
        self._render_pending = False
        self._flash_job: Optional[str] = None
        self._poll_job: Optional[str] = None
        self._badge_job: Optional[str] = None
        self._last_running: set = set()
        self.badges: Dict[str, List[Any]] = {}
        self.muninn_status: Any = None
        self.muninn_error: Optional[str] = None
        self._muninn_result: Dict[str, Any] = {}
        self._muninn_thread: Optional[threading.Thread] = None
        self._last_badge_refresh = 0.0
        self._hint_is_badges = False
        self.runner = Runner(paths.log_dir(), extra_env={
            "ASGARD_DATA": str(paths.data_dir()), "ASGARD_APP": str(paths.CODE_ROOT)})
        self._init_fonts()
        self._init_window()
        self._build_header()
        self._build_footer()
        self._build_grid()
        self.reload(show_warnings=True)
        self._poll_job = self.root.after(1000, self._poll)
        self._badge_job = self.root.after(100, self._start_muninn)

    def px(self, value: float) -> int:
        return int(round(value * self.s))

    # ---- setup ---------------------------------------------------------

    def _init_fonts(self) -> None:
        families = set(tkfont.families(self.root))
        default = tkfont.nametofont("TkDefaultFont").actual("family")
        base = next((f for f in ("Segoe UI", "Noto Sans", "DejaVu Sans", "Helvetica") if f in families), default)
        semi = "Segoe UI Semibold" if "Segoe UI Semibold" in families else base
        weight = "normal" if semi != base else "bold"

        def make(family: str, size: int, w: str = "normal") -> "tkfont.Font":
            return tkfont.Font(root=self.root, family=family, size=size, weight=w)

        self.f_title = make(semi, 20, weight)
        self.f_sub = make(base, 10)
        self.f_name = make(semi, 11, weight)
        self.f_desc = make(base, 9)
        self.f_pill = make(semi, 8, weight)
        self.f_mono = make(semi, 14, weight)
        self.f_search = make(base, 10)
        self.f_foot = make(base, 9)

    def _init_window(self) -> None:
        r, p = self.root, self.p
        r.title("Asgard")
        r.configure(bg=p["bg"])
        sw, sh = r.winfo_screenwidth(), r.winfo_screenheight()
        w, h = min(self.px(980), sw - 80), min(self.px(660), sh - 120)
        r.geometry(f"{w}x{h}+{max(0, (sw - w) // 2)}+{max(0, (sh - h) // 3)}")
        r.minsize(self.px(420), self.px(360))
        ico, png = paths.PACKAGE_DIR / "asgard.ico", paths.PACKAGE_DIR / "asgard.png"
        try:
            if winutil.IS_WINDOWS and ico.exists():
                r.iconbitmap(default=str(ico))
            elif png.exists():
                self._icon = tk.PhotoImage(file=str(png))
                r.iconphoto(True, self._icon)
        except tk.TclError:
            pass
        style = ttk.Style(r)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("Asgard.Vertical.TScrollbar", troughcolor=p["bg"], background=p["scroll"],
                        bordercolor=p["bg"], lightcolor=p["scroll"], darkcolor=p["scroll"],
                        arrowcolor=p["text2"], gripcount=0, relief="flat")
        style.map("Asgard.Vertical.TScrollbar", background=[("active", p["scroll_hover"])])
        r.protocol("WM_DELETE_WINDOW", self.close)
        r.report_callback_exception = self._callback_error  # type: ignore[assignment]
        r.bind("<Control-f>", lambda e: self._focus_search())
        r.bind("<F5>", lambda e: self.reload(show_warnings=True))
        r.bind("<FocusIn>", self._on_app_focus, add="+")
        r.update_idletasks()
        winutil.set_dark_title_bar(r, self.dark)

    def _build_header(self) -> None:
        p, px = self.p, self.px
        header = tk.Frame(self.root, bg=p["bg"])
        header.pack(side="top", fill="x", padx=px(self.MARGIN), pady=(px(22), px(14)))
        titles = tk.Frame(header, bg=p["bg"])
        titles.pack(side="left")
        tk.Label(titles, text="Asgard", font=self.f_title, fg=p["text"], bg=p["bg"]).pack(anchor="w")
        self.subtitle = tk.Label(titles, text="", font=self.f_sub, fg=p["text2"], bg=p["bg"])
        self.subtitle.pack(anchor="w")

        right = tk.Frame(header, bg=p["bg"])
        right.pack(side="right")
        bw, bh = px(36), px(34)
        self.menu_button = tk.Canvas(right, width=bw, height=bh, bg=p["bg"], highlightthickness=0,
                                     cursor="hand2", takefocus=1)
        self.menu_button.pack(side="right", padx=(px(8), 0))
        self._draw_menu_button(False)
        self.menu_button.bind("<Enter>", lambda e: self._draw_menu_button(True))
        self.menu_button.bind("<Leave>", lambda e: self._draw_menu_button(False))
        for seq in ("<ButtonRelease-1>", "<Return>", "<space>"):
            self.menu_button.bind(seq, lambda e: self._show_main_menu())

        self.search_box = tk.Frame(right, bg=p["field"], highlightthickness=1,
                                   highlightbackground=p["field_border"], highlightcolor=p["field_border"])
        self.search_box.pack(side="right")
        glass = tk.Canvas(self.search_box, width=px(30), height=px(30), bg=p["field"], highlightthickness=0)
        glass.pack(side="left")
        cx, cy, rr = px(15), px(14), px(5)
        glass.create_oval(cx - rr, cy - rr, cx + rr, cy + rr, outline=p["text2"], width=max(1, px(1.4)))
        glass.create_line(cx + rr * 0.7, cy + rr * 0.7, cx + rr * 1.7, cy + rr * 1.7,
                          fill=p["text2"], width=max(1, px(1.6)), capstyle="round")
        self.search_var = tk.StringVar()
        self.search = tk.Entry(self.search_box, textvariable=self.search_var, font=self.f_search,
                               width=24, relief="flat", bd=0, bg=p["field"], fg=p["text"],
                               insertbackground=p["text"], highlightthickness=0)
        self.search.pack(side="left", ipady=px(4), padx=(px(4), px(10)))
        self.placeholder = tk.Label(self.search_box, text="Search apps", font=self.f_search,
                                    fg=p["text3"], bg=p["field"], cursor="xterm")
        self.placeholder.place(in_=self.search, x=1, rely=0.5, anchor="w")
        self.placeholder.bind("<Button-1>", lambda e: self.search.focus_set())
        self.search_var.trace_add("write", lambda *a: self._on_search())
        self.search.bind("<FocusIn>", lambda e: self.search_box.configure(highlightbackground=p["accent"],
                                                                          highlightcolor=p["accent"]))
        self.search.bind("<FocusOut>", lambda e: self.search_box.configure(highlightbackground=p["field_border"],
                                                                           highlightcolor=p["field_border"]))
        self.search.bind("<Escape>", lambda e: self.search_var.set(""))
        for seq in ("<Down>", "<Return>"):
            self.search.bind(seq, lambda e: self._focus_grid())

    def _draw_menu_button(self, hover: bool) -> None:
        c, p, px = self.menu_button, self.p, self.px
        c.delete("all")
        w, h = int(c["width"]), int(c["height"])
        if hover:
            rounded_rect(c, 1, 1, w - 1, h - 1, px(6), fill=p["button_hover"], outline="")
        d = max(2, px(2))
        for k in (-1, 0, 1):
            x = w / 2 + k * px(6)
            c.create_oval(x - d, h / 2 - d, x + d, h / 2 + d, fill=p["text"], outline="")

    def _build_footer(self) -> None:
        p, px = self.p, self.px
        footer = tk.Frame(self.root, bg=p["bg"])
        footer.pack(side="bottom", fill="x", padx=px(self.MARGIN), pady=(px(6), px(12)))
        tk.Label(footer, text=f"Asgard {__version__}", font=self.f_foot, fg=p["text3"], bg=p["bg"]).pack(side="left")
        self.hint_text = "Click a tile to open it. Right-click for more."
        self.hint = tk.Label(footer, text=self.hint_text, font=self.f_foot, fg=p["text3"], bg=p["bg"])
        self.hint.pack(side="right")

    def _build_grid(self) -> None:
        p = self.p
        body = tk.Frame(self.root, bg=p["bg"])
        body.pack(side="top", fill="both", expand=True)
        self.canvas = tk.Canvas(body, bg=p["bg"], highlightthickness=0, bd=0, takefocus=1,
                                yscrollincrement=self.px(24))
        self.vbar = ttk.Scrollbar(body, orient="vertical", command=self.canvas.yview,
                                  style="Asgard.Vertical.TScrollbar")
        self.canvas.configure(yscrollcommand=self.vbar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self._vbar_shown = False
        c = self.canvas
        c.bind("<Configure>", lambda e: self._schedule_render())
        c.bind("<Motion>", self._on_motion)
        c.bind("<Leave>", self._on_leave)
        c.bind("<ButtonPress-1>", self._on_press)
        c.bind("<ButtonRelease-1>", self._on_release)
        c.bind("<Button-3>", self._on_right_click)
        # On Windows the wheel event goes to the focused widget, so listen everywhere
        # and scroll when the pointer is over the tiles.
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self.root.bind_all(seq, self._on_wheel, add="+")
        c.bind("<FocusIn>", lambda e: self._set_canvas_focus(True))
        c.bind("<FocusOut>", lambda e: self._set_canvas_focus(False))
        for seq, move in (("<Left>", (-1, 0)), ("<Right>", (1, 0)), ("<Up>", (0, -1)), ("<Down>", (0, 1))):
            c.bind(seq, lambda e, m=move: self._move(*m))
        c.bind("<Home>", lambda e: self._move_to(0))
        c.bind("<End>", lambda e: self._move_to(len(self.visible) - 1))
        for seq in ("<Return>", "<space>"):
            c.bind(seq, lambda e: self._activate_index(self.focus_index))
        for seq in ("<App>", "<Menu>", "<Shift-F10>"):  # the Menu key is "App" on Windows, "Menu" on X11
            try:
                c.bind(seq, lambda e: self._menu_for_focused())
            except tk.TclError:
                pass
        c.bind("<Key>", self._type_to_search)

    # ---- data ----------------------------------------------------------

    def reload(self, show_warnings: bool = False) -> None:
        try:
            self.apps, warnings = catalog.load_catalog(self.defaults, self.local)
        except CatalogError as exc:
            self.apps, warnings = [], [f"Asgard's built-in tile list is damaged. Reinstall Asgard.\n\n{exc}"]
        self._apply_filter()
        if warnings and show_warnings:
            self.root.after(300, lambda: messagebox.showwarning(
                "Asgard tiles", "\n\n".join(warnings), parent=self.root))

    def _apply_filter(self) -> None:
        q = self.search_var.get().strip().lower()
        self.visible = [a for a in self.apps
                        if not q or q in a.name.lower() or q in a.description.lower() or q in a.id]
        self.focus_index = min(self.focus_index, max(0, len(self.visible) - 1))
        self.hover = None
        total = len(self.apps)
        self.subtitle.configure(text=f"{len(self.visible)} of {total} apps" if q else f"{total} apps")
        self._schedule_render()

    def _on_search(self) -> None:
        if self.search_var.get():
            self.placeholder.place_forget()
        else:
            self.placeholder.place(in_=self.search, x=1, rely=0.5, anchor="w")
        self._apply_filter()

    def _display_state(self, app: App) -> str:
        return RUNNING if self.runner.is_running(app.id) else catalog.state(app)

    # ---- rendering -----------------------------------------------------

    def _schedule_render(self) -> None:
        if not self._render_pending:
            self._render_pending = True
            self.root.after_idle(self._render)

    def _render(self) -> None:
        self._render_pending = False
        c, p, px = self.canvas, self.p, self.px
        c.delete("all")
        self.boxes = []
        W, H, G, M = px(self.TILE_W), px(self.TILE_H), px(self.GAP), px(self.MARGIN)
        width = max(c.winfo_width(), W + 2 * M)
        self.cols = max(1, (width - 2 * M + G) // (W + G))
        top = px(4)
        if not self.visible:
            q = self.search_var.get().strip()
            message = f"No apps match “{q}”." if q else "No tiles to show. Check apps.local.json."
            c.create_text(M, top + px(12), text=message, anchor="nw", font=self.f_sub, fill=p["text2"])
            self._set_scroll(top + px(60))
            return
        for i, app in enumerate(self.visible):
            row, col = divmod(i, self.cols)
            self._draw_tile(i, app, M + col * (W + G), top + row * (H + G))
        rows = math.ceil(len(self.visible) / self.cols)
        self._set_scroll(top + rows * (H + G) - G + M)
        if self.canvas_focused and self.keyboard:
            x1, y1, x2, y2 = self.boxes[self.focus_index]
            o = px(3)
            rounded_rect(c, x1 - o, y1 - o, x2 + o, y2 + o, px(11), fill="",
                         outline=p["accent"], width=max(2, px(2)))

    def _set_scroll(self, content_h: int) -> None:
        c = self.canvas
        c.configure(scrollregion=(0, 0, c.winfo_width(), content_h))
        need = content_h > c.winfo_height() + 1
        if need and not self._vbar_shown:
            self.vbar.pack(side="right", fill="y", before=c)
            self._vbar_shown = True
        elif not need and self._vbar_shown:
            self.vbar.pack_forget()
            self._vbar_shown = False
            c.yview_moveto(0)

    def _draw_tile(self, i: int, app: App, x: int, y: int) -> None:
        c, p, px = self.canvas, self.p, self.px
        st = self._display_state(app)
        dim = st == COMING_SOON
        W, H, pad, icon = px(self.TILE_W), px(self.TILE_H), px(self.PAD), px(self.ICON)
        hovered = i == self.hover and not dim
        fill = p["card_press"] if i == self.pressed and not dim else (p["card_hover"] if hovered else p["card"])
        rounded_rect(c, x, y, x + W, y + H, px(8), fill=fill,
                     outline=p["border_hover"] if hovered else p["border"], width=1)
        icon_fill = mix(app.color, p["card"], 0.5) if dim else app.color
        rounded_rect(c, x + pad, y + pad, x + pad + icon, y + pad + icon, px(10), fill=icon_fill, outline="")
        c.create_text(x + pad + icon / 2, y + pad + icon / 2, text=app.monogram, font=self.f_mono,
                      fill=mix("#FFFFFF", icon_fill, 0.25) if dim else "#FFFFFF")
        badges = None if dim else self.badges.get(app.id)
        if badges:
            self._draw_badge(x + pad + icon - px(3), y + pad + px(3), badges[0].count, fill)
        name_y = y + pad + icon + px(12)
        name = fit_lines(self.f_name, app.name, W - 2 * pad, 1)
        c.create_text(x + pad, name_y, text=name[0] if name else "", anchor="nw", font=self.f_name,
                      fill=p["text3"] if dim else p["text"])
        desc_y = name_y + self.f_name.metrics("linespace") + px(2)
        lines = fit_lines(self.f_desc, app.description, W - 2 * pad, 2)
        c.create_text(x + pad, desc_y, text="\n".join(lines), anchor="nw", font=self.f_desc,
                      fill=p["text3"] if dim else p["text2"])
        pill = p["pills"].get(st)
        if pill:
            self._draw_pill(x + W - pad, y + pad + icon / 2, *pill)
        self.boxes.append((x, y, x + W, y + H))

    def _draw_badge(self, cx: float, cy: float, count: int, ring: str) -> None:
        """A count on the icon's corner, like a phone's app badge, ringed in the card colour."""
        c, p, px = self.canvas, self.p, self.px
        text = "99+" if count > 99 else str(count)
        h = px(20)
        w = max(h, self.f_pill.measure(text) + px(12))
        o = max(1, px(2))
        for grow, colour in ((o, ring), (0, p["badge_bg"])):
            x1, y1, x2, y2 = cx - w / 2 - grow, cy - h / 2 - grow, cx + w / 2 + grow, cy + h / 2 + grow
            if w == h:  # one digit: a true circle; smoothed polygons look square at this size
                c.create_oval(x1, y1, x2, y2, fill=colour, outline="")
            else:
                rounded_rect(c, x1, y1, x2, y2, h / 2 + grow, fill=colour, outline="")
        c.create_text(cx, cy, text=text, font=self.f_pill, fill=p["badge_fg"])

    def _draw_pill(self, right: float, cy: float, label: str, fg: str, bg: str, dot: bool) -> None:
        c, px = self.canvas, self.px
        padx, h = px(8), px(20)
        dot_w = px(10) if dot else 0
        w = self.f_pill.measure(label) + 2 * padx + dot_w
        x1, y1 = right - w, cy - h / 2
        rounded_rect(c, x1, y1, right, y1 + h, h / 2, fill=bg, outline="")
        if dot:
            r = px(3)
            c.create_oval(x1 + padx, cy - r, x1 + padx + 2 * r, cy + r, fill=fg, outline="")
        c.create_text(x1 + padx + dot_w, cy, text=label, anchor="w", font=self.f_pill, fill=fg)

    # ---- mouse and keyboard ------------------------------------------

    def _index_at(self, event: Any) -> Optional[int]:
        x, y = self.canvas.canvasx(event.x), self.canvas.canvasy(event.y)
        for i, (x1, y1, x2, y2) in enumerate(self.boxes):
            if x1 <= x <= x2 and y1 <= y <= y2:
                return i
        return None

    def _on_motion(self, event: Any) -> None:
        i = self._index_at(event)
        if i != self.hover:
            self.hover = i
            clickable = i is not None and self._display_state(self.visible[i]) != COMING_SOON
            self.canvas.configure(cursor="hand2" if clickable else "")
            self._show_badge_hint(self.visible[i] if i is not None else None)
            self._schedule_render()

    def _on_leave(self, event: Any) -> None:
        if self.hover is not None or self.pressed is not None:
            self.hover = self.pressed = None
            self.canvas.configure(cursor="")
            self._show_badge_hint(None)
            self._schedule_render()

    def _show_badge_hint(self, app: Optional[App]) -> None:
        """Spell out a tile's badges in the footer while it's pointed at or focused."""
        badges = self.badges.get(app.id) if app is not None and catalog.state(app) != COMING_SOON else None
        if badges:
            self.hint.configure(text=f"{app.name}: " + " · ".join(b.text for b in badges), fg=self.p["text2"])
            self._hint_is_badges = True
        elif self._hint_is_badges:
            self.hint.configure(text=self.hint_text, fg=self.p["text3"])
            self._hint_is_badges = False

    def _on_press(self, event: Any) -> None:
        self.keyboard = False
        self.canvas.focus_set()
        self.pressed = self._index_at(event)
        self._schedule_render()

    def _on_release(self, event: Any) -> None:
        i = self._index_at(event)
        pressed, self.pressed = self.pressed, None
        self._schedule_render()
        if i is not None and i == pressed:
            self.focus_index = i
            self._activate_index(i)

    def _on_right_click(self, event: Any) -> None:
        i = self._index_at(event)
        if i is not None:
            self.focus_index = i
            self._tile_menu(self.visible[i], event.x_root, event.y_root)

    def _on_wheel(self, event: Any) -> None:
        if not self._vbar_shown:
            return
        if self.root.winfo_containing(event.x_root, event.y_root) is not self.canvas:
            return
        if getattr(event, "num", None) in (4, 5):
            step = -1 if event.num == 4 else 1
        else:
            step = -1 if event.delta > 0 else 1
        self.canvas.yview_scroll(step * 2, "units")

    def _set_canvas_focus(self, focused: bool) -> None:
        self.canvas_focused = focused
        self._schedule_render()

    def _focus_search(self) -> None:
        self.search.focus_set()
        self.search.select_range(0, "end")

    def _focus_grid(self) -> None:
        if self.visible:
            self.keyboard = True
            self.canvas.focus_set()
            self._schedule_render()

    def _move(self, dx: int, dy: int) -> None:
        n = len(self.visible)
        if not n:
            return
        i = self.focus_index
        if dx:
            i = max(0, min(n - 1, i + dx))
        if dy:
            j = i + dy * self.cols
            if 0 <= j < n:
                i = j
            elif dy < 0:
                self._focus_search()
                return
        self._move_to(i)

    def _move_to(self, i: int) -> None:
        if not self.visible:
            return
        self.keyboard = True
        self.focus_index = max(0, min(len(self.visible) - 1, i))
        self._show_badge_hint(self.visible[self.focus_index])
        self._render()
        x1, y1, x2, y2 = self.boxes[self.focus_index]
        top, bottom = self.canvas.canvasy(0), self.canvas.canvasy(self.canvas.winfo_height())
        region = float(self.canvas.cget("scrollregion").split()[-1] or 1)
        if y1 < top:
            self.canvas.yview_moveto(max(0.0, (y1 - self.px(8)) / region))
        elif y2 > bottom:
            self.canvas.yview_moveto(max(0.0, (y2 + self.px(8) - self.canvas.winfo_height()) / region))

    def _type_to_search(self, event: Any) -> None:
        if event.char and event.char.isprintable() and not event.char.isspace() and not (event.state & 0x4):
            self.search.focus_set()
            self.search.insert("end", event.char)

    # ---- actions -------------------------------------------------------

    def _activate_index(self, i: int) -> None:
        if 0 <= i < len(self.visible):
            self.activate(self.visible[i])

    def activate(self, app: App) -> None:
        st = catalog.state(app)
        if st == COMING_SOON:
            self.flash(f"{app.name} is coming soon.")
            return
        if st == NEEDS_SETUP:
            self.set_up(app)
            return
        if st == MISSING:
            target = catalog.expand(str(app.launch.get("target", "")))
            if messagebox.askyesno(f"{app.name} wasn't found",
                                   f"Asgard couldn't find:\n{target}\n\nChoose where {app.name} is now?",
                                   parent=self.root):
                self.set_up(app)
            return
        try:
            spec = catalog.build_spec(app)
        except CatalogError as exc:
            messagebox.showerror("Asgard", str(exc), parent=self.root)
            return
        if spec.kind == "internal":
            self._internal(spec.target)
            return
        if spec.kind == "url":
            webbrowser.open(spec.target)
            self.flash(f"Opened {app.name} in your browser.")
            return
        if spec.kind == "shell":
            try:
                winutil.open_path(spec.target)
                self.flash(f"Opening {app.name}…")
            except OSError as exc:
                messagebox.showerror(f"Couldn't open {app.name}", str(exc), parent=self.root)
            return
        if self.runner.is_running(app.id) and not messagebox.askyesno(
                app.name, f"{app.name} is already open. Open another window?", parent=self.root):
            return
        try:
            self.runner.start(app.id, app.name, spec)
        except LaunchError as exc:
            messagebox.showerror(f"Couldn't open {app.name}", str(exc), parent=self.root)
            return
        self.flash(f"Opening {app.name}…")
        self._schedule_render()

    def set_up(self, app: App) -> None:
        path = filedialog.askopenfilename(
            parent=self.root, title=f"Where is {app.name}? Choose the file you use to start it",
            filetypes=[("Apps and scripts", "*.pyw *.py *.exe *.ps1 *.cmd *.bat *.lnk"), ("All files", "*.*")])
        if not path:
            return
        launch = catalog.infer_launch(path)
        try:
            catalog.save_launch_override(app.id, launch, self.local)
        except (CatalogError, OSError) as exc:
            messagebox.showerror("Asgard", f"Couldn't save the location.\n\n{exc}", parent=self.root)
            return
        self.reload()
        updated = next((a for a in self.apps if a.id == app.id), None)
        if updated and catalog.state(updated) == READY:
            self.activate(updated)

    def _internal(self, command: str) -> None:
        if command == "uninstall":
            from . import valhalla
            if valhalla.confirm_and_run(self.root):
                self.close()
        else:
            messagebox.showerror("Asgard", f"Unknown internal command: {command}", parent=self.root)

    def flash(self, message: str, ms: int = 4000) -> None:
        self.hint.configure(text=message, fg=self.p["text2"])
        if self._flash_job:
            self.root.after_cancel(self._flash_job)
        self._flash_job = self.root.after(ms, lambda: self.hint.configure(text=self.hint_text, fg=self.p["text3"]))

    # ---- menus ---------------------------------------------------------

    def _menu_for_focused(self) -> None:
        if self.visible:
            x1, y1, x2, y2 = self.boxes[self.focus_index]
            cx = self.canvas.winfo_rootx() + int(x1 + self.px(24))
            cy = self.canvas.winfo_rooty() + int(y1 - self.canvas.canvasy(0) + self.px(24))
            self._tile_menu(self.visible[self.focus_index], cx, cy)

    def _tile_menu(self, app: App, x: int, y: int) -> None:
        st = catalog.state(app)
        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(label="Open", command=lambda: self.activate(app),
                         state="disabled" if st == COMING_SOON else "normal")
        kind = (app.launch or {}).get("type")
        if app.status == "external" or app.source == "local":
            menu.add_command(label="Change location…", command=lambda: self.set_up(app))
        target = catalog.expand(str(app.launch.get("target", ""))) if kind not in (None, "url", "internal") else ""
        if target and Path(target).exists():
            menu.add_command(label="Open file location", command=lambda: winutil.open_path(Path(target).parent))
        log = paths.log_dir() / f"{app.id}.log"
        if log.exists():
            menu.add_command(label="View log", command=lambda: winutil.open_in_editor(log))
        try:
            menu.tk_popup(x, y)
        finally:
            menu.grab_release()

    def _show_main_menu(self) -> None:
        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(label="Edit my tiles…", command=self._edit_tiles)
        menu.add_command(label="Reload tiles", accelerator="F5", command=lambda: self.reload(show_warnings=True))
        menu.add_command(label="Open Asgard folder", command=lambda: winutil.open_path(paths.data_dir()))
        menu.add_command(label="Back up Muninn now", command=self._backup_muninn,
                         state="normal" if self.muninn_status is not None else "disabled")
        menu.add_command(label="About Asgard", command=self._about)
        menu.add_separator()
        menu.add_command(label="Uninstall Asgard…", command=lambda: self._internal("uninstall"))
        b = self.menu_button
        try:
            menu.tk_popup(b.winfo_rootx(), b.winfo_rooty() + b.winfo_height())
        finally:
            menu.grab_release()

    def _edit_tiles(self) -> None:
        try:
            winutil.open_in_editor(catalog.ensure_local_manifest(self.local))
            self.flash("Save the file, then press F5 to reload your tiles.", 8000)
        except OSError as exc:
            messagebox.showerror("Asgard", str(exc), parent=self.root)

    def _about(self) -> None:
        import sqlite3
        if self.muninn_status is not None:
            db = f"Muninn: version {self.muninn_status.version}, {self.muninn_status.path}"
        elif self.muninn_error:
            db = f"Muninn: not available. {self.muninn_error}"
        else:
            db = "Muninn: starting"
        messagebox.showinfo("About Asgard", (
            f"Asgard {__version__}\n\nPython {sys.version.split()[0]}, SQLite {sqlite3.sqlite_version}\n"
            f"{sys.executable}\n\nCode: {paths.CODE_ROOT}\nData: {paths.data_dir()}\n{db}"), parent=self.root)

    def _backup_muninn(self) -> None:
        if muninn is None or self.muninn_status is None:
            return
        try:
            con = muninn.connect(self.muninn_status.path)
            try:
                target = muninn.backup(con, label="manual", keep=5, replace=True)
            finally:
                con.close()
        except Exception as exc:  # shown to the person; the details go to the log
            write_launcher_log(traceback.format_exc())
            messagebox.showerror("Asgard", f"Couldn't back up Muninn.\n\n{exc}", parent=self.root)
            return
        self.flash(f"Backed up Muninn to {target.name} in the backups folder.", 8000)

    # ---- Muninn ----------------------------------------------------------

    def _start_muninn(self) -> None:
        """Create or upgrade Muninn off the window's thread; a backup can take a moment."""
        self._badge_job = None
        if muninn is None:
            self.muninn_error = "Its files are missing from this install. Run setup again."
            return

        def work() -> None:
            try:
                self._muninn_result["status"] = muninn.prepare()
            except Exception as exc:  # reported once the window picks up the result
                self._muninn_result["error"] = exc
                self._muninn_result["trace"] = traceback.format_exc()

        self._muninn_thread = threading.Thread(target=work, name="muninn-prepare", daemon=True)
        self._muninn_thread.start()
        self._badge_job = self.root.after(100, self._await_muninn)

    def _await_muninn(self) -> None:
        self._badge_job = None
        if self._muninn_thread is not None and self._muninn_thread.is_alive():
            self._badge_job = self.root.after(100, self._await_muninn)
            return
        result = self._muninn_result
        if "error" in result:
            self.muninn_error = str(result["error"])
            write_launcher_log(f"Muninn could not start: {result.get('trace', self.muninn_error)}")
            self.flash("Muninn isn't available, so tiles show no counts. Details are in About.", 10000)
            return
        self.muninn_status = result.get("status")
        if self.muninn_status is not None and self.muninn_status.migrated and not self.muninn_status.created:
            self.flash(f"Muninn was updated to version {self.muninn_status.version}.", 6000)
        self._refresh_badges()

    def _refresh_badges(self) -> None:
        self._badge_job = None
        if muninn is not None and self.muninn_status is not None:
            fresh = muninn.tile_badges(self.muninn_status.path)
            self._last_badge_refresh = time.monotonic()
            if fresh != self.badges:
                self.badges = fresh
                self._schedule_render()
        self._badge_job = self.root.after(BADGE_REFRESH_MS, self._refresh_badges)

    def _on_app_focus(self, event: Any) -> None:
        """Coming back to Asgard refreshes the counts, at most every few seconds."""
        if self.muninn_status is None or time.monotonic() - self._last_badge_refresh < 5:
            return
        if self._badge_job:
            self.root.after_cancel(self._badge_job)
        self._refresh_badges()

    # ---- housekeeping --------------------------------------------------

    def _poll(self) -> None:
        try:
            finished = self.runner.poll()
            for done in finished:
                if done.crashed_early:
                    tail = log_tail(done.log_path)
                    msg = f"{done.name} closed right after starting (exit code {done.returncode})."
                    if tail:
                        msg += f"\n\nLast lines of its log:\n{tail}"
                    messagebox.showerror(f"{done.name} didn't start", msg, parent=self.root)
            running = self.runner.running_ids()
            if finished or running != self._last_running:
                self._last_running = running
                self._schedule_render()
        finally:
            self._poll_job = self.root.after(1000, self._poll)

    def _callback_error(self, exc_type: Any, exc: BaseException, tb: Any) -> None:
        text = "".join(traceback.format_exception(exc_type, exc, tb))
        write_launcher_log(text)
        messagebox.showerror("Asgard hit a problem",
                             f"{exc}\n\nDetails are in {paths.log_dir() / 'launcher.log'}", parent=self.root)

    def close(self) -> None:
        for job in (self._poll_job, self._flash_job, self._badge_job):
            if job:
                try:
                    self.root.after_cancel(job)
                except tk.TclError:
                    pass
        self.runner.close()
        self.root.destroy()


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def write_launcher_log(text: str) -> None:
    try:
        paths.log_dir().mkdir(parents=True, exist_ok=True)
        with open(paths.log_dir() / "launcher.log", "a", encoding="utf-8") as fh:
            fh.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')}\n{text}\n")
    except OSError:
        pass


def main(argv: Optional[List[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if "--uninstall" in args:
        from . import valhalla
        return valhalla.main(args)
    if tk is None:
        message = "Asgard needs Python with Tcl/Tk (tkinter). Ask IT for a Python install that includes Tcl/Tk."
        write_launcher_log(message)
        print(message, file=sys.stderr)
        return 2
    try:
        paths.data_dir().mkdir(parents=True, exist_ok=True)
        os.chdir(paths.data_dir())  # never hold the app folder open, so upgrades can replace it
    except OSError:
        pass
    winutil.enable_dpi_awareness()
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        write_launcher_log(f"Could not open a window: {exc}")
        return 2
    try:
        LauncherWindow(root)
        root.mainloop()
    except Exception as exc:  # last resort: pythonw has no console to print to
        write_launcher_log(traceback.format_exc())
        try:
            messagebox.showerror("Asgard hit a problem",
                                 f"{exc}\n\nDetails are in {paths.log_dir() / 'launcher.log'}")
        except Exception:
            pass
        return 1
    return 0
