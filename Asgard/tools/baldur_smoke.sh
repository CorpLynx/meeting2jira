#!/usr/bin/env bash
# End-to-end demo of Baldur on the Baldur spec's worked example (Linux, macOS or WSL).
# Builds a throwaway git repo with real reflog checkouts, Odin-style calendar events
# and a temporary Muninn, then runs the CLI: setup, collect, estimate, days, approve, report.
# Expected: PROJ-42 1h30m and PROJ-51 30m (policy ambient), sessions 09:20-12:25 and 14:00-15:05.
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export TZ=America/New_York
export ASGARD_HOME="$(mktemp -d)"
export GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL="$ASGARD_HOME/gitconfig"
touch "$GIT_CONFIG_GLOBAL"
REPO="$ASGARD_HOME/src/asgard"
mkdir -p "$REPO" && cd "$REPO"
g() { GIT_AUTHOR_DATE="$1" GIT_COMMITTER_DATE="$1" git "${@:2}"; }
git init -q . && git symbolic-ref HEAD refs/heads/main
git config user.email brandon@agency.gov && git config user.name Brandon
echo a > a.txt && git add a.txt
GIT_AUTHOR_NAME=Sam GIT_AUTHOR_EMAIL=sam@agency.gov g "2026-09-30T15:00:00" commit -qm "initial"
g "2026-09-30T15:30:00" checkout -qb feature/PROJ-42-retry
g "2026-09-30T15:31:00" checkout -q main
g "2026-09-30T15:32:00" checkout -qb feature/PROJ-51-x
g "2026-09-30T15:33:00" checkout -q feature/PROJ-42-retry
n=0
commit() { n=$((n+1)); echo "$n" >> "f$n.txt"; git add "f$n.txt"; g "2026-10-01T$1:00" commit -qm "$2"; }
commit 09:50 "retry on 503"; commit 10:20 "backoff"; commit 10:55 "jitter"; commit 11:40 "tests"
g "2026-10-01T12:05:00" checkout -q feature/PROJ-51-x
commit 12:10 "form layout"; commit 12:25 "form validation"
g "2026-10-01T12:27:00" checkout -q feature/PROJ-42-retry
commit 14:30 "retry logging"; commit 15:05 "docs"
cd "$ROOT"
PYTHONPATH="$ROOT" python3 - <<'PY'
from asgard import muninn
from asgard.muninn import odin
muninn.prepare()
con = muninn.connect()
cal = muninn.ensure_source(con, "calendar", "outlook")
with muninn.Run(con, "odin", cal, "calendar") as run:     # what Odin's calendar sync would write
    for i, (s, e) in enumerate([("2026-10-01T13:00:00Z", "2026-10-01T16:00:00Z"),
                                ("2026-10-01T16:30:00Z", "2026-10-01T20:30:00Z")]):
        odin.upsert_calendar_event(run, {"external_id": f"m{i}", "title": f"Meeting {i}", "starts_at": s,
                                         "ends_at": e, "is_all_day": 0, "show_as": "busy",
                                         "response": "accepted", "is_cancelled": 0})
PY
B="python3 apps/baldur/cli.py"
$B setup --email brandon@agency.gov --project PROJ --root "$ASGARD_HOME/src"
$B collect
$B estimate --from 2026-10-01 --to 2026-10-01 --report
$B days --from 2026-09-29 --to 2026-10-02
$B approve --date 2026-10-01
$B report 2026-10-01
rm -rf "$ASGARD_HOME"
