# Asgard toolset evaluation

Oct 2, 2026 · @Brandon

Build Asgard as one pure-Python package on the IT-approved python.exe, with Muninn as a local SQLite hub every app shares. Use PowerShell only for Windows plumbing, and give every AI feature a copy-paste fallback so nothing waits on an AI approval.

Four settings decide which column each app lands in: Edge's remote-debugging policy, your M365 cloud, Jira token access, and Copilot's MCP policy. Assumed image: standard user, AppLocker or App Control enforcing, Constrained Language Mode for unsigned PowerShell, TLS inspection, PIV sign-in, software only via the catalog.

## At a glance

Rows run in build order: Muninn first, because every other app reads or writes it.

| App | Ideal toolset | Least-permission federal option | Deciding risk |
| --- | --- | --- | --- |
| Muninn (data) | SQLite (WAL) + SQLAlchemy or SQLModel + Alembic | Stdlib `sqlite3`, numbered SQL migrations, FTS5 search | Database file inside a OneDrive-synced folder |
| Ysildir (MCP) | Official Python MCP SDK, stdio, VS Code Copilot agent mode | Small stdlib JSON-RPC stdio server; CLI `--json` fallback | Copilot's MCP policy is off by default |
| Odin (Jira) | Python + `httpx` or `atlassian-python-api`, PAT, incremental JQL sync | Stdlib `urllib` + PAT from Credential Manager; `curl.exe` for PIV or Kerberos | PATs disabled, or PIV required at the proxy |
| Baldur (work log) | git log + reflog + PR data, calibrated session model, scikit-learn later | Stdlib `subprocess` + `statistics`, grid-search calibration | No real hours to calibrate against |
| Loki (BLUFs) | Graph Meeting AI Insights or transcripts API + LLM | Saved Copilot prompt in Teams; paste the recap into Loki | AI Insights API absent in GCC High and DoD |
| Freya (reviews) | Muninn evidence, two-pass LLM, every claim cites an ID | Evidence pack + prompt to clipboard, run in M365 Copilot Chat | No approved LLM API |
| Heimdall (SeCcHm) | SeCcHm's API; else Playwright driving installed Edge | Power Automate for desktop, or a fill-assist screen | Edge `RemoteDebuggingAllowed` off blocks every browser driver |
| Bifrost (BEARs) | openpyxl + Confluence REST + polling | openpyxl + stdlib or `curl.exe` multipart upload | Template needs Excel to recalculate or run macros |
| Valkyrie (install) | IT bundle or `winget configure`, then a Python bootstrap | Preflight + catalog requests + per-user Python setup | Venv launchers blocked in your profile |
| Valhalla (uninstall) | IT-managed uninstall + Valhalla for user data | Replay Valkyrie's install ledger in reverse | Removing IT-managed prerequisites |

