"""An in-memory Jira Data Center for Odin's tests, and a temporary Muninn to go with it.

FakeJira answers the calls odin.jira.JiraClient makes, with issue and worklog JSON shaped like
Jira's REST API v2, and understands the few JQL queries Odin sends. fail("add_worklog", exc)
makes the next call of that name raise exc, which is how the tests reach timeouts and refusals.
OdinTestCase gives each test its own ASGARD_HOME with a fresh Muninn and Odin's folder in it.
"""
import datetime as dt
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "apps" / "odin"
for folder in (ROOT, APP):
    if str(folder) not in sys.path:
        sys.path.insert(0, str(folder))

from asgard import muninn  # noqa: E402
from odin.jira import JiraError  # noqa: E402

BASE_URL = "https://jira.example.gov"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "odin"


def jira_time(value: Any) -> str:
    if isinstance(value, dt.datetime):
        return value.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000+0000")
    return str(value)


class FakeJira:
    def __init__(self, user: str = "jdoe"):
        self.user = user
        self.issues: Dict[str, Dict[str, Any]] = {}       # key -> issue JSON
        self.ever_assigned: Dict[str, set] = {}           # key -> users it has been assigned to
        self.worklogs: Dict[str, List[Dict[str, Any]]] = {}
        self.deleted: List[Dict[str, Any]] = []           # {"worklogId", "updatedTime"}
        self.moved: Dict[str, str] = {}                   # old key -> current key
        self.created: List[Dict[str, Any]] = []
        self.transitions: List[tuple] = []
        self.posted: List[tuple] = []                     # add_worklog calls that landed
        self.searches: List[str] = []
        self.calls: List[str] = []
        self._fail: Dict[str, List[BaseException]] = {}
        self._next_issue = 10000
        self._next_worklog = 50000
        self.clock = dt.datetime(2026, 9, 24, 12, 0, tzinfo=dt.timezone.utc)

    # ---- test helpers --------------------------------------------------------------------------
    def fail(self, method: str, exc: BaseException, times: int = 1) -> None:
        self._fail.setdefault(method, []).extend([exc] * times)

    def _call(self, method: str) -> None:
        self.calls.append(method)
        queue = self._fail.get(method)
        if queue:
            raise queue.pop(0)

    def tick(self) -> str:
        self.clock += dt.timedelta(minutes=1)
        return jira_time(self.clock)

    def add_issue(self, key: str, summary: str = "An issue", status: str = "To Do", category: str = "new",
                  issuetype: str = "Task", parent: Optional[str] = None, assignee: Optional[str] = None,
                  labels: Optional[List[str]] = None, subtask: bool = False,
                  creator: Optional[str] = None) -> Dict[str, Any]:
        self._next_issue += 1
        now = self.tick()
        fields = {
            "summary": summary, "status": {"name": status, "statusCategory": {"key": category}},
            "issuetype": {"name": issuetype, "subtask": subtask}, "project": {"key": key.rsplit("-", 1)[0]},
            "parent": {"key": parent} if parent else None, "assignee": {"name": assignee} if assignee else None,
            "reporter": {"name": self.user}, "creator": {"name": creator or self.user}, "labels": list(labels or []),
            "components": [], "created": now,
            "updated": now, "resolution": None, "resolutiondate": None, "priority": {"name": "Medium"},
            "duedate": None,
        }
        issue = {"id": str(self._next_issue), "key": key, "fields": fields, "changelog": {"histories": []}}
        self.issues[key] = issue
        self.ever_assigned[key] = {assignee} if assignee else set()
        self.worklogs.setdefault(key, [])
        return issue

    def add_jira_worklog(self, key: str, seconds: int, started: Any, comment: str = "",
                         author: Optional[str] = None) -> Dict[str, Any]:
        self._next_worklog += 1
        issue = self.issues[key]
        now = self.tick()
        worklog = {"id": str(self._next_worklog), "issueId": issue["id"], "author": {"name": author or self.user},
                   "started": jira_time(started), "timeSpentSeconds": int(seconds), "comment": comment,
                   "created": now, "updated": now}
        self.worklogs[key].append(worklog)
        issue["fields"]["updated"] = now
        return worklog

    def delete_worklog(self, key: str, worklog_id: str) -> None:
        self.worklogs[key] = [w for w in self.worklogs[key] if w["id"] != worklog_id]
        self.issues[key]["fields"]["updated"] = self.tick()
        self.deleted.append({"worklogId": int(worklog_id), "updatedTime": int(self.clock.timestamp() * 1000)})

    def _current(self, key: str) -> Optional[str]:
        key = self.moved.get(key, key)
        return key if key in self.issues else None

    # ---- the API Odin calls --------------------------------------------------------------------
    def server_info(self):
        self._call("server_info")
        return {"version": "9.12.0", "deploymentType": "Server"}

    def myself(self):
        self._call("myself")
        return {"name": self.user, "key": self.user, "displayName": "Jordan Doe"}

    def personal_access_tokens(self):
        return None      # a Jira that does not expose token expiry

    def fields(self):
        self._call("fields")
        return [{"id": "customfield_10008", "name": "Epic Link"}, {"id": "customfield_10002", "name": "Story Points"},
                {"id": "customfield_10005", "name": "Sprint"}, {"id": "summary", "name": "Summary"}]

    def statuses(self):
        self._call("statuses")
        return [{"name": "To Do", "statusCategory": {"key": "new"}},
                {"name": "In Progress", "statusCategory": {"key": "indeterminate"}},
                {"name": "Done", "statusCategory": {"key": "done"}}]

    def get_issue(self, key, fields=""):
        self._call("get_issue")
        current = self._current(key)
        if current is None:
            raise JiraError(f"GET issue {key} -> HTTP 404", 404)
        return self.issues[current]

    def get_project(self, key):
        return {"issueTypes": [{"name": "Sub-task", "subtask": True}, {"name": "Task", "subtask": False}]}

    def create_issue(self, fields):
        self._call("create_issue")
        self.created.append(fields)
        key = f"{fields['project']['key']}-{900 + len(self.created)}"
        assignee = (fields.get("assignee") or {}).get("name")
        issue = self.add_issue(key, fields["summary"], issuetype=fields["issuetype"]["name"],
                               parent=(fields.get("parent") or {}).get("key"), assignee=assignee,
                               labels=fields.get("labels"), subtask=True)
        return issue["key"]

    def issue(self, key, extra_fields=(), changelog=True):
        self._call("issue")
        current = self._current(key)
        return json.loads(json.dumps(self.issues[current])) if current else None

    def _matches(self, jql: str) -> List[Dict[str, Any]]:
        clauses = [c.strip() for c in re.split(r"\s+AND\s+", jql.split(" ORDER BY ")[0])]
        out = list(self.issues.values())
        for clause in clauses:
            if clause == "assignee was currentUser()":
                out = [i for i in out if self.user in self.ever_assigned.get(i["key"], set())]
            elif clause == "worklogAuthor = currentUser()":
                out = [i for i in out if any(w["author"]["name"] == self.user for w in self.worklogs[i["key"]])]
            elif m := re.match(r"^parent = (\S+)$", clause):
                out = [i for i in out if (i["fields"].get("parent") or {}).get("key") == m.group(1)]
            elif m := re.match(r"^key in \((.*)\)$", clause):
                keys = [k.strip() for k in m.group(1).split(",")]
                missing = [k for k in keys if self._current(k) is None]
                if missing:
                    raise JiraError(f"GET search -> HTTP 400: An issue with key '{missing[0]}' does not exist", 400)
                wanted = {self._current(k) for k in keys}
                out = [i for i in out if i["key"] in wanted]
            elif m := re.match(r'^project = "([^"]+)"$', clause):
                out = [i for i in out if i["fields"]["project"]["key"] == m.group(1)]
            elif m := re.match(r'^labels = "([^"]+)"$', clause):
                out = [i for i in out if m.group(1) in i["fields"]["labels"]]
            elif clause == "creator = currentUser()":
                out = [i for i in out if (i["fields"].get("creator") or {}).get("name") == self.user]
            elif m := re.match(r"^updated >= '(\d{4}/\d\d/\d\d \d\d:\d\d)'$", clause):
                since = dt.datetime.strptime(m.group(1), "%Y/%m/%d %H:%M").astimezone(dt.timezone.utc)
                out = [i for i in out if muninn.from_ts(muninn.odin.parse_time(i["fields"]["updated"])) >= since]
            elif re.match(r"^(updated|worklogDate) >= -\d+d$", clause):
                pass
            else:
                raise JiraError(f"GET search -> HTTP 400: FakeJira doesn't understand {clause!r}", 400)
        return sorted(out, key=lambda i: (i["fields"]["updated"], i["key"]))

    def search(self, jql, extra_fields=(), changelog=False, page_size=50, limit=None):
        self._call("search")
        self.searches.append(jql)
        found = [json.loads(json.dumps(i)) for i in self._matches(jql)]
        if limit is not None:
            found = found[:limit]
        for start in range(0, len(found), page_size):
            yield found[start:start + page_size]

    def search_issue_keys(self, jql, max_results=5):
        self._call("search_issue_keys")
        self.searches.append(jql)
        return [i["key"] for i in self._matches(jql)][:max_results]

    def issue_worklogs(self, key):
        self._call("issue_worklogs")
        current = self._current(key)
        if current is None:
            raise JiraError(f"GET worklog {key} -> HTTP 404", 404)
        return json.loads(json.dumps(self.worklogs[current]))

    def find_worklog(self, key, marker):
        for w in self.issue_worklogs(key):
            if marker in (w.get("comment") or ""):
                return w
        return None

    def add_worklog(self, key, seconds, started, comment):
        self._call("add_worklog")
        current = self._current(key)
        if current is None:
            raise JiraError(f"POST worklog {key} -> HTTP 404", 404)
        worklog = self.add_jira_worklog(current, max(60, int(seconds)), started, comment)
        self.posted.append((current, max(60, int(seconds)), jira_time(started), comment))
        return worklog["id"]

    def transition(self, key, name):
        self._call("transition")
        self.transitions.append((key, name))
        issue = self.issues[self._current(key)]
        issue["fields"]["status"] = {"name": name, "statusCategory": {"key": "done"}}
        issue["fields"]["updated"] = self.tick()
        return True

    def deleted_worklogs(self, since_ms, max_pages=50):
        self._call("deleted_worklogs")
        values = [d for d in self.deleted if d["updatedTime"] >= since_ms]
        yield values, int(self.clock.timestamp() * 1000)


