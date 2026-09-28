# Parity checklist

Every row below exists in meeting2jira because it was, at some point, a bug. A rewrite starts with
none of them. Work through this before pointing a flow at real data.

Ordered by what goes wrong if you skip it.

## Will corrupt Jira if missed

- [ ] **Dedupe by deterministic label.** Compute `m2j-<hash>`, search before creating, attach on create.
      Without this every run re-creates every meeting. See [dedupe.md](dedupe.md).
- [ ] **Hash matches the existing implementation byte for byte.** Verified against real output, not
      assumed. A mismatch means two sub-tasks per meeting, with nothing to indicate why.
- [ ] **Create action retry policy set to none.** The platform default retries, and a retried timeout
      on a create is a duplicate issue.
- [ ] **`For each` concurrency set to 1.** Parallel iterations both see "not found" and both create.
- [ ] **Ambiguous failures search before re-creating.** Timeout or 502/503/504 on a create may already
      have succeeded. Exactly one match means done; several or a failed search means stop and report.
- [ ] **A per-run creation cap.** One safety valve against a misconfigured first run filling Jira.
      Default here is 40.

## Will misreport time

- [ ] **Only ended meetings.** A worklog must reflect time actually spent, so never push a meeting that
      hasn't finished.
- [ ] **Worklog duration from the calendar item**, computed from start and end, not from a rounded
      display value. A 58-minute meeting logs 58 minutes.
- [ ] **Worklog started timestamp is the meeting's start**, in UTC, in the format Jira expects
      (`yyyy-MM-ddTHH:mm:ss.000+0000`).
- [ ] **Enabling worklogs does not backfill.** Turning time logging on must not retroactively log every
      sub-task ever created. The tool tracks intent-at-create-time for exactly this reason.
- [ ] **Worklog failure does not lose the sub-task.** Record the issue first; retry the worklog later,
      bounded, and never twice.

## Will include the wrong meetings

- [ ] Skip cancelled, all-day, declined, private, and shown-as-free items.
- [ ] Skip personal appointments with no attendees.
- [ ] Minimum and maximum duration bounds.
- [ ] Text exclusions: subject, organiser, location substrings, and whole categories (OOO, PTO, and so
      on). Case-insensitive.
- [ ] Filters evaluated cheapest-first, and the reported reason is the *first* one that matched — this
      matters when comparing a flow's decisions against the tool's.
- [ ] Routing: default parent, plus per-rule overrides, first match winning.
- [ ] Tour of duty, if used: a meeting wholly outside working hours is labelled, rerouted, or skipped —
      but one that merely **overruns** the end of the tour counts as inside, because work that ran late
      is still work.

## Will upset a security reviewer

- [ ] **Private meeting subjects are withheld from logs**, not merely from Jira. Note a flow's run
      history keeps whatever it touched for 28 days and cannot be scrubbed retroactively.
- [ ] **The Jira credential is not visible in run history or flow definitions.**
- [ ] **TLS verification is never disabled**, including any custom connector definition.
- [ ] **The calendar is never modified**, and Jira issues are never deleted. One-way, always.
- [ ] **Only the user's own calendar** — no shared, delegate, or other people's calendars.
- [ ] Document that calendar data is now processed in the Power Platform cloud rather than staying on
      the workstation. This is the change the ISSO will care about most; the existing approval does not
      cover it.

## Operability

- [ ] A failed run is visible without opening logs. The tool writes `last_run.json` and surfaces it in
      `status` and the environment check; a flow needs an equivalent, since a silently failing scheduled
      flow looks exactly like a working one.
- [ ] An expired PAT produces an actionable message, not a bare 401.
- [ ] A dry-run or preview mode that creates nothing and shows exactly what would happen.

## Sanity check

Run the flow and the existing tool against the **same week**, with the flow in preview mode, and
compare decisions meeting by meeting. Any divergence is either a filter you haven't implemented or a
hash mismatch. Both matter, and both are much cheaper to find here than in Jira.
