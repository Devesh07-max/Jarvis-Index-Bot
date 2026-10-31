# JARVIS Index Bot — Specification Compliance Matrix (v3)

Legend:
- **Implemented** — corresponding source handlers/logic are present.
- **Implemented; live verification pending** — source implementation is present, but a real Telegram/MongoDB/Render interaction has not been exercised here.
- **Partial / limitation** — source behavior exists but does not fully prove the original acceptance condition.

## 21-screen workflow

| Screen | Required workflow | v3 status |
|---|---|---|
| 1 | Main menu and exact button rows | Implemented |
| 2 | Telegram account connection, OTP, 2FA, cancellation | Implemented; live verification pending |
| 3 | Account status, refresh, disconnect, back | Implemented; live verification pending |
| 4 | Disconnect confirmation | Implemented |
| 5 | User's registered channels, pagination | Implemented; live MongoDB verification pending |
| 6 | Add public/private channel and validate access | Implemented; live verification pending |
| 7 | Manage/remove registered channel | Implemented |
| 8 | Select channel for indexing | Implemented |
| 9 | Manual/Automatic mode selection | Implemented |
| 10 | Receive and parse manual template | Implemented; live scan verification pending |
| 11 | Explicit Start Scanning for automatic mode | Implemented |
| 12 | Progress, counts, cancellation | Implemented; live scan verification pending |
| 13 | Scan result summary | Implemented |
| 14 | Full preview and publish approval action | Implemented |
| 15 | Missing-link resolution by link or forward | Implemented; live Telegram-origin verification pending; jobs show `Waiting for Resolution` until all links are resolved |
| 16 | Regenerate with saved resolutions | Implemented; live scan verification pending |
| 17 | Second publish confirmation | Implemented with signed, expiring token |
| 18 | Publish result and failure message | Implemented; live Bot API verification pending |
| 19 | User-owned indexing jobs and pagination | Implemented; live MongoDB verification pending |
| 20 | Account and entitlement summary | Implemented; live session verification pending |
| 21 | Help and navigation shortcuts | Implemented |

## Acceptance checklist

| Requirement | v3 status / evidence |
|---|---|
| Final name JARVIS Index Bot | Implemented in welcome text, README and service metadata |
| Main menu layout | Implemented in `app/keyboards.py` |
| Back and Cancel behavior | Implemented through Back callbacks, `/cancel`, and Cancel buttons; scan cancellation is checkpoint-based |
| Every advertised button has a handler | Implemented in bot callback/message handlers; not click-tested in a live bot |
| Telegram account connection | Implemented through Telethon |
| OTP and 2FA | Implemented; credentials are not persisted |
| One account per user | Enforced by unique `telegram_sessions.user_id` and login checks |
| Encrypted sessions | Fernet encryption with user-bound payload for new sessions; legacy v2 sessions upgrade on reconnect |
| Unlimited registered channels | No application-level channel cap |
| Private accessible channels | Implemented; requires user's Telethon account access |
| Manual Template Mode | Implemented |
| Automatic Topic Mode | Implemented as a deterministic keyword heuristic |
| Related lectures grouped | Implemented by subject/material buckets |
| Oldest suitable message | Implemented; streaming history is requested oldest-first and the first qualifying message is retained |
| Unicode formatting preservation | Implemented for template text, line layout and Devanagari normalization |
| Missing-link resolution | Implemented for message links and verifiable forwarded messages |
| Wrong-channel/deleted-message rejection | Implemented by resolving the entity and fetching the original message |
| Saved resolution persistence | Implemented in `manual_resolutions`; validated on regeneration |
| Preview required | Implemented; publish requires `Preview Ready` and an unexpired preview |
| Two-step publishing | Implemented with a 10-minute signed confirmation token |
| Bot administrator/post permission | Implemented before publish; live permission behavior pending |
| Publishing through Bot API | Implemented via `bot.send_message`, not Telethon |
| Multi-user isolation | User-scoped queries, callback ownership checks and per-user FSM/session keys |
| Premium expiry | Implemented; checked on access, scan checkpoints and startup |
| Owner premium grant/extend/revoke/list | Implemented; premium list paginated |
| Large channel histories | Streaming, bounded uncertain-caption sample, builder snapshots and checkpoints |
| FloodWait handling | Implemented with short sleep intervals and progress/cancel checks; live behavior pending |
| Duplicate scan prevention | Unique sparse `active_key` index for user+channel active scans |
| Duplicate publishing prevention | Atomic status transition, signed one-time approval, per-chunk persistence and ambiguous-outcome hold |
| Render restart persistence | MongoDB-backed jobs/sessions/workflow state; real Render restart pending |
| Database isolation | Hard-coded database allow-list; rejects any database name other than `jarvis_hyperlink_index` |
| Secrets excluded | No real credentials included; `.env` and secrets excluded by `.gitignore`/`.dockerignore` |
| Automated tests | 47 local tests pass; external-service behavior is mocked or untested |
| Render deployment | Blueprint and Dockerfile present; actual deployment pending |

## Explicit partial limitations

1. **Automatic classification:** multilingual terms and document filenames are supported, but classification remains keyword-based. Unusual abbreviations, local naming conventions and ambiguous subjects can remain in the review sample.
2. **Live integration:** OTP, 2FA, Telethon session revocation, private channel access, Telegram rate limits, Bot API posting, MongoDB Atlas and Render restarts were not exercised with real accounts/services.
3. **Pending auth after restart:** OTP/2FA clients are process-local by design. FSM state is durable, but authentication states are reset after restart so the user must restart connection.
4. **Publishing ambiguity:** Bot API `sendMessage` has no idempotency key. A request whose response is lost may have been delivered. v3 blocks retry, records known chunks, and creates a persistent channel hold. Owner review is required.
5. **Worker topology:** run one Render polling worker replica per bot token. Multi-replica polling/job coordination is not implemented.
6. **Test environment:** runtime dependency installation could not be completed in this environment. `aiogram`, `telethon`, and `motor` are pinned in requirements but full app import/startup could not be run locally.
