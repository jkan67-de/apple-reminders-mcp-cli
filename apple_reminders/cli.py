#!/usr/bin/env python3
"""Apple Reminders CLI — same operations as the MCP, JSON-by-default output.

Usage:
  reminders lists [--pretty]
  reminders list [--show-completed] [--pretty]
  reminders create <title> [--list NAME] [--notes ...] [--priority High|Medium|Low] [--flag] [--url ...]
  reminders create-multi [--file FILE]                # JSON list of reminder dicts (--file or stdin)
  reminders complete <title>
  reminders delete <title>
  reminders move <title> --list NAME
  reminders move-multi [--file FILE]                   # JSON list of {title, list} dicts
  reminders pending [--pretty]
  reminders fire [--reason TEXT]                       # manually fire the trigger email
  reminders cleanup [--dry-run]                        # delete trigger emails from iCloud Inbox

All commands accept --pretty for human-readable output.
"""
import argparse
import json
import sys
from typing import Any

from . import main as rmd  # the MCP module — reuse its functions


def emit(obj: Any, pretty: bool) -> None:
    if pretty:
        if isinstance(obj, list) and obj and isinstance(obj[0], dict):
            for item in obj:
                line = " · ".join(f"{k}={v}" for k, v in item.items() if v is not None and v != "")
                print(line)
        elif isinstance(obj, dict):
            for k, v in obj.items():
                print(f"{k}: {v}")
        else:
            print(obj)
    else:
        json.dump(obj, sys.stdout, default=str)
        sys.stdout.write("\n")


def cmd_lists(args):
    emit(rmd.list_reminder_lists(), args.pretty)


def cmd_list(args):
    emit(rmd.list_reminders(show_completed=args.show_completed, list_name=args.list or ""), args.pretty)


def cmd_create(args):
    kwargs = {"title": args.title}
    if args.list: kwargs["list_name"] = args.list
    if args.notes: kwargs["notes"] = args.notes
    if args.priority: kwargs["priority"] = args.priority
    if args.flag: kwargs["flagged"] = True
    if args.url: kwargs["url"] = args.url
    if args.force: kwargs["force"] = True
    emit(rmd.create_reminder(**kwargs), args.pretty)


def cmd_create_multi(args):
    """Read a JSON list of reminder dicts from --file or stdin, create them as a single batch.

    Each dict needs at least `title`. Optional: `notes`, `list`, `priority`, `flagged`, `url`.

    Example file:
      [
        {"title": "Buy milk", "list": "Shopping List", "priority": "High"},
        {"title": "Call dentist", "notes": "Ask about insurance", "flagged": "true"}
      ]
    """
    if args.file:
        with open(args.file) as f:
            raw = f.read()
    else:
        if sys.stdin.isatty():
            print("ERROR: no --file given and stdin is a TTY. Pipe JSON via stdin or pass --file PATH.", file=sys.stderr)
            sys.exit(2)
        raw = sys.stdin.read()
    try:
        reminders = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"ERROR: invalid JSON: {e}", file=sys.stderr)
        sys.exit(2)
    if not isinstance(reminders, list):
        print("ERROR: input must be a JSON list of reminder dicts.", file=sys.stderr)
        sys.exit(2)
    for i, r in enumerate(reminders):
        if not isinstance(r, dict) or "title" not in r:
            print(f"ERROR: item {i} missing required `title` field.", file=sys.stderr)
            sys.exit(2)
    emit(rmd.create_multiple_reminders(reminders, force=args.force), args.pretty)


def cmd_complete(args):
    emit(rmd.complete_reminder(args.title), args.pretty)


def cmd_delete(args):
    emit(rmd.delete_reminder(args.title), args.pretty)


def cmd_move(args):
    emit(rmd.move_reminder(title=args.title, list_name=args.list), args.pretty)


def cmd_move_multi(args):
    """Read a JSON list of {title, list} dicts from --file or stdin and move them in one batch."""
    if args.file:
        with open(args.file) as f:
            raw = f.read()
    else:
        if sys.stdin.isatty():
            print("ERROR: no --file given and stdin is a TTY. Pipe JSON via stdin or pass --file PATH.", file=sys.stderr)
            sys.exit(2)
        raw = sys.stdin.read()
    try:
        items = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"ERROR: invalid JSON: {e}", file=sys.stderr)
        sys.exit(2)
    if not isinstance(items, list):
        print("ERROR: input must be a JSON list of {title, list} dicts.", file=sys.stderr)
        sys.exit(2)
    for i, r in enumerate(items):
        if not isinstance(r, dict) or "title" not in r or "list" not in r:
            print(f"ERROR: item {i} missing required `title` and/or `list` field.", file=sys.stderr)
            sys.exit(2)
    emit(rmd.move_multiple_reminders(items), args.pretty)


def cmd_pending(args):
    emit(rmd.list_pending_commands(), args.pretty)


def cmd_confirm(args):
    """Reconcile dispatch ledger against live phone state.

    Flips pending → active (creates that landed),
    delete-pending → deleted (deletes that took),
    complete-pending → completed (completes that took).
    Idempotent — safe to run as cron. Exits non-zero on failure so
    heartbeat-wrap catches silent breakage.
    """
    result = rmd.confirm_pending(verbose=args.verbose)
    emit(result, args.pretty)
    if not result.get("success"):
        sys.exit(1)


