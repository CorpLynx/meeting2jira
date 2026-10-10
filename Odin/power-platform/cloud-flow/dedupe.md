# Dedupe in a stateless cloud flow

**Build this before anything else.** A cloud flow has no local database, so without a dedupe key it
re-creates the same sub-task on every run. This is the single hardest part of a Power Automate
version, and it is the part that quietly produces a mess in Jira if you get it wrong.

## The mechanism

Every sub-task Odin creates carries a deterministic label:

```
m2j-<first 10 hex characters of sha256(subject | start_utc | end_utc)>
```

Computed from the meeting alone. No state, no database, no ordering. Same meeting, same label, from
any source, on any machine, forever. That is what makes a stateless flow viable:

```
for each calendar event:
    label = "m2j-" + sha256(normalised subject | start | end).hex[:10]
    results = GET /rest/api/2/search?jql=labels="<label>"
      exactly 1  -> already synced, skip
      0          -> create it, with that label attached
      more than 1 -> stop and report; do not guess
```

Attach the label on create or the next run will not find it and you will get a duplicate.

## Reproducing the hash exactly

This is the fiddly part. The label must match **byte for byte** or the flow and the existing tool
will not recognise each other's work, and you will end up with two sub-tasks per meeting.

The Python definition, from `Asgard/apps/odin/odin/models.py`:

```python
subject = " ".join(self.subject.split()).lower()
basis = f"{subject}|{iso_utc(self.start_utc)}|{iso_utc(self.end_utc)}"
return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:32]
```

and the label takes the first 10 characters of that.

Four details that will bite you:

1. **Whitespace is collapsed, then trimmed.** `" ".join(s.split())` turns any run of whitespace into a
   single space and strips the ends. `"Sprint  Planning "` and `"Sprint Planning"` must hash the same.
2. **The subject is lower-cased**, the timestamps are not.
3. **Timestamps are UTC, formatted `%Y-%m-%dT%H:%M:%SZ`** — no sub-seconds, no offset, literal `Z`.
   The Outlook connector can return local times with offsets; convert first.
4. **The separator is a single pipe**, with no spaces around it.

Power Automate has no native SHA-256 expression. Your options, worst to best:

| Approach | Verdict |
|---|---|
| `Compose` + string functions only | Impossible. There is no hash function. |
| An Azure Function or Logic App for the hash | Works, but now you are maintaining a service just to hash a string. |
| **Office Scripts** (`Run script` action) | Reasonable. JavaScript, runs in the M365 tenant, can do SHA-256 via `crypto.subtle`. |
| **Let the existing tool own the hash** | Best. If a machine is in the loop anyway, don't reimplement it. |

That last row is worth sitting with: if reproducing the hash requires standing up a Function or an
Office Script, the "no-code" version now has code in it — just somewhere less testable than where the
code lives today.

## Verify before you trust it

Build the hash step, then check it against values the real implementation produces:

```bash
cd app
PYTHONPATH=src python3 -c "
from datetime import datetime, timezone
from odin.models import Meeting
from odin.sync import dedupe_label
start = datetime(2026, 9, 21, 14, 0, tzinfo=timezone.utc)
m = Meeting(source='t', key='k', subject='Sprint  Planning ',
            start_utc=start, end_utc=datetime(2026, 9, 21, 15, 0, tzinfo=timezone.utc))
print('subject   ', repr(m.subject))
print('hash      ', m.content_hash)
print('label     ', dedupe_label(m))
"
```

If your flow produces a different label for that input, stop and fix it. A mismatch here is not a
cosmetic bug — it is a duplicate sub-task for every meeting, and the labels give no hint that the two
systems disagree.

## The other half: recovery

The same lookup covers the ambiguous-create case. A `POST` that times out or returns 502/503/504 may
have been applied anyway; Jira created the issue and the response was lost coming back. Retrying
duplicates it, and so does giving up and trying next run.

So on an ambiguous failure, search for the label before doing anything:

- exactly one match → it worked, carry on
- none → safe to create
- several, or the search itself fails → report it, change nothing

Power Automate's default behaviour is to **retry failed actions automatically**, which is precisely
wrong for a create. Set the HTTP action's retry policy to **None** and handle the failure yourself
with `Configure run after`. This is the one place where the platform's helpful default causes the
exact bug the existing tool was fixed to avoid.
