# Requirements: Odin MCP server and Copilot meeting notes

## Read this first (for the implementing agent)

**Odin is meeting2jira under its new name.** This spec was written against the public
`meeting2jira` repository (baseline commit `5bd4c89` on `main`). The on-premise Odin codebase has
features this baseline doesn't have, so some names, modules, CLI commands, config keys and schema
details here will be out of date.

Before writing any code, do task 0 in `tasks.md`: compare this spec with the code in front of you
and **edit the spec** (all three files) to match it. Treat this spec's statements about the codebase
as assumptions to check, not facts. Each one is tagged **(baseline)**: for example a module path,
the `synced` table's columns, or the CLI's command list. Requirements that aren't tagged describe
intended behavior and stay valid whatever the code looks like. Change them only with the user's
agreement.

Where the on-prem code already has something this spec plans to build (a notes table, an MCP
server, a Graph source, an attachment call), reuse it and narrow the spec. Don't build a second copy.

## Introduction

Odin creates one Jira sub-task for each meeting the user attended. Microsoft 365 Copilot produces
AI meeting notes (Teams "intelligent recap" or "AI notes") for many of those meetings. This feature
lets the user:

1. **Get** Copilot meeting notes as Markdown.
2. **Store** them in Odin's local SQLite database, linked to the meeting they belong to.
3. **Optionally upload** them to Jira as a `.md` attachment on that meeting's sub-task.

All of this is exposed through a new **Odin MCP server**, so an MCP client (Kiro, or another approved
client on the workstation) can drive it in conversation. The same operations are also available
from Odin's CLI, so they work without an MCP client and can be scheduled.

### Decisions already made (with the user)

| Decision | Choice |
|---|---|
| Where notes come from | A **pluggable notes-source interface**. Phase 1 imports notes the user saves or copies out of Teams (a file or pasted text; no approvals needed). Phase 2 adds a Microsoft Graph source once IT registers an app. A browser-session source (like `playwright-app/`) can be added later behind the same interface. |
| Where the MCP server lives | A **separate top-level folder** (`odin-mcp/`, following the `playwright-app/` precedent). It may use the MCP Python SDK from pip. It reuses the core app's config, state DB, token store and Jira client; it does not duplicate them. |
| Where the core logic lives | In the core app's package **(baseline: `app/src/meeting2jira/`)**, standard library only, so the CLI can use it without pip. The MCP server is a thin adapter over it. |
| How notes reach Jira | As an **attachment** (`.md` file) on the meeting's sub-task. |

### Glossary

- **Notes**: one Markdown document of Copilot-generated meeting notes for one meeting.
- **Notes source**: anything that produces notes: a file import, pasted text, Graph (phase 2).
- **Synced meeting**: a meeting with a row in the state DB, i.e. a meeting Odin created a sub-task
  for. **(baseline: table `synced`, primary key `key`, with `issue_key`, `summary`, `start_utc`,
  `content_hash`)**
- **Linked notes**: notes attached to a synced meeting in the state DB.
- **Unmatched notes**: notes that are stored but not yet linked to a meeting.

## Requirements

### Requirement 1: Import notes as Markdown

**User Story:** As the user, I want to import Copilot meeting notes I've saved or copied from
Teams, so that they are kept as clean Markdown without me reformatting them.

#### Acceptance Criteria

1. WHEN the user imports a `.md` or `.txt` file THEN the system SHALL store its text as Markdown,
   decoding UTF-8 (with or without BOM) and falling back to the Windows ANSI code page.
2. WHEN the user imports pasted text (through the MCP tool or `--stdin` on the CLI) THEN the system
   SHALL treat it the same as a `.md` file.
3. WHEN the user imports a `.docx` or `.html` file THEN the system SHALL convert headings, lists,
   bold/italic, links and tables to Markdown using the standard library only, and SHALL keep the
   original file's name as the notes' source reference.
4. IF the file format is not supported THEN the system SHALL refuse the import and name the
   supported formats, without storing anything.
5. WHEN notes are imported THEN the system SHALL normalize them: LF line endings, no trailing
   whitespace, and at most one blank line in a row. It SHALL NOT otherwise rewrite the content.
6. IF the imported text is empty after normalization, or larger than `notes.max_bytes` THEN the
   system SHALL refuse it with a message that says why and what limit applies.
7. WHEN the same notes content (same normalized-content hash) is imported again THEN the system
   SHALL report it as already stored and SHALL NOT create a duplicate.

### Requirement 2: Pluggable notes sources

**User Story:** As the maintainer, I want every way of getting notes to go through one interface, so
that adding Graph (or a browser-session source) later changes nothing downstream.

#### Acceptance Criteria

1. THE system SHALL define a notes-source interface that returns notes records (Markdown text plus
   metadata: title, meeting start time if known, online-meeting id if known, source name, source
   reference). Storage, matching and upload SHALL depend only on that record.
2. THE phase 1 sources SHALL be `file` and `paste`, and SHALL need no network access, app
   registration or third-party package.
