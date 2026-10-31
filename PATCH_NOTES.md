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
