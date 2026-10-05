"""Heimdall's command line. Every command a UI needs has a --json form with a stable shape.

    heimdall init                       create the form file from the example
    heimdall fields                     list the form's fields
    heimdall template list | show | save | edit | delete
    heimdall fill [--template NAME]     open Edge, fill the form, stop before Submit

Exit codes: 0 done, 1 an expected problem (the message says what to do), 2 usage, 130 stopped.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any, Optional, Sequence

from . import browser   # imports Playwright only inside browser.run()
from . import form as forms
from . import templates as tpl
from .form import Form, FormError
from .templates import Plan, Store, TemplateError

EXPECTED = (FormError, TemplateError)


def _say(text: str) -> None:
    print(text, flush=True)


def _emit(data: Any) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=False))


def _show_value(value: Any) -> str:
    if value is None:
        return "(left as is)"
    if value is True:
        return "checked"
    if value is False:
        return "unchecked"
    return '""' if value == "" else str(value)


def _load_form(args: argparse.Namespace) -> Form:
    return forms.load(args.form)


def _store(args: argparse.Namespace) -> Store:
    return Store(args.templates)


# ------------------------------------------------------------------ commands


def cmd_init(args: argparse.Namespace) -> int:
    target = Path(args.form) if args.form else forms.form_path()
    if target.exists() and not args.force:
        raise FormError(f"{target} already exists. Edit it, or run 'heimdall init --force' to start again "
                        "from the example (your templates are kept).")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(forms.EXAMPLE, target)
    _say(f"Created {target}.")
    _say("Edit instance_url, catalog_sys_id and the fields to match the SeCcHm form, then run 'heimdall fields'.")
    return 0


def cmd_fields(args: argparse.Namespace) -> int:
    form = _load_form(args)
    if args.json:
        _emit({"form": str(form.path), "item_url": _item_url(form),
               "fields": [f.to_json() for f in form.fields]})
        return 0
    _say(f"Form: {form.path}")
    for f in form.fields:
        extras = []
        if f.required:
            extras.append("required")
        if f.default is not None:
            extras.append(f"default {_show_value(f.default)}")
        tail = f"  ({', '.join(extras)})" if extras else ""
        _say(f"  {f.label}  [{f.kind}]{tail}")
    return 0


def cmd_template_list(args: argparse.Namespace) -> int:
    form = _load_form(args)
    store = _store(args)
    store.check_usable()
    rows = []
    for name in store.names():
        saved = store.get(name)
        rows.append({"name": saved.name, "description": saved.description, "updated": saved.updated,
                     "fields_set": len(saved.values), "attachment": saved.attachment,
                     "problems": tpl.problems(form, saved)})
    if args.json:
        _emit({"templates": rows})
        return 0
    if not rows:
        _say("No templates yet. Save one with: heimdall template save NAME --set \"LABEL=VALUE\" ...")
        return 0
    for row in rows:
        note = f"  - {row['description']}" if row["description"] else ""
        flag = "  [needs fixing]" if row["problems"] else ""
        _say(f"{row['name']}  ({row['fields_set']} of {len(form.fields)} fields){note}{flag}")
    return 0


def cmd_template_show(args: argparse.Namespace) -> int:
    form = _load_form(args)
    saved = _store(args).get(args.name)
    bad = tpl.problems(form, saved)
    if args.json:
        _emit({"name": saved.name, "description": saved.description, "updated": saved.updated,
               "attachment": saved.attachment, "values": saved.values, "problems": bad})
        return 0
    _say(f"{saved.name}" + (f"  - {saved.description}" if saved.description else ""))
    for f in form.fields:
        value = saved.values.get(f.label)
        source = ""
        if value is None and f.default is not None:
            value, source = f.default, "  (form default)"
        _say(f"  {f.label}: {_show_value(value)}{source}")
    _say(f"  Attachment: {saved.attachment or '(none)'}")
    for problem in bad:
        _say(f"  Problem: {problem}")
    return 1 if bad else 0


def cmd_template_save(args: argparse.Namespace) -> int:
    form = _load_form(args)
    store = _store(args)
    saved = store.put(form, args.name, tpl.parse_assignments(args.set), args.attachment,
                      args.description or "", replace=args.replace)
    store.save()
    _say(f'Saved template "{saved.name}" ({len(saved.values)} of {len(form.fields)} fields).')
    return 0


def cmd_template_edit(args: argparse.Namespace) -> int:
    form = _load_form(args)
    store = _store(args)
    if not (args.set or args.unset or args.attachment or args.no_attachment
            or args.description is not None or args.rename):
        raise TemplateError("Nothing to change. Use --set, --unset, --attachment, --no-attachment, "
                            "--description or --rename.")
    saved = store.edit(form, args.name, tpl.parse_assignments(args.set), args.unset,
                       attachment=args.attachment, clear_attachment=args.no_attachment,
                       description=args.description, rename=args.rename)
    store.save()
    _say(f'Updated template "{saved.name}".')
    return 0


def cmd_template_delete(args: argparse.Namespace) -> int:
    store = _store(args)
    gone = store.delete(args.name)
    store.save()
    _say(f'Deleted template "{gone.name}".')
    return 0


def cmd_fill(args: argparse.Namespace) -> int:
    form = _load_form(args)
    saved = _store(args).get(args.template) if args.template else None
    the_plan = tpl.plan(form, saved, tpl.parse_assignments(args.set), args.attachment, args.no_attachment)
    if args.dry_run:
        if args.json:
            body = the_plan.to_json()
            body["item_url"] = _item_url(form)
            _emit(body)
        else:
            _print_plan(form, the_plan)
        return 0

    try:
        browser.run(form, the_plan, lambda m: _say(f"heimdall: {m}"), trace=args.trace)
    except browser.BrowserError as exc:
        print(f"Heimdall: {exc}", file=sys.stderr)
        return 1
    return 0


def _print_plan(form: Form, the_plan: Plan) -> None:
    _say(f"Would open {_item_url(form)}")
    _say(f"Template: {the_plan.template or '(none; form defaults and --set only)'}")
    for f, value in the_plan.steps:
        _say(f"  {f.label}: {_show_value(value)}")
    _say(f"  Attachment: {the_plan.attachment or '(none)'}")
    _say("Heimdall stops before Submit; you review and submit in Edge.")


def _item_url(form: Form) -> str:
    return browser.item_url(form)


# ------------------------------------------------------------------ parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="heimdall", description="Heimdall: fill SeCcHm submissions from templates.")
    parser.add_argument("--form", type=Path, help="form file (default: settings\\heimdall.json under Asgard's data folder)")
    parser.add_argument("--templates", type=Path, help="templates file (default: settings\\heimdall-templates.json)")
    sub = parser.add_subparsers(dest="command", metavar="command")

    def add(group: Any, name: str, func: Any, help_text: str) -> argparse.ArgumentParser:
        p = group.add_parser(name, help=help_text, description=help_text)
        p.set_defaults(func=func)
        return p

    def setting(p: argparse.ArgumentParser) -> None:
        p.add_argument("--set", action="append", default=[], metavar="LABEL=VALUE",
                       help="a field value; repeat for each field. Checkboxes take true or false")

    def json_flag(p: argparse.ArgumentParser) -> None:
        p.add_argument("--json", action="store_true", help="machine-readable output for a UI")

    p = add(sub, "init", cmd_init, "Create the form file from the example")
    p.add_argument("--force", action="store_true", help="replace an existing form file")

    json_flag(add(sub, "fields", cmd_fields, "List the form's fields"))

    t = sub.add_parser("template", help="List, show, save, edit or delete templates",
                       description="Templates are saved combinations of field values.")
    t.set_defaults(func=None, group=t)
    tsub = t.add_subparsers(dest="template_command", metavar="action")

    json_flag(add(tsub, "list", cmd_template_list, "List saved templates"))

    p = add(tsub, "show", cmd_template_show, "Show a template's values for every field")
    p.add_argument("name")
    json_flag(p)

    p = add(tsub, "save", cmd_template_save, "Save a new template")
    p.add_argument("name")
    setting(p)
    p.add_argument("--attachment", metavar="PATH", help="file to attach (checked when you fill)")
    p.add_argument("--description", metavar="TEXT")
    p.add_argument("--replace", action="store_true", help="overwrite a template with this name")

    p = add(tsub, "edit", cmd_template_edit, "Change part of a template")
    p.add_argument("name")
    setting(p)
    p.add_argument("--unset", action="append", default=[], metavar="LABEL", help="stop setting this field")
    group = p.add_mutually_exclusive_group()
    group.add_argument("--attachment", metavar="PATH")
    group.add_argument("--no-attachment", action="store_true", help="remove the attachment")
    p.add_argument("--description", metavar="TEXT")
    p.add_argument("--rename", metavar="NEW_NAME")

    p = add(tsub, "delete", cmd_template_delete, "Delete a template")
    p.add_argument("name")

    p = add(sub, "fill", cmd_fill, "Open Edge, fill the form from a template, and stop before Submit")
    p.add_argument("--template", "-t", metavar="NAME")
    setting(p)
    group = p.add_mutually_exclusive_group()
    group.add_argument("--attachment", metavar="PATH", help="attach this file instead of the template's")
    group.add_argument("--no-attachment", action="store_true", help="don't attach anything this time")
    p.add_argument("--dry-run", action="store_true", help="show what would be filled; don't open Edge")
    p.add_argument("--trace", action="store_true", help="record a Playwright trace (holds session data)")
    json_flag(p)
    return parser


def _tolerant_output() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")   # type: ignore[union-attr]
        except (AttributeError, ValueError, OSError):
            pass


def main(argv: Optional[Sequence[str]] = None) -> int:
    _tolerant_output()
    parser = build_parser()
    args = parser.parse_args(argv)
    func = getattr(args, "func", None)
    if func is None:
        (getattr(args, "group", None) or parser).print_help()
        return 2
    if getattr(args, "json", False) and func is cmd_fill and not args.dry_run:
        print("Heimdall: --json works with --dry-run only.", file=sys.stderr)
        return 2
    try:
        return int(func(args) or 0)
    except EXPECTED as exc:
        print(f"Heimdall: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"Heimdall: couldn't read or write {exc.filename or 'a file'} ({exc.strerror}).", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Heimdall: stopped.", file=sys.stderr)
        return 130
