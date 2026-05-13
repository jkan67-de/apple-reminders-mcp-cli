"""One-time iCloud HSA2 setup for Apple Reminders MCP.

Authenticates via SMS code (Apple's HSA2 / two-factor flow), trusts the
session, dedups duplicate cookies, and saves session state for the MCP.

IDEMPOTENT: re-running detects an already-valid session and exits without
prompts or SMS. Pass --force to redo auth from scratch (e.g. after revoking
the app-specific password).

Exit codes:
  0  success (either re-auth completed OR existing session was still valid)
  1  failure (auth rejected, network error, etc.)
"""
import argparse
import json
import os
import sys
from http.cookiejar import LWPCookieJar

from pyicloud import PyiCloudService

CONFIG_DIR = os.path.expanduser("~/.config/apple-reminders")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
SESSION_DIR = os.path.join(CONFIG_DIR, "session")


def _dedupe_cookiejar(cookie_dir: str, apple_id: str) -> None:
    """Strip empty-domain duplicate cookies. Mirrors main.py's helper —
    pyicloud's session-validation calls cookies.get() without a domain and
    trips on duplicates, which manifests as a spurious 'MFA required' state.
    Must run BEFORE PyiCloudService is constructed."""
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
    if len(keep) == len(list(jar)):
        return  # nothing to dedupe
    jar.clear()
    for c in keep:
        jar.set_cookie(c)
    jar.save(ignore_discard=True, ignore_expires=True)


def existing_session_still_valid() -> bool:
    """Return True if config + cookiejar let us hit CloudKit without re-auth.

    We try a cheap CloudKit call (the same one used at the bottom of full
    auth as the 'success' check). If that works without raising, the session
    is good and the SMS dance is unnecessary.
    """
    if not os.path.exists(CONFIG_PATH):
        print("  No config.json — never authenticated.")
        return False

    try:
        with open(CONFIG_PATH) as f:
            cfg = json.load(f)
        # CRITICAL: dedupe the cookiejar before constructing PyiCloudService.
        # Without this, pyicloud sees duplicate empty-domain cookies and
        # falsely reports MFA required even when the session is fine.
        _dedupe_cookiejar(SESSION_DIR, cfg["apple_id"])
        api = PyiCloudService(
            apple_id=cfg["apple_id"],
            password=cfg["app_password"],
            cookie_directory=SESSION_DIR,
        )
    except Exception as e:
        print(f"  Existing session load failed: {e}")
        return False

    # If 2FA is suddenly required, the session has expired or been invalidated.
    if api._is_mfa_required() or getattr(api, "requires_2sa", False):
        print("  Session expired (2FA now required) — full re-auth needed.")
        return False

    # Try a no-op CloudKit call. If it works, we're good.
    try:
        ck_root = api._webservices["ckdatabasews"]["url"]
        url = f"{ck_root}/database/1/com.apple.reminders/production/private/changes/zone"
        body = {"zones": [{"zoneID": {"zoneName": "Reminders"}}]}
        r = api.session.post(url, params=dict(api.session.params), json=body)
        if r.status_code != 200:
            print(f"  CloudKit returned {r.status_code} — session no longer valid.")
            return False
        data = r.json()
        records = data.get("zones", [{}])[0].get("records", [])
        reminder_count = sum(1 for rec in records if rec.get("recordType") == "Reminder")
        print(f"  Existing session still valid — {reminder_count} reminder records reachable.")
        return True
    except Exception as e:
        print(f"  CloudKit probe failed: {e}")
        return False


