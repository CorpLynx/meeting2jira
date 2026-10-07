# Valkyrie, Valhalla and Muninn

Valkyrie (identity `valkyrie`, not built) sets up a new engineer's machine; Valhalla (built) uninstalls Asgard. Neither writes facts. They matter to Muninn because one creates its first rows and the other decides whether the file survives.

## Valkyrie

| Does | Muninn calls |
| --- | --- |
| Runs the day-one preflight and saves the report as a file (not in Muninn) | none |
| Installs Asgard, which creates Muninn at first start | `muninn.prepare()` via the launcher |
| Records who you are, once: git emails, Jira user, GitHub login, M365 UPN | `muninn.add_identity(con, kind, value, source_id)` on `open_app("valkyrie", …)` |
| Registers the sources you use (Jira base URL, GitHub API URL) | `muninn.ensure_source()`; a password or token in a URL is dropped |
| Stores tokens | Credential Manager (or Odin's DPAPI file), never Muninn |

Identities drive every `is_mine` flag, so Valkyrie asks for each value and shows what it will store. An empty identity list is refused by Baldur (no commits are "yours").

## Valhalla

- **Without purge** it removes the program, shortcuts and the Settings > Apps entry, and keeps all data: `muninn.db` and its `-wal`/`-shm`, `backups\`, settings and logs.
- **With purge** it also removes those, plus any `muninn.before-restore-*.db` copies a restore set aside (each is a whole database).
- **Before a purge** it should offer an export: `muninn.backup(con, folder, label="export")` to a folder you choose. Not built yet.
- The Edge profile Heimdall uses (`heimdall\`) is missing from the purge set; add it when Heimdall's layout settles.
- Scheduled tasks an app created (Baldur's weekly collect) must be in the install ledger so Valhalla removes them; today Baldur's task isn't (see the consolidation spec).
