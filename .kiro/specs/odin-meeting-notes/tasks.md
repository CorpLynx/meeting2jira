# Implementation Plan: Odin MCP server and Copilot meeting notes

Work top to bottom. Each task ends with its tests passing via `python tools/run_tests.py`
(see `.kiro/steering/workflow.md`). Run the PowerShell syntax check whenever a `.ps1`/`.cmd`
changes. Anything that touches the real tenant, Jira instance or workstation is reported as
"needs target-machine verification".

- [ ] 0. Reconcile this spec with the on-prem Odin codebase (no code changes)
  - Read all three spec files, the steering files, and the on-prem code they reference.
  - For every **(baseline)** tag in `requirements.md` and `design.md`, check it against the code
    and fix the spec: package and module names, the `synced` table columns (is a raw subject
    stored?), CLI name and commands, config file layout, `JiraClient.request` signature, how
    `playwright-app/` (or its successor) locates the core app.
  - Look for on-prem features that overlap with this spec (per-meeting notes or attachments, an
    existing MCP server, a Graph source, DPAPI helpers). Narrow the spec to reuse them, and note
    each one under a new "On-prem reconciliation" section at the top of `requirements.md`.
  - Put the five open questions in `design.md` to the user, and record the answers in the spec.
    Don't start task 1 until questions 1 (encryption) and 5 (overlap) are answered.
  - Update `tasks.md` to match (rename files, drop tasks for things that already exist, add tasks
    for any reconciliation work).
  - _Requirements: all_

- [ ] 1. State: add the `notes` table and data access
  - [ ] 1.1 Add the `notes` DDL and indexes to the state module's schema, created on open;
    leave `synced` untouched.
    - _Requirements: 3.1, 3.2, 3.3_
  - [ ] 1.2 Add the state methods listed in the design (`add_notes`, `find_notes_by_hash`,
    `link_notes`, `notes_for_meeting`, `unmatched_notes`, `get_notes`, `delete_notes`,
    `mark_uploaded`, `note_upload_attempt`, `pending_uploads`, `meetings_near`). If question 1
    decided on encryption, encrypt/decrypt `markdown` here, using the existing DPAPI helper.
    - _Requirements: 3.2, 3.5, 4.6, 7.6_
  - [ ] 1.3 Tests: a migration from a pre-feature DB fixture keeps every `synced` row; `forget`
    leaves notes unmatched; duplicate `content_hash` is rejected by the unique index.
    - _Requirements: 3.3, 3.4, 9.2_

- [ ] 2. Config: the `notes` section
  - Add the defaults from Requirement 8, with validation (types, `match_tolerance_minutes` 0-240,
    `max_bytes` > 0, a known `source.type`, `attachment_name_template` fields limited to
    `{date}`, `{slug}`, `{issue}`), the example config entry, and README config-reference rows.
  - Tests: defaults load; each bad value gives an actionable `ConfigError`.
  - _Requirements: 8.1, 8.2_

- [ ] 3. Notes model, normalization and sources
  - [ ] 3.1 `NotesRecord`, `normalize_markdown()`, `notes_hash()` in the new notes module.
    - _Requirements: 1.5, 1.7_
  - [ ] 3.2 `NotesSource` interface, plus `FileSource` (`.md`/`.txt` with utf-8-sig and cp1252
    fallback) and `PasteSource`; enforce `max_bytes` and the empty check.
    - _Requirements: 1.1, 1.2, 1.4, 1.6, 2.1, 2.2_
  - [ ] 3.3 Notes-header parser for title and start time, written to the formats confirmed in
    task 0 (open question 4), with a fixture for each.
    - _Requirements: 4.2_
  - [ ] 3.4 Converters: `.docx` (zipfile + ElementTree) and `.html` (html.parser) to Markdown,
    with fixture tests. Skip any format task 0 found isn't available in the tenant.
    - _Requirements: 1.3, 9.4_

- [ ] 4. Matching and linking (`NotesService`)
  - Implement `import_notes`, `link`, `list_unmatched`, `get`, `delete` and the matching rules from
    the design.
  - Tests:
    - the matching table: explicit by meeting key, explicit by issue key, unknown explicit, 0/1/2
      time candidates, title-only refused, tolerance boundaries, summary-template containment
    - re-linking uploaded notes requires confirmation
    - duplicate import returns the existing id
  - _Requirements: 4.1-4.6, 1.7, 3.5_