3. WHERE a Graph source is configured (phase 2) THE system SHALL fetch Copilot notes for the user's
   own ended meetings only, read-only, using delegated permissions. It SHALL NOT read other users'
   meetings, chats or files.
4. THE system SHALL NOT use EWS for any notes source. **(baseline non-negotiable 9)**

### Requirement 3: Store notes in the SQLite state database

**User Story:** As the user, I want notes kept in Odin's local database next to the meeting records,
so that they survive between runs and can be found by meeting.

#### Acceptance Criteria

1. THE system SHALL store notes in the existing state database **(baseline:
   `%LOCALAPPDATA%\meeting2jira\state.db`)** in a new table, created by an additive migration on
   open. It SHALL NOT alter, drop or rewrite existing tables or rows.
2. EACH notes row SHALL record: the Markdown text, its content hash, the source and source
   reference, the title and meeting start time read from the notes (if any), when it was imported,
   the linked meeting (nullable), how it was linked, and Jira upload state.
3. WHEN the database is opened by a version without this feature THEN the existing app SHALL keep
   working unchanged. The new table is ignored, not a failure.
4. THE system SHALL keep notes when a meeting is removed from state with `forget`, leaving them
   unmatched rather than deleting them. **(baseline: `forget` deletes a `synced` row)**
5. THE system SHALL provide a way to delete a notes row on the user's request, and SHALL NOT delete
   notes automatically.

### Requirement 4: Link notes to the right meeting

**User Story:** As the user, I want imported notes linked to the meeting they're for, automatically
when it's obvious and by my choice when it isn't, so that the wrong meeting never gets the notes.

#### Acceptance Criteria

1. WHEN the user names a meeting explicitly (meeting key or Jira issue key) THEN the system SHALL
   link the notes to that meeting, and SHALL refuse if no such synced meeting exists.
2. WHEN no meeting is named AND the notes include a meeting start time THEN the system SHALL look
   for synced meetings starting within `notes.match_tolerance_minutes` of it, and SHALL link
   automatically only if exactly one candidate also matches on title (normalized, case- and
   whitespace-insensitive, with containment either way against the stored summary).
3. IF there are zero candidates, or more than one THEN the system SHALL store the notes as
   unmatched and SHALL return the candidates (meeting key, issue key, summary, start time) so the
   user can choose.
4. THE system SHALL NOT link notes based on title alone, with no time evidence.
5. WHEN the user links or re-links unmatched notes to a meeting THEN the system SHALL record the
   link as `explicit`. Re-linking notes that were already uploaded SHALL require confirmation,
   because the attachment stays on the old sub-task.
6. A meeting MAY have more than one notes row (for example a re-generated recap). Listing a
   meeting's notes SHALL show all of them, newest first.

### Requirement 5: Optionally upload notes to Jira as an attachment

**User Story:** As the user, I want to choose to attach a meeting's notes to its Jira sub-task as a
`.md` file, so that the record in Jira includes what was discussed.

#### Acceptance Criteria

1. THE upload SHALL be opt-in. `notes.upload_to_jira` SHALL default to `false`, and when it is
   `false` nothing SHALL be uploaded automatically. An explicit upload request from the user (MCP
   tool or CLI) SHALL work regardless of the setting.
2. WHEN notes are uploaded THEN the system SHALL attach them to the linked meeting's sub-task as a
   UTF-8 `.md` file named from `notes.attachment_name_template`, using the Jira Data Center
   attachments API with the existing personal-access-token auth. **(baseline: `JiraClient` in
   `jira.py`)**
3. IF the notes are not linked to a meeting THEN the system SHALL refuse the upload and say how to
   link them.
4. WHEN dry-run is requested THEN the system SHALL show exactly what would be uploaded (issue key,
   file name, size, first lines) and SHALL upload nothing. The MCP upload tool SHALL default to
   dry-run.
5. WHEN the same content has already been uploaded to the same issue THEN the system SHALL report
   it as already uploaded and SHALL NOT upload it again.
6. IF an upload's outcome is ambiguous (a timeout or a gateway error after sending) THEN before any
   retry the system SHALL check the issue's attachments for the expected file name and size, and
   SHALL record the existing attachment instead of uploading a duplicate.
7. THE system SHALL NOT delete or replace attachments in Jira. Uploading changed notes SHALL add a
   new attachment with a distinguishing name (e.g. a `-v2` suffix).
8. IF Jira rejects the upload (attachments disabled, too large, no permission) THEN the system
   SHALL report Jira's reason with a specific next step, SHALL record the failed attempt, and SHALL
   stop retrying after a bounded number of attempts.
9. WHERE `notes.upload_to_jira` is `true` THE daily run MAY upload linked, not-yet-uploaded notes,
   subject to the same per-run cap discipline as creates. **(baseline: `max_creates_per_run`)**

### Requirement 6: Odin MCP server

**User Story:** As the user, I want an MCP server for Odin, so that I can import, review, link and
upload meeting notes by talking to an AI assistant instead of typing commands.

#### Acceptance Criteria

