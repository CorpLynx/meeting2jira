# Updates

Oct 6, 2026 · status: proposed · owner: Asgard core

**Decision: Asgard updates every app; no app updates itself.** One updater means one place that checks where code comes from, one compatibility check against Muninn's schema, one rollback, and one record of what is installed. An app that fetched its own code would be a second, unreviewed software-install path on a federal machine.

## What exists today

`asgard/install.py` copies a payload into `%LOCALAPPDATA%\Asgard\app.new`, swaps it in (the old folder is renamed `app.old-<time>` and removed after a good swap), and appends `{version, at}` to the ledger's history. There is no update check, no version comparison and no downgrade guard; you rerun `setup-Asgard.cmd` from a new copy.

## The model

- **One version for the payload** (`VERSION`, semver), plus each app's own version in its manifest entry (see [platform-consolidation.md](platform-consolidation.md)). The About box lists all of them and Muninn's schema version.
- **A payload is a folder** with `VERSION`, `asgard/`, `apps/`, any vendored wheels in `vendor/`, and `payload.sha256`: one line per file, so the updater can verify what it copies.
- **A source is configured, never discovered**: an IT-controlled file share (`\\server\share\Asgard\`) or an internal HTTPS URL, set in `settings.json` (`"updates": {"source": "...", "check": "daily"}`). No internet source. Empty means updates are manual, as today.

## The update, step by step

1. **Check** at most daily, at launcher start, off the window's thread: read `VERSION` and `payload.sha256` from the source. HTTPS verifies TLS against the Windows store (never disabled); a share is read with the user's own rights.
2. **Compare.** Newer → offer it. Same → nothing. Older → refuse unless you pass `--allow-downgrade`, and refuse even then when Muninn's schema is newer than the old payload's migrations (it couldn't open the file).
3. **Check compatibility.** Read the new payload's manifests: the schema it will migrate to must be inside the `supported` range of every installed app, including external ones such as Odin, whose range is recorded in its manifest entry. If an external app would be left behind, say which and stop before migrating (see "Holding a migration back").
4. **Stage** into `app.new` and verify every file against `payload.sha256`. A mismatch deletes the staged copy and reports it; nothing installed changes.
5. **Wait for idle.** The launcher's runner knows which apps it started; the swap waits until they've exited, and asks you to close any that haven't.
6. **Swap** (`swap_in`): `app` → `app.old-<time>`, `app.new` → `app`. Keep the old folder until the next start succeeds, so a failed start can roll back.
7. **Record** the version, time, source and payload hash in the ledger's history.
8. **Restart Asgard.** `muninn.prepare()` takes the pre-migration backup (`muninn-<date>-before-v<N>.db`), migrates, and reports in `Status`.

## Rolling back

- **Code:** swap `app.old-<time>` back (`Asgard.pyw --rollback`, to build). Possible until the next successful start removes it.
- **Schema:** migrations are forward-only. After a migration, rolling the code back needs the database from before it: `Asgard.pyw --muninn restore muninn-<date>-before-v<N>.db`. Anything written since is lost, which is why the updater says so before migrating and why migrations stay additive.

## Holding a migration back

Today `prepare()` migrates whenever the code has a newer migration. Once external apps declare ranges, it should migrate only to the highest version every installed app supports, and show "Muninn's update waits for Odin to support version N" until they do. `--muninn migrate --force` overrides it. This keeps an Asgard update from stopping Odin's scheduled sync with a `VersionError`.

## Dependencies in an update

When an app needs a package (allowed under [dependency-policy.md](dependency-policy.md)), the payload carries the pinned wheel in `vendor/` and its hash in `payload.sha256`. The updater never runs `pip` against a network index at update time, so an update installs exactly what was reviewed, and works without pip access.

## What apps do

- Declare their version and `supported` schema range in their manifest entry.
- Never download code, never write into `app\`, never prompt to update. Report "a newer version is needed" by raising `VersionError` from `open_app`; the launcher turns that into the update offer.
- Odin is a separate deliverable with its own release process. Asgard reads Odin's declared range; it never updates Odin's files.

## To build, in order

1. `payload.sha256` written by the release step; `install.py` verifies it.
2. Version comparison and the downgrade guard in `install.py`.
3. Manifests with `version` and `supported` (consolidation step 1).
4. The update source setting, check and offer in the launcher.
5. Idle wait, keep-old-until-good-start, `--rollback`.
6. Holding a migration back for external apps.

## Open decisions

- [ ] The source: a share IT controls, or an internal HTTPS location.
- [ ] Whether updates install on offer (proposed) or also silently at logon.
- [ ] Signing: hashes from a trusted source are enough on an IT share; an HTTPS source may need a signed `payload.sha256`.
