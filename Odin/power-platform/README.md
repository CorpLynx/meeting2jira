# A Power Platform version of meeting2jira

## First, a terminology correction that changes what you ask IT for

**Power BI** and **Power Automate** are different products, and only one of them can do this job:

| Product | What it is | Role here |
|---|---|---|
| **Power Automate** | Workflow automation (formerly Flow) | This is the one that could replace meeting2jira: read the calendar on a schedule, create Jira sub-tasks. |
| **Power Automate for desktop** | Local RPA, preinstalled on Windows 11 | The most realistic option on a locked-down machine. See [power-automate-desktop/](power-automate-desktop/). |
| **Power BI** | Analytics and dashboards | Cannot create Jira issues. It *is* the right tool for reporting on the time once it's recorded. See [power-bi/](power-bi/). |

So if you go asking for "Power BI" to build the automation, you'll be given the wrong product. Ask
for **Power Automate**, and be specific about which flavour (below).

---

## The blocker you need to know before spending any effort

**Microsoft's Power Automate Jira connector works with Jira Cloud only — not Jira Data Center.**
Atlassian documents this directly
([Atlassian KB](https://ja.confluence.atlassian.com/jirakb/microsoft-power-platform-power-automate-connector-does-not-work-with-jira-data-center-1167731016.html)).
Your Jira is Data Center, which we just finished removing all Cloud support for.

That single fact eliminates the easy path. Everything else follows from it:

- The built-in Jira actions are unavailable, so you'd call the REST API with the **HTTP** action or a
  **custom connector**. Both are **premium**, requiring a paid Power Automate licence beyond what
  comes bundled with M365.
- A cloud flow runs in Microsoft's cloud and has no route to an on-premises Jira behind the agency
  firewall. Bridging that needs an **on-premises data gateway**, which is *also* premium, and needs
  someone to install and register it.
- You're in a government cloud. Microsoft
  [disables new connectors by default in GCC High and DoD](https://learn.microsoft.com/en-us/power-platform/admin/connector-off-by-default),
  so even the Office 365 Outlook connector may need an admin to turn on.

Net: a cloud flow needs a **premium licence, a gateway, an admin, and an ISSO review** — versus the
current tool, which needs none of those and already works.

*Sources above were consulted and rephrased; no content is reproduced verbatim.*

---

## Three options, honestly compared

### Option 1 — Power Automate for desktop as a launcher *(recommended if you want Power Platform involved)*

A desktop flow on your workstation that runs the existing tool. Power Automate for desktop is
[preinstalled on Windows 11](https://learn.microsoft.com/en-us/power-automate/desktop-flows/getting-started-windows-11)
and has a
[Run PowerShell script action](https://learn.microsoft.com/en-us/power-automate/desktop-flows/actions-reference/scripting).

- **Cost:** none beyond what you have.
- **Network:** runs locally, so it reaches Jira Data Center the same way the tool does today.
- **Keeps:** every safety property — dedupe, ambiguous-create recovery, worklog retry, private-subject
  withholding — because the logic is unchanged.
- **Gains:** a GUI run history, and a place for colleagues to trigger it without a terminal.
- **Honest limitation:** this is a nicer button on the existing tool, not a reimplementation. If the
  goal is "get off the custom script", this does not achieve it.

Build guide: **[power-automate-desktop/](power-automate-desktop/)**

### Option 2 — Cloud flow calling the Jira REST API

A scheduled cloud flow: Office 365 Outlook connector reads your calendar, HTTP actions call Jira.

- **Cost:** premium licence per user, plus gateway setup.
- **Needs from IT:** premium licence, on-premises data gateway installed and registered, connectors
  enabled in GCC High/DoD, and a service account or PAT stored in the flow.
- **Loses, unless you rebuild it:** the local state database. A stateless flow will happily create the
  same sub-task every run. See the dedupe section below — this is solvable, and the current design
  already hands you the key.
- **New ISSO question:** calendar data would be processed in the Power Platform cloud rather than
  staying on your machine. That is a materially different data-handling posture and needs review on
  its own terms, not a rubber stamp of the existing approval.

Blueprint and reference flow definition: **[cloud-flow/](cloud-flow/)**

### Option 3 — Leave it as is

The tool needs no licence, no gateway, no admin, and no app registration. That was the entire design
premise. Worth stating plainly as the baseline the others have to beat.

---

## The one thing a no-code rewrite usually gets wrong

meeting2jira is careful about exactly one hard problem: **never create a duplicate sub-task.** It
solves that with a sqlite database recording what it has pushed, matched on either a source-specific
key or a source-independent content hash.

A cloud flow has no such database. The usual result is a flow that re-creates last week's meetings
every time it runs, or one that only ever looks at "yesterday" and silently drops anything it missed.

**You already have the fix.** Every sub-task this tool creates carries a deterministic label:

```
m2j-<first 10 hex chars of sha256(normalised subject | start | end)>
```

Same meeting, same label, computed from the meeting alone with no state required. That makes a
stateless flow safe:

1. Compute the label from the calendar event.
2. `GET /rest/api/2/search?jql=labels="m2j-<hash>"`
3. If exactly one result — already done, skip. If none — create it. If several — stop and report.

That is the same exact-lookup the tool uses to recover from an ambiguous create, reused as the
primary dedupe mechanism. If you build a cloud flow, **build this first**; everything else is
cosmetic by comparison.

`cloud-flow/dedupe.md` works through it, including how to reproduce the hash in a flow expression —
which is the fiddly part, since the label must match byte for byte or the two systems will not see
each other's work.

## What else a rewrite has to consciously re-decide

These are behaviours the current tool has because each one was a bug at some point. A rewrite starts
without them:

| Behaviour | Why it exists |
|---|---|
| Only ended meetings are pushed | A worklog must reflect time actually spent. |
| Worklog duration comes from the calendar item | Not from a rounded display value. |
| State is written before the worklog and transition | So a later failure cannot cause a duplicate. |
| Ambiguous create failures are searched, never blindly retried | A proxy timeout may mean Jira already created it. |
| Failed worklogs retry, bounded at three attempts | Unbounded retries hammer a permanently rejected worklog. |
| Enabling worklogs does not backfill history | The first run would otherwise log months of old issues. |
| Private meeting subjects stay out of logs, not just out of Jira | `skip_private` is meant to keep them out of scope entirely. |
| A per-run creation cap | One safety valve against a misconfigured first run. |

`cloud-flow/parity-checklist.md` turns this into something you can tick off.

## Recommendation

If the goal is **convenience**, take Option 1: a desktop flow wrapping the existing tool, no licence
required, nothing lost.

If the goal is **getting off a custom script entirely**, Option 2 is achievable but costs a premium
licence, a gateway, and a fresh security review — and you must build the label-based dedupe before
anything else or it will quietly create duplicates.

Either way, Power BI is worth a look independently: the state database already holds every sub-task,
its parent, duration and date, which is most of a "how much of my week is meetings" dashboard. See
[power-bi/](power-bi/).
