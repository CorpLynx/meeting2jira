# Dependency policy

Oct 6, 2026 · decided by Brandon: standard-library-only is no longer a requirement. Oct 9, 2026: use the best module for each job, and note the alternatives in case it isn't available on-premises.

Python packages are allowed in Asgard and Odin when they are **declared, pinned and justified**. The rule changed from "never" to "deliberately": adding a package is a reviewed decision, never a side effect.

## The rules

1. **Declared.** Every non-stdlib import is listed in its deliverable's requirements file: `Asgard/requirements.txt`, `Odin/app/requirements.txt`, `Odin/graph-app/requirements.txt`, `Odin/playwright-app/requirements.txt`. A guardrail test fails on an undeclared import.
2. **Pinned exactly** (`name==1.2.3`). Ranges make a broken run impossible to reproduce and let an unreviewed version in. The guardrail test rejects anything but `==`.
3. **Justified.** A comment above each line says what it is for, and why it beats the alternatives, the standard library included.
4. **Best module first.** Use the module that does the job best, whether or not it's compiled. App Control checks every DLL, and a compiled wheel's `.pyd` files are unsigned DLLs. So the comment says "native: needs IT approval" for a compiled package, or one that pulls compiled dependencies in.
5. **Alternatives noted.** The same comment says what to use if the package isn't available on-premises, in order of preference. That might be an older line of the same package, another package, the standard-library way, or the feature's degraded path (often the CLI or the clipboard tier). An on-premises gap is then a known swap, not a redesign.
6. **Optional where it can be.** Import a package only in the module that needs it, and say what to install when it's missing (Heimdall imports Playwright only in `fill`). An app's other commands keep working without it.
7. **Shipped, not fetched.** Updates carry reviewed wheels in the payload ([updates.md](updates.md)); nothing runs `pip` against a network index on the user's machine.
8. **Never weakens TLS.** Packages that bring certifi (requests, httpx) must be paired with `truststore` so the agency CA in the Windows store is trusted; verification is never turned off.

## What stays standard-library by design

Not because packages are forbidden, but because these are imported by everything:

- **`asgard.muninn`.** Every app and Odin import it. A dependency there becomes every app's dependency, and Odin's scheduled sync would fail on a machine that lacks it.
- **`asgard/` core modules** that the launcher needs to start and to show an error (`paths`, `launcher`, `catalog`, `install`, `valhalla`). Setup must be able to run before any package is installed.
- **Odin's `meeting2jira` package** keeps working with nothing installed; a package may add an optional path (as `graph-app` does), not become a requirement for the daily run.

## Candidates worth considering

| Package | Pure Python | Would replace | Note |
| --- | --- | --- | --- |
| `truststore` | yes | hand-built SSL contexts | Windows store for `requests`/`httpx`; stdlib `urllib` already uses it |
| `keyring` | yes (Windows backend) | ctypes Credential Manager code | Same store; less code to maintain |
| `openpyxl` | yes | — | Bifrost's workbook |
| `playwright` | no (bundles `node.exe`) | — | Heimdall `fill` only; AppLocker may block its node.exe in a user-profile path |
| `msal` | yes | hand-rolled OAuth | Odin's Graph path, already pinned |
| `mcp` | no: its dependencies `pydantic-core`, `cryptography`, `cffi`, `rpds-py` and, on Windows, `pywin32` are compiled | a hand-written JSON-RPC server | Ysildir, the official MCP SDK (proposed 2.3.0; needs Python 3.10+). If it isn't on-premises, use `mcp` 1.x, then `fastmcp`, then a standard-library server, then the CLI and clipboard tier (`.kiro/specs/ysildir-mcp/design.md`, "Modules") |
| `pydantic` | no (`pydantic-core`) | hand-written argument checks | Ysildir's argument and result models; it comes with `mcp` |

SQLAlchemy, Pydantic and Alembic are not proposed for Muninn: the stdlib `sqlite3` layer with numbered SQL migrations works, is tested, and keeps Muninn importable by Odin. Pydantic v2 needs the compiled `pydantic-core`.

## Approval

The federal workstation may still require ISSO approval per package. Record it in the requirements file comment (`# approved: <ticket/date>` or `# approval: pending`), so the file doubles as the software bill of materials.
