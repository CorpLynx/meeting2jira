# Option 2: a cloud flow calling the Jira Data Center REST API

The route to genuinely retiring the custom script. It is achievable, and it is the expensive option.

## What you must have first

Read this list before designing anything; any single missing item stops the whole approach.

| Requirement | Why | Who provides it |
|---|---|---|
| **Power Automate premium licence** | The Jira connector is Cloud-only, so Jira Data Center must be called with the HTTP action or a custom connector. Both are premium. | Licensing / procurement |
| **On-premises data gateway** | A cloud flow has no route to an on-prem Jira behind the firewall. The gateway is itself a premium feature. | IT, installed on a machine with line of sight to Jira |
| **Connectors enabled** | Microsoft [disables new connectors by default in GCC High and DoD](https://learn.microsoft.com/en-us/power-platform/admin/connector-off-by-default). | Power Platform admin |
| **A Jira credential the flow can hold** | A PAT in a flow connection or Azure Key Vault. Note this is no longer DPAPI-bound to one user on one machine — a different risk profile. | You + ISSO |
| **A fresh ISSO review** | Calendar data would be processed in the Power Platform cloud instead of staying on your workstation. Materially different from what was approved. | ISSO |

If the gateway or the premium licence is refused, stop here and use
[../power-automate-desktop/](../power-automate-desktop/).

*Microsoft and Atlassian documentation consulted and rephrased; nothing reproduced verbatim.*

## Shape of the flow

```
Recurrence  (weekdays, shortly after your tour of duty ends)
  │
Get calendar events V4   (Office 365 Outlook)
  │  start = midnight N days ago, end = now
  │  NOTE: this returns recurring instances expanded, which is what you want
  │
For each event
  ├─ Filter: ended? not cancelled? not all-day? not private? responded? busy? duration in range?
  ├─ Compute label  m2j-<hash>              ← see dedupe.md; build this FIRST
  ├─ HTTP GET  /rest/api/2/search?jql=labels="<label>"
  │     1 result   → skip
  │     >1 results → report, change nothing
  │     0 results  → continue
  ├─ Route to a parent issue (default, or by subject/category, or tour-of-duty parent)
  ├─ HTTP POST /rest/api/2/issue           retry policy: NONE
  │     on ambiguous failure → re-run the label search before ever creating again
  ├─ HTTP POST /rest/api/2/issue/{key}/worklog     (if logging time)
  └─ HTTP POST /rest/api/2/issue/{key}/transitions (if transitioning)
```

`flow-definition.json` is a readable skeleton of this in Logic App / Power Automate definition form.

**Treat it as a blueprint, not an importable artefact.** Power Automate solution packages carry
connection references, environment IDs, and API versions specific to your tenant. Hand-written
definitions generally do not import cleanly. Use it to see the intended shape, the ordering, and the
settings that matter — particularly `"retryPolicy": {"type": "none"}` on the create — then build it in
the designer.

## Settings that are easy to miss and cause real bugs

| Setting | Value | Why |
|---|---|---|
| Create action retry policy | **none** | Power Automate retries failed actions by default. On a create, a retried timeout is a duplicate issue. This is the platform default actively working against you. |
| Concurrency on the `For each` | **1** | Parallel iterations race on the label search: two events can both see "not found" and both create. |
| Flow run history retention | 28 days, fixed | You cannot extend it. If you need an audit trail beyond that, write one somewhere yourself. |
| Trigger window vs `only_ended` | Wider window, always | Re-runs cannot duplicate once dedupe works, so a wider window is free. Never build a "deferred meetings" queue. |

## Where this will surprise you

- **Recurring meetings.** `Get calendar events V4` expands them into instances, which is right. Verify
  it, because the whole series sharing one id is the classic source of "only one standup ever appeared".
- **Timezones.** The connector can hand back local times with offsets. The hash needs UTC in a precise
  format. Convert before hashing, or every label will be wrong.
- **Private meetings.** The connector returns the subject regardless of sensitivity. The existing tool
  withholds private subjects from its own logs; a flow's run history will capture whatever it touches,
  and you cannot retroactively scrub 28 days of run history.
- **Throttling.** The Outlook connector and Jira both rate-limit. A backfill over months of calendar
  will hit limits that a daily run never does.

## Parity

`parity-checklist.md` lists the behaviours the current tool has because each one was once a bug. Work
through it before trusting a flow with real data.
