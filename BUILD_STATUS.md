# JARVIS Index Bot v3 — Build and Verification Report

## Build outcome

v3 addresses the Critical, High and Medium findings from the v2 source audit while retaining the existing aiogram + Telethon + Motor/MongoDB architecture and the existing bot workflow.

## Audit findings addressed

### Critical

- **Automatic saved resolutions:** saved overrides are now applied to automatic entries and the automatic renderer consumes the resolved entry list rather than stale group IDs.
- **Crash-safe publishing:** each chunk has an in-flight marker and is persisted immediately after Bot API success. Startup converts interrupted `Publishing` jobs to `Publish Review Required`; no automatic retry is attempted.
- **Premium enforcement:** premium is checked on account connection, channel registration, mode selection, template submission, scan creation/execution/checkpoints, preview, resolution, regeneration, approval and publishing. Owner bypass remains.
- **Century PDF classification:** the specific Century PDF phrase is distinct from generic PDF; generic PDFs have their own bucket.

### High

- Per-user Telethon locks prevent concurrent duplicate client creation.
- Pending login clients have a 10-minute expiry, periodic cleanup, cancellation cleanup, persistent OTP-request throttling, and a failed-code/password attempt limit. Disconnect also loads and logs out persisted sessions that were not cached by the current worker.
- OTP/2FA inputs are not stored in FSM data; messages are deleted where Telegram permits, with a warning if deletion fails.
- Mongo-backed FSM storage persists non-secret workflow state; pending OTP/2FA states are reset after restart.
- Preview and publishing callbacks validate user ownership, job state, channel match, expiry, premium entitlement and signed second confirmation.
- Publishing is restricted to `Preview Ready`; empty or unresolved previews are rejected.
- Persistent channel-level publish holds prevent a new index from being published while an earlier partial publish requires owner review. Owner commands expose review and manual hold clearing.
- Jobs with unresolved template entries use the `Waiting for Resolution` status and transition to `Preview Ready` only after the final resolution. JobStore is now used by the bot for enqueue, claim, checkpoint, cancel and finish transitions. A unique sparse `active_key` index prevents simultaneous scans for the same user's same channel.

### Medium

- Channel history scans captions plus document filenames without downloading media; MIME metadata is retained but not used as subject text.
- Unicode normalization preserves combining marks, including Devanagari. Common Hindi material terms were added.
- Manual matching uses precision-weighted multi-token matching; a generic one-word overlap does not qualify. Oldest-first selection is preserved.
- Channel/job/premium lists use database-side pagination.
- APP_SECRET is now used to sign short-lived, user/job-bound publishing confirmations and must be at least 32 bytes.
- Premium extension uses an atomic MongoDB update pipeline; expired subscriptions are excluded and deactivated on access/startup and by a periodic expiry task.
- Public channel links use public usernames; private channels use `t.me/c` links.
- Saved resolutions are marked invalid when the target is no longer accessible; successful resolutions are retained for later regenerations.

## Automated verification

- `python -m compileall -q .`: **passed**.
- `pytest -q`: **47 passed**.
- ZIP archive integrity: **passed** (`unzip -t` reported no errors).
- No real credentials are included.

Tests include existing v2 coverage plus v3 regression cases for Hindi normalization, generic-vs-Century PDF, filename metadata, false-positive manual matching, oldest selection, saved automatic overrides, public links, user-bound session encryption, user-scoped job claims, checkpoint snapshots, atomic premium grant, expired entitlement checks, signed confirmation binding, one-time publish claims, immediate chunk persistence, interrupted-publish recovery and empty publish rejection.

## Full specification status

- All 21 named user-screen flows have corresponding handlers/keyboards in the application source. Their actual Telegram interaction has not been live end-to-end tested.
- Manual and Automatic indexing, missing-link resolution, preview, two-step publishing, owner premium commands, user-scoped records, pagination and Render worker configuration are implemented in source.
- Acceptance items that depend on Telegram/MongoDB/Render service behavior remain unverified until deployment with real credentials.

See `SPEC_COMPLIANCE.md` for the screen-by-screen and acceptance checklist.

## Not verified in this environment

- Runtime dependency installation could not be completed in this environment; `aiogram`, `telethon`, and `motor` remain unavailable locally.
- Full `main.py` import/startup and aiogram handler registration.
- Real Telegram OTP/2FA, private-channel history, forwarded-message metadata, FloodWait and session revocation.
- Actual MongoDB Atlas indexes, atomic pipeline behavior, TTL behavior, concurrent user load and real Render restart recovery.
- Real Bot API admin permission checks and single-/multi-chunk publishing.
- Render Blueprint deployment and runtime health.

## Known limitations

- Automatic topic extraction remains a deterministic keyword heuristic. It is not a semantic ML classifier; abbreviations and unusual naming conventions may remain uncertain.
- Pending Telegram authorization clients are process-local. After a Render restart, users must restart the login flow; OTP and 2FA secrets are intentionally not persisted.
- A Bot API request can be delivered while its response is lost. Such a job is held for owner review and must not be retried automatically. Use `/publish_review` and only use `/clear_publish_hold USER_ID CHANNEL_ID` after manually inspecting the destination channel.
- Run one Render worker replica per bot token. Horizontal scaling needs a separate distributed worker/queue and coordinated Telegram polling.
- Telegram forwards with hidden origin are rejected because their source cannot be verified.

## Deployment safety

- Keep `MONGO_DB_NAME=jarvis_hyperlink_index`; the application refuses any other database name.
- Restrict the MongoDB database user to this database where possible.
- Keep `SESSION_ENCRYPTION_KEY` stable and backed up.
- Keep `APP_SECRET` stable to preserve current short-lived approval signatures; changing it invalidates outstanding approvals.
- Never commit `.env`, Telegram credentials, bot tokens, or session strings.
- Test first with a dedicated bot and channel.
