"""Microsoft Graph calendar source for meeting2jira (Path D).

Three modules, split so that only one of them needs anything from pip:

    config.py   national-cloud endpoints, the graph.json file, validation.   stdlib
    mapping.py  Graph event -> schema v1. A duplicate; see its docstring.    stdlib
    client.py   /me/calendarView over urllib: paging, retries, caps.         stdlib
    auth.py     msal: token acquisition and a DPAPI-protected cache.         needs msal

Only auth.py imports msal, and it does so lazily. That is deliberate: it means the fetch, the
mapping and the whole export can be tested offline on any machine, and the single pip dependency is
confined to the one file that genuinely needs it.
"""
