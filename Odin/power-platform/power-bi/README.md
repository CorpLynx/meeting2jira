# Power BI: reporting on the time, not creating it

Power BI cannot create Jira sub-tasks — it's an analytics tool. But it's a genuinely good fit for the
question behind all of this: *where does my week actually go?* And unlike the automation options, this
one needs nothing new: the data already exists.

## Two data sources, and the tradeoff

### `state.db` — local, free, already populated

`%LOCALAPPDATA%\Asgard\odin\state.db`, table `synced`, one row per sub-task created:

| Column | Use |
|---|---|
| `issue_key` | the Jira sub-task |
| `parent` | which parent it was filed under — the "what was this time for" axis |
| `summary` | rendered title |
| `start_utc` | when the meeting happened |
| `minutes` | duration; sum this |
| `worklog_logged`, `worklog_id` | whether time was logged, for spotting gaps |
| `created_at` | when it was pushed |

Complete for everything pushed, needs no Jira access, and never leaves the machine.

**The catch:** Power BI Desktop has no built-in SQLite connector. Options, best first:

1. **Export to CSV and point Power BI at that.** `report.ps1` here does it. Boring, robust, no drivers,
   no admin. On a locked-down machine this is almost certainly the answer.
2. An ODBC SQLite driver — needs an install, so probably needs admin.
3. Query Jira instead, below.

### Jira itself — authoritative, needs access

If Jira Data Center is reachable from Power BI, query it directly and you get the real worklogs
including anything edited in Jira after the fact:

```
labels = "meeting" AND created >= startOfMonth()
```

Authoritative, and it captures manual corrections `state.db` never sees. Needs a working connection
and a credential, so it's the heavier option.

**A neat property:** the deterministic `m2j-<hash>` label means you can always join the two sources
back together per meeting, if you ever want both.

## Getting started

```powershell
cd path\to\app
..\power-platform\power-bi\report.ps1
```

Writes `meetings.csv` next to `state.db`. In Power BI Desktop: **Get Data → Text/CSV**, point at it,
then set types — `start_utc` as Date/Time, `minutes` as Whole Number.

Worth building first:

- **Minutes per parent per week.** The headline: which bucket of work is eating the time. This is also
  the evidence for the org's tracking requirement.
- **Meeting hours per week over time**, with a trend line.
- **Count vs total duration.** Many short meetings and few long ones are different problems.
- **Rows where `worklog_logged = 0`** — sub-tasks whose time never made it into Jira. A gap here is
  usually a real failure worth chasing.

Two measures to start with:

```
Total Hours = DIVIDE ( SUM ( 'meetings'[minutes] ), 60 )

Avg Meeting Length =
    DIVIDE ( SUM ( 'meetings'[minutes] ), DISTINCTCOUNT ( 'meetings'[issue_key] ) )
```

Add a proper date table marked as a date table if you want time intelligence
(week-over-week, month-to-date) to behave.

## Before you put this anywhere shared

`state.db` and any CSV drawn from it **contain meeting subjects**. The summaries are rendered from
them, so a dashboard built on this is as sensitive as your calendar titles.

Consider whether subjects in your organisation can carry CUI. If they can, report on `parent`,
`minutes` and dates and leave `summary` out of the model entirely — the interesting questions are
answerable without it. Note also that private meetings were never pushed, so they are legitimately
absent rather than missing.

A published-to-the-service dataset means this data leaves your machine, which is a different
conversation with your ISSO than the local tool. A local `.pbix` you never publish is not.
