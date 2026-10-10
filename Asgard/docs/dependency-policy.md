# Dependency policy

Oct 6, 2026 · decided by Brandon: standard-library-only is no longer a requirement. Oct 9, 2026: use the best module for each job, and give its alternatives in `MODULES.md` in case it isn't available on-premises.

Python packages are allowed in Asgard and Odin when they are **declared, pinned and justified**. The rule changed from "never" to "deliberately": adding a package is a reviewed decision, never a side effect.

## The rules

1. **Declared.** Every non-stdlib import is listed in its deliverable's requirements file: `Asgard/requirements.txt` (Asgard, its apps and Odin, which is an Asgard app since Oct 10, 2026), `Odin/graph-app/requirements.txt`, `Odin/playwright-app/requirements.txt`. A guardrail test fails on an undeclared import.
2. **Pinned exactly** (`name==1.2.3`). Ranges make a broken run impossible to reproduce and let an unreviewed version in. The guardrail test rejects anything but `==`.
3. **Justified.** A comment above each line says what it is for, and why it beats the alternatives, the standard library included.
4. **Best module first.** Use the module that does the job best, whether or not it's compiled; when two are as good, prefer the pure-Python one (`py3-none-any`). App Control checks every DLL, and a compiled wheel's `.pyd` files are unsigned DLLs. So the comment says "native: needs IT approval" for a compiled package, or one that pulls compiled dependencies in.
5. **Optional where it can be.** Import a package only in the module that needs it, and say what to install when it's missing (Heimdall imports Playwright only in `fill`). An app's other commands keep working without it.
6. **Shipped, not fetched.** Updates carry reviewed wheels in the payload ([updates.md](updates.md)); nothing runs `pip` against a network index on the user's machine.
7. **Never weakens TLS.** Packages that bring certifi (requests, httpx) must be paired with `truststore` so the agency CA in the Windows store is trusted; verification is never turned off.
8. **Documented with alternatives.** Every pinned package has a section in its product's `MODULES.md` ([Asgard](../MODULES.md), [Odin](../../Odin/MODULES.md)): where it's imported, what happens without it, and a standard-library alternative and other-package alternatives in case it isn't available on-prem. Give the alternatives in order of preference: an older line of the same package, another package, the standard-library way, or the feature's degraded path (often a CLI or the clipboard tier). An on-premises gap is then a known swap, not a redesign. The guardrail tests fail when a package has no section, a section is missing one of those rows, or an importing file isn't listed.

## What stays standard-library by design

Not because packages are forbidden, but because these are imported by everything:

- **`asgard.muninn`.** Every app and Odin import it. A dependency there becomes every app's dependency, and Odin's scheduled sync would fail on a machine that lacks it.
- **`asgard/` core modules** that the launcher needs to start and to show an error (`paths`, `launcher`, `catalog`, `install`, `valhalla`). Setup must be able to run before any package is installed.
- **Odin's daily run** (`apps/odin`: the meeting push, the Jira sync, posting) keeps working with nothing installed, because the Python install is Asgard's default and IT may approve no packages at all. A package may add an optional path (Odin's window uses PySide6 like every Asgard window; `graph-app` uses `msal`), not become a requirement for the daily run.

## Candidates worth considering

What's actually in use, and the alternatives to each, is in `MODULES.md`; this table is what might be added.

| Package | Pure Python | Would replace | Note |
| --- | --- | --- | --- |
| `truststore` | yes | hand-built SSL contexts | Windows store for `requests`/`httpx`; stdlib `urllib` already uses it |
| `keyring` | yes (Windows backend) | ctypes Credential Manager code | Same store; less code to maintain |
| `openpyxl` | yes | — | Bifrost's workbook |
| `playwright` | no (bundles `node.exe`) | — | Heimdall `fill` only; AppLocker may block its node.exe in a user-profile path |
| `msal` | yes | hand-rolled OAuth | Odin's Graph path, already pinned |
| `mcp` | no: its dependencies `pydantic-core`, `cryptography`, `cffi`, `rpds-py` and, on Windows, `pywin32` are compiled | a hand-written JSON-RPC server | Ysildir, the official MCP SDK (proposed 2.3.0; needs Python 3.10+). If it isn't on-premises, use `mcp` 1.x, then `fastmcp`, then a standard-library server, then the CLI and clipboard tier (`.kiro/specs/ysildir-mcp/design.md`, "Modules") |
| `pydantic` | no (`pydantic-core`) | hand-written argument checks | Ysildir's argument and result models; it comes with `mcp` |

### Considered for Odin and not used (Oct 10, 2026)

Odin's code was reviewed for packages that would make it simpler, once it moved onto Muninn. None earns a place in the daily run yet:

| Package | What it would replace | Why not |
| --- | --- | --- |
| `jira` 3.10.5 (pycontribs) | Most of `odin/jira.py`: URLs, paging, auth | Its `ResilientSession` retries every method, POST included, on a connection error or a 503. A 503 from a proxy after Jira made the sub-task or the worklog would make it twice, which is the one thing Odin must never do; using it safely means `max_retries=0` and Odin's own retry policy on top. It brings 11 packages (requests, urllib3, certifi, charset-normalizer, idna, oauthlib, requests-oauthlib, requests-toolbelt, defusedxml, packaging, typing_extensions) plus `truststore` (rule 7), each for IT to approve, and the Python install would lose Odin without them. It has no call for `/worklog/deleted` or `/rest/pat`, Muninn wants Jira's raw JSON rather than its objects, and an SSO login page would surface as a JSON error instead of Odin's advice. It would save about 150 of `jira.py`'s 470 lines; the safety and diagnosis code stays either way. |
| `requests` + `truststore` | The urllib transport | The same dependency and fallback cost for one real gain, connection reuse (urllib opens a new TLS connection per call), which matters only on the first sync. If that ever matters, `http.client` keep-alive for GETs is the stdlib way. |
| `keyring` | `odin/credstore.py` (DPAPI) and Baldur's Credential Manager code | Pure Python on Windows (with `pywin32-ctypes` and the `jaraco` packages), but the daily run must work without it, so the ctypes code would stay as the fallback: more code, not less. Moving Odin's token into Credential Manager is a separate decision (a one-time migration from the DPAPI file). |
| `pydantic` | `odin/config.py`'s validation | Native (`pydantic-core`), approval pending, and its messages are generic where Odin's say what to change in the config. |

Revisit when a package is approved on the workstation and the Python install carries it: then `jira` with `max_retries=0` behind Odin's own retry rules is the first candidate.

SQLAlchemy, Pydantic and Alembic are not proposed for Muninn: the stdlib `sqlite3` layer with numbered SQL migrations works, is tested, and keeps Muninn importable by Odin. Pydantic v2 needs the compiled `pydantic-core`.

## Approval

The federal workstation may still require ISSO approval per package. Record it in the requirements file comment (`# approved: <ticket/date>` or `# approval: pending`), so the file doubles as the software bill of materials.