def full_reauth() -> None:
    """The original interactive flow — prompts, SMS, trust, save."""
    # Prefill Apple ID from existing config if present, so user only has to
    # re-enter password (the most likely thing that changed).
    default_apple_id = ""
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH) as f:
                default_apple_id = json.load(f).get("apple_id", "")
        except Exception:
            pass

    if default_apple_id:
        prompt = f"Apple ID (email) [{default_apple_id}]: "
    else:
        prompt = "Apple ID (email): "
    apple_id = input(prompt).strip() or default_apple_id
    if not apple_id:
        print("Apple ID required.")
        sys.exit(1)
    app_password = input("Apple ID app-specific password: ").strip()

    os.makedirs(SESSION_DIR, exist_ok=True)

    print("\nConnecting to iCloud...")
    api = PyiCloudService(
        apple_id=apple_id,
        password=app_password,
        cookie_directory=SESSION_DIR,
    )

    if api._is_mfa_required():
        print("\n2FA required.")

        if api._can_request_sms_2fa_code():
            print("Requesting SMS code to your trusted phone number...")
            if not api._request_sms_2fa_code():
                print("Failed to request SMS.")
                sys.exit(1)
            print("SMS sent — check your Messages app.")
        else:
            print("No SMS path available — check your trusted devices for an automatic push.")

        code = input("Enter 6-digit code: ").strip()
        if not api.validate_2fa_code(code):
            print("Code rejected.")
            sys.exit(1)
        print("2FA verified!")

        if api.trust_session():
            print("Session trusted.")
        else:
            print("Warning: trust_session returned False.")
    elif api.requires_2sa:
        print("\n2-step verification required (legacy flow).")
        devices = api.trusted_devices
        for i, d in enumerate(devices):
            print(f"  {i}: {d.get('deviceName', 'Unknown')}")
        idx = int(input("Choose device number: "))
        device = devices[idx]
        if api.send_verification_code(device):
            code = input("Enter verification code: ").strip()
            if not api.validate_verification_code(device, code):
                print("Failed.")
                sys.exit(1)
            api.trust_session()
    else:
        print("No 2FA needed — session is still valid!")

    # Save credentials
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        json.dump({"apple_id": apple_id, "app_password": app_password}, f)
    os.chmod(CONFIG_PATH, 0o600)

    # Strip empty-domain duplicate cookies from the LWP jar — pyicloud's
    # _validate_token uses cookies.get() without a domain and trips on duplicates.
    jar_path = os.path.join(SESSION_DIR, f"{apple_id.replace('@','').replace('.','')}.cookiejar")
    if os.path.exists(jar_path):
        jar = LWPCookieJar(jar_path)
        jar.load(ignore_discard=True, ignore_expires=True)
        keep = [c for c in jar if c.domain]
        jar.clear()
        for c in keep:
            jar.set_cookie(c)
        jar.save(ignore_discard=True, ignore_expires=True)
        print(f"Cleaned cookiejar: kept {len(keep)} cookies.")

    # Verify reminders work via CloudKit
    print("\nTesting CloudKit reminders access...")
    try:
        ck_root = api._webservices["ckdatabasews"]["url"]
        url = f"{ck_root}/database/1/com.apple.reminders/production/private/changes/zone"
        body = {"zones": [{"zoneID": {"zoneName": "Reminders"}}]}
        r = api.session.post(url, params=dict(api.session.params), json=body)
        data = r.json()
        records = data.get("zones", [{}])[0].get("records", [])
        reminder_count = sum(1 for rec in records if rec.get("recordType") == "Reminder")
        print(f"Success! Found {reminder_count} reminder records via CloudKit")
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)

    print("\nDone. Restart Claude / your client and the MCP should work.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="iCloud HSA2 setup for Apple Reminders MCP. Idempotent — skips re-auth if existing session is valid."
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force full re-auth (SMS + trust). Use after revoking the app-specific password or if the session is mysteriously broken.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Check existing session validity and exit. Don't prompt, don't re-auth. Exit 0 if valid, 1 if not.",
    )
    args = parser.parse_args()

    if args.check:
        print("Checking existing session...")
        sys.exit(0 if existing_session_still_valid() else 1)

    if args.force:
        print("--force passed: skipping session check, doing full re-auth.")
        full_reauth()
        return

    print("Checking existing session before prompting for re-auth...")
    if existing_session_still_valid():
        print("\nNothing to do — session is still valid. Use --force to redo auth anyway.")
        return

    print("\nExisting session not valid (or not present). Starting full auth flow.\n")
    full_reauth()


if __name__ == "__main__":
    main()
