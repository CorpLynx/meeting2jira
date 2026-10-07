# Huginn and Muninn

Identity `huginn` · not built yet · Odin's other raven: the scheduled collection that keeps Muninn fresh

## Decision: Huginn schedules, it doesn't write

Huginn could have been one process that collects from Jira, git and Teams itself. It can't, under rule 2: Jira tables are Odin's, git tables Baldur's, and the guard refuses Huginn writing either. That is the right outcome, because each collector's rules (what's yours, what counts as deleted, keys) live in its owning app.

So Huginn is:

1. **One per-user scheduled task** (no admin; a single task rather than one per app) that runs `Asgard.pyw --collect` at logon and on an interval.
2. **A runner** that calls each enabled app's collect entry point in turn, each in its own process under its own identity: `odin sync`, `baldur collect`, later `loki recaps`. A failure in one doesn't stop the others.
3. **A log line per pass** in `logs\huginn.log`, and nothing in Muninn beyond what each app's own `Run` records. The `huginn` identity stays reserved for a pass record if one is ever needed.

## What each app provides

An entry point in its manifest (see [../app-contract.md](../app-contract.md)): `collect: {command, interval_minutes, needs_network}`. Huginn reads manifests, so a new app joins the schedule by declaring it.

## Why not each app's own task

Baldur's weekly task today bakes `sys.executable` into the command (a Python upgrade breaks it) and isn't in the install ledger (Valhalla doesn't remove it). One task, created and recorded by Asgard, fixes both; see [../platform-consolidation.md](../platform-consolidation.md).

## Interaction with Muninn

- Runs never overlap per app: the runner holds a per-app lock file, because two overlapping runs of one stream can move its cursor backwards (finding 14).
- Each app's run is `incremental` except a periodic `full` pass (weekly) that may sweep deletions.
- `prepare()` still runs only in the launcher; Huginn opens nothing before Asgard has created Muninn, and reports `NotReady` once rather than every interval.
