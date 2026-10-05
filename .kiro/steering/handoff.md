---
inclusion: manual
---
# Session handoff (run with /handoff)

Write `.kiro/session-handoff.md` so a fresh session can pick this up without re-reading the
conversation. This is a scratch note for the next session (git-ignored). It is NOT the project's
`HANDOFF.md`; update that only for real status, backlog or verification-checklist changes.
Keep it to 25 lines or fewer:

```
# Session handoff - <date time>
Goal: <one line, e.g. backlog item P1-A>
Done: <bullets, with file names>
Left: <bullets, in order>
Failing tests: <test ids or "none">  (last: <RESULT line from .test-output/last-summary.txt>)
Needs target-machine verification: <items, or "none">
Decisions made: <bullets, with the reason>
Watch out for: <anything surprising>
```

Then reply with "Handoff written. Start a new session to continue." and stop.
When a later session finishes the work in the note, delete `.kiro/session-handoff.md`.
