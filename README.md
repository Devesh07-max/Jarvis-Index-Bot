# JARVIS Index Bot

A multi-user Telegram channel navigation-index bot. The Telegram Bot API is used for menus, previews and publishing; Telethon uses each user's separately authorized Telegram account to read channel history.

## v3 implementation

- The 21-screen user journey is represented in the bot handlers and inline keyboards: account connection/status/disconnection, channel registration/management, both indexing modes, progress, preview, missing-link resolution, regeneration, two-step publishing, jobs, account and help.
- One Telethon account per bot user; unlimited registered channels. Private channels must be accessible to that user's connected account.
- Phone/OTP/2FA login, one-hour login-request throttling, five failed credential attempts maximum, 10-minute pending-login expiry, periodic pending-client cleanup, and cancellation cleanup. Disconnect loads and logs out a persisted session even if the current worker has not cached it.
- Telethon StringSessions are encrypted with Fernet. New session payloads are user-bound inside the encrypted value. Legacy v2 sessions are accepted and upgraded on first successful reconnect. Never change `SESSION_ENCRYPTION_KEY` unless you intend to make existing stored sessions unreadable.
- Mongo-backed FSM state persistence for non-secret workflow data. OTP and 2FA passwords are not written to MongoDB; a restart clears pending auth states because the pending Telethon authorization client is process-local.
- Mongo-backed job store with checkpoints, builder snapshots, restart recovery, per-user/per-channel active-scan uniqueness, and database-side pagination.
- Streaming channel history processing; no full-history media downloads. Caption and document filename metadata are indexed. FloodWait sleep is broken into short intervals so cancellation and premium-expiry checks can run.
- Manual Template Mode preserves template text, Unicode, headings, indentation and tree glyphs; matching independently evaluates caption and filename, checks known topic/material conflicts, supports small-cap Unicode normalization, aliases and conservative fuzzy matching, and keeps the oldest qualifying message.
- Automatic Topic Mode groups subjects/material types using caption and filename metadata. A caption takes precedence unless the filename contradicts its topic; conflicting evidence is held for review. Empty-caption documents can be classified from names such as `simple_interest_class_notes.pdf`. Generic names such as `notes.pdf`, `quiz.pdf` or `syllabus.pdf` can inherit a recent explicit chapter heading only within a short five-message / six-hour context window; otherwise they remain unresolved because they do not identify a subject.
- Material detection distinguishes lectures, digital notes, worksheets, century PDFs, generic PDFs, planners, tests and practice sets. Ambiguous one-word fragments such as `Election`, `Basic`, `Income` and `MP` are not automatically promoted to chapters; they are review samples unless meaningful topic context is present.
- Preview allows explicit publishing of available links even when some rows remain unresolved. The confirmation screen warns how many rows will remain unlinked. Manual and automatic previews share the same publishing workflow.
- Ordinary bot UI messages and inline keyboard labels use the JARVIS small-caps style; supplied manual index templates and generated navigation-index content retain their original layout and link formatting.
- Saved manual resolutions are validated against the selected channel on regeneration. Public-channel links use `t.me/<username>/<message_id>`; private-channel links use `t.me/c/...`.
- Publishing requires a valid preview, no unresolved entries, non-empty index, active premium entitlement, continued user-channel access, bot administrator/post permission, and a signed 10-minute second confirmation.
- Each published chunk is recorded immediately. Explicit Bot API `RetryAfter` responses are safely retried up to five times; an interrupted/ambiguous publish is never retried automatically. The affected channel is placed on a persistent publish hold until the owner manually inspects the channel and clears the hold.
- Premium grant/extension uses an atomic MongoDB update pipeline; expiry is checked on access and during startup. Owner-only commands include `/grant`, `/revoke`, `/premium`, `/premium_list`, `/publish_review`, and `/clear_publish_hold`.
- Database selection is locked to `jarvis_hyperlink_index`; the app refuses any other `MONGO_DB_NAME`.

## Requirements

- Python 3.12 (Render Blueprint pins 3.12.10)
- MongoDB Atlas / MongoDB 5.0+ (premium extension uses `$dateAdd` in an update pipeline)
- Telegram bot token from @BotFather
- `API_ID` and `API_HASH` from https://my.telegram.org

## Environment setup

1. Copy `.env.example` to `.env` locally. Never commit `.env`.
2. Set `BOT_TOKEN`, `API_ID`, `API_HASH`, `MONGO_URI`, and `OWNER_ID`.
3. Generate `SESSION_ENCRYPTION_KEY`:
   ```bash
   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
   ```
