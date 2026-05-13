"""Apple iCloud Reminders MCP server.

Reads reminders via CloudKit API (direct read access).
Creates reminders via iCloud Drive bridge (JSON command files processed by iPhone Shortcut).
"""
import base64
import gzip
import json
import os
import re
import signal
import smtplib
import sys
import uuid
from datetime import datetime
from email.message import EmailMessage
from io import BytesIO
from typing import Any

from mcp.server.fastmcp import FastMCP
from pyicloud import PyiCloudService

mcp = FastMCP("apple-reminders")

_api = None


# ---------------------------------------------------------------------------
# Email-trigger path
# ---------------------------------------------------------------------------
# iOS 17+ Personal Automation can run a Shortcut silently when an email
# matching specific criteria arrives (Run Immediately + Don't Ask Before
# Running + push-mail enabled). We send a low-content email with a known
# subject so the iPhone-side automation matches and fires the Shortcut.
#
# iOS 18.2 has a known regression with locked-screen-off — test on your
# actual iOS version before relying on this in production.

_smtp_config: dict | None = None


def _load_smtp_config() -> dict | None:
    """Load SMTP creds + trigger recipient from
    ~/.config/apple-reminders/config.json. Returns None unless ALL of
    `smtp_user`, `smtp_app_password`, `trigger_email_to` are present."""
    global _smtp_config
    if _smtp_config is not None:
        return _smtp_config or None
    config_path = os.path.expanduser("~/.config/apple-reminders/config.json")
    if not os.path.exists(config_path):
        _smtp_config = {}
        return None
    try:
        with open(config_path) as f:
            cfg = json.load(f)
    except Exception:
        _smtp_config = {}
        return None
    user = cfg.get("smtp_user")
    pw = cfg.get("smtp_app_password")
    to = cfg.get("trigger_email_to")
    if not user or not pw or not to:
        _smtp_config = {}
        return None
    _smtp_config = {
        "smtp_user": user,
        "smtp_app_password": pw,
        "trigger_email_to": to,
        "trigger_email_subject": cfg.get("trigger_email_subject") or "PROCESS-REMINDERS-TRIGGER",
        "smtp_host": cfg.get("smtp_host") or "smtp.gmail.com",
        "smtp_port": int(cfg.get("smtp_port") or 587),
    }
    return _smtp_config


def send_trigger_email(reason: str = "") -> bool:
    """Send a trigger email so the iPhone's iOS Personal Automation fires the
    Shortcut. Subject is the matcher; body is just for traceability.

    Best-effort: returns False on any failure (missing config, SMTP error).
    NEVER raises — the upload itself already succeeded.

    Returns True if SMTP delivery succeeded, False otherwise.
    """
    cfg = _load_smtp_config()
    if not cfg:
        return False  # not configured

    msg = EmailMessage()
    msg["Subject"] = cfg["trigger_email_subject"]
    msg["From"] = cfg["smtp_user"]
    msg["To"] = cfg["trigger_email_to"]
    body = (
        f"apple-reminders trigger.\n\n"
        f"Reason: {reason or '(unspecified)'}\n"
        f"Sent: {datetime.now().isoformat()}\n\n"
        f"This email triggers an iOS Personal Automation that runs the\n"
        f"companion Shortcut. Optionally auto-archived by an inbox filter.\n"
    )
    msg.set_content(body)

    try:
        with smtplib.SMTP(cfg["smtp_host"], cfg["smtp_port"], timeout=10) as smtp:
            smtp.starttls()
            smtp.login(cfg["smtp_user"], cfg["smtp_app_password"])
            smtp.send_message(msg)
            return True
    except smtplib.SMTPAuthenticationError as e:
        print(f"[email-trigger] auth failed (check app password): {e}", file=sys.stderr)
        return False
    except Exception as e:
        print(f"[email-trigger] send failed: {e}", file=sys.stderr)
        return False


