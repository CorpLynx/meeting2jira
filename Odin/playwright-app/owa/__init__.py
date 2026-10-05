"""Read the signed-in user's own calendar out of Outlook on the web, for machines where the
classic Outlook COM interface is unavailable (that is, "new Outlook").

Deliberately a separate application from app/. The shipped tool is standard-library only, which is
what made it approvable on a locked-down machine; this one needs Playwright from pip. Keeping them
apart means the working COM app is untouched and its guardrails stay strict.

The two meet at the same place any other source does: schema-v1 JSON, consumed by
`python -m meeting2jira push --input <file>`. Nothing in the core pipeline changes.

    capture.py   Playwright. Reuses the existing browser session, learns the calendar endpoint by
                 observing one request, then calls that endpoint directly for the exact window.
    mapping.py   Pure conversion to schema v1. No Playwright import, so it is testable anywhere.
"""

__version__ = "0.1.0"
