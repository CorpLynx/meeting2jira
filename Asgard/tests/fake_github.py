"""A small fake GitHub Enterprise Server REST API on 127.0.0.1, for Baldur's GitHub tests.

It answers what Baldur reads (/user, a repository's pulls, one pull, its reviews and commits,
and the issue search), checks the token, sends ETags and answers If-None-Match with 304, pages
long lists with Link headers, and records every request so a test can see what was asked.
"""
from __future__ import annotations

import hashlib
import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple

TOKEN = "ghp_fake_token_for_tests"


def pr(number: int, title: str, author: str, head: str, *, state: str = "open", merged_at: Optional[str] = None,
       draft: bool = False, requested: Tuple[str, ...] = (), created: str = "2026-09-30T14:00:00Z",
       updated: str = "2026-10-01T15:00:00Z", repo: str = "csb/asgard",
       merge_commit_sha: Optional[str] = None) -> Dict[str, Any]:
    return {"number": number, "title": title, "user": {"login": author}, "head": {"ref": head},
            "state": "closed" if merged_at else state, "merged_at": merged_at,
            "closed_at": merged_at, "draft": draft, "created_at": created, "updated_at": updated,
            "requested_reviewers": [{"login": r} for r in requested],
            "merge_commit_sha": merge_commit_sha,
            "html_url": f"https://github.agency.gov/{repo}/pull/{number}"}


def review(rid: int, who: str, state: str, at: str = "2026-10-01T16:00:00Z") -> Dict[str, Any]:
    return {"id": rid, "user": {"login": who}, "state": state, "submitted_at": at}


class FakeGitHub:
    def __init__(self, login: str = "bdoe", token: str = TOKEN, page_size: int = 100) -> None:
        self.login, self.token, self.page_size = login, token, page_size
        self.pulls: Dict[str, List[Dict[str, Any]]] = {}
        self.reviews: Dict[Tuple[str, int], List[Dict[str, Any]]] = {}
        self.commits: Dict[Tuple[str, int], List[str]] = {}
        self.requested: List[Tuple[str, int]] = []
        self.status: Dict[str, int] = {}             # path -> forced HTTP status
        self.redirect: Dict[str, str] = {}           # path -> Location of a 302
        self.requests: List[Tuple[str, Optional[str], int]] = []   # (path, If-None-Match, status sent)
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def do_GET(self) -> None:          # noqa: N802 - http.server's name
                fake._answer(self)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.api = f"http://127.0.0.1:{self.server.server_address[1]}/api/v3"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def find(self, full: str, number: int) -> Optional[Dict[str, Any]]:
        return next((p for p in self.pulls.get(full, []) if p["number"] == number), None)

    # ---- answering -------------------------------------------------------

    def _route(self, path: str, query: Dict[str, str]) -> Tuple[int, Any]:
        parts = [p for p in path.split("/") if p]
        if parts == ["user"]:
            return 200, {"login": self.login}
        if parts == ["search", "issues"]:
            items = [{"number": n, "repository_url": f"{self.api}/repos/{full}",
                      "pull_request": {"url": f"{self.api}/repos/{full}/pulls/{n}"}} for full, n in self.requested]
            return 200, {"total_count": len(items), "items": items}
        if len(parts) >= 4 and parts[0] == "repos" and parts[3] == "pulls":
            full = f"{parts[1]}/{parts[2]}"
            if full not in self.pulls:
                return 404, {"message": "Not Found"}
            if len(parts) == 4:
                return 200, sorted(self.pulls[full], key=lambda p: p["updated_at"], reverse=True)
            number = int(parts[4])
            found = self.find(full, number)
            if found is None:
                return 404, {"message": "Not Found"}
            if len(parts) == 5:
                return 200, found
            if parts[5] == "reviews":
                return 200, self.reviews.get((full, number), [])
            if parts[5] == "commits":
                return 200, [{"sha": s} for s in self.commits.get((full, number), [])]
        return 404, {"message": "Not Found"}

    def _answer(self, h: BaseHTTPRequestHandler) -> None:
        url = urllib.parse.urlsplit(h.path)
        path = url.path[len("/api/v3"):] if url.path.startswith("/api/v3") else url.path
        query = dict(urllib.parse.parse_qsl(url.query))
        etag_in = h.headers.get("If-None-Match")
        if h.headers.get("Authorization") != f"Bearer {self.token}":
            return self._send(h, path, etag_in, 401, {"message": "Bad credentials"})
        if path in self.redirect:
            self.requests.append((path, etag_in, 302))
            h.send_response(302)
            h.send_header("Location", self.redirect[path])
            h.send_header("Content-Length", "0")
            h.end_headers()
            return None
        if path in self.status:
            return self._send(h, path, etag_in, self.status[path], {"message": "forced"})
        status, data = self._route(path, query)
        link = None
        if status == 200 and isinstance(data, list) and len(data) > self.page_size:
            page = int(query.get("page", "1"))
            start = (page - 1) * self.page_size
            if start + self.page_size < len(data):
                nxt = dict(query, page=str(page + 1))
                link = f'<{self.api}{path}?{urllib.parse.urlencode(nxt)}>; rel="next"'
            data = data[start:start + self.page_size]
        body = json.dumps(data, sort_keys=True).encode("utf-8")
        etag = '"' + hashlib.sha1(body).hexdigest() + '"'
        if status == 200 and etag_in == etag:
            return self._send(h, path, etag_in, 304, None, etag=etag)
        self._send(h, path, etag_in, status, data, etag=etag if status == 200 else None, link=link, body=body)

    def _send(self, h: BaseHTTPRequestHandler, path: str, etag_in: Optional[str], status: int, data: Any,
              etag: Optional[str] = None, link: Optional[str] = None, body: Optional[bytes] = None) -> None:
        self.requests.append((path, etag_in, status))
        payload = b"" if status == 304 else (body if body is not None else json.dumps(data).encode("utf-8"))
        h.send_response(status)
        h.send_header("Content-Type", "application/json")
        if etag:
            h.send_header("ETag", etag)
        if link:
            h.send_header("Link", link)
        h.send_header("Content-Length", str(len(payload)))
        h.end_headers()
        if payload:
            h.wfile.write(payload)
