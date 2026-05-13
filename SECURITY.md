# Security

## Threat model

This tool acts on behalf of an Apple ID with broad access to iCloud (Reminders + iCloud Drive). The blast radius of a compromise is bounded by what an app-specific password gives access to — which is *most* iCloud services. Treat the host that runs this as you would treat any machine signed in to your iCloud.

## Where secrets live

All credentials live in **`~/.config/apple-reminders/config.json`**, never in this repo. The file should be `chmod 600`.

| Field | What it is | Revocable at |
|---|---|---|
| `apple_id` + `app_password` | Apple ID and an [app-specific password](https://support.apple.com/en-us/102654) | <https://appleid.apple.com/account/manage> → Security |
| `smtp_user` + `smtp_app_password` | Gmail address + [Gmail App Password](https://myaccount.google.com/apppasswords) for sending the trigger email | <https://myaccount.google.com/apppasswords> |
| `icloud_imap_user` + `icloud_imap_app_password` | iCloud address + a second app-specific password for IMAP cleanup (optional) | <https://appleid.apple.com/account/manage> → Security |

The pyicloud session state (cookies) lives under `~/.config/apple-reminders/session/` — also sensitive, also 0600.

## What's *not* in this repo

- No credentials, secrets, tokens, or environment files are committed
- No personal record IDs, share URLs, or contact info
- `state/dispatched.jsonl` (the local dispatch ledger, which contains your reminder titles) is `.gitignore`d
- `__pycache__/`, `.venv/` are `.gitignore`d

## Inputs and trust boundaries

- **MCP tool arguments** (`title`, `notes`, `url`, etc.) flow into JSON command files written to iCloud Drive. They're never passed to a shell, `eval`, or template engine. JSON encoding handles escaping.
- **File names** in `Claude-Reminders/` are server-side UUIDs (`cmd-<8hex>.json`) — never user-derived.
- **The trigger email subject** (default `PROCESS-REMINDERS-TRIGGER`) is effectively a shared secret between the Linux side and the iOS Personal Automation. Anyone who can send mail to your iCloud address *and* knows the subject can wake the Shortcut. The Shortcut only processes its own queued JSON files in iCloud Drive, so the worst case is that they cause your phone to re-process an already-empty queue. Still: pick a non-obvious subject if you're paranoid.
- **No inbound network listener.** The MCP server speaks only stdio; the CLI is invoked locally.

## Outbound connections

| To | Why | Protocol |
|---|---|---|
| `*.icloud.com` (CloudKit, iCloud Drive) | Read reminders, upload command files | HTTPS (pyicloud) |
| `smtp.gmail.com:587` | Send trigger email | SMTP + STARTTLS |
| `imap.mail.me.com:993` | (Optional) delete trigger emails on iCloud Inbox | IMAP over TLS |

No telemetry, no analytics, no third-party hosts.

## Dependencies

- [`pyicloud`](https://github.com/picklepete/pyicloud) — community-maintained library that talks to Apple's internal iCloud APIs (no official public SDK exists). It has historically broken when Apple changes things. Pin to a known-working version in `uv.lock`.
- [`mcp[cli]`](https://github.com/modelcontextprotocol/python-sdk) — official Anthropic MCP SDK.

Run `uv sync` to install from the locked versions; review `uv.lock` before doing so on a sensitive machine.

## Operational guidance

- Use a dedicated Apple ID + app-specific password for this tool if you can — that way revoking it doesn't disrupt anything else.
- Revoke app-specific passwords at the URLs in the table above the moment a machine is suspected compromised.
- Avoid backing up `~/.config/apple-reminders/` to anywhere that's not encrypted at rest.
- The trigger email path requires opening Gmail SMTP outbound — if you're behind a strict egress firewall, allowlist `smtp.gmail.com:587` only.

## Reporting an issue

Open a [private security advisory](https://github.com/jkan67-de/apple-reminders-mcp-cli/security/advisories/new) on the repo. Don't open a public issue for anything sensitive.
