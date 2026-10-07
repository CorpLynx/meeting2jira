# Bifrost and Muninn

Identity `bifrost` · not built yet · module to add: `asgard.muninn.submissions` (shared with Heimdall)

Bifrost fills the BEARs workbook from Muninn, uploads it to Confluence as an attachment, and watches the page for its review status.

## Tables

| Owns (shared with Heimdall) | Reads |
| --- | --- |
| `submissions` (`system = 'bears'` rows), `submission_status_history` (trigger) | `work_items`, `work_item_aliases`, `accomplishments` and `v_time_by_item_month` if the workbook reports effort |

## The flow

1. **Create.** `submissions.create(con, "bears", title, fields, work_item_key)` in `draft`. `fields` holds the values written, by named range (ask the template owner for named ranges; cell addresses break when a row is added).
2. **Build the workbook** with openpyxl into `artifact_path` under `%LOCALAPPDATA%\Asgard\bifrost\` (not in Muninn). No lock held.
3. **Approve.** `submissions.approve(con, id)` after you've looked at the file.
4. **Upload, with the outgoing protocol.** Before the call, record the attempt (`attachment_version` to be, a marker in the attachment's comment). Upload with `POST /rest/api/content/{pageId}/child/attachment`. Success records `attachment_id`, `attachment_version`, `external_id` (the page id) and `submitted_at`. A timeout leaves it in doubt: list the page's attachments and look for the marker before trying again, so the page never gets two copies.
5. **Watch.** Poll labels, properties or comments every 15–30 minutes with backoff (`next_poll_at`). `submissions.record_status()` moves the state; the trigger records history; emits `submission.status_changed`.

## What protects Bifrost's data

The same state machine CHECKs and history triggers as Heimdall; UNIQUE (system, external_id); `attachment_version >= 1`; v3 checks `work_item_key`.

## Secrets and network

The Confluence PAT lives in Credential Manager (the shared `asgard.secrets` module proposed in [../platform-consolidation.md](../platform-consolidation.md)); HTTP goes through the shared client (TLS verification always on, the agency CA from the Windows store, `ca_bundle` and proxy settings, retries that never repeat a non-idempotent call blindly).

## Tests Bifrost needs

Against a fake Confluence: an upload that times out after the server stored it ends with one attachment; a returned workbook re-uploads as version 2; history has one row per state change.