def cleanup_trigger_emails(dry_run: bool = False) -> dict:
    """Connect to iCloud IMAP and delete trigger emails (matching subject +
    sender) from the Inbox. Designed to run daily via systemd timer so the
    Inbox doesn't accumulate trigger emails.

    Reads from same `~/.config/apple-reminders/config.json`:
      - `icloud_imap_user` (e.g. your-icloud@icloud.com)
      - `icloud_imap_app_password` (generated at appleid.apple.com)
      - `trigger_email_subject` (matched in subject)
      - `smtp_user` (matched as sender)

    Returns dict {found, deleted, error}. Best-effort — never raises.
    Exit silently if credentials missing (so timer doesn't fail before
    the password is added).
    """
    import imaplib
    config_path = os.path.expanduser("~/.config/apple-reminders/config.json")
    if not os.path.exists(config_path):
        return {"found": 0, "deleted": 0, "error": "config missing"}
    try:
        with open(config_path) as f:
            cfg = json.load(f)
    except Exception as e:
        return {"found": 0, "deleted": 0, "error": f"config load: {e}"}

    user = cfg.get("icloud_imap_user")
    pw = cfg.get("icloud_imap_app_password")
    subject = cfg.get("trigger_email_subject", "PROCESS-REMINDERS-TRIGGER")
    sender = cfg.get("smtp_user")
    if not sender:
        return {"found": 0, "deleted": 0, "error": "smtp_user not set"}

    if not user or not pw:
        # Not configured yet — silent no-op so daily timer doesn't error.
        return {"found": 0, "deleted": 0, "error": "icloud creds not set"}

    try:
        imap = imaplib.IMAP4_SSL("imap.mail.me.com", 993)
        imap.login(user, pw)
    except Exception as e:
        return {"found": 0, "deleted": 0, "error": f"imap login: {e}"}

    try:
        imap.select("INBOX")
        typ, data = imap.search(None, f'(FROM "{sender}" SUBJECT "{subject}")')
        if typ != "OK":
            return {"found": 0, "deleted": 0, "error": f"search failed: {typ}"}
        # data[0] is None/empty when no matches — handle both safely
        ids = (data[0] or b"").split()
        found = len(ids)
        deleted = 0
        if not dry_run:
            for msg_id in ids:
                imap.store(msg_id, "+FLAGS", "\\Deleted")
                deleted += 1
            imap.expunge()
        return {"found": found, "deleted": deleted, "dry_run": dry_run, "error": None}
    finally:
        try:
            imap.close()
        except Exception:
            pass
        imap.logout()


def notify_phone(reason: str) -> dict:
    """Send the trigger email so the iOS Personal Automation runs the
    Shortcut silently in background. Returns {"email_sent": bool}."""
    email_sent = send_trigger_email(reason=reason)
    return {"email_sent": email_sent}


def _dedupe_cookiejar(cookie_dir: str, apple_id: str) -> None:
    """Strip empty-domain duplicate cookies — pyicloud's cookies.get() trips on them."""
    from http.cookiejar import LWPCookieJar
    safe = apple_id.replace("@", "").replace(".", "")
    jar_path = os.path.join(cookie_dir, f"{safe}.cookiejar")
    if not os.path.exists(jar_path):
        return
    jar = LWPCookieJar(jar_path)
    try:
        jar.load(ignore_discard=True, ignore_expires=True)
    except Exception:
        return
    keep = [c for c in jar if c.domain]
    if len(keep) == sum(1 for _ in jar):
        return
    jar.clear()
    for c in keep:
        jar.set_cookie(c)
    jar.save(ignore_discard=True, ignore_expires=True)


def get_api() -> PyiCloudService:
    """Get or create an authenticated iCloud session."""
    global _api
    if _api is not None:
        return _api

    config_path = os.path.expanduser("~/.config/apple-reminders/config.json")
    cookie_dir = os.path.expanduser("~/.config/apple-reminders/session")

    if not os.path.exists(config_path):
        raise ValueError(
            "Run setup first: cd ~/apple-reminders && uv run python3 setup_auth.py"
        )

    with open(config_path) as f:
        config = json.load(f)

    os.makedirs(cookie_dir, exist_ok=True)
    _dedupe_cookiejar(cookie_dir, config["apple_id"])

    _api = PyiCloudService(
        apple_id=config["apple_id"],
        password=config["app_password"],
        cookie_directory=cookie_dir,
    )

    if _api.requires_2fa or _api.requires_2sa:
        _api = None
        raise ValueError(
            "Session expired. Run: cd ~/apple-reminders && uv run python3 setup_auth.py"
        )

    return _api


def get_claude_folder():
    """Get or create the Claude folder in iCloud Drive."""
    api = get_api()
    drive = api.drive
    try:
        return drive["Claude-Reminders"]
    except (KeyError, IndexError):
        drive.create_folders(drive.root.data["drivewsid"], "Claude-Reminders")
        return drive["Claude-Reminders"]