- [ ] 5. Jira attachments
  - [ ] 5.1 Refactor `JiraClient.request` into a byte-level `_send` that keeps retries, hints and
    ambiguity; `request` stays the JSON wrapper. All existing Jira tests stay green, unchanged.
    - _Requirements: 5.2_
  - [ ] 5.2 Add `add_attachment()` (stdlib multipart, `X-Atlassian-Token: no-check`, UTF-8
    filename) and `list_attachments()`.
    - _Requirements: 5.2_
  - [ ] 5.3 Extend the mock Jira server to parse multipart, and test success, header presence,
    413/403 hints, and non-ASCII file names.
    - _Requirements: 5.8, 9.1_

- [ ] 6. Upload flow
  - Implement `NotesService.upload` (dry-run, already-uploaded check, name template, `-v{n}`
    versioning, attempt cap, ambiguous-upload recovery by name and size) and `upload_pending`.
  - Hook `upload_pending` into the daily push, after creates, only when `notes.upload_to_jira`;
    upload errors go into the run summary and never affect creates.
  - Tests: every row of the upload section in the design, plus "setting off means no automatic
    upload" and "explicit upload works with the setting off".
  - _Requirements: 5.1-5.9_

- [ ] 7. CLI: `notes` subcommands
  - `notes import <path> | --stdin [--meeting X] [--title T] [--start ISO]`
  - `notes list [--unmatched]`, `notes show <id>`, `notes link <id> <meeting> [--confirm]`
  - `notes upload <id> [--dry-run]`, `notes delete <id> --confirm`
  - Keep existing commands, flags and exit codes unchanged (the CLI surface is a contract).
  - Tests: one test per subcommand through `main()`, exit codes 0/1/2 as the steering defines.
  - _Requirements: 6.8_

- [ ] 8. Guardrails and security
  - Add guardrails: no core module imports `mcp` or any non-stdlib package (the existing stdlib
    test should already cover new modules; prove it with a wiring assertion that they're in the
    glob); and a check that notes content is not passed to logging calls in the notes modules.
  - README security notes: what notes data is stored, where, whether it's encrypted, and when it
    leaves the machine.
  - _Requirements: 7.1-7.7_

- [ ] 9. `odin-mcp/` server
  - [ ] 9.1 Scaffold the folder from the design, with a pinned `requirements.txt` and a launcher.
    Reuse the core-app discovery logic from `playwright-app/` (or its on-prem successor), and add
    it to the existing parity test so the copies can't drift.
    - _Requirements: 6.5_
  - [ ] 9.2 `core.py`: locate and import the core package, load config, open `State` per call,
    and build `JiraClient` lazily from the stored token.
    - _Requirements: 6.5, 6.6_
  - [ ] 9.3 `server.py`: register the tools and resources from the design table with accurate
    annotations, path-argument confinement, and error mapping. Use stdio transport only.
    - _Requirements: 6.1-6.4, 6.7_
  - [ ] 9.4 Tests (pytest): spawn over stdio, check `list_tools` annotations, call each tool against
    a temp data dir and mock Jira, check no token in any output, and check out-of-root paths are
    refused.
    - _Requirements: 6.6, 9.3_
  - [ ] 9.5 `odin-mcp/README.md`: registering the server with the client chosen in task 0, an
    example conversation, and security notes.
    - _Requirements: 6.1, 7.5_

- [ ] 10. Documentation and steering
  - Update `structure.md` (the new modules, `odin-mcp/`, the `notes` table under Contracts as an
    additive schema), `tech.md` (the rule that `odin-mcp/` may use pip and the core may not),
    `README.md`, `ARCHITECTURE.md`, and the HANDOFF status table and target-machine checklist.
  - Add any gotchas found along the way to `debug-playbook.md`.
  - _Requirements: 7.5, 9.5_

- [ ] 11. Phase 2: Graph Copilot source (blocked until IT registers an app; do not start without the user)
  - [ ] 11.1 Confirm the Copilot AI-insights API's status (v1.0 or beta) and availability in the
    tenant's cloud, plus the permission and licensing needs; record the result in `design.md`.
  - [ ] 11.2 If the exporters need a join URL: add an optional schema-v1 field without reading the
    item body, with exporter and `sources` tests.
  - [ ] 11.3 `GraphCopilotSource`: device-code auth over urllib, DPAPI-stored refresh token,
    national-cloud hosts, insights to Markdown, own ended meetings only. Tested against a mocked
    token endpoint and a mocked Graph endpoint.
  - [ ] 11.4 Wire up `fetch_copilot_notes` (MCP) and `notes fetch` (CLI).
  - _Requirements: 2.3, 2.4, 8.3_