1. THE MCP server SHALL run locally over **stdio only**. It SHALL NOT open a network listener.
2. THE MCP server SHALL expose at least these tools:
   - `list_meetings`: recent synced meetings, with a notes count for each
   - `import_notes`: from a file path or pasted text, optionally naming the meeting
   - `list_unmatched_notes`: with candidate meetings for each
   - `link_notes`: link notes to a meeting
   - `get_notes`: one notes row's Markdown and metadata
   - `upload_notes`: dry-run by default
   - `delete_notes`: with confirmation
   - `fetch_copilot_notes`: phase 2; runs the configured remote source
3. THE MCP server SHALL expose each meeting's notes as a readable MCP resource (e.g.
   `odin://meetings/{meeting_key}/notes`).
4. EVERY tool SHALL declare MCP tool annotations accurately: read-only tools as read-only, and
   `upload_notes` and `delete_notes` as non-read-only and destructive or external, so the client
   asks the user before running them.
5. THE MCP server SHALL use the core app's config, state database, token store and Jira client.
   It SHALL NOT keep its own copies of any of them.
6. THE MCP server SHALL NOT return the Jira token, or any secret, in any tool result, resource or
   log line.
7. IF a tool fails THEN the server SHALL return an MCP error result with the same actionable
   message the CLI would print. It SHALL NOT crash the server process.
8. THE same operations SHALL be available as CLI subcommands (e.g. `notes import|list|link|show|
   upload|delete`) with matching behavior, so the feature works without an MCP client.

### Requirement 7: Security, privacy and the existing non-negotiables

**User Story:** As the user (and the ISSO reviewing Odin), I want the notes feature to keep Odin's
existing security posture, so that adding it doesn't reopen the approval.

#### Acceptance Criteria

1. THE core app SHALL stay standard-library only. The MCP SDK and any other pip packages SHALL be
   confined to `odin-mcp/`. **(baseline guardrail: `test_stdlib_only`)**
2. ALL HTTPS calls SHALL keep TLS verification on, and SHALL use HTTPS only (HTTP only for
   localhost in tests). **(baseline non-negotiables 2-3)**
3. THE system SHALL NOT require admin rights for any step. **(baseline non-negotiable 7)**
4. THE system SHALL NOT log notes content. Logs MAY include notes ids, hashes, sizes, titles and
   issue keys.
5. THE README's security notes SHALL state plainly what notes data is stored locally, where, and
   when it leaves the machine (only on upload to Jira, or the phase-2 Graph read).
6. THE feature SHALL record whether notes at rest are encrypted. The default (plain SQLite in the
   user's profile, like the rest of state) and the alternative (DPAPI-encrypted notes text) SHALL be
   put to the user as a decision during task 0. See design, "Open questions".
7. THE Exchange/Outlook data-minimization rule still applies to the calendar exporters. This
   feature SHALL NOT make the calendar exporters read `Body`, attendees or recipients.
   **(baseline non-negotiable 6)**

### Requirement 8: Configuration

**User Story:** As the user, I want the notes feature configured in the same config file as the rest
of Odin, with safe defaults.

#### Acceptance Criteria

1. THE system SHALL add a `notes` config section with defaults:
   - `enabled`: `true`
   - `upload_to_jira`: `false`
   - `attachment_name_template`: `"meeting-notes-{date}-{slug}.md"`
   - `match_tolerance_minutes`: `15`
   - `max_bytes`: `1000000`
   - `max_uploads_per_run`: `20`
   - `max_upload_attempts`: `3`
   - `source`: `{"type": "file"}`
2. EVERY new key SHALL have a default, validation with an actionable error, a row in the README
   config reference, and an entry in the example config. **(baseline contract 5)**
3. WHERE the Graph source is configured (phase 2) THE config SHALL hold only non-secret settings
   (cloud, tenant id, client id). Any refresh token SHALL be stored with DPAPI like the Jira token.

### Requirement 9: Verification

**User Story:** As the maintainer, I want this feature tested to the same standard as the rest of
Odin, and honest about what was verified where.

#### Acceptance Criteria

1. Core-app tests SHALL be stdlib `unittest`, so they still run on the workstation without pip.
   Jira upload tests SHALL run against the existing local `http.server` mock, including multipart
   parsing, the `X-Atlassian-Token` header, the ambiguous-upload recovery and duplicate
   suppression.
2. THE state migration SHALL be tested against a database created by the previous version, with
   no rows lost. **(baseline: `test_state.py` pattern)**
3. `odin-mcp/` SHALL have its own tests that start the server over stdio and call each tool,
   including the tool annotations and the no-secrets-in-output rule.
4. Converters SHALL be tested with fixture files: `.docx`, `.html`, `.md` with BOM, and `.txt` in
   cp1252.
5. Anything that depends on the real tenant (Copilot export formats, Graph availability in
   GCC/GCC High/DoD, the workstation's MCP client, Jira's attachment limits) SHALL be reported as
   "needs target-machine verification" and added to the HANDOFF checklist.