4. Generate `APP_SECRET` (at least 32 random bytes):
   ```bash
   python -c "import secrets; print(secrets.token_urlsafe(48))"
   ```
5. Keep `MONGO_DB_NAME=jarvis_hyperlink_index` exactly.

Install and run:

```bash
python -m venv .venv
# Windows: .venv\\Scripts\\activate
# Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
python main.py
```

## MongoDB setup

Create an Atlas cluster and a database user with access only to `jarvis_hyperlink_index` where practical. Configure network access for the Render worker. The application creates indexes in its dedicated database, including TTL indexes for workflow state and previews. It does not select or mutate another bot's database.

## Telegram setup

- Create a bot using @BotFather and set `BOT_TOKEN`.
- Obtain `API_ID` and `API_HASH` from https://my.telegram.org.
- Add the publishing bot to each destination channel as administrator with posting permission.
- Users connect their own Telegram account through the OTP/2FA flow.
- The connected user account must have access to every registered private channel.
- To disconnect, use Connect Account > Disconnect Account. Users can also revoke sessions from Telegram Settings > Devices.

## Render deployment

1. Push this directory to a private Git repository.
2. Create a Render Blueprint from the repository using `render.yaml`, or create a **Background Worker** manually. This is a Telegram long-polling process, not an HTTP Web Service.
3. If an existing Web Service is using this same bot token, stop it before starting the worker. Two active pollers cause Telegram `Conflict: terminated by other getUpdates request` errors.
4. Configure all required environment variables. Keep `MONGO_DB_NAME` exactly `jarvis_hyperlink_index`.
5. Deploy the worker (`python main.py`). The worker does not need an exposed HTTP port. `No open ports detected` on a Web Service means the wrong Render service type was created; changing code or setting a fake port is not the fix.
6. Run only **one worker replica** for a given bot token. Do not scale this polling worker horizontally.

The worker's durable data is in MongoDB, so no local persistent disk is required for sessions/jobs. `app.web:make_app()` exposes simple FastAPI health routes but is not started by the background worker.

## User guide

1. Open the bot and press Start.
2. Connect Telegram Account; provide phone number, OTP, and 2FA password if requested.
3. Register a public username/channel ID or a private channel accessible to the connected account.
4. Add the publishing bot as a channel administrator with post permission.
5. Select Create Index, select a channel, then choose Manual Template Mode or Automatic Topic Mode.
6. Wait for the scan. Review the generated preview and any uncertain captions.
7. Resolve missing entries by sending a Telegram message link or forwarding the original message.
8. Approve & Publish, then confirm the destination within 10 minutes.

## Owner commands

- `/grant USER_ID DAYS` — grant or extend premium.
- `/revoke USER_ID` — revoke premium.
- `/premium USER_ID` — inspect premium status.
- `/premium_list [PAGE]` — list active premium users, 30 per page.
- `/publish_review` — inspect jobs whose Bot API publish outcome is ambiguous.
- `/clear_publish_hold USER_ID CHANNEL_ID` — clear a channel's publishing hold **only after manually inspecting/removing partial posts**.

## Tests

Run:

```bash
python -m compileall -q .
pytest -q
```

The v3 local suite contains 47 passing tests. It covers core index matching/grouping, Hindi normalization, filename metadata, Century PDF vs generic PDF, saved resolution regeneration, Unicode rendering, session encryption/user binding, job state transitions/checkpoints, atomic publishing transitions, publish confirmation signatures, chunk persistence/recovery and configuration guards. Tests use fakes/mocks where external services are involved.

## Verification limits and safety notes

- Live Telegram OTP/2FA, actual private-channel access, Telegram FloodWait behavior, real Bot API publishing, MongoDB Atlas concurrency/restart recovery and Render deployment were **not** exercised in the build environment.
- Runtime dependencies (`aiogram`, `telethon`, and `motor`) were unavailable in this build environment, so full application import/startup was not verified. Python syntax compilation and the dependency-light unit suite passed.
- Automatic classification is a deterministic keyword heuristic, not an ML classifier. Multilingual terms are improved, but abbreviations and unusual captions still need review.
- A process crash during `sendMessage` can have an unknown delivery outcome because Telegram Bot API has no idempotency key. The bot records chunks immediately and blocks automatic retry; an owner must inspect the channel and clear the persistent hold after manual review.
- The polling worker is intended to run as one replica. A separate distributed worker/queue service would be required for horizontal scaling.
- OTP/2FA pending clients are in memory. After a Render restart, the user must restart Telegram account connection; durable FSM state does not persist authentication secrets.
- Telegram forwards that hide their origin cannot be verified and are rejected.