Spelling: Heimdall, and Muninn (Odin's raven of memory, apt for a data layer). Worth fixing before they become package names.

## How the pieces fit

&#91;embedded content: Asgard · ten apps around one SQLite hub\]

Collectors (Odin, Baldur, Loki) write facts to Muninn; Freya, Ysildir, Heimdall and Bifrost read them, so each app can be built and replaced alone.

## Foundation choices

Every app inherits these eight choices; most of the least-permission wins happen here, once.

| Layer | Ideal | Least-permission federal | Why |
| --- | --- | --- | --- |
| Shape | One `asgard` package, ten entry points, one repo | Same | One install, one uninstall; apps share Muninn instead of importing each other |
| Runtime | Python 3.12+ managed with `uv` | IT-installed python.exe from the catalog, 3.10 or newer | Executables in your profile (uv, venv launchers, pip shims) are what AppLocker path rules block |
| Distribution | Wheel from an internal package index, or an IT-built MSI | One `asgard.pyz` zipapp run by python.exe | No venv, no new executables; delete one file to remove |
| Dependencies | Anything on PyPI | Pure-Python wheels only (`py3-none-any` in the filename) | App Control checks every DLL, and compiled `.pyd` files are unsigned DLLs |
| Scripting | PowerShell 7 where Windows-native | Windows PowerShell 5.1, cmdlets only | Unsigned scripts run in Constrained Language Mode: no `Add-Type`, almost no COM |
| UI | One hub window: tkinter + ttk + `sv-ttk` theme | Same; sv-ttk is pure Python plus Tcl | Stdlib, looks native on Windows 11, no localhost server or WebView |
| Secrets | Windows Credential Manager via `keyring` | Same; keyring's Windows backend is pure Python | Per-user, DPAPI-protected, no admin; never in Muninn or config files |
| TLS | `truststore.inject_into_ssl()` at startup | Same, or stdlib `urllib` | The TLS-inspection CA lives in the Windows store, not in certifi's bundle |

- **Python is policed at the interpreter, PowerShell per script.** AppLocker script rules cover .ps1, .bat, .cmd, .vbs and .js. Once python.exe is approved, Asgard needs no further allow-listing, so brief your ISSO on what it does.
- **Test a venv on day one.** `python -m venv` puts a python.exe launcher in your profile. Path rules block it; publisher rules for PSF-signed binaries allow it. If blocked, use the zipapp or `pip install --user` with `python -m asgard`.
- **Python's Windows installer is changing.** The classic installer is deprecated in 3.14 and stops with 3.16; the new [Python install manager](https://www.python.org/downloads/latest/pymanager) installs runtimes per user. Ask IT now how they will package Python after 3.15.
- **TLS inspection.** `requests` and `httpx` trust certifi, so they fail until truststore is injected (Python 3.10+). [pip 24.2+ already uses system certificates](https://pip.pypa.io/en/stable/topics/https-certificates/); stdlib `urllib` loads the Windows store too.
- **Proxy.** Python ignores PAC files. Internal Jira and Confluence usually bypass the proxy; for packages, point pip at the agency mirror rather than fighting proxy auth.
- **tkinter.** Keep one hub with a `ttk.Notebook` tab per app, and run network calls on a worker thread polled with `after()` so the window never freezes.

## AI access in three tiers

Build every AI feature to run at all three tiers, because federal AI access is approved per tool and per data flow, not per laptop.

| Tier | How it works | Needs | Use for |
| --- | --- | --- | --- |
| 1. API | Asgard calls an approved model endpoint directly | Endpoint access, credentials, an approved data flow | Freya's pipeline, batch BLUFs, Baldur's commit tagging |
| 2. MCP | Your approved AI client calls Asgard through Ysildir | Copilot's MCP policy on; VS Code allowing local servers | Questions over Muninn; Freya and Loki as MCP prompts |
| 3. Clipboard | Asgard builds an evidence pack plus prompt; you paste it into M365 Copilot Chat | Nothing new | The floor for every AI feature, working on day one |

- **Tier 1 candidates:** Claude through [Amazon Bedrock in AWS GovCloud](https://www.anthropic.com/news/claude-in-amazon-bedrock-fedramp-high) (FedRAMP High, DoD IL4/5), [Claude for Government](https://support.claude.com/en/articles/13756069) (FedRAMP High), Azure OpenAI in Azure Government, or an agency gateway. Your agency picks.
- **Tier 3** rides on M365 Copilot, [generally available in GCC High since December 2025](https://mc.merill.net/message/RM509108), so prompts stay inside your tenant's boundary.
- **Skip local models** for the "lower-tier model" idea. Ollama or llama.cpp mean executables in your profile, multi-GB downloads and slow CPU inference. Use the smallest model on whichever approved endpoint you get.
- **One switch.** Put tier selection in one module (Mímir, the well Odin drinks from for wisdom), so apps ask for a completion and never care which tier answers.

## Muninn: data layer

Muninn should be one local SQLite file plus a small Python module that owns its schema. Apps never call each other; they write facts to Muninn and read what the others wrote.

| Layer | Ideal | Least-permission federal |
| --- | --- | --- |
| Store | SQLite in WAL mode | Same, via stdlib `sqlite3` |
| Access | SQLAlchemy Core or SQLModel, Pydantic models | `dataclasses` + hand-written SQL |
| Migrations | Alembic | Numbered `.sql` files tracked with `PRAGMA user_version` |
| Search | FTS5 plus embeddings from an approved API | FTS5 alone (usually built into Python's sqlite3; the preflight checks) |
| Location | `%LOCALAPPDATA%\Asgard\muninn.db` | Same |

A starting schema, one table per kind of fact:

| Table | Written by | Read by | Key columns |
| --- | --- | --- | --- |
| `work_items` | Odin | Freya, Baldur, Loki, Ysildir | key, summary, status, epic, updated, resolved |
| `commits` | Baldur | Freya, Loki | sha, repo, authored\_at, adds, dels, ticket\_key |
| `work_sessions` | Baldur | Freya, Odin | start, end, minutes, repo, ticket\_key, confidence |
| `meetings`, `blufs` | Loki | Freya, Ysildir | meeting\_id, date, title, bluf, source\_link |
| `submissions` | Heimdall, Bifrost | Ysildir | system, external\_id, status, last\_polled |
| `events` | Every app | Freya, Ysildir | ts, app, kind, ref, payload (JSON) |

- **Keep it out of OneDrive.** If Known Folder Move redirects Documents or Desktop, a database there will fight the sync engine for file locks.
- **Concurrency.** WAL mode plus `PRAGMA busy_timeout` lets several Asgard windows read while one writes.
- **Provenance.** Store the source ID (Jira key, commit SHA, meeting ID) with every fact so Freya and Loki can cite it.
- **Secrets stay out.** Tokens live in Credential Manager; BitLocker already covers the file at rest.
- **Its partner.** Name the scheduled collectors Huginn, Odin's other raven: they fly out to Jira, git and Teams and bring facts back to Muninn.

## Ysildir: MCP server

Ysildir should be a thin stdio MCP server over Asgard's core functions. Whether anything may call it depends on two policies you don't control.

| Layer | Ideal | Least-permission federal |
| --- | --- | --- |
| Server | Official Python MCP SDK (FastMCP), stdio transport | About 200 lines of stdlib JSON-RPC 2.0: `initialize`, `tools/list`, `tools/call`, `prompts/list`, `prompts/get` |
| Client | VS Code + GitHub Copilot agent mode | Same if allowed; else Copilot runs `python -m asgard … --json` in the terminal; else tier 3 |
| Exposes | Tools (Muninn queries, Odin actions), resources (Muninn views), prompts (Freya, Loki) | Read-only tools first |
| Jira and Confluence | Reuse [mcp-atlassian](https://github.com/sooperset/mcp-atlassian) (supports Server/DC with PATs) | Odin's own functions behind Ysildir |

- **The gate.** For Copilot Business and Enterprise, the ["MCP servers in Copilot" policy is disabled by default](https://docs.github.com/en/copilot/concepts/about-mcp); an org admin must enable it. VS Code adds an [enterprise `ChatMCP` policy](https://code.visualstudio.com/docs/enterprise/ai-settings) that limits where MCP servers can come from. Your GitHub is GitHub Enterprise Server, and Copilot isn't part of the server: you sign in with the enterprise's GitHub.com or GHE.com account, which holds the Copilot license and this policy, and Copilot works on your local clone ([Copilot on GHES](https://docs.github.com/en/enterprise-server@3.21/copilot/copilot-on-ghes/about-copilot-on-ghes)). Without that account, Ysildir starts at the terminal or clipboard fallback.
- **Why the stdlib fallback.** The SDK pulls compiled wheels such as pydantic-core, which App Control can block. MCP over stdio is plain JSON-RPC, so the protocol itself needs nothing extra.
- **No port.** Stdio means nothing listens on the network, so there is no firewall rule to request.
- **Prompts as commands.** MCP prompts appear as slash commands in VS Code chat: the cheapest way to share Freya and Loki with teammates.
- **Treat Jira text as untrusted.** A ticket can carry instructions aimed at the model; keep write tools (create issue, submit) behind VS Code's confirmation prompt.
- **Approval.** mcp-atlassian is community-maintained, not an Atlassian product, so it needs the same software approval as anything else.

## Odin: Jira

Odin needs only Python and Jira's REST API. Drop PowerShell from it unless authentication forces your hand.

| Layer | Ideal | Least-permission federal |
| --- | --- | --- |
| Client | `httpx` or `requests` + `atlassian-python-api` | Stdlib `urllib.request` + `json` |
| Auth | Personal access token (Jira DC 8.14+) as a Bearer header | Same token, read from Credential Manager |
| If PATs won't work | Kerberos or PIV through a Python library | Built-in `curl.exe`: `--negotiate -u :` for Kerberos, `--cert "CurrentUser\MY\<thumbprint>"` for PIV |
| Sync | Incremental JQL (`updated >= last_sync`) into Muninn on a schedule | Same, run when the hub opens |
| Writes | Create, transition, comment, log work from Baldur's drafts | Same, each confirmed in the UI |

- **Plan for 2029.** Atlassian Data Center reaches [end of life on March 28, 2029](https://www.atlassian.com/migration); new-customer sales ended March 30, 2026. [Atlassian Government Cloud holds FedRAMP Moderate](https://www.businesswire.com/news/home/20250317008810/en/Atlassian-Achieves-FedRAMPR-Moderate-Authorization-for-Atlassian-Government-Cloud), so keep Jira calls behind one interface a Cloud adapter (API tokens, REST v3) can slot into.
- **PIV at the proxy.** If the reverse proxy demands your smart-card certificate, a PAT alone won't get through. Python's `ssl` can't use a non-exportable card key; [Schannel curl can](https://curl.se/docs/manpage.html), with Windows prompting for your PIN.
- **Why curl.exe.** It lives in System32, so default AppLocker rules allow it, and it trusts the Windows certificate store. `Invoke-RestMethod` does the same and works in Constrained Language Mode, if you'd rather keep PowerShell here.
- **Rate limits.** Jira DC admins can throttle REST calls; sync incrementally and back off on HTTP 429.

## Baldur: git work-log estimates

Skip PyTorch. With no labeled hours and a small tabular dataset, a calibrated session heuristic beats a neural net, and it runs on the stdlib.

| Layer | Ideal | Least-permission federal |
| --- | --- | --- |
| Collect | `git log --numstat` and `git reflog` across local repos; PRs and reviews via `gh` | `subprocess` calls to git.exe; GitHub REST via `urllib` + PAT if `gh` isn't in the catalog |
| Estimate | Calibrated session model; scikit-learn regression once you have months of real hours | Same session model in pure Python, calibrated by grid search |
| Map to tickets | Branch and commit keys (`ABC-123`), a small model for the leftovers | Regex, then `difflib` fuzzy match against Muninn's `work_items` |
| Output | Draft Jira worklogs through Odin | Same, each confirmed by you |

The method:

1. Group your commits by author date into sessions: commits less than a gap apart share one (start the gap at 2 hours).
2. Credit each session a lead-in for work before its first commit (start at 1 hour).
3. Widen sessions with other evidence: reflog entries, PR reviews, and Jira transitions from Odin.
4. Log real hours for two to four weeks, then grid-search the gap and lead-in that minimize error.
5. Split each session across tickets by commit share and store it in `work_sessions`.

- **Reflog is local and expires** after 90 days by default, so collect weekly.
- **Author date, not committer date.** Rebases rewrite committer dates; collect from local clones before squash merges flatten history.
- **Where an LLM helps:** tagging commits (feature, fix, chore) and guessing tickets for unlabeled commits, not producing the hour number.
- **Drafts only.** If estimates feed a timesheet or Jira worklog, you confirm each one; labor records carry compliance weight.

## Loki: BLUF writer

Loki's ideal path depends on your M365 cloud. Copilot's meeting-insights API exists only in the global service, while plain transcripts also work in GCC High and DoD.

| Layer | Ideal | Least-permission federal |
| --- | --- | --- |
| Meetings, global service | [Meeting AI Insights API](https://learn.microsoft.com/en-us/microsoftteams/platform/graph-api/meeting-transcripts/meeting-insights): Copilot's notes and action items as JSON; needs a Copilot license and `OnlineMeetingAiInsight.Read.All` | A saved BLUF prompt run in Teams' Copilot, or the recap copied into Loki |
| Meetings, GCC High or DoD | [Transcripts API](https://learn.microsoft.com/en-us/graph/api/calltranscript-get) (`OnlineMeetingTranscript.Read.All`) plus your own tier 1 summary | Same as above |
| Auth | Entra app registration + MSAL, with admin consent | None: no app registration |
| Code changes | Diffs and PR text from Baldur's collector, summarized by an LLM | Same data through the clipboard tier |
| Delivery | Draft email through Graph | Clipboard, or a `mailto:` link that opens a prefilled Outlook draft for short BLUFs |

- **Insights API limits.** It is [not available in US Government L4 or L5](https://learn.microsoft.com/en-us/microsoft-365/copilot/extensibility/api/ai-services/meeting-insights/callaiinsight-get). Results can take up to four hours after a meeting, and only transcribed meetings have them.
- **Tenant switches.** Admins can turn off Graph access to transcripts (403 `GraphAccessToTranscriptsDisabled`) or speaker attribution; detect both and fall back.
- **App registration is the real cost.** Federal tenants usually block user consent, so the Graph path is an IT request with a security review.
- **Keep less.** Store the BLUF and a link in Muninn, not full transcripts; transcripts carry their own retention rules.
- **One template.** BLUF line, then decisions, actions (owner, date), risks and asks, for meetings and code changes alike.

## Freya: yearly review writer

Freya is mostly a query, not an agent. Aggregate Muninn's facts with plain code, then run one or two model passes that must cite evidence IDs.

| Layer | Ideal | Least-permission federal |
| --- | --- | --- |
| Inputs | Muninn: `work_items`, `work_sessions`, `commits`, `blufs` | Same |
| Pipeline | Code groups facts by quarter and epic; pass 1 summarizes each quarter as JSON, pass 2 writes the narrative per performance element | Code builds one evidence pack (top items per quarter) plus the prompt |
| Model | Tier 1 API with a long-context model | Tier 3: paste into M365 Copilot Chat |
| Guardrail | Every claim cites a Jira key or SHA; a checker rejects IDs that aren't in Muninn | You spot-check the cited IDs |
| Output | Plain text per performance element | Same |

- **Rubric in, rubric out.** Paste your actual performance elements and standards into the prompt so the narrative maps to how you're rated.
- **Size the pack.** Chat prompts have length limits, so roll up to quarterly highlights before pasting, or attach the pack as a file if your Copilot allows it.
- **Plain text wins.** Appraisal systems take pasted text, so skip python-docx and its compiled lxml dependency.
- **Zero-code variant.** With Ysildir live, Freya can be an MCP prompt: Copilot pulls the evidence itself and you review the draft.

## Heimdall: SeCcHm submissions

Ask the system owner for an API before writing a line of Playwright. If you must drive the browser, one Edge policy decides whether you can at all.

| Layer | Ideal | Least-permission federal |
| --- | --- | --- |
| Path | SeCcHm's API or a service account | Power Automate for desktop, or a fill-assist screen in the hub |
| Browser driver | Playwright for Python with `channel="msedge"` (no browser download), a dedicated profile folder, codegen and tracing | Power Automate's Edge extension, if allowed |
| PIV sign-in | IT sets Edge's `AutoSelectCertificateForUrls` for the site; you type the PIN | You sign in by hand; the flow continues in that session |
| Data | Field values from Muninn via `string.Template` | Same |
| Submit | Stops on the review page; you click Submit | Same |

- **The kill switch.** If Edge's [`RemoteDebuggingAllowed`](https://learn.microsoft.com/en-us/deployedge/microsoft-edge-browser-policies/remotedebuggingallowed) policy is disabled, Edge refuses remote debugging, and Playwright, Selenium and Puppeteer all stop. Check `edge://policy` first.
- **No default profile.** Since Chrome 136, [remote debugging is ignored on the default profile](https://developer.chrome.com/blog/remote-debugging-port), and tooling reports Edge matching. Use a dedicated `--user-data-dir`, as Playwright's persistent context does.
- **Playwright's hidden exe.** Playwright for Python runs a bundled `node.exe` from site-packages. If packages live in your profile, path-based AppLocker blocks it; ask IT to install Playwright in an allowed path.
- **[Power Automate for desktop](https://learn.microsoft.com/en-us/power-automate/desktop-flows/setup)** ships with Windows 11 and runs attended flows with a work account. Those flows live in the tenant's default environment, which needs Dataverse, so your Power Platform admin decides.
- **Fill-assist** shows each prepared field with a Copy button: zero permissions, and it still removes the composing work.
- **Keep a human on Submit.** Security submissions usually carry an attestation in your name.

## Bifrost: BEARs submissions

Bifrost is the easiest app to keep least-permission: openpyxl is pure Python, and a Confluence attachment is one REST call.

| Layer | Ideal | Least-permission federal |
| --- | --- | --- |
| Excel | openpyxl into the BEARs template; `keep_vba=True` for .xlsm | Same |
| Upload | Confluence REST `POST /rest/api/content/{pageId}/child/attachment` with a PAT and `X-Atlassian-Token: no-check` | Stdlib multipart body, or `curl.exe -F "file=@BEARs.xlsx"` |
| Status | Poll page labels, properties or comments with backoff | Same, while the hub is open or from a per-user scheduled task running `pythonw` |
| Notify | Windows toast | A tkinter dialog |

- **Formulas without values.** openpyxl keeps formulas but writes no computed results. If reviewers read formula cells in Confluence's preview, Excel must recalculate first; `comtypes` (pure Python) can drive Excel where CLM PowerShell can't.
- **Map by name.** Ask the template owner to add named ranges, so Bifrost writes by name instead of cell addresses that break when a row is added.
- **Same clock as Jira.** Confluence Data Center shares the March 28, 2029 end of life.
- **Poll politely.** Every 15–30 minutes with backoff is plenty; a page watch plus an Outlook rule is the zero-code alternative.

## Valkyrie: installer for new engineers

Split Valkyrie into what needs IT (installing software) and what doesn't (everything per-user), and automate only the second half.

| Layer | Ideal | Least-permission federal |
| --- | --- | --- |
| Software | An IT bundle in Software Center or Intune, or `winget configure` with a DSC file | Valkyrie lists what's missing and drafts the catalog request with a justification |
| Per-user setup | Python script (steps below) | Same |
| Before Python exists | One small signed .ps1 | A one-page manual first step |
| Language | Python | Python; no Bash |

The per-user steps, in order:

1. Run the day-one preflight and save the results.
2. `git config --global http.sslBackend schannel`, so git trusts the agency CA from the Windows store.
3. `pip config set global.index-url <agency mirror>`.
4. Prompt for Jira, Confluence and GitHub tokens and store them with `keyring`.
5. Install Asgard (wheel or `asgard.pyz`) under `%LOCALAPPDATA%\Asgard`.
6. Add a Start menu shortcut and a per-user uninstall entry under `HKCU\Software\Microsoft\Windows\CurrentVersion\Uninstall\Asgard`, so Settings > Apps can call Valhalla.
7. Write every path, registry key, task and credential name to `install-ledger.json`.

- **Drop Bash.** Two script languages double every fix; Bash here means Git Bash or WSL, and WSL is often disabled.
- **Don't lean on `-ExecutionPolicy Bypass`.** A GPO-set execution policy overrides it, and the attempt gets logged.
- **winget** is often disabled by policy, and its per-user installs land in your profile where AppLocker blocks them, so it stays in the ideal column.
- **Playwright** earns a place only for web-only onboarding steps such as access requests; deep links plus a checklist break less.

## Valhalla: uninstaller

Valhalla should replay Valkyrie's install ledger in reverse, not hunt for leftovers.

| Layer | Ideal | Least-permission federal |
| --- | --- | --- |
| Mechanism | IT uninstall for packaged parts, Valhalla for user data | `python -m asgard.valhalla` reads `install-ledger.json` and undoes each entry in reverse |
| Removes | App files, `%LOCALAPPDATA%\Asgard`, keyring entries, scheduled tasks, shortcuts, HKCU keys, the Edge automation profile | Same, with System32's `cmdkey /delete` and `schtasks /delete` as fallbacks |
| Keeps | Python, Git, VS Code and anything else IT manages | Same |
| If Python is gone | Not needed | A cmdlets-only PowerShell script reading the same ledger |

- **Offer an export first.** Muninn holds a year of Freya's evidence; zip it to a folder the user picks before deleting.
- **Remove only what the ledger lists.** Everything else stays, which keeps Valhalla safe on shared or reimaged machines.
- **No Bash version** here either; one Python path plus the PowerShell fallback covers Windows.

## Day-one preflight

These read-only checks take about 15 minutes and tell you which column each app lands in. Attach the results when you file IT requests.

| Check | How | Result that changes the plan | Affects |
| --- | --- | --- | --- |
| PowerShell language mode | `$ExecutionContext.SessionState.LanguageMode` | `ConstrainedLanguage`: cmdlets-only scripts | Valkyrie, Valhalla |
| Execution policy | `Get-ExecutionPolicy -List` | `AllSigned` at MachinePolicy or UserPolicy: sign scripts or avoid .ps1 | Valkyrie, Valhalla |
| AppLocker | `Get-AppLockerPolicy -Effective -Xml` | Path-only exe rules: nothing runs from your profile | All |
| App Control (WDAC) | `Get-CimInstance -Namespace root\Microsoft\Windows\DeviceGuard -ClassName Win32_DeviceGuard`, field `UsermodeCodeIntegrityPolicyEnforcementStatus` | `2` (enforced): pure-Python dependencies only | All |
| Python and tkinter | `py -0p`, then `python -c "import sys, tkinter; print(sys.version, tkinter.TkVersion)"` | Missing, or below 3.10: catalog request | All |
| Venv launcher | `python -m venv %TEMP%\vt`, then `%TEMP%\vt\Scripts\python -c "print(1)"` | Blocked: zipapp or `pip install --user` route | All |
| SQLite FTS5 | `python -c "import sqlite3; sqlite3.connect(':memory:').execute('create virtual table t using fts5(x)')"` | Error: plain `LIKE` search instead | Muninn |
| TLS inspection | `python -c "import urllib.request; urllib.request.urlopen('https://pypi.org')"` | Certificate error: ask IT about the CA chain; success: truststore will fix `requests` | All |
| Package source | `pip config list`, then `pip download openpyxl --no-deps -d %TEMP%` | No PyPI: get the agency mirror URL | All |
| Edge automation | Open `edge://policy`; look for `RemoteDebuggingAllowed`, `AutoSelectCertificateForUrls`, `ExtensionInstallBlocklist` | `RemoteDebuggingAllowed` false: no Playwright or Selenium | Heimdall, Valkyrie |
| Tools on PATH | `where git gh code curl` | No `gh`: GitHub REST fallback | Baldur, Odin |
| Jira tokens | Jira profile > Personal Access Tokens | Page missing: Kerberos or PIV via `curl.exe` | Odin, Bifrost, Ysildir |
| Copilot MCP | VS Code command `MCP: List Servers`, then add a test server | Blocked: CLI or clipboard tier | Ysildir, Freya, Loki |
| M365 cloud | Your Outlook on the web address | `office365.us` or `apps.mil`: GCC High or DoD, so no Insights API | Loki |

## Open questions

- [ ] Is your tenant GCC, GCC High or DoD? Decides Loki's ideal path.
- [ ] Are Jira and Confluence on Data Center or Government Cloud, and do PATs pass the proxy?
- [ ] AppLocker only, or App Control user-mode enforcement too? Decides the pure-Python rule.
- [ ] Which LLM endpoints are approved for your data: Bedrock GovCloud, Azure Government, or an agency gateway?
- [ ] Do SeCcHm and BEARs have APIs or service accounts? Decides Heimdall's and Bifrost's ideal path.
- [ ] Is "MCP servers in Copilot" enabled for your org? Decides Ysildir's client.
- [ ] Who must approve automated submissions to SeCcHm and BEARs: your ISSO, the system owners, or both?

## Sources

Checked October 2, 2026.

- [About MCP](https://docs.github.com/en/copilot/concepts/about-mcp), GitHub Docs
- [AI settings for enterprises](https://code.visualstudio.com/docs/enterprise/ai-settings), VS Code docs
- [About Copilot on GHES](https://docs.github.com/en/enterprise-server@3.21/copilot/copilot-on-ghes/about-copilot-on-ghes), GitHub Docs (checked October 4, 2026)
- [RemoteDebuggingAllowed policy](https://learn.microsoft.com/en-us/deployedge/microsoft-edge-browser-policies/remotedebuggingallowed), Microsoft Edge docs
- [Changes to remote debugging switches](https://developer.chrome.com/blog/remote-debugging-port), Chrome for Developers
- [Meeting AI Insights API](https://learn.microsoft.com/en-us/microsoftteams/platform/graph-api/meeting-transcripts/meeting-insights), Microsoft Learn
- [Get callAiInsight](https://learn.microsoft.com/en-us/microsoft-365/copilot/extensibility/api/ai-services/meeting-insights/callaiinsight-get), Microsoft Learn (national cloud availability)
- [Get callTranscript](https://learn.microsoft.com/en-us/graph/api/calltranscript-get), Microsoft Graph docs
- [RM509108: Microsoft 365 Copilot in GCC High](https://mc.merill.net/message/RM509108), Microsoft 365 Roadmap mirror
- [Data Center end of life](https://www.atlassian.com/migration), Atlassian
- [Atlassian Government Cloud reaches FedRAMP Moderate](https://www.businesswire.com/news/home/20250317008810/en/Atlassian-Achieves-FedRAMPR-Moderate-Authorization-for-Atlassian-Government-Cloud), Business Wire
- [mcp-atlassian](https://github.com/sooperset/mcp-atlassian), GitHub
- [HTTPS certificates](https://pip.pypa.io/en/stable/topics/https-certificates/), pip docs
- [Python install manager](https://www.python.org/downloads/latest/pymanager), python.org
- [PEP 773 discussion](https://discuss.python.org/t/pep-773-a-python-installation-manager-for-windows/77900), discuss.python.org
- [Claude in Amazon Bedrock: FedRAMP High and DoD IL4/5](https://www.anthropic.com/news/claude-in-amazon-bedrock-fedramp-high), Anthropic
- [Claude for Government FAQ](https://support.claude.com/en/articles/13756069), Claude Help Center
- [curl man page](https://curl.se/docs/manpage.html) (`--cert` with Schannel), curl
- [Power Automate for desktop setup](https://learn.microsoft.com/en-us/power-automate/desktop-flows/setup), Microsoft Learn