class LandsThenFails(FakeJira):
    """add_worklog lands in Jira, then the answer is lost (a proxy timeout on the way back)."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.lose_answers = 0

    def add_worklog(self, key, seconds, started, comment):
        worklog_id = super().add_worklog(key, seconds, started, comment)
        if self.lose_answers:
            self.lose_answers -= 1
            raise JiraError("POST worklog failed: timed out", ambiguous=True)
        return worklog_id


class OdinTestCase(unittest.TestCase):
    """A fresh ASGARD_HOME with Muninn prepared, and Odin's folder in it."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self._saved_home = os.environ.get("ASGARD_HOME")
        os.environ["ASGARD_HOME"] = str(self.home)
        self.addCleanup(self._restore_home)
        muninn.prepare(self.home / "muninn.db", backups=self.home / "backups")
        self.data = self.home / "odin"
        self.data.mkdir()

    def _restore_home(self) -> None:
        if self._saved_home is None:
            os.environ.pop("ASGARD_HOME", None)
        else:
            os.environ["ASGARD_HOME"] = self._saved_home

    def open(self, readonly: bool = False):
        from odin import store
        con = store.open_muninn(readonly=readonly)
        self.addCleanup(con.close)
        return con

    def peek(self):
        """A plain connection for assertions (no guard)."""
        con = muninn.connect(self.home / "muninn.db")
        self.addCleanup(con.close)
        return con

    NOW = dt.datetime(2026, 9, 24, 12, 0, tzinfo=dt.timezone.utc)

    def push(self, meetings, cfg, jira=None, dry_run=False, now=None, window=None, source="outlook-com",
             seen_before=None):
        """What `odin push` does between opening Muninn and the summary (cli._live, steps 3 and 4)."""
        from odin import collect, store
        from odin.sync import run
        now = now or self.NOW
        con = self.open(readonly=dry_run)
        if dry_run:
            return run(meetings, cfg, con, None, data_dir=self.data, dry_run=True, now=now, seen_before=seen_before)
        sid = store.jira_source(con, cfg["jira"]["base_url"])
        store.remember_me(con, sid, jira.myself())
        ctx = collect.context(con, jira, sid)
        ids, self.calendar = store.store_calendar(con, store.calendar_source(con, source), meetings, window)
        return run(meetings, cfg, con, jira, data_dir=self.data, now=now, event_ids=ids, ctx=ctx,
                   seen_before=seen_before)
