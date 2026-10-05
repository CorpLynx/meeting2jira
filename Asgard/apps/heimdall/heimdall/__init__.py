"""Heimdall: fill a SeCcHm (ServiceNow) catalog item in Edge from a saved template, then stop.

    form       the form description: instance, catalog item, and the fields on it (stdlib)
    templates  saved combinations of values for those fields (stdlib)
    browser    drives Edge through Playwright; the only module that needs a third-party package
    cli        the command line: init, fields, template list/show/save/edit/delete, fill

Run it with apps/heimdall/cli.py. A UI can import form and templates directly; neither imports
Playwright. Heimdall never clicks Submit.
"""
