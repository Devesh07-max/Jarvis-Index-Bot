# JARVIS Index Bot — reliability and indexing update

## Main changes
- Fixed Mongo collection attribute access used by bot handlers.
- Fixed Mongo FSM serialization for Aiogram `State` objects.
- Improved Unicode small-cap decoding, aliases, conservative fuzzy matching, and independent caption/filename scoring.
- Added material/topic conflict checks to prevent Simple Interest / Compound Interest and lecture / notes cross-links.
- Uses filename metadata for empty-caption documents.
- Generic filenames can inherit only a recent explicit chapter context (five messages / six hours); otherwise they remain unresolved for review.
- Ambiguous one-word fragments are held for review instead of being invented as chapters.
- Manual and automatic previews expose the same review/publish path, including explicitly confirmed partial publishing.
- Added `/create`, `/auto`, `/manual`, `/channels`, `/jobs`, and `/account` shortcuts.
- Styled ordinary UI messages and inline buttons while preserving supplied templates and generated index layouts.
- Kept the existing database name, encrypted-session design, job model, publishing safeguards, and feature set.

## Render deployment
This project is configured as a **Background Worker** in `render.yaml`.
Stop the old Web Service before starting the worker with the same bot token. A polling bot does not bind an HTTP port; `No open ports detected` on a Web Service is a service-type mismatch, not a port setting to patch.
Use exactly one active worker replica per bot token.

## Search limitations by selected preference
Document extraction is metadata-only as requested. The bot reads captions, Telegram document filenames, MIME metadata, and nearby explicit chapter context. It does not download PDFs or OCR their contents. A generic file named only `document.pdf` with no useful nearby topic remains unresolved rather than receiving a guessed link.

## Verification
`python -m compileall -q .`
`python -m pytest -q` — 64 tests passed in the patch workspace.

Live Telegram, MongoDB Atlas, and Render behavior still needs deployment-side verification.

## Source-to-Target mode update
- Added per-user saved channel-pair records in the dedicated `jarvis_hyperlink_index` database.
- Added paginated Saved Pairs UI, Add/Update/Delete pair actions, and one-tap Create Hyperlinks.
- Added Telethon message range retrieval and source-index hyperlink extraction.
- Added ordered source-to-target mapping with URL/attribution normalization and preview warnings.
- Reuses existing preview, approval, and safe Bot API publishing flow.
- Added per-entry verified target-link correction and persisted preview edits.
- Added focused tests for Telegram message-link parsing, caption normalization, ordered mapping, and destination rewriting.

## Follow-up reliability patch

- Fixed the indexing launch flow to acknowledge the callback before starting the background scan, show an immediate running state, and report actionable failure details instead of silently appearing idle.
- Added live scan-stage updates (Telegram connection, message reading with scanned counts, matching, preview rendering, completion) and progress checkpoints every 25 messages.
- Added an owner-only Admin Panel at `/admin` with inline Grant Premium, Revoke Premium, Premium Users, and Publishing Review actions. Grant accepts `USER_ID DAYS`; revoke accepts `USER_ID`.
- Source-to-Target pair setup now resolves and automatically registers both channels from the IDs/usernames entered in the 9-line form. The target channel must already have this bot as an administrator with posting permission.
- Source-to-Target generation now reports its current stage while reading source/target ranges, reading the index, matching files, and building the preview.
- Improved Telethon channel resolution for raw numeric channel IDs by falling back to the connected account's dialogs when the entity is not yet cached.

## Source-to-Target existing-index workflow (2026-11-16)

- Source channel access is performed with the user's connected Telethon account; the bot does not need source-channel admin rights.
- The saved pair now requires the existing target index message link. Source and target index text must match before the pair is saved, preventing accidental edits to the wrong message.
- The bot follows each Telegram hyperlink in the source index and fetches that exact source message through the connected account, then matches only those linked source files against the selected target file range using normalized captions and filenames.
- Target-range scanning and per-link source fetching report progress in the bot chat.
- Source index formatting is retained while mapped TextUrl entities are rewritten to target message URLs. Unmatched source links are stripped so they cannot accidentally continue pointing to the source channel.
- Publishing Source-to-Target mode edits the already-existing target index message through the connected Telegram account; it does not post a duplicate index. The account must own that target index message or have the channel's Edit Messages admin right.
- The bot itself does not need to be a target-channel admin for this edit workflow.
