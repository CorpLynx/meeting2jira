# Dependency policy

Oct 6, 2026 · decided by Brandon: standard-library-only is no longer a requirement.

Python packages are allowed in Asgard and Odin when they are **declared, pinned and justified**. The rule changed from "never" to "deliberately": adding a package is a reviewed decision, never a side effect.

## The rules

1. **Declared.** Every non-stdlib import is listed in its deliverable's requirements file: `Asgard/requirements.txt`, `Odin/app/requirements.txt`, `Odin/graph-app/requirements.txt`, `Odin/playwright-app/requirements.txt`. A guardrail test fails on an undeclared import.
2. **Pinned exactly** (`name==1.2.3`). Ranges make a broken run impossible to reproduce and let an unreviewed version in. The guardrail test rejects anything but `==`.
3. **Justified.** A comment above each line says what it is for and why the stdlib isn't enough.
4. **Pure Python first.** Prefer `py3-none-any` wheels. App Control checks every DLL, and a compiled wheel's `.pyd` files are unsigned DLLs, so a compiled dependency needs an explicit note ("native: needs IT approval") and a fallback path.
5. **Optional where it can be.** Import a package only in the module that needs it, and say what to install when it's missing (Heimdall imports Playwright only in `fill`). An app's other commands keep working without it.
6. **Shipped, not fetched.** Updates carry reviewed wheels in the payload ([updates.md](updates.md)); nothing runs `pip` against a network index on the user's machine.
7. **Never weakens TLS.** Packages that bring certifi (requests, httpx) must be paired with `truststore` so the agency CA in the Windows store is trusted; verification is never turned off.

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

SQLAlchemy, Pydantic and Alembic are not proposed for Muninn: the stdlib `sqlite3` layer with numbered SQL migrations works, is tested, and keeps Muninn importable by Odin. Pydantic v2 needs the compiled `pydantic-core`.

## Approval

The federal workstation may still require ISSO approval per package. Record it in the requirements file comment (`# approved: <ticket/date>` or `# approval: pending`), so the file doubles as the software bill of materials.
