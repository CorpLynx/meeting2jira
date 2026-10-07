"""Baldur's window: a week of days, one day's tickets and report, approve, reject, copy for timesheet.

Asgard's tile starts it (apps/baldur/baldur.pyw). Everything that reads git or Muninn runs
on a worker thread with its own connection and the window polls for the answer with
after(), the launcher's pattern, so the window never freezes and never holds Muninn's
write lock while it waits for you. desk.py holds what the window shows and does.

Keyboard: arrows move in either list, Enter edits the selected ticket's figure, Delete
puts it back to the estimate, F5 reloads, Ctrl+Left and Ctrl+Right change week.
"""
from __future__ import annotations

import datetime as dt
import queue
import sqlite3
import sys
import threading
import time
import traceback
from typing import Any, Callable, Dict, List, Optional, Tuple

from asgard import muninn, paths, winutil
from asgard.launcher import DARK, LIGHT

from . import cli, desk
from . import estimate as E
from . import settings as config
from .collect import CollectError
from .github import GitHubError
from .gitread import GitError

try:
    import tkinter as tk
    from tkinter import messagebox, simpledialog, ttk
    from tkinter import font as tkfont
except ImportError:          # main() explains what to do
    tk = None  # type: ignore[assignment]

EXPECTED = (desk.DeskError, muninn.MuninnError, CollectError, config.SettingsError, GitError, cli.CliError,
            GitHubError, sqlite3.Error)
SETUP_HINT = ("Set Baldur up first, from Command Prompt in its folder:\n"
              "  baldur.cmd setup --from-git --project PROJ --root C:\\src")


def open_db() -> sqlite3.Connection:
    return muninn.open_app("baldur", supported=cli.SCHEMA)


def log_problem(text: str) -> None:
    try:
        paths.log_dir().mkdir(parents=True, exist_ok=True)
        with open(paths.log_dir() / "baldur.log", "a", encoding="utf-8") as fh:
            fh.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')}\n{text}\n")
    except OSError:
        pass


class Worker:
    """One job at a time off the window's thread; the window polls for the result."""

    def __init__(self, root: "tk.Tk", connect: Callable[[], sqlite3.Connection], synchronous: bool = False) -> None:
        self.root, self.connect, self.synchronous = root, connect, synchronous
        self.results: "queue.Queue[Tuple[Callable[[Any], None], Any]]" = queue.Queue()
        self.thread: Optional[threading.Thread] = None

    def _call(self, job: Callable[[sqlite3.Connection], Any]) -> Tuple[bool, Any]:
        try:
            con = self.connect()
        except Exception as exc:              # noqa: BLE001 - reported by the window
            return False, exc
        try:
            return True, job(con)
        except Exception as exc:              # noqa: BLE001
            exc.trace = traceback.format_exc()  # type: ignore[attr-defined]
            return False, exc
        finally:
            con.close()

    def run(self, job: Callable[[sqlite3.Connection], Any], done: Callable[[Any], None],
            failed: Callable[[BaseException], None]) -> None:
        if self.synchronous:                  # tests: no thread, same order of events
            ok, value = self._call(job)
            (done if ok else failed)(value)
            return

        def work() -> None:
            ok, value = self._call(job)
            self.results.put((done if ok else failed, value))

        self.thread = threading.Thread(target=work, name="baldur-job", daemon=True)
        self.thread.start()
        self.root.after(50, self._poll)

    def _poll(self) -> None:
        try:
            callback, value = self.results.get_nowait()
        except queue.Empty:
            self.root.after(50, self._poll)
            return
        callback(value)