def cmd_fire(args):
    """Manually re-fire the trigger email so the iPhone's Personal Automation runs
    the Shortcut to consume any queued commands. Useful for retry/debug."""
    result = rmd.notify_phone(reason=args.reason)
    ok = result.get("email_sent", False)
    emit({"success": ok, **result}, args.pretty)
    if not ok:
        sys.exit(1)


def cmd_cleanup(args):
    """Delete trigger emails from iCloud Inbox (Option D cleanup). Safe to run
    anytime — only deletes emails matching the configured subject + sender.
    Designed for daily systemd timer; can also be run manually for ad-hoc cleanup."""
    result = rmd.cleanup_trigger_emails(dry_run=args.dry_run)
    emit(result, args.pretty)
    if result.get("error") and "not set" not in result["error"]:
        sys.exit(1)


def main():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--pretty", action="store_true", help="Human-readable output instead of JSON")

    p = argparse.ArgumentParser(prog="reminders", description="Apple Reminders CLI (JSON output by default — pass --pretty after subcommand for human-readable)")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("lists", parents=[common], help="List all reminder lists").set_defaults(func=cmd_lists)

    pl = sub.add_parser("list", parents=[common], help="List reminders (optionally filtered by list name)")
    pl.add_argument("--show-completed", action="store_true")
    pl.add_argument("--list", help="Filter to one reminder list by exact name (e.g. 'Shopping List', 'House Cleanup'). Use `reminders lists` to see available names.")
    pl.set_defaults(func=cmd_list)

    pc = sub.add_parser("create", parents=[common], help="Create a new reminder")
    pc.add_argument("title")
    pc.add_argument("--list", help="Target reminder list name")
    pc.add_argument("--notes")
    pc.add_argument("--priority", choices=["High", "Medium", "Low"])
    pc.add_argument("--flag", action="store_true")
    pc.add_argument("--url")
    pc.add_argument("--force", action="store_true",
                    help="Skip dispatch-ledger dedupe; create even if an active duplicate exists.")
    pc.set_defaults(func=cmd_create)

    pm = sub.add_parser(
        "create-multi",
        parents=[common],
        help="Create multiple reminders in one batch (JSON list of dicts via --file or stdin). Fires the trigger email once for the whole batch.",
    )
    pm.add_argument("--file", help="Path to a JSON file. Omit to read from stdin (pipe).")
    pm.add_argument("--force", action="store_true",
                    help="Skip dispatch-ledger dedupe; create all items even if active duplicates exist.")
    pm.set_defaults(func=cmd_create_multi)

    px = sub.add_parser("complete", parents=[common], help="Mark a reminder complete by title")
    px.add_argument("title")
    px.set_defaults(func=cmd_complete)

    pd = sub.add_parser("delete", parents=[common], help="Delete a reminder by title")
    pd.add_argument("title")
    pd.set_defaults(func=cmd_delete)

    pmv = sub.add_parser("move", parents=[common], help="Move a reminder to another list by title")
    pmv.add_argument("title")
    pmv.add_argument("--list", required=True, help="Destination list name")
    pmv.set_defaults(func=cmd_move)

    pmvm = sub.add_parser(
        "move-multi",
        parents=[common],
        help="Move multiple reminders in one batch (JSON list of {title, list} dicts via --file or stdin). One trigger-email fire.",
    )
    pmvm.add_argument("--file", help="Path to a JSON file. Omit to read from stdin (pipe).")
    pmvm.set_defaults(func=cmd_move_multi)

    sub.add_parser("pending", parents=[common], help="List pending iCloud Drive commands awaiting iPhone Shortcut").set_defaults(func=cmd_pending)

    pcf = sub.add_parser(
        "confirm",
        parents=[common],
        help="Reconcile dispatch ledger vs phone state (flip pending statuses to terminal). Idempotent — safe to cron.",
    )
    pcf.add_argument("--verbose", action="store_true", help="Include per-row transition log in the output.")
    pcf.set_defaults(func=cmd_confirm)

    pf = sub.add_parser(
        "fire",
        parents=[common],
        help="Manually re-fire the trigger email to wake the iPhone Shortcut (e.g. to retry a missed batch).",
    )
    pf.add_argument("--reason", default="manual fire from CLI", help="Logged in the email body for traceability.")
    pf.set_defaults(func=cmd_fire)

    pcl = sub.add_parser(
        "cleanup",
        parents=[common],
        help="Delete trigger emails from iCloud Inbox. Designed for a daily systemd timer; can be run manually.",
    )
    pcl.add_argument("--dry-run", action="store_true", help="Just count matches, don't delete.")
    pcl.set_defaults(func=cmd_cleanup)

    args = p.parse_args()
    try:
        args.func(args)
    except Exception as e:
        err = {"error": type(e).__name__, "message": str(e)}
        if args.pretty:
            print(f"ERROR: {err['error']}: {err['message']}", file=sys.stderr)
        else:
            json.dump(err, sys.stderr)
            sys.stderr.write("\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
