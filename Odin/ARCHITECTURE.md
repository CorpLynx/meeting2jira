# Architecture

How the files fit together. For *why* it is split this way, see
[README design notes](README.md#design-notes); for setup, see [INSTALL.md](INSTALL.md).

The shape of it in one line: **thin PowerShell reads Windows, standard-library Python makes every
decision, and the two meet at a versioned JSON contract.**

Everything the program needs to run lives in **`Odin/app/`**. Copying that folder to a machine is a
complete install; the rest of `Odin/` is sibling deliverables and docs, and the repo root above it
is development support. Paths in this document are relative to `Odin/`. That holds because every path inside
`app/` is derived from the file's own location rather than the working directory, so the folder can
sit anywhere.

Inside it, **`app/src/`** is the program proper, split into its two halves — `meeting2jira/` (the
Python logic) and `windows/` (the Windows-native layer). `tests/` and `tools/` sit outside `src/`
because they verify the program rather than being part of it. The diagram below is all `app/`
except where noted.

## File interaction

```mermaid
flowchart TD
    subgraph entry["Entry points"]
        CMD["<b>meeting2jira.cmd</b><br/>Windows dispatcher<br/><i>no logic, only routing</i>"]
        M2J["<b>m2j</b><br/>macOS / Linux dev<br/><i>no Outlook, no DPAPI</i>"]
    end

    OUTLOOK[("Classic Outlook<br/>your own calendar<br/><i>read only</i>")]

    subgraph ps["src/windows/ — Windows-native, makes no decisions"]
        DOCTOR["<b>Test-Environment.ps1</b><br/>read-only preflight<br/><i>CLM-safe</i>"]
        SYNCPS["<b>Invoke-MeetingSync.ps1</b><br/>orchestrator<br/><i>CLM-safe</i>"]
        EXPORT["<b>Export-OutlookMeetings.ps1</b><br/>Outlook COM → JSON<br/><i>needs FullLanguage</i>"]
        TASK["<b>Register-MeetingSyncTask.ps1</b><br/>weekday scheduled task<br/><i>CLM-safe</i>"]
    end

    EXPORTJSON[/"export JSON<br/><b>schema_version 1</b><br/><i>the contract</i>"/]
    CSVFILE[/"Outlook CSV<br/><i>exported by hand</i>"/]

    subgraph py["src/meeting2jira/ — all logic, standard library only"]
        MAIN["<b>__main__.py</b><br/>CLI, logging, exit codes"]
        CONFIG["<b>config.py</b><br/>DEFAULTS, merge, validate"]
        SOURCES["<b>sources.py</b><br/>JSON + CSV readers"]
        MODELS["<b>models.py</b><br/>Meeting, dedupe identity"]
        RULES["<b>rules.py</b><br/>filters, tour of duty, routing<br/><i>the judgement</i>"]
        SYNCPY["<b>sync.py</b><br/>the push loop"]
        JIRA["<b>jira.py</b><br/>REST client<br/><i>only module using the network</i>"]
        STATE["<b>state.py</b><br/>sqlite dedupe"]
        CRED["<b>credstore.py</b><br/>DPAPI token"]
    end

    subgraph data["Local app data — stays in your profile"]
        CONFIGJSON[("config.json")]
        TOKEN[("jira_token.dpapi")]
        STATEDB[("state.db")]
        LASTRUN[("last_run.json")]
        LOGS[("logs/")]
    end

    JIRADC[["<b>Jira Data Center</b><br/>REST v2, bearer PAT"]]

    CMD -->|doctor| DOCTOR
    CMD -->|"bare / preview / csv / sync"| SYNCPS
    CMD -->|schedule| TASK
    CMD -->|"check / status / set-token"| MAIN
    M2J -->|"check / status / csv"| MAIN

    TASK -.->|"registers, then fires daily"| SYNCPS
    TASK -->|"reads tour_of_duty.end<br/>for the run time"| CONFIGJSON

    SYNCPS -->|"Path A"| EXPORT
    EXPORT -->|COM, unguarded fields only| OUTLOOK
    EXPORT --> EXPORTJSON
    OUTLOOK -.->|"Path B: File > Export"| CSVFILE

    SYNCPS -->|"push --input / --csv"| MAIN
    EXPORTJSON --> SOURCES
    CSVFILE --> SOURCES

    MAIN --> CONFIG
    MAIN --> SOURCES
    MAIN --> CRED
    MAIN --> STATE
    MAIN --> SYNCPY
    MAIN -->|"builds client"| JIRA
    MAIN -->|"writes on every real run"| LASTRUN
    MAIN --> LOGS

    CONFIG --> CONFIGJSON
    CRED --> TOKEN
    STATE --> STATEDB
    SOURCES --> MODELS

    SYNCPY -->|"decide per meeting"| RULES
    SYNCPY -->|"find / record / worklog"| STATE
    SYNCPY -->|"create, worklog, transition,<br/>search on ambiguous failure"| JIRA
    RULES --> MODELS
    RULES -->|"parse_hhmm, filter keys"| CONFIG

    JIRA -->|HTTPS, verification always on| JIRADC

    DOCTOR -->|"health of the last sync"| LASTRUN
    DOCTOR -.->|"reports, never changes"| CONFIGJSON

    classDef winNative fill:#1f3864,stroke:#0f1f3d,color:#ffffff
    classDef pyLogic fill:#0b5345,stroke:#062e26,color:#ffffff
    classDef contract fill:#7d6608,stroke:#4a3c05,color:#ffffff
    classDef store fill:#4a235a,stroke:#2a1433,color:#ffffff
    classDef external fill:#78281f,stroke:#4a1811,color:#ffffff
    classDef entryPoint fill:#1b4f72,stroke:#0d2838,color:#ffffff

    class DOCTOR,SYNCPS,EXPORT,TASK winNative
    class MAIN,CONFIG,SOURCES,MODELS,RULES,SYNCPY,JIRA,STATE,CRED pyLogic
    class EXPORTJSON,CSVFILE contract
    class CONFIGJSON,TOKEN,STATEDB,LASTRUN,LOGS store
    class JIRADC external
    class CMD,M2J entryPoint
```

Reading it:

- **Arrows are "calls" or "reads/writes".** Dotted arrows are indirect: the scheduled task fires the
  orchestrator later, and `Test-Environment.ps1` only reports.
- **Nothing in `src/windows/` decides anything.** The exporter dumps what is on the calendar; the
  Python side filters, routes, and dedupes. That keeps the testable logic in one place and means the
  PowerShell only has to be correct about extracting data.
- **`sources.py` is the only thing that knows which path produced a meeting.** Everything downstream
  sees `Meeting` objects, which is why a future Microsoft Graph source changes nothing in
  `rules`/`sync`/`jira`.
- **`jira.py` is the only module that touches the network**, and `credstore.py` is the only one that
  touches the secret.

## What happens to one meeting

```mermaid
flowchart TD
    START(["one Meeting from a source"]) --> DUP{"already seen<br/>in this run?"}
    DUP -->|"same key or<br/>content hash"| DROP["skip silently"]
    DUP -->|no| FILTERS{"filters<br/><i>cheapest first</i>"}

    FILTERS -->|"cancelled, all-day, not ended,<br/>declined, private, free,<br/>too short/long, text, category"| SKIP["SKIP<br/><i>with the reason</i>"]
    FILTERS -->|passes| TOD{"tour of duty"}

    TOD -->|"wholly outside<br/>+ action = skip"| SKIP
    TOD -->|"wholly outside<br/>+ action = route"| ROUTED["parent = outside_parent<br/><i>before rules, deliberately</i>"]
    TOD -->|"inside, or partial<br/><i>partial counts as inside</i>"| RULES{"rules<br/><i>first match wins</i>"}

    RULES -->|"rule says skip"| SKIP
    RULES -->|"rule matches"| RULEPARENT["parent = rule.parent"]
    RULES -->|"no match"| DEFPARENT["parent = default_parent"]

    ROUTED --> SEEN
    RULEPARENT --> SEEN
    DEFPARENT --> SEEN
    SEEN{"in state.db?"} -->|yes| EXISTS["EXISTS<br/><i>nothing to do</i>"]
    SEEN -->|no| CAP{"under<br/>max_creates_per_run?"}

    CAP -->|no| CAPPED["left for the next run"]
    CAP -->|yes| DRY{"dry run?"}
    DRY -->|yes| WOULD["WOULD<br/><i>creates nothing</i>"]
    DRY -->|no| CREATE["POST the sub-task<br/><i>with the m2j-hash label</i>"]

    CREATE -->|created| RECORD["record in state.db<br/><b>immediately</b>"]
    CREATE -->|"ambiguous failure:<br/>timeout or 502/503/504"| RECOVER{"JQL search<br/>for the label"}
    CREATE -->|"rejected outright"| ERROR["ERROR<br/><i>reported, retried next run</i>"]

    RECOVER -->|"exactly one match"| RECORD
    RECOVER -->|"none, several,<br/>or search failed"| ERROR

    RECORD --> WORKLOG{"log_work?"}
    WORKLOG -->|yes| LOGTIME["worklog = the meeting's<br/>real duration"]
    WORKLOG -->|no| TRANSITION
    LOGTIME -->|"failed"| RETRY["retried next run,<br/>up to 3 attempts"]
    LOGTIME -->|ok| TRANSITION{"transition_to?"}
    TRANSITION -->|yes| MOVE["move the sub-task"]
    TRANSITION -->|no| DONE(["done"])
    MOVE --> DONE

    classDef good fill:#0b5345,stroke:#062e26,color:#ffffff
    classDef bad fill:#78281f,stroke:#4a1811,color:#ffffff
    classDef neutral fill:#34495e,stroke:#1c2833,color:#ffffff
    class CREATE,RECORD,LOGTIME,MOVE,DONE good
    class ERROR bad
    class SKIP,DROP,EXISTS,CAPPED,WOULD,RETRY neutral
```

The two things that keep repeated runs safe:

1. **State is recorded the instant Jira accepts the create**, before the worklog and transition. A
   failure in either can never produce a duplicate sub-task.
2. **An ambiguous create is never blindly retried.** It may already have succeeded, so the
   deterministic `m2j-<hash>` label is looked up first. Anything less than exactly one match is
   reported for a human rather than guessed at.

Because of those, the scan window is not a correctness mechanism — widening `-DaysBack` is always
safe, which is why there is no deferral queue anywhere in this project.

## Supporting files

| Path | Role |
|---|---|
| `app/tools/Test-PowerShellSyntax.ps1` | Parses every `.ps1` and rejects PowerShell 7-only syntax. Real under 5.1. |
| `app/tools/Invoke-WindowsChecks.ps1` | The Windows-only checks: 5.1 parsing, DPAPI, the PS→Python handoff, the CSV push, the entry point, `Test-Environment.ps1` at runtime, and the task start time. Safe on the real workstation. |
| `app/tests/` | `unittest`, offline. `test_guardrails.py` enforces the non-negotiables: stdlib-only, TLS never disabled, no execution-policy bypass, no guarded Outlook properties, CLM-safe scripts. |
| `infra/windows-test-vm/` | Terraform for a throwaway Windows Server host to run those checks on, over SSM with no inbound rules. Outside `app/`, never shipped; it uploads `app/` alone. |
| `.kiro/steering/` | Project rules loaded automatically by Kiro: product scope, tech constraints, structure, workflow, PowerShell and test specifics. |
| `.kiro/agents/`, `.kiro/hooks/`, `tools/` | Kiro dev tooling: cheaper-model subagents, hooks, and the compact test runner they call. Outside `app/`, never shipped. See `KIRO_SETUP.md`. |
