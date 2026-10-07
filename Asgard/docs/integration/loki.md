# Loki and Muninn

Identity `loki` · not built yet · module to add: `asgard.muninn.loki`

Loki turns meeting recaps and code changes into BLUFs (bottom line up front: the one sentence, then decisions, actions, risks, asks). It keeps what a BLUF needs and nothing from raw transcripts.

## Tables

| Owns | Reads |
| --- | --- |
| `meetings`, `action_items`, `blufs`, `citations` (owner_type `bluf` only) | `calendar_events` / `v_busy_meetings` (to match a recap to your calendar), `commits`, `pull_requests`, `work_items`, `work_item_aliases` |

`citations` is shared with Freya at table level; `asgard.muninn.citations` keeps Loki to rows with `owner_type = 'bluf'`.

## The flows

**1. A recap arrives.** One `muninn.Run(con, "loki", src, stream)` per source:

| Source (`sources.kind`, name) | `recap_origin` | `external_id` |
| --- | --- | --- |
| `teams`, `graph` (needs an IT app registration) | `graph_insights` or `graph_transcript` | Graph meeting id |
| `manual`, `paste` | `paste` | sha256 of normalised title and start, so pasting twice is one meeting |
| `manual`, `file` | `file` | the same hash |

`loki.upsert_meeting(run, recap)` stores title, times, organizer, attendee count, `source_link` and `notes_summary` (Copilot's notes or your pasted recap, never a transcript), links `calendar_event_id` when exactly one busy calendar event matches start and title, and upserts the action items it found. Emits `meeting.recapped`.

**2. A BLUF is drafted, approved, sent.**

1. `loki.draft(con, subject_type, subject_ref, text, sections, tier, model, prompt_version)` writes `blufs` in `draft` and parses cited IDs into `citations` (`owner_type = 'bluf'`); `citations.verify()` resolves them. Emits `bluf.drafted`.
2. `loki.approve(con, bluf_id)`: `draft → approved`, sets `approved_at`. Emits `bluf.approved`.
3. Sending. Clipboard and `mailto:` can't be confirmed by Asgard, so the person confirms "sent" and `loki.mark_posted(con, bluf_id, sent_via)` records it. Graph mail or a Teams post follow the outgoing protocol in [README.md](README.md#something-that-leaves-asgard): a `sending` marker before the call (the message id or a marker in the body), settled after.

**3. An action item becomes a Jira issue.** Only Odin writes to Jira, and only Loki writes `action_items`, so this is a handshake by events:

1. You choose "file in Jira"; Loki emits `action_item.file_requested` with the item id, text and project.
2. Odin consumes it, creates the issue, and emits `work_item.created` with `{"action_item_id": …}` in the payload.
3. Loki consumes that and sets `action_items.work_item_key`.

Nothing is created in Jira without the person's request, and a replayed event creates nothing twice because Odin checks for an issue already created for that action item (a marker in its description).

## What protects Loki's data

- `blufs`: `approved` needs `approved_at`; `posted` needs `posted_at` and `sent_via` (CHECKs).
- `meetings`: UNIQUE (source, external_id); `ends_at >= starts_at`.
- v3: `action_items.work_item_key` must be a Jira key.
- Search indexes meeting titles and notes, action items and BLUFs automatically (triggers).
- **To add in the migration that ships Loki:** a trigger that refuses editing `body_md` or `bottom_line` once a BLUF is `posted` (it is the record of what was sent).

## Data handling

- No transcripts, ever (rule 9); `notes_summary` only. Transcripts carry their own retention rules.
- In GCC High and DoD the Meeting AI Insights API doesn't exist; the paste path is the floor everywhere.
- What a model sees depends on Mímir's tier; record `tier`, `model` and `prompt_version` on every BLUF.

## Tests Loki needs

- The same recap pasted twice is one meeting; a Graph recap for the same meeting is a second source, linked to the same calendar event.
- The action-item handshake replayed twice files one Jira issue.
- A posted BLUF can't be edited (after the migration); `integrity.check()` clean.
