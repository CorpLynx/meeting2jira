# Option 1: Power Automate for desktop as a launcher

A desktop flow that runs the existing tool on a schedule, with a GUI run history. No premium licence,
no gateway, no admin. Every safety property is preserved because the logic is untouched.

Be clear about what this is: **a better button on the existing tool**, not a reimplementation. If the
objective is to retire the custom script, this does not do that — see [../cloud-flow/](../cloud-flow/).

## Why this is the realistic option here

- Power Automate for desktop is
  [preinstalled on Windows 11](https://learn.microsoft.com/en-us/power-automate/desktop-flows/getting-started-windows-11).
- It has a
  [Run PowerShell script action](https://learn.microsoft.com/en-us/power-automate/desktop-flows/actions-reference/scripting).
- It runs **on your machine**, so it reaches Jira Data Center exactly as the tool does now. No gateway.
- Nothing about the Jira connector's Cloud-only limitation applies, because it isn't used.

*Microsoft documentation consulted and rephrased; nothing reproduced verbatim.*

## Build it

1. Open **Power Automate** from the Start menu and sign in. If prompted for an environment, pick your
   organisation's.
2. **New flow** → name it `Odin daily`.
3. Add one action: **Run PowerShell script**. Paste the contents of
   [`run-odin.ps1`](run-odin.ps1), editing `$AppPath` to where you put `app/`.
4. Set the action's **Output variable** to `PsOutput` and the error output to `PsError`, so failures
   surface in the run history rather than vanishing.
5. Add **If** → `PsError` is not empty → **Display an infobar**, or send yourself a message. Without
   this, a failed run looks identical to a successful one in the run list.
6. **Save**, then **Run** once and confirm the output shows the created/skipped counts.

## Schedule it

Desktop flows can be scheduled from the Power Automate cloud portal, but that path needs a premium
licence and a machine registered to your environment — which lands you back in the same licensing
conversation.

**Use Task Scheduler instead**, which is free and already supported:

```powershell
cd path\to\app
.\odin schedule
```

That registers a per-user weekday task, deriving the run time from your tour of duty if configured.
The desktop flow then becomes the *manual* "run it now" button, and the scheduled task is the
unattended path. Those two roles are genuinely different, and it's fine to use both.

## Two things people get wrong here

**Don't have the flow re-implement the filters.** It's tempting to add UI actions that read Outlook
and decide what counts. Everything then diverges from the config file, and you maintain two sets of
rules that disagree. The flow should pass arguments and read the exit code, nothing more.

**Read the exit code, don't scrape the text.** The CLI's exit codes are a stable contract:

| Code | Meaning |
|---|---|
| 0 | fine |
| 1 | finished, but individual items failed |
| 2 | config, usage, or credential problem |
| 130 | interrupted |

`run-odin.ps1` already turns these into a message worth showing.