class BaldurWindow:
    def __init__(self, root: "tk.Tk", *, connect: Callable[[], sqlite3.Connection] = open_db,
                 load_settings: Callable[[], config.Settings] = config.load, synchronous: bool = False,
                 today: Optional[dt.date] = None, dark: Optional[bool] = None) -> None:
        self.root = root
        self.load_settings = load_settings
        self.settings = load_settings()
        self.today = today or dt.date.today()
        self.monday = desk.week_of(self.today)
        self.day: Optional[dt.date] = None
        self.view: Optional[desk.DayView] = None
        self.week: List[desk.DayRow] = []
        self.figures: Dict[Tuple[dt.date, str], int] = {}   # figures you typed, not yet approved
        self.worker = Worker(root, connect, synchronous)
        self.busy = False
        self.dark = winutil.apps_use_dark_theme() if dark is None else dark
        self.p = DARK if self.dark else LIGHT
        self._github_job: Optional[str] = None
        self._init_fonts()
        self._init_window()
        self._build()
        self.reload()
        if not synchronous:
            self._github_job = self.root.after(1500, self.github_tick)

    # ---- building ------------------------------------------------------

    def _init_fonts(self) -> None:
        families = set(tkfont.families(self.root))
        default = tkfont.nametofont("TkDefaultFont").actual("family")
        base = next((f for f in ("Segoe UI", "Noto Sans", "DejaVu Sans", "Helvetica") if f in families), default)
        mono = next((f for f in ("Cascadia Mono", "Consolas", "Menlo", "DejaVu Sans Mono") if f in families),
                    tkfont.nametofont("TkFixedFont").actual("family"))
        self.f_title = tkfont.Font(root=self.root, family=base, size=18, weight="bold")
        self.f_text = tkfont.Font(root=self.root, family=base, size=10)
        self.f_bold = tkfont.Font(root=self.root, family=base, size=10, weight="bold")
        self.f_mono = tkfont.Font(root=self.root, family=mono, size=10)

    def _init_window(self) -> None:
        r, p = self.root, self.p
        r.title("Baldur")
        r.configure(bg=p["bg"])
        sw, sh = r.winfo_screenwidth(), r.winfo_screenheight()
        w, h = min(1180, sw - 60), min(760, sh - 100)
        r.geometry(f"{w}x{h}+{max(0, (sw - w) // 2)}+{max(0, (sh - h) // 3)}")
        r.minsize(760, 480)
        style = ttk.Style(r)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TFrame", background=p["bg"])
        style.configure("TLabel", background=p["bg"], foreground=p["text"], font=self.f_text)
        style.configure("Sub.TLabel", foreground=p["text2"])
        style.configure("Title.TLabel", font=self.f_title)
        style.configure("Head.TLabel", font=self.f_bold)
        style.configure("TButton", font=self.f_text, padding=(10, 4))
        style.configure("Accent.TButton", foreground=p["badge_fg"], background=p["accent"])
        style.map("Accent.TButton", background=[("disabled", p["border"]), ("active", p["accent"])])
        style.configure("Treeview", background=p["card"], fieldbackground=p["card"], foreground=p["text"],
                        font=self.f_text, rowheight=24, bordercolor=p["border"])
        style.configure("Treeview.Heading", background=p["bg"], foreground=p["text2"], font=self.f_bold)
        style.map("Treeview", background=[("selected", p["accent"])], foreground=[("selected", p["badge_fg"])])
        r.protocol("WM_DELETE_WINDOW", self.close)
        r.report_callback_exception = self._callback_error  # type: ignore[assignment]
        r.bind("<F5>", lambda e: self.reload())
        r.bind("<Control-Left>", lambda e: self.move_week(-1))
        r.bind("<Control-Right>", lambda e: self.move_week(1))
        r.update_idletasks()
        winutil.set_dark_title_bar(r, self.dark)

    def _build(self) -> None:
        p = self.p
        top = ttk.Frame(self.root, padding=(20, 16, 20, 8))
        top.pack(side="top", fill="x")
        ttk.Label(top, text="Baldur", style="Title.TLabel").pack(side="left")
        ttk.Label(top, text="   Development time from git. You approve; Odin posts.",
                  style="Sub.TLabel").pack(side="left", pady=(8, 0))
        self.b_estimate = ttk.Button(top, text="Estimate this week", command=self.estimate_week)
        self.b_estimate.pack(side="right")
        self.b_collect = ttk.Button(top, text="Collect from git", command=self.collect)
        self.b_collect.pack(side="right", padx=(0, 8))

        nav = ttk.Frame(self.root, padding=(20, 0, 20, 8))
        nav.pack(side="top", fill="x")
        self.b_prev = ttk.Button(nav, text="< Week", command=lambda: self.move_week(-1))
        self.b_prev.pack(side="left")
        self.b_this = ttk.Button(nav, text="This week", command=self.this_week)
        self.b_this.pack(side="left", padx=6)
        self.b_next = ttk.Button(nav, text="Week >", command=lambda: self.move_week(1))
        self.b_next.pack(side="left")
        self.week_label = ttk.Label(nav, text="", style="Head.TLabel")
        self.week_label.pack(side="left", padx=12)

        self.status = ttk.Label(self.root, text="", style="Sub.TLabel", padding=(20, 4, 20, 10))
        self.status.pack(side="bottom", fill="x")

        body = ttk.Panedwindow(self.root, orient="horizontal")
        body.pack(side="top", fill="both", expand=True, padx=20)

        left = ttk.Frame(body)
        self.days = ttk.Treeview(left, columns=("dev", "tickets", "review"), show="tree headings",
                                 selectmode="browse", height=7)
        self.days.heading("#0", text="Day")
        self.days.heading("dev", text="Dev")
        self.days.heading("tickets", text="Tickets")
        self.days.heading("review", text="Review")
        self.days.column("#0", width=120, stretch=False)
        self.days.column("dev", width=60, anchor="e", stretch=False)
        self.days.column("tickets", width=220)
        self.days.column("review", width=130)
        self.days.pack(side="top", fill="both", expand=True)
        self.days.bind("<<TreeviewSelect>>", lambda e: self._day_selected())
        self.reviews_label = ttk.Label(left, text="Pull requests", style="Head.TLabel", padding=(0, 12, 0, 4))
        self.reviews_label.pack(side="top", anchor="w")
        self.reviews = ttk.Treeview(left, columns=("what", "who"), show="tree headings", selectmode="browse",
                                    height=5)
        self.reviews.heading("#0", text="Pull request")
        self.reviews.heading("what", text="Waiting on")
        self.reviews.heading("who", text="Who")
        self.reviews.column("#0", width=150, stretch=False)
        self.reviews.column("what", width=230)
        self.reviews.column("who", width=110, stretch=False)
        self.reviews.pack(side="top", fill="x")
        self.reviews.bind("<Return>", lambda e: self.open_review())
        self.reviews.bind("<Double-1>", lambda e: self.open_review())
        self.review_urls: Dict[str, str] = {}
        body.add(left, weight=2)

        right = ttk.Frame(body, padding=(16, 0, 0, 0))
        self.day_label = ttk.Label(right, text="Pick a day", style="Head.TLabel")
        self.day_label.pack(side="top", anchor="w", pady=(0, 6))
        self.tickets = ttk.Treeview(right, columns=("estimate", "figure", "jira", "review"), show="tree headings",
                                    selectmode="browse", height=5)
        for col, text, width, anchor in (("#0", "Ticket", 110, "w"), ("estimate", "Estimate", 80, "e"),
                                         ("figure", "Your figure", 90, "e"), ("jira", "In Jira", 70, "e"),
                                         ("review", "Review", 320, "w")):
            self.tickets.heading(col, text=text)
            self.tickets.column(col, width=width, anchor=anchor, stretch=(col == "review"))
        self.tickets.pack(side="top", fill="x")
        self.tickets.bind("<Return>", lambda e: self.edit_figure())
        self.tickets.bind("<Double-1>", lambda e: self.edit_figure())
        self.tickets.bind("<Delete>", lambda e: self.clear_figure())

        actions = ttk.Frame(right, padding=(0, 8, 0, 8))
        actions.pack(side="top", fill="x")
        self.b_edit = ttk.Button(actions, text="Edit figure...", command=self.edit_figure)
        self.b_edit.pack(side="left")
        self.b_approve = ttk.Button(actions, text="Approve day", style="Accent.TButton", command=self.approve)
        self.b_approve.pack(side="left", padx=6)
        self.b_reject = ttk.Button(actions, text="Reject day", command=self.reject)
        self.b_reject.pack(side="left")
        self.b_copy = ttk.Button(actions, text="Copy for timesheet", command=self.copy_timesheet)
        self.b_copy.pack(side="right")

        holder = ttk.Frame(right)
        holder.pack(side="top", fill="both", expand=True)
        self.report = tk.Text(holder, wrap="none", font=self.f_mono, bg=p["card"], fg=p["text"],
                              insertbackground=p["text"], relief="flat", highlightthickness=1,
                              highlightbackground=p["border"], padx=10, pady=8, takefocus=True)
        scroll = ttk.Scrollbar(holder, orient="vertical", command=self.report.yview)
        self.report.configure(yscrollcommand=scroll.set, state="disabled")
        scroll.pack(side="right", fill="y")
        self.report.pack(side="left", fill="both", expand=True)
        body.add(right, weight=3)
        self.buttons = [self.b_estimate, self.b_collect, self.b_prev, self.b_this, self.b_next,
                        self.b_edit, self.b_approve, self.b_reject, self.b_copy]

    # ---- jobs ----------------------------------------------------------

    def _job(self, label: str, job: Callable[[sqlite3.Connection], Any], done: Callable[[Any], None],
             quiet: bool = False) -> bool:
        """Run job off the window's thread. quiet: a background job, whose problems go to the status line."""
        if self.busy:
            if not quiet:
                self.say("Still working on the last request...")
            return False
        self.busy = True
        for b in self.buttons:
            b.state(["disabled"])
        self.say(label)

        def finish(value: Any) -> None:
            self._enable()
            done(value)

        def failed(exc: BaseException) -> None:
            if not quiet:
                self._failed(exc)
                return
            self._enable()
            if not isinstance(exc, EXPECTED):
                log_problem(getattr(exc, "trace", "") or repr(exc))
            self.say(f"GitHub: {exc}")

        self.worker.run(job, finish, failed)
        return True

    def _enable(self) -> None:
        self.busy = False
        for b in self.buttons:
            b.state(["!disabled"])
        self._update_actions()

    def _failed(self, exc: BaseException) -> None:
        self._enable()
        if isinstance(exc, EXPECTED):
            text = str(exc)
            if "doesn't know which commits are yours" in text or "project_keys" in text:
                text += "\n\n" + SETUP_HINT
            self.say(text.splitlines()[0])
            messagebox.showwarning("Baldur", text, parent=self.root)
            return
        log_problem(getattr(exc, "trace", "") or repr(exc))
        self.say(f"Something went wrong: {exc}")
        messagebox.showerror("Baldur hit a problem", f"{exc}\n\nDetails are in {paths.log_dir() / 'baldur.log'}",
                             parent=self.root)

    def say(self, text: str) -> None:
        self.status.configure(text=text)

    # ---- the week ------------------------------------------------------

    def reload(self) -> None:
        self.settings = self.load_settings()
        if self.settings.broken:
            self.say(f"{config.settings_path()} has a mistake in it, so Baldur won't run until it's fixed: "
                     f"{self.settings.warnings[0]}")
            for b in self.buttons:
                b.state(["disabled"])
            return
        monday, settings = self.monday, self.settings
        self._job("Reading the week...", lambda con: desk.load_week(con, settings, monday), self._show_week)

    def move_week(self, step: int) -> None:
        self.monday += dt.timedelta(days=7 * step)
        self.day = None
        self.reload()

    def this_week(self) -> None:
        self.monday = desk.week_of(self.today)
        self.day = None
        self.reload()

    def _show_week(self, rows: List[desk.DayRow]) -> None:
        self.week = rows
        sunday = self.monday + dt.timedelta(days=6)
        self.week_label.configure(text=f"Week of {self.monday:%a %d %b} to {sunday:%a %d %b %Y}")
        self.days.delete(*self.days.get_children())
        for row in rows:
            self.days.insert("", "end", iid=row.day.isoformat(), text=f"{row.day:%a %d %b}",
                             values=(E.fmt(row.dev) if row.has_work else "-", row.tickets, row.review))
        total = sum(r.dev for r in rows)
        self.say(f"{E.fmt(total)} of development estimated this week. Pick a day to review it.")
        pick = self.day or next((r.day for r in reversed(rows) if r.has_work and r.day <= self.today), None)
        if pick:
            self.days.selection_set(pick.isoformat())
            self.days.see(pick.isoformat())
            self._day_selected()
        else:
            self._show_day(None)

    def _day_selected(self) -> None:
        sel = self.days.selection()
        if not sel:
            return
        day = dt.date.fromisoformat(sel[0])
        if self.view is not None and self.view.day == day and self.day == day:
            return
        self.day = day
        settings = self.settings
        self._job(f"Reading {day:%a %d %b}...", lambda con: desk.load_day(con, settings, day), self._show_day)

    # ---- one day -------------------------------------------------------

    def _show_day(self, view: Optional[desk.DayView]) -> None:
        self.view = view
        self.tickets.delete(*self.tickets.get_children())
        self.report.configure(state="normal")
        self.report.delete("1.0", "end")
        if view is None:
            self.day_label.configure(text="No commits of yours this week")
            self.report.configure(state="disabled")
            self._update_actions()
            return
        self.day_label.configure(text=f"{view.day:%A %d %B %Y}")
        for t in view.tickets:
            typed = self.figures.get((view.day, t.key))
            figure = E.fmt(typed) + " (yours)" if typed is not None else E.fmt(t.figure)
            jira = "?" if t.held is None else E.fmt(t.held)
            self.tickets.insert("", "end", iid=t.key, text=t.key,
                                values=(E.fmt(t.estimate), figure, jira, t.problem or t.review))
        if view.tickets:
            first = view.tickets[0].key
            self.tickets.selection_set(first)
            self.tickets.focus(first)
        self.report.insert("1.0", view.report)
        self.report.configure(state="disabled")
        self._update_actions()
        self.say(f"{view.day:%a %d %b}: edit any figure, then approve or reject the day.")

    def _update_actions(self) -> None:
        view = self.view
        has_open = bool(view and any(t.approvable and (t.open_id is not None or t.approved_id is None)
                                     for t in view.tickets))
        for b, ok in ((self.b_approve, has_open), (self.b_reject, has_open),
                      (self.b_edit, bool(view and view.tickets)), (self.b_copy, view is not None)):
            b.state(["!disabled"] if ok and not self.busy else ["disabled"])

    def selected_ticket(self) -> Optional[desk.TicketRow]:
        if self.view is None:
            return None
        sel = self.tickets.selection()
        return next((t for t in self.view.tickets if sel and t.key == sel[0]), None)

    def edit_figure(self, answer: Optional[str] = None) -> None:
        t, view = self.selected_ticket(), self.view
        if t is None or view is None:
            return
        if answer is None:
            answer = simpledialog.askstring(
                "Edit figure", f"Minutes for {t.key} on {view.day:%a %d %b}\n(like 1h15m, 75 or 1:15):",
                initialvalue=E.fmt(self.figures.get((view.day, t.key), t.figure)), parent=self.root)
        if answer is None or not answer.strip():
            return
        try:
            minutes = cli.parse_day_minutes(answer)
        except cli.CliError as exc:
            messagebox.showwarning("Baldur", str(exc), parent=self.root)
            return
        if t.approved_id is not None and t.open_id is None:
            if minutes == t.approved:
                return
            if not messagebox.askyesno("Change an approved figure",
                                       f"Approve {E.fmt(minutes)} for {t.key} instead of {E.fmt(t.approved)}? "
                                       "Odin posts the difference.", parent=self.root):
                return
            approved_id = t.approved_id
            self._job(f"Approving {t.key} {E.fmt(minutes)}...",
                      lambda con: desk.change_figure(con, approved_id, minutes), lambda _: self._refresh_day())
            return
        if not t.approvable:
            messagebox.showinfo("Baldur", f"{t.key} has nothing to approve on this day.", parent=self.root)
            return
        self.figures[(view.day, t.key)] = minutes
        self._show_day(view)
        self.tickets.selection_set(t.key)

    def clear_figure(self) -> None:
        t, view = self.selected_ticket(), self.view
        if t is not None and view is not None and self.figures.pop((view.day, t.key), None) is not None:
            self._show_day(view)
            self.tickets.selection_set(t.key)

    def approve(self, confirm: bool = True) -> None:
        view = self.view
        if view is None:
            return
        figures = {k: v for (d, k), v in self.figures.items() if d == view.day}
        lines = [f"  {t.key}  {E.fmt(figures.get(t.key, t.estimate))}" for t in view.tickets
                 if t.approvable and (t.open_id is not None or t.approved_id is None)]
        if confirm and not messagebox.askyesno(
                "Approve day", f"Approve {view.day:%a %d %b}?\n\n" + "\n".join(lines) +
                "\n\nOdin posts approved time to Jira on its next run.", parent=self.root):
            return
        day, settings = view.day, self.settings

        def done(ids: List[int]) -> None:
            for key in list(figures):
                self.figures.pop((day, key), None)
            self._refresh_day()
            self.say(f"Approved {len(ids)} ticket(s) for {day:%a %d %b}.")

        self._job("Approving...", lambda con: desk.approve_day(con, settings, day, figures), done)

    def reject(self, confirm: bool = True) -> None:
        view = self.view
        if view is None:
            return
        if confirm and not messagebox.askyesno("Reject day", f"Reject the estimate for {view.day:%a %d %b}? "
                                               "Nothing is posted for it.", parent=self.root):
            return
        day, settings = view.day, self.settings
        self._job("Rejecting...", lambda con: desk.reject_day(con, settings, day), lambda _: self._refresh_day())

    def _refresh_day(self) -> None:
        self.view = None
        self.reload()

    def copy_timesheet(self) -> str:
        if self.view is None:
            return ""
        text = desk.timesheet(self.view)
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.say(f"Copied {self.view.day:%a %d %b} for your timesheet.")
        return text

    def estimate_week(self) -> None:
        first, last, settings = self.monday, self.monday + dt.timedelta(days=6), self.settings

        def done(result: Any) -> None:
            new = [c for c in result.changes if c.action in ("new", "updated")]
            self.view = None
            self.reload()
            self.say(f"Stored {len(new)} new or changed proposal(s) for this week.")

        self._job("Estimating the week...", lambda con: desk.estimate(con, settings, first, last), done)

    def collect(self) -> None:
        settings = self.settings

        def done(result: Any) -> None:
            failed = result.failed
            self.view = None
            self.reload()
            text = f"Read {len(result.repos)} repositories."
            if failed:
                text += " Couldn't read: " + ", ".join(f"{r.name} ({r.error})" for r in failed)
            self.say(text)

        self._job("Reading your repositories (this can take a minute)...", lambda con: desk.collect(con, settings),
                  done)

    # ---- pull requests -------------------------------------------------

    def github_tick(self) -> None:
        """Sync GitHub now (when it's on and a token is saved), then every poll_minutes."""
        self._github_job = None
        settings = self.settings

        def job(con: sqlite3.Connection) -> Tuple[Any, List[desk.ReviewItem]]:
            result = desk.sync_github(con, settings) if not settings.broken else None
            return result, desk.review_items(con)

        def done(value: Tuple[Any, List[desk.ReviewItem]]) -> None:
            result, items = value
            self.show_reviews(items, synced=result is not None)

        started = self._job("Checking GitHub...", job, done, quiet=True)
        minutes = int(settings.values.get("poll_minutes", 5)) if not settings.broken else 5
        self._github_job = self.root.after(minutes * 60_000 if started else 5000, self.github_tick)

    def show_reviews(self, items: List[desk.ReviewItem], synced: bool = True) -> None:
        self.reviews.delete(*self.reviews.get_children())
        self.review_urls = {}
        for n, item in enumerate(items):
            iid = f"r{n}"
            if item.kind == "requested":
                what = "your review" + (" (draft)" if item.draft else "") + f": {item.title}"
            else:
                what = f"{item.state.replace('_', ' ')} your PR: {item.title}"
            self.reviews.insert("", "end", iid=iid, text=item.label, values=(what, item.who))
            self.review_urls[iid] = item.url
        asked = sum(1 for i in items if i.kind == "requested")
        host = self.settings.github_host() if not self.settings.broken else None
        if not host:
            self.reviews_label.configure(text="Pull requests: GitHub is off")
        elif not synced and not items:
            self.reviews_label.configure(text="Pull requests: no token yet (baldur.cmd github token)")
        else:
            self.reviews_label.configure(text=f"Pull requests: {asked} {'review' if asked == 1 else 'reviews'} "
                                              "requested of you")
        if synced:
            self.say(f"GitHub checked at {dt.datetime.now():%H:%M}.")

    def open_review(self) -> Optional[str]:
        sel = self.reviews.selection()
        url = self.review_urls.get(sel[0]) if sel else None
        if url and url.startswith("https://"):
            import webbrowser
            webbrowser.open(url)
        return url

    # ---- housekeeping --------------------------------------------------

    def _callback_error(self, exc_type: Any, exc: BaseException, tb: Any) -> None:
        log_problem("".join(traceback.format_exception(exc_type, exc, tb)))
        messagebox.showerror("Baldur hit a problem", f"{exc}\n\nDetails are in {paths.log_dir() / 'baldur.log'}",
                             parent=self.root)

    def close(self) -> None:
        if self._github_job:
            try:
                self.root.after_cancel(self._github_job)
            except tk.TclError:
                pass
        self.root.destroy()


def main(argv: Optional[List[str]] = None) -> int:
    if tk is None:
        print("Baldur's window needs Python with Tcl/Tk (tkinter). The command line works without it: baldur.cmd")
        return 2
    root = tk.Tk()
    try:
        con = open_db()
        con.close()
    except muninn.MuninnError as exc:
        root.withdraw()
        messagebox.showerror("Baldur", str(exc), parent=root)
        root.destroy()
        return 1
    BaldurWindow(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