# ── Dispatch ledger ────────────────────────────────────────────────────────────
# Append-only log of every reminder operation we've dispatched. Solves three
# problems at once:
#   1. Cleanup: we always know the EXACT title we sent, so deletes don't have
#      to rely on the (formerly-mangled, now-fixed) `reminders list` decode.
#   2. Pre-create dedupe: caller can check the ledger for an active matching
#      title before firing a create, preventing the 9-dupe cascade from
#      2026-05-10.
#   3. Auto-confirm: a separate poll can flip `pending → active` (for creates)
#      or `delete-pending → deleted` (for deletes) by comparing against
#      `reminders list`. Decoder fix landed 2026-05-10 same day, so list reads
#      are now trustworthy.
#
# Format: one JSON object per line in `state/dispatched.jsonl`. Required keys:
#   {ts, action: reminder|delete|complete|move, sent_title, list, status}
# Status lifecycle:
#   create:    pending → active → (deleted|completed)
#   delete:    delete-pending → deleted
#   complete:  complete-pending → completed
#
# File locking: fcntl.LOCK_EX serialises concurrent writers. Cron + interactive
# sessions can both call upload_command without corrupting the JSONL.

import fcntl
from pathlib import Path

LEDGER_PATH = Path(__file__).parent / "state" / "dispatched.jsonl"


def _ledger_append(entry: dict) -> None:
    """Append one JSON entry to the dispatch ledger, fcntl-locked.

    Best-effort: ledger write failures must NOT break the upload path
    (an upload that landed but didn't log is recoverable; an upload that
    didn't land because the ledger was unwriteable is not).
    """
    try:
        LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(LEDGER_PATH, "a", encoding="utf-8") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    except Exception:
        # Swallow — never block a real dispatch on ledger I/O failure.
        pass


def _ledger_status_for(action: str) -> str:
    """Initial ledger status given the dispatch action."""
    return {
        "reminder": "pending",        # create — Shortcut not yet drained
        "delete": "delete-pending",
        "complete": "complete-pending",
    }.get(action, "unknown")


# Statuses that count a reminder as still "out there" — should block a duplicate
# create. Excludes terminal states (deleted, completed) and create dispatches we
# know didn't land. "pending" is included because the Shortcut might just not
# have drained yet; if we let a second create through, we'd ship 2 dupes.
_LEDGER_ACTIVE_STATUSES = frozenset({"pending", "active"})


