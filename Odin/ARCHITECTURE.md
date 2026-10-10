# Architecture

How Odin's files fit together. Odin's code is in [`Asgard/apps/odin`](../Asgard/apps/odin) since Oct 10, 2026; its contract with Muninn, the order of the daily run and what protects Jira are in [Asgard/docs/integration/odin.md](../Asgard/docs/integration/odin.md). For *why* it is split this way, see the [README design notes](README.md#design-notes); for setup, [INSTALL.md](INSTALL.md).

The shape of it in one line: **thin PowerShell reads Windows, standard-library Python makes every decision, the two meet at a versioned JSON contract, and Muninn holds every record.**

Paths below are relative to `Asgard/apps/odin/` unless they say otherwise.

## File interaction

```mermaid
flowchart TD
    subgraph entry["Entry points"]
        CMD["<b>odin.cmd</b><br/>Windows dispatcher<br/><i>no logic, only routing</i>"]
        TILE["<b>odin.pyw</b><br/>the Odin tile<br/><i>Asgard's shared window</i>"]
        EXP["<b>Odin/graph-app, playwright-app</b><br/>optional exporters<br/><i>hand their file to odin.cmd</i>"]
    end

    OUTLOOK[("Classic Outlook<br/>your own calendar<br/><i>read only</i>")]

    subgraph ps["windows/ - Windows-native, makes no decisions"]
        DOCTOR["<b>Test-Environment.ps1</b><br/>read-only preflight"]
        SYNCPS["<b>Invoke-MeetingSync.ps1</b><br/>orchestrator<br/><i>CLM-safe</i>"]
        EXPORT["<b>Export-OutlookMeetings.ps1</b><br/>Outlook COM to JSON<br/><i>needs FullLanguage</i>"]
        TASK["<b>Register-MeetingSyncTask.ps1</b><br/>the Asgard Odin daily task"]
    end

    EXPORTJSON[/"export JSON<br/><b>schema_version 1</b><br/><i>the contract</i>"/]
    CSVFILE[/"Outlook CSV<br/><i>exported by hand</i>"/]

    subgraph py["odin/ - all logic, standard library only"]
        CLI["<b>cli.py</b><br/>commands, logging, exit codes"]
        UIB["<b>ui_backend.py</b><br/>the window's reads"]
        SOURCES["<b>sources.py</b> + <b>models.py</b><br/>JSON and CSV readers"]
        RULES["<b>rules.py</b><br/>filters, tour of duty, routing"]
        SYNCPY["<b>sync.py</b><br/>meetings to sub-tasks"]
        COLLECT["<b>collect.py</b><br/>Jira into Muninn"]
        POSTING["<b>posting.py</b><br/>worklogs to Jira"]
        STORE["<b>store.py</b><br/>Muninn, journal, lock"]
        HISTORY["<b>history.py</b><br/>state.db, imported once"]
        JIRA["<b>jira.py</b><br/>REST client<br/><i>the only network code</i>"]
        CRED["<b>credstore.py</b><br/>DPAPI token"]
    end

    MUNINN[("<b>muninn.db</b><br/>asgard.muninn.odin")]
    subgraph data["%LOCALAPPDATA%\Asgard\odin\"]
        FILES[("config.json, jira_token.dpapi,<br/>last_run.json, logs, exports")]
    end

    JIRADC[["<b>Jira Data Center</b><br/>REST v2, bearer PAT"]]

    CMD -->|doctor| DOCTOR
    CMD -->|"bare / preview / csv"| SYNCPS
    CMD -->|schedule| TASK
    CMD -->|"check / status / sync / post / report"| CLI
    EXP --> CMD
    TILE --> UIB
    TILE -.->|"buttons start"| CLI
    TASK -.->|"fires each weekday"| SYNCPS

    SYNCPS -->|"Path A"| EXPORT
    EXPORT -->|COM, unguarded fields only| OUTLOOK
    EXPORT --> EXPORTJSON
    OUTLOOK -.->|"Path B: File > Export"| CSVFILE
    SYNCPS -->|"cli.py daily"| CLI
    EXPORTJSON --> SOURCES
    CSVFILE --> SOURCES

    CLI --> SOURCES
    CLI --> SYNCPY
    CLI --> COLLECT
    CLI --> POSTING
    CLI --> HISTORY
    CLI --> CRED
    CLI --> FILES
    SYNCPY --> RULES
    SYNCPY --> STORE
    SYNCPY --> POSTING
    SYNCPY -->|"label search, create,<br/>transition"| JIRA
    COLLECT --> STORE
    COLLECT -->|"search, issue, worklogs"| JIRA
    POSTING --> STORE
    POSTING -->|"add_worklog, find_worklog"| JIRA
    HISTORY --> STORE
    STORE --> MUNINN
    UIB --> MUNINN
    JIRA -->|HTTPS, verification always on| JIRADC

    classDef winNative fill:#1f3864,stroke:#0f1f3d,color:#ffffff
    classDef pyLogic fill:#0b5345,stroke:#062e26,color:#ffffff
    classDef contract fill:#7d6608,stroke:#4a3c05,color:#ffffff
    classDef store fill:#4a235a,stroke:#2a1433,color:#ffffff
    classDef external fill:#78281f,stroke:#4a1811,color:#ffffff
    classDef entryPoint fill:#1b4f72,stroke:#0d2838,color:#ffffff

    class DOCTOR,SYNCPS,EXPORT,TASK winNative
    class CLI,UIB,SOURCES,RULES,SYNCPY,COLLECT,POSTING,STORE,HISTORY,JIRA,CRED pyLogic
    class EXPORTJSON,CSVFILE contract
    class MUNINN,FILES store
    class JIRADC external
    class CMD,TILE,EXP entryPoint
```

Reading it:

- **Nothing in `windows/` decides anything.** The exporter dumps what is on the calendar; Python filters, routes and dedupes. The testable logic stays in one place.
- **`sources.py` is the only thing that knows which path produced a meeting.** Everything downstream sees `Meeting` objects, so a new calendar source (Graph, OWA) changes nothing in `rules`, `sync` or `jira`.
- **`jira.py` is the only module that touches the network**, `credstore.py` the only one that touches the secret, and `store.py` the only one that writes Muninn (through `asgard.muninn.odin`, under Muninn's guard).
- **The window only reads.** Its buttons start the same `cli.py` commands the scheduled task runs, so there is one code path to Jira.

## What happens to one meeting

```mermaid
flowchart TD
    START(["one Meeting from a source"]) --> DUP{"already seen<br/>in this run?"}
    DUP -->|"same key or<br/>content hash"| DROP["skip silently"]
    DUP -->|no| FILTERS{"filters<br/><i>cheapest first</i>"}

    FILTERS -->|"cancelled, all-day, not ended,<br/>declined, private, free,<br/>too short/long, text, category"| SKIP["SKIP<br/><i>with the reason</i>"]
    FILTERS -->|passes| TOD{"tour of duty"}

    TOD -->|"wholly outside<br/>+ action = skip"| SKIP
    TOD -->|"wholly outside<br/>+ action = route"| ROUTED["parent = outside_parent"]
    TOD -->|"inside, or partial"| RULES{"rules<br/><i>first match wins</i>"}

    RULES -->|"rule says skip"| SKIP
    RULES -->|"rule matches"| RULEPARENT["parent = rule.parent"]
    RULES -->|"no match"| DEFPARENT["parent = default_parent"]

    ROUTED --> SEEN
    RULEPARENT --> SEEN
    DEFPARENT --> SEEN
    SEEN{"in meeting_subtasks,<br/>the journal or state.db?"} -->|yes| EXISTS["EXISTS<br/><i>linked to its calendar event</i>"]
    SEEN -->|no| CAP{"under<br/>max_creates_per_run?"}

    CAP -->|no| CAPPED["left for the next run"]
    CAP -->|yes| DRY{"dry run?"}
    DRY -->|yes| WOULD["WOULD<br/><i>creates nothing</i>"]
    DRY -->|no| LOOK{"your issue with the<br/>m2j-hash label in Jira?"}
    LOOK -->|"exactly one<br/><i>an earlier run made it</i>"| RECORD
    LOOK -->|"several, or the<br/>search failed"| ERROR
    LOOK -->|none| CREATE["POST the sub-task<br/><i>with the m2j-hash label</i>"]

    CREATE -->|created| RECORD["record in meeting_subtasks<br/><b>immediately</b><br/><i>or the journal, and stop creating</i>"]
    CREATE -->|"ambiguous failure:<br/>timeout, 5xx, cut off"| RECOVER{"JQL search<br/>for the label"}
    CREATE -->|"rejected outright"| ERROR["ERROR<br/><i>reported, retried next run</i>"]

    RECOVER -->|"exactly one match"| RECORD
    RECOVER -->|"none, several,<br/>or search failed"| ERROR

    RECORD --> READ["read the sub-task<br/>back into Muninn"]
    READ --> WORKLOG{"log_work?"}
    WORKLOG -->|yes| LOGTIME["begin_meeting_post, then Jira,<br/>then finish_post"]
    WORKLOG -->|no| TRANSITION
    LOGTIME -->|"refused (4xx)"| RETRY["retried by later runs,<br/>14 days, three refusals"]
    LOGTIME -->|"no answer"| STUCK["stays sending; the next start<br/>finds it by its marker"]
    LOGTIME -->|ok| TRANSITION{"transition_to?"}
    TRANSITION -->|yes| MOVE["move the sub-task"]
    TRANSITION -->|no| DONE(["done"])
    MOVE --> DONE

    classDef good fill:#0b5345,stroke:#062e26,color:#ffffff
    classDef bad fill:#78281f,stroke:#4a1811,color:#ffffff
    classDef neutral fill:#34495e,stroke:#1c2833,color:#ffffff
    class CREATE,RECORD,READ,LOGTIME,MOVE,DONE good
    class ERROR bad
    class SKIP,DROP,EXISTS,CAPPED,WOULD,RETRY,STUCK neutral
```

The things that keep repeated runs safe:

1. **The record is written the instant Jira accepts the create**, before the worklog and the transition, so a failure in either can never produce a duplicate sub-task. If Muninn can't take it, it goes to `unrecorded.jsonl` and the run stops creating.
2. **Jira is asked before every create.** A sub-task an earlier run made but couldn't record (an answer lost or cut off, a run stopped halfway, a full disk) carries the meeting's deterministic `m2j-<hash>` label, so the next run finds it instead of making it again; an ambiguous create is looked up at once. Several matches, or a search that fails, are reported for a person and nothing is created.
3. **Every worklog goes through a `sending` row with a marker**, committed before the call, so time is never logged twice; a lost answer is settled by searching for the marker.

Because of those, the scan window is not a correctness mechanism: widening `-DaysBack` is always safe.

## Supporting files

| Path | Role |
|---|---|
| `tools/Test-PowerShellSyntax.ps1` | Parses every `.ps1` and rejects PowerShell 7-only syntax. Real under 5.1. |
| `tools/Invoke-WindowsChecks.ps1` | The Windows-only checks (`odin selftest`): 5.1 parsing, DPAPI, the PowerShell-to-Python handoff, the CSV push, the entry point, `Test-Environment.ps1` at runtime, and the task start time. Safe on the real workstation. |
| `Asgard/tests/test_odin_*.py` | `unittest`, offline, against a temporary Muninn and `tests/fake_jira.py`. `test_odin_guardrails.py` enforces the non-negotiables: TLS is never disabled, no execution-policy bypass, no guarded Outlook properties, CLM-safe ASCII scripts, and every exporter package documented in `MODULES.md`. `test_dependencies.py` keeps the daily run on the standard library. |
| `.github/workflows/odin-checks.yml` (repo root) | The Odin tests, the exporters' tests and the PowerShell checks, on Linux and on a Windows runner. |
| `infra/windows-test-vm/` (repo root) | Terraform for a throwaway Windows Server host to run the Windows checks on, over SSM with no inbound rules. Never shipped. |
| `.kiro/` (repo root) | Kiro steering, subagents and hooks for development. Never shipped. See `KIRO_SETUP.md`. |