def _ledger_read_all() -> list[dict]:
    """Read all rows from the ledger. Returns [] if file missing or unreadable."""
    if not LEDGER_PATH.exists():
        return []
    rows: list[dict] = []
    try:
        with open(LEDGER_PATH, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue  # skip malformed lines, don't fail
    except Exception:
        return []
    return rows


def _ledger_rewrite(rows: list[dict]) -> bool:
    """Atomically rewrite the ledger from the given rows.

    Uses fcntl to coexist with concurrent _ledger_append() calls. Writes to a
    temp file then renames — so even if we crash mid-write, the ledger is never
    half-written. Returns True on success.
    """
    try:
        LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = LEDGER_PATH.with_suffix(LEDGER_PATH.suffix + ".tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                for r in rows:
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")
                f.flush()
                os.fsync(f.fileno())
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        os.replace(tmp_path, LEDGER_PATH)
        return True
    except Exception:
        return False


@mcp.tool()
def confirm_pending(verbose: bool = False) -> dict[str, Any]:
    """Reconcile the dispatch ledger against the live phone state.

    For every ledger row in a *-pending state, check if the corresponding
    operation actually took on the phone (via `list_reminders`):

    - `pending` (create) + title found in list → `active` (with `confirmed_ts`)
    - `delete-pending` + title NOT found in list → `deleted`
    - `complete-pending` + title found in completed → `completed`

    Rows whose phone-side state hasn't changed yet are left untouched (the
    Shortcut may just not have drained yet — try again later).

    This is the auto-maintenance step that closes the dispatch loop. Run as
    cron (every 5-15 min) or on-demand. Idempotent.

    Returns counts of transitions applied and (if verbose) the full transition log.
    """
    rows = _ledger_read_all()
    if not rows:
        return {"success": True, "checked": 0, "transitions": {}, "message": "ledger empty or missing"}

    # Snapshot live phone state — both active and completed.
    try:
        active_records = list_reminders(show_completed=False)
        all_records = list_reminders(show_completed=True)
    except Exception as e:
        return {"success": False, "error": f"could not read live reminders: {e}"}

    # Build lookup sets keyed by (list, title) — exact match.
    active_set = {(r["list"], r["title"]) for r in active_records}
    completed_set = {(r["list"], r["title"]) for r in all_records} - active_set

    now = datetime.now().astimezone().isoformat(timespec="seconds")
    transitions = {"pending→active": 0, "delete-pending→deleted": 0, "complete-pending→completed": 0}
    transition_log: list[dict] = []
    changed = False

    for r in rows:
        action = r.get("action", "reminder")
        status = r.get("status")
        title = r.get("sent_title", "")
        list_name = r.get("list") or "Reminder"
        key = (list_name, title)

        if status == "pending" and action == "reminder":
            # Did the create land?
            if key in active_set or key in completed_set:
                r["status"] = "active"
                r["confirmed_ts"] = now
                transitions["pending→active"] += 1
                if verbose:
                    transition_log.append({"transition": "pending→active", "title": title, "list": list_name})
                changed = True

        elif status == "delete-pending" and action == "delete":
            # Did the delete take? (title gone from active list)
            # Note: delete may have hit a different reminder if the Shortcut's
            # Find filter found a different match, but we record from our intent.
            if key not in active_set:
                r["status"] = "deleted"
                r["confirmed_ts"] = now
                transitions["delete-pending→deleted"] += 1
                if verbose:
                    transition_log.append({"transition": "delete-pending→deleted", "title": title, "list": list_name})
                changed = True

        elif status == "complete-pending" and action == "complete":
            # Did the complete land?
            if key in completed_set:
                r["status"] = "completed"
                r["confirmed_ts"] = now
                transitions["complete-pending→completed"] += 1
                if verbose:
                    transition_log.append({"transition": "complete-pending→completed", "title": title, "list": list_name})
                changed = True

    if changed:
        if not _ledger_rewrite(rows):
            return {"success": False, "error": "ledger rewrite failed"}

    result = {
        "success": True,
        "checked": len(rows),
        "transitions": transitions,
        "total_transitions": sum(transitions.values()),
    }
    if verbose:
        result["log"] = transition_log
    return result


def _ledger_find_active(title: str, list_name: str) -> list[dict]:
    """Return all ledger entries matching {title, list} that are still active.

    Used by create-side dedupe to refuse a duplicate before firing.
    Empty list means "safe to create".
    """
    if not LEDGER_PATH.exists():
        return []
    matches: list[dict] = []
    target_list = list_name or "Reminder"
    try:
        with open(LEDGER_PATH, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                # Treat missing `action` as create — older seeded rows (pre-2026-05-10
                # auto-log) don't have it but still represent live reminders.
                action = e.get("action", "reminder")
                if action != "reminder":
                    continue
                if e.get("status") not in _LEDGER_ACTIVE_STATUSES:
                    continue
                if e.get("sent_title") == title and (e.get("list") or "Reminder") == target_list:
                    matches.append(e)
    except Exception:
        # Best-effort read — never block a real dispatch if ledger is broken.
        return []
    return matches


def upload_command(command: dict) -> str:
    """Upload a command JSON file to iCloud Drive/Claude-Reminders/.

    Pure upload — does NOT fire the trigger. The MCP tool that calls this
    is responsible for firing `notify_phone()` once per batch (after the
    LAST upload), not per command. The iPhone Shortcut processes the entire
    `Claude-Reminders/` folder per run, so one fire wakes the consumer for
    any number of queued commands.

    Side effect: appends one row to the dispatch ledger
    (`state/dispatched.jsonl`) so we have a server-side record of what
    we sent — independent of the (sometimes lagged, formerly-mangled) phone
    state. See module-level "Dispatch ledger" comment.
    """
    folder = get_claude_folder()
    filename = f"cmd-{uuid.uuid4().hex[:8]}.json"
    buf = BytesIO(json.dumps(command, indent=2).encode("utf-8"))
    buf.name = filename
    folder.upload(buf)

    # Log to ledger AFTER successful upload — only record dispatches that
    # actually reached iCloud. (If folder.upload raises, we never log.)
    action = command.get("action", "unknown")
    entry = {
        "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
        "action": action,
        "sent_title": command.get("title", ""),
        "list": command.get("list", ""),
        "status": _ledger_status_for(action),
        "cmd_filename": filename,
    }
    # Optional fields if present in command
    for opt in ("priority", "notes", "flagged", "url"):
        if opt in command:
            entry[opt] = command[opt]
    _ledger_append(entry)

    return filename


@mcp.tool()
def create_reminder(
    title: str,
    notes: str = "",
    list_name: str = "",
    priority: str = "",
    flagged: bool = False,
    url: str = "",
    force: bool = False,
) -> dict[str, Any]:
    """Create an Apple Reminder that syncs to iPhone.

    The reminder is queued to iCloud Drive and processed when you open the Reminders app.

    Pre-create dedupe: if the dispatch ledger already has an active reminder
    with the same {title, list}, the create is REFUSED to prevent the dupe
    cascade we hit 2026-05-10. Override with `force=True` if you genuinely
    want the duplicate.

    Args:
        title: The reminder title.
        notes: Optional notes/description.
        list_name: Which reminder list (e.g. "Reminders", "Shopping"). Empty for default list.
        priority: Priority level: "High", "Medium", "Low", or "" for none.
        flagged: Whether to flag the reminder.
        url: Optional URL to attach.
        force: Skip dedupe check and create even if an active duplicate exists.
    """
    target_list = list_name or "Reminder"
    if not force:
        existing = _ledger_find_active(title, target_list)
        if existing:
            return {
                "success": False,
                "skipped": True,
                "reason": "duplicate",
                "title": title,
                "list": target_list,
                "existing_count": len(existing),
                "message": (
                    f"Refused: {len(existing)} active reminder(s) with title "
                    f"{title!r} already in {target_list!r}. "
                    f"Pass force=True to override."
                ),
            }
    # Always include `list` so the iOS Shortcut can read it unconditionally.
    command = {"action": "reminder", "title": title, "list": target_list}
    if notes:
        command["notes"] = notes
    if priority:
        command["priority"] = priority
    if flagged:
        command["flagged"] = "true"
    if url:
        command["url"] = url
    upload_command(command)
    notify_phone(reason=f"reminder: {title}")
    return {
        "success": True,
        "title": title,
        "message": "Queued — verify-workflow spawned to confirm delivery.",
    }


@mcp.tool()
def create_multiple_reminders(
    reminders: list[dict[str, str]],
    force: bool = False,
) -> dict[str, Any]:
    """Create multiple Apple Reminders at once.

    Each reminder is queued to iCloud Drive and processed when you open the Reminders app.

    Pre-create dedupe: each item is checked against the dispatch ledger.
    Items with an active duplicate {title, list} are SKIPPED (not created)
    unless `force=True`. Skipped items are returned in the result for visibility.

    Args:
        reminders: List of reminder dicts. Each has "title" (required) and optional:
                   "notes", "list", "priority" (High/Medium/Low), "flagged" (true/false), "url".
        force: Skip dedupe check and create all items even if active duplicates exist.
    """
    created: list[str] = []
    skipped: list[dict] = []
    for rem in reminders:
        title = rem["title"]
        target_list = rem.get("list") or "Reminder"
        if not force:
            existing = _ledger_find_active(title, target_list)
            if existing:
                skipped.append({
                    "title": title,
                    "list": target_list,
                    "reason": "duplicate",
                    "existing_count": len(existing),
                })
                continue
        # Always include `list` so iOS Shortcut reads it unconditionally.
        command = {"action": "reminder", "title": title, "list": target_list}
        for key in ("notes", "priority", "flagged", "url"):
            if rem.get(key):
                command[key] = rem[key]
        upload_command(command)
        created.append(title)

    # Fire the trigger ONCE after the whole batch — only if we actually
    # created something. Skip the fire if every item was deduped (no point
    # waking the Shortcut for an empty queue).
    if created:
        notify_phone(reason=f"batch: {len(created)} reminders ({len(skipped)} skipped)")

    return {
        "success": True,
        "count": len(created),
        "titles": created,
        "skipped": skipped,
        "skipped_count": len(skipped),
        "message": (
            f"Created {len(created)} of {len(reminders)} requested. "
            f"{len(skipped)} skipped as duplicates."
            if skipped else "Open Reminders app on your iPhone to sync."
        ),
    }


@mcp.tool()
def complete_reminder(title: str) -> dict[str, Any]:
    """Mark a reminder as complete by title.

    Uses the dispatch ledger to find the EXACT stored title if a matching
    entry exists (handles cases where the user passed a partial title or
    a slightly different form than what was actually dispatched). Falls back
    to the user-provided title if no ledger match — same as before.

    The Shortcut's Find Reminders is exact-match (correct design — see
    apple-reminders SKILL.md), so passing the verbatim ledger title gives
    the highest hit rate.

    Args:
        title: The reminder title (or substring of one — ledger lookup
               will find the exact form).
    """
    resolved_title = title
    # Try exact match against active ledger entries first
    exact = [e for e in _ledger_read_all()
             if e.get("status") in _LEDGER_ACTIVE_STATUSES
             and e.get("action", "reminder") == "reminder"
             and e.get("sent_title") == title]
    if not exact:
        # No exact — try substring match, but only if it uniquely identifies one
        candidates = [e for e in _ledger_read_all()
                      if e.get("status") in _LEDGER_ACTIVE_STATUSES
                      and e.get("action", "reminder") == "reminder"
                      and title in (e.get("sent_title") or "")]
        # Dedupe by sent_title (multiple ledger rows for the same title from different fires)
        unique_titles = {e["sent_title"] for e in candidates}
        if len(unique_titles) == 1:
            resolved_title = candidates[0]["sent_title"]

    upload_command({"action": "complete", "title": resolved_title})
    notify_phone(reason=f"complete: {resolved_title}")
    return {
        "success": True,
        "title_sent": resolved_title,
        "title_input": title,
        "ledger_resolved": resolved_title != title,
        "message": "Open Reminders app on your iPhone to sync.",
    }


@mcp.tool()
def delete_reminder(title: str) -> dict[str, Any]:
    """Delete a reminder by title.

    Finds the reminder matching the title and removes it.
    Processed when you open the Reminders app.

    Args:
        title: The title of the reminder to delete (partial match).
    """
    upload_command({"action": "delete", "title": title})
    notify_phone(reason=f"delete: {title}")
    return {
        "success": True,
        "title": title,
        "message": "Open Reminders app on your iPhone to sync.",
    }


@mcp.tool()
def move_reminder(title: str, list_name: str) -> dict[str, Any]:
    """Move a reminder to a different list by title.

    VPS-side smart delete+create (single fire). The iOS Shortcut's
    `Set List of Reminders to ...` action is a long-standing iOS bug: returns
    success but doesn't actually persist the move (verified with hardcoded
    destination 2026-05-09). Doing it server-side as delete+create works
    today — preserves the reminder's exact title; notes/due/url are NOT
    currently preserved (CloudKit fields not exposed by list_reminders).

    Args:
        title: The title of the reminder to move (partial match).
        list_name: The destination list (e.g., "Shopping List", "📌 Action").
    """
    matches = [r for r in list_reminders(show_completed=False)
               if title.lower() in r.get("title", "").lower()]
    if not matches:
        return {"success": False, "error": f"no incomplete reminder matching {title!r} found"}
    original_title = matches[0]["title"]
    original_list = matches[0].get("list", "?")
    if original_list == list_name:
        return {"success": True, "title": original_title, "message": "already in target list — no-op"}

    upload_command({"action": "delete", "title": original_title})
    upload_command({"action": "reminder", "title": original_title, "list": list_name})
    notify_phone(reason=f"move: {original_title!r} {original_list} → {list_name}")
    return {
        "success": True,
        "title": original_title,
        "from": original_list,
        "to": list_name,
        "message": "Open Reminders app on your iPhone to sync.",
    }


@mcp.tool()
def move_multiple_reminders(items: list[dict[str, str]]) -> dict[str, Any]:
    """Move multiple reminders in one batch (single trigger-email fire).

    Same delete+create as `move_reminder`, batched: all deletes queued,
    all creates queued, then ONE fire drains the whole batch.

    Args:
        items: List of dicts, each with "title" + "list" keys.
    """
    all_reminders = list_reminders(show_completed=False)
    moved = []
    skipped = []
    for it in items:
        target_title_partial = it["title"]
        target_list = it["list"]
        matches = [r for r in all_reminders
                   if target_title_partial.lower() in r.get("title", "").lower()]
        if not matches:
            skipped.append({"title": target_title_partial, "reason": "not found"})
            continue
        original_title = matches[0]["title"]
        original_list = matches[0].get("list", "?")
        if original_list == target_list:
            skipped.append({"title": original_title, "reason": "already in target list"})
            continue
        upload_command({"action": "delete", "title": original_title})
        upload_command({"action": "reminder", "title": original_title, "list": target_list})
        moved.append({"title": original_title, "from": original_list, "to": target_list})

    if moved:
        notify_phone(reason=f"batch move: {len(moved)} reminders")
    return {
        "success": True,
        "count": len(moved),
        "moves": moved,
        "skipped": skipped,
        "message": "Open Reminders app on your iPhone to sync." if moved else "nothing to move",
    }


def _read_varint(data: bytes, offset: int) -> tuple[int, int]:
    """Read a protobuf varint starting at offset.

    Returns (value, num_bytes_consumed). Returns (0, 0) on malformed input.
    """
    value = 0
    shift = 0
    n = 0
    while offset + n < len(data) and n < 10:
        byte = data[offset + n]
        value |= (byte & 0x7F) << shift
        n += 1
        if (byte & 0x80) == 0:
            return (value, n)
        shift += 7
    return (0, 0)


def _decode_title(b64: str) -> str:
    """Decode a gzipped CRDT protobuf TitleDocument to plain text.

    Strategy: scan the decompressed bytes for protobuf field-2 length-delimited
    markers (tag byte 0x12), read the varint length, extract that many bytes,
    and try to decode as UTF-8. Return the longest valid UTF-8 string found that
    looks like a title (has at least one alphanumeric char). This handles
    multi-byte UTF-8 (emoji, em-dash, accented chars) correctly.

    Falls back to the legacy printable-ASCII regex if the protobuf scan finds
    nothing, so we degrade gracefully on unexpected payload shapes.

    Bug history:
    - 2026-05-09: original printable-ASCII regex fix landed (handles ASCII titles
      with length-byte prefix mangling).
    - 2026-05-10: discovered the regex fails on titles with multi-byte UTF-8 chars
      (emoji, em-dash) — splits the title and returns only the longest ASCII
      fragment, often with an orphaned leading space. E.g. "🌷 Call mum — Mother's
      Day" → " Mother's Day". Caused 90+ minutes of misdiagnosis assuming the iOS
      Shortcut was stripping emojis. Phone storage was always correct.
      This protobuf-aware version is the proper fix.
    """
    try:
        raw = gzip.decompress(base64.b64decode(b64))
    except Exception:
        return "(decode error)"

    # New: protobuf-aware UTF-8 decode.
    # We scan byte-by-byte for 0x12 (field 2, wire type 2 = length-delimited).
    # CRITICAL: do NOT skip past matched chunks even on success — protobuf payloads
    # are nested (outer field 2 wraps metadata + an inner field 2 holding the actual
    # title). If we skip past the outer chunk's bytes, we miss the inner title.
    # Instead always advance by 1 byte and let the scan find every nested marker.
    # The longest valid UTF-8 candidate with at least one alphanumeric wins.
    candidates: list[str] = []
    i = 0
    while i < len(raw):
        if raw[i] != 0x12:
            i += 1
            continue
        # Try to interpret as field-2 length-delimited at this offset
        length, consumed = _read_varint(raw, i + 1)
        if consumed == 0 or length == 0 or i + 1 + consumed + length > len(raw):
            i += 1
            continue
        chunk_start = i + 1 + consumed
        try:
            s = raw[chunk_start:chunk_start + length].decode("utf-8")
            if any(c.isalnum() for c in s):
                candidates.append(s)
        except UnicodeDecodeError:
            pass
        i += 1   # always advance by one — find every nested marker

    if candidates:
        return max(candidates, key=len)

    # Fallback: legacy printable-ASCII regex (for payloads where the protobuf
    # scan finds nothing — e.g. unexpected CRDT shapes).
    matches = re.findall(rb"[\x20-\x7e]{3,}", raw)
    if not matches:
        return "(untitled)"
    candidate = max(matches, key=len).decode("utf-8")
    if len(candidate) > 1 and ord(candidate[0]) == len(candidate) - 1:
        candidate = candidate[1:]
    return candidate


def _get_cloudkit_records() -> list[dict]:
    """Fetch all records from the Reminders CloudKit zone, paginating until exhausted."""
    api = get_api()
    ck_root = api._webservices["ckdatabasews"]["url"]
    params = dict(api.session.params)
    url = f"{ck_root}/database/1/com.apple.reminders/production/private/changes/zone"

    all_records: list[dict] = []
    sync_token: str | None = None
    for _ in range(50):  # safety cap
        zone: dict[str, Any] = {"zoneID": {"zoneName": "Reminders"}}
        if sync_token:
            zone["syncToken"] = sync_token
        r = api.session.post(url, params=params, json={"zones": [zone]})
        z = r.json().get("zones", [{}])[0]
        all_records.extend(z.get("records", []))
        if not z.get("moreComing"):
            break
        sync_token = z.get("syncToken")
        if not sync_token:
            break
    return all_records


@mcp.tool()
def list_reminders(show_completed: bool = False, list_name: str = "") -> list[dict[str, Any]]:
    """List all Apple Reminders from your iPhone via CloudKit.

    Args:
        show_completed: Include completed reminders (default False).
        list_name: If non-empty, return only reminders in this list (exact match
                   against the list display name e.g. "Shopping List", "House Cleanup").
                   Use list_reminder_lists() to discover available list names.
    """
    records = _get_cloudkit_records()

    # Build reminder-UUID -> list-name map by joining each List's ReminderIDs.
    # CloudKit stores list membership on the List record, not the Reminder.
    # NOTE: the loop variable below is `_lname`, NOT `list_name` — using
    # `list_name` here would shadow the function parameter and break the
    # post-filter at the bottom (bug discovered + fixed 2026-05-10).
    uuid_to_list: dict[str, str] = {}
    for rec in records:
        if rec.get("recordType") != "List":
            continue
        f = rec.get("fields", {})
        if f.get("Deleted", {}).get("value", 0):
            continue
        _lname = f.get("Name", {}).get("value", "?")
        try:
            ids = json.loads(f.get("ReminderIDs", {}).get("value", "[]"))
        except Exception:
            ids = []
        for rid in ids:
            uuid_to_list[rid] = _lname

    reminders = []
    for rec in records:
        if rec.get("recordType") != "Reminder":
            continue
        fields = rec.get("fields", {})
        deleted = fields.get("Deleted", {}).get("value", 0)
        if deleted:
            continue
        completed = fields.get("Completed", {}).get("value", 0)
        if completed and not show_completed:
            continue

        title_doc = fields.get("TitleDocument", {}).get("value", "")
        title = _decode_title(title_doc) if title_doc else "(untitled)"

        due_ms = fields.get("DueDate", {}).get("value")
        due = datetime.fromtimestamp(due_ms / 1000).isoformat() if due_ms else None

        flagged = fields.get("Flagged", {}).get("value", 0)
        priority = fields.get("Priority", {}).get("value", 0)

        # Resolve list membership via UUID map
        record_name = rec.get("recordName", "")
        uuid = record_name.split("/", 1)[1] if "/" in record_name else record_name
        # NOTE: `_lname` again (NOT `list_name`) — same shadowing bug as the
        # List loop above. The post-filter at the bottom uses `list_name` which
        # is the function parameter; we must not overwrite it here.
        _lname = uuid_to_list.get(uuid, "(orphan)")

        reminders.append({
            "title": title,
            "list": _lname,
            "completed": bool(completed),
            "due": due,
            "flagged": bool(flagged),
            "priority": priority,
        })

    # Optional post-filter by list name (exact match on display name).
    # Done after collection so the join logic stays in one place.
    if list_name:
        reminders = [r for r in reminders if r["list"] == list_name]

    return reminders


@mcp.tool()
def list_reminder_lists() -> list[dict[str, Any]]:
    """List all Apple Reminder lists from your iPhone via CloudKit."""
    records = _get_cloudkit_records()
    lists = []
    for rec in records:
        if rec.get("recordType") != "List":
            continue
        fields = rec.get("fields", {})
        name = fields.get("Name", {}).get("value", "(untitled)")
        deleted = fields.get("Deleted", {}).get("value", 0)
        if deleted:
            continue
        lists.append({"name": name})
    return lists


@mcp.tool()
def list_pending_commands() -> list[dict[str, Any]]:
    """List commands waiting to be processed on iPhone.

    Shows reminder commands that have been uploaded to iCloud Drive
    but not yet processed by the iPhone Shortcut.
    """
    folder = get_claude_folder()
    pending = []
    for item in folder.dir():
        try:
            node = folder[item]
            if hasattr(node, "open") and str(item).endswith(".json"):
                pending.append({"filename": str(item)})
        except Exception:
            pending.append({"filename": str(item)})
    return pending


def shutdown_handler(signum, frame):
    sys.exit(0)


if __name__ == "__main__":
    signal.signal(signal.SIGINT, shutdown_handler)
    signal.signal(signal.SIGTERM, shutdown_handler)
    mcp.run(transport="stdio")
