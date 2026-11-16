import asyncio, logging, uuid, html, secrets, re, os
from datetime import datetime, timezone, timedelta
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery, BotCommand
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramRetryAfter
from .config import Settings
from .database import Database
from .security import SessionCipher
from .telegram_client import UserTelegram
from .premium import Premium
from . import keyboards as K
from .indexer import manual_index, automatic_index, render_index, render_manual_template, IndexEntry, StreamingIndexBuilder
from .html_chunks import split_html
from .pagination import page_slice
from .fsm_storage import MongoFSMStorage
from .indexer import render_automatic_index, small_caps
from .jobs import JobStore
from .publishing import approval_signature, confirmation_matches, claim_publish, mark_chunk_inflight, clear_chunk_inflight, record_chunk, commit_published, mark_publish_failure, recover_interrupted_publishes
from .web import start_http_server, stop_http_server
from .source_target import parse_tme_message_url, source_links, match_messages, render_linked_index, normalize_index_text

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log=logging.getLogger("jarvis")

class Flow(StatesGroup):
    phone=State(); otp=State(); twofa=State(); channel=State(); template=State(); resolve_link=State(); resolve_forward=State(); grant=State(); source_pair=State(); source_edit=State()


class DesignTextMiddleware:
    """Apply the bot's small-caps visual style to ordinary UI messages.

    User-supplied index templates and generated navigation indexes are left
    untouched so their original formatting and clickable links are preserved.
    """
    _TOKEN = re.compile(r"(<[^>]*>|&(?:#[0-9]+|#x[0-9a-fA-F]+|[a-zA-Z]+);)")

    async def __call__(self, make_request, bot, method):
        if method.__class__.__name__ in {"SendMessage", "EditMessageText"}:
            value = getattr(method, "text", None)
            if value and "→" not in value and "📋" not in value and "<code>" not in value:
                # Keep literal slash commands copyable in the help screen.
                if not any(command in value for command in ("/start", "/scan", "/auto", "/manual", "/cancel")):
                    pieces = self._TOKEN.split(value)
                    method.text = "".join(piece if (piece.startswith("<") or piece.startswith("&")) else small_caps(piece) for piece in pieces)
        return await make_request(bot, method)


async def run():
    # Bind Render's required HTTP port before database/Telegram startup so the
    # Web Service port scanner can detect it while the bot initializes.
    render_port = int(os.environ.get('PORT', '8000'))
    http_server, http_task = await start_http_server(render_port)
    cfg=Settings.from_env(); db=Database(cfg.mongo_uri,cfg.mongo_db_name); await db.initialize()
    bot=Bot(cfg.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    bot.session.middleware.register(DesignTextMiddleware())
    dp=Dispatcher(storage=MongoFSMStorage(db.db.fsm_states))
    cipher=SessionCipher(cfg.session_encryption_key); tg=UserTelegram(cfg.api_id,cfg.api_hash,cipher,db); premium=Premium(db); job_store=JobStore(db.indexing_jobs)
    bot_me=await bot.get_me(); bot_username=bot_me.username
    await bot.set_my_commands([
        BotCommand(command="start", description="Open the JARVIS dashboard"),
        BotCommand(command="help", description="How to use the bot"),
        BotCommand(command="scan", description="Start channel indexing"),
        BotCommand(command="create", description="Create a new navigation index"),
        BotCommand(command="auto", description="Start automatic indexing"),
        BotCommand(command="manual", description="Start manual template indexing"),
        BotCommand(command="channels", description="Manage registered channels"),
        BotCommand(command="jobs", description="View indexing jobs"),
        BotCommand(command="account", description="View your account"),
        BotCommand(command="preview", description="Open your latest index preview"),
        BotCommand(command="publish", description="Review your latest index for publishing"),
        BotCommand(command="history", description="View previous indexing jobs"),
        BotCommand(command="status", description="Check bot and account status"),
        BotCommand(command="stats", description="View indexing statistics"),
        BotCommand(command="settings", description="Open account settings"),
        BotCommand(command="cancel", description="Cancel the current operation"),
    ])

    async def pending_login_cleanup_loop():
        while True:
            await asyncio.sleep(60)
            try:
                await tg.cleanup_expired_pending()
                await premium.expire_due()
            except Exception as exc:
                log.warning("Periodic cleanup failed: %s",type(exc).__name__)
    auth_cleanup_task=asyncio.create_task(pending_login_cleanup_loop())
    background_tasks=set()
    def spawn_background(coro):
        task=asyncio.create_task(coro)
        background_tasks.add(task)
        def completed(done):
            background_tasks.discard(done)
            if not done.cancelled():
                error=done.exception()
                if error: log.error("Background task failed: %s",type(error).__name__)
        task.add_done_callback(completed)
        return task
    def expired(value, now=None):
        if not value: return False
        if getattr(value,"tzinfo",None) is None: value=value.replace(tzinfo=timezone.utc)
        return value <= (now or datetime.now(timezone.utc))
    def preview_expired(preview, now=None):
        deadline=preview.get("expires_at")
        if not deadline:
            created=preview.get("created_at")
            if not created: return True
            if getattr(created,"tzinfo",None) is None: created=created.replace(tzinfo=timezone.utc)
            deadline=created+timedelta(days=7)
        return expired(deadline,now)

    async def require_premium(user_id):
        return user_id==cfg.owner_id or await premium.active(user_id)
    async def channels_for(uid, page=0):
        total=await db.registered_channels.count_documents({"user_id":uid})
        pages=max(1,(total+7)//8); page=max(0,min(int(page),pages-1))
        rows=await db.registered_channels.find({"user_id":uid}).sort("title",1).skip(page*8).limit(8).to_list(8)
        return rows,page,pages,total
    async def owned_job(uid, job_id): return await db.indexing_jobs.find_one({"user_id":uid,"job_id":job_id})
    async def invalidate_approvals(uid):
        await db.indexing_jobs.update_many({"user_id":uid,"approval_token":{"$exists":True}}, {"$unset":{"approval_token":"","approval_expires_at":""}})
    async def safe_delete_sensitive(message):
        try:
            await message.delete(); return True
        except Exception:
            return False
    def redact_error(exc):
        value=str(exc)[:300]
        for secret in (cfg.bot_token,cfg.api_hash,cfg.mongo_uri,cfg.session_encryption_key,cfg.app_secret):
            if secret: value=value.replace(secret,"[REDACTED]")
        return html.escape(value or type(exc).__name__)
    async def home_message(target, text=None):
        if text is None:
            text = (f"<b>{small_caps('JARVIS INDEX BOT')}</b>\n"
                    f"{small_caps('Your professional Telegram indexing assistant')}\n\n"
                    f"{small_caps('Create accurate navigation indexes with smart search, review and publishing.')}")
        await target.answer(text, reply_markup=K.main())

    @dp.message(CommandStart())
    async def start(m:Message, state:FSMContext):
        await tg.cancel_login(m.from_user.id); await invalidate_approvals(m.from_user.id)
        await state.clear(); await db.users.update_one({"user_id":m.from_user.id},{"$set":{"user_id":m.from_user.id,"username":m.from_user.username,"last_seen":datetime.now(timezone.utc)}},upsert=True)
        await home_message(m)

    @dp.message(Command("help"))
    async def help_command(m:Message):
        await m.answer(
            "<b>JARVIS Index Bot — Help</b>\\n\\n"
            "/scan — choose a channel and indexing mode\\n"
            "/auto — open automatic indexing\\n"
            "/manual — open manual template indexing\\n"
            "/preview — open your latest preview\\n"
            "/publish — review your latest preview\\n"
            "/history — previous indexing jobs\\n"
            "/status — connection and latest job status\\n"
            "/stats — scan statistics\\n"
            "/settings — account controls\\n"
            "/cancel — cancel the current flow",
            reply_markup=K.main())

    @dp.message(Command("scan"))
    @dp.message(Command("create"))
    @dp.message(Command("auto"))
    @dp.message(Command("manual"))
    async def scan_command(m:Message, state:FSMContext):
        if not await require_premium(m.from_user.id):
            return await m.answer("Premium access required.", reply_markup=K.main())
        rows,_,_,_=await channels_for(m.from_user.id)
        if not rows:
            return await m.answer("Connect your Telegram account and register a channel first.", reply_markup=K.main())
        command=(m.text or "").split()[0].split("@",1)[0].lower()
        preferred = "automatic" if command=="/auto" else "manual" if command=="/manual" else None
        await state.clear()
        await state.update_data(preferred_mode=preferred)
        await show_channel_picker(m,m.from_user.id,0,"choose")

    @dp.message(Command("preview"))
    @dp.message(Command("publish"))
    async def latest_preview_command(m:Message):
        p=await db.previews.find_one({"user_id":m.from_user.id},{"_id":0},sort=[("created_at",-1)])
        if not p:
            return await m.answer("No saved preview found. Start with /scan.",reply_markup=K.main())
        job_id=p.get("job_id")
        if not job_id or preview_expired(p):
            return await m.answer("Your latest preview has expired. Please regenerate it.",reply_markup=K.main())
        await m.answer("Open your latest index preview. Review, resolve missing links, and publish from there.",
            reply_markup=K.kb([[("👁 Open Latest Preview",f"preview:{job_id}")],[("🏠 Main Menu","home")]]))

    @dp.message(Command("channels"))
    async def channels_command(m:Message):
        rows,page,pages,total=await channels_for(m.from_user.id,0)
        if not rows:
            return await m.answer("<b>📡 My Channels</b>\nNo channels registered yet.",reply_markup=K.main())
        buttons=[[(f"📚 {row['title'][:48]}",f"manage:{row['channel_id']}")] for row in rows]
        buttons.append([("➕ Add Channel","add_channel")])
        buttons.append([("🏠 Main Menu","home")])
        await m.answer(f"<b>{small_caps('My Registered Channels')}</b> · {total}",reply_markup=K.kb(buttons))

    @dp.message(Command("jobs"))
    @dp.message(Command("history"))
    async def jobs_command(m:Message):
        rows,_,pages,total=await db.user_page("indexing_jobs",m.from_user.id,0,8,"created_at",True)
        if not rows:
            return await m.answer("No indexing jobs yet. Start with /scan.",reply_markup=K.main())
        buttons=[[(f"{row.get('channel_title','Channel')[:25]} · {row.get('status','Unknown')}",f"job:{row['job_id']}")] for row in rows]
        buttons.append([("🏠 Main Menu","home")])
        await m.answer(f"<b>{small_caps('Your Indexing Jobs')}</b> · {total} total · Page 1/{pages}",reply_markup=K.kb(buttons))

    @dp.message(Command("account"))
    async def account_command(m:Message):
        uid=m.from_user.id
        session=await db.telegram_sessions.find_one({"user_id":uid})
        channel_count=await db.registered_channels.count_documents({"user_id":uid})
        job_count=await db.indexing_jobs.count_documents({"user_id":uid})
        await m.answer(f"<b>{small_caps('My Account')}</b>\nTelegram account: {'Connected' if session else 'Not connected'}\nRegistered channels: {channel_count}\nIndexing jobs: {job_count}",reply_markup=K.main())

    @dp.message(Command("status"))
    async def status_command(m:Message):
        connected=bool(await db.telegram_sessions.find_one({"user_id":m.from_user.id}))
        latest=await db.indexing_jobs.find_one({"user_id":m.from_user.id},sort=[("created_at",-1)])
        channels=await db.registered_channels.count_documents({"user_id":m.from_user.id})
        await m.answer(
            f"<b>JARVIS Status</b>\\nTelegram account: {'Connected' if connected else 'Not connected'}"
            f"\\nRegistered channels: {channels}"
            f"\\nLatest job: {html.escape(latest.get('status','None') if latest else 'None')}",
            reply_markup=K.main())

    @dp.message(Command("stats"))
    async def stats_command(m:Message):
        jobs=await db.indexing_jobs.count_documents({"user_id":m.from_user.id})
        completed=await db.indexing_jobs.count_documents({"user_id":m.from_user.id,"status":"Published"})
        scanned=0
        async for row in db.indexing_jobs.find({"user_id":m.from_user.id},{"messages_scanned":1}):
            scanned += int(row.get("messages_scanned",0) or 0)
        await m.answer(f"<b>Your JARVIS Statistics</b>\\nIndexing jobs: {jobs}\\nPublished indexes: {completed}\\nMessages scanned: {scanned:,}",
            reply_markup=K.main())

    @dp.message(Command("settings"))
    async def settings_command(m:Message):
        await m.answer("<b>Account Settings</b>\\nManage your connected Telegram account and registered channels below.",
            reply_markup=K.kb([[("🔐 Telegram Account","connect")],[("📡 Manage Channels","manage")],[("🏠 Main Menu","home")]]))

    @dp.message(Command("admin"))
    async def admin(m:Message):
        if m.from_user.id!=cfg.owner_id: return await m.answer("Not authorized.")
        await m.answer(
            "<b>JARVIS Admin Panel</b>\nManage premium access and publishing safety.",
            reply_markup=K.kb([
                [("➕ Grant Premium","admin_grant"),("➖ Revoke Premium","admin_revoke")],
                [("📋 Premium Users","admin_premium_list")],
                [("🛡 Publishing Review","admin_publish_review")],
                [("🏠 Main Menu","home")],
            ]))

    @dp.callback_query(F.data=="admin_grant")
    async def admin_grant_start(c:CallbackQuery,state:FSMContext):
        if c.from_user.id!=cfg.owner_id: return await c.answer("Not authorized.",show_alert=True)
        await state.clear(); await state.set_state(Flow.grant)
        await c.message.answer("Send the user's Telegram ID and premium duration in days, separated by a space.\nExample: <code>123456789 30</code>")
        await c.answer()

    @dp.message(Flow.grant)
    async def admin_grant_receive(m:Message,state:FSMContext):
        if m.from_user.id!=cfg.owner_id: await state.clear(); return await m.answer("Not authorized.")
        try:
            parts=(m.text or "").split()
            action=(await state.get_data()).get("admin_action","grant")
            if len(parts)!=(1 if action=="revoke" else 2): raise ValueError()
            uid=int(parts[0])
            if uid<=0: raise ValueError()
            if action=="revoke":
                if len(parts)!=1: raise ValueError()
                await premium.revoke(uid)
                await state.clear()
                return await m.answer(f"✅ Premium revoked for <code>{uid}</code>",
                    reply_markup=K.kb([[("➕ Grant Premium","admin_grant")],[("🏠 Admin Panel","admin_home")]]))
            if len(parts)!=2: raise ValueError()
            days=int(parts[1])
            if days<1 or days>3650: raise ValueError()
            expiry=await premium.grant(uid,days)
            await state.clear()
            await m.answer(f"✅ Premium activated for <code>{uid}</code>\nDuration: {days} days\nExpires: <code>{expiry.isoformat()}</code>",
                reply_markup=K.kb([[("➕ Grant Another","admin_grant")],[("📋 Premium Users","admin_premium_list")],[("🏠 Admin Panel","admin_home")]]))
        except Exception:
            await m.answer("Invalid details. Send exactly: <code>USER_ID DAYS</code> (e.g. <code>123456789 30</code>).")

    @dp.callback_query(F.data=="admin_home")
    async def admin_home(c:CallbackQuery):
        if c.from_user.id!=cfg.owner_id: return await c.answer("Not authorized.",show_alert=True)
        await c.message.answer("<b>JARVIS Admin Panel</b>",reply_markup=K.kb([
            [("➕ Grant Premium","admin_grant"),("➖ Revoke Premium","admin_revoke")],
            [("📋 Premium Users","admin_premium_list")],
            [("🛡 Publishing Review","admin_publish_review")],
        ])); await c.answer()

    @dp.callback_query(F.data=="admin_revoke")
    async def admin_revoke_start(c:CallbackQuery,state:FSMContext):
        if c.from_user.id!=cfg.owner_id: return await c.answer("Not authorized.",show_alert=True)
        await state.clear(); await state.set_state(Flow.grant)
        await state.update_data(admin_action="revoke")
        await c.message.answer("Send the Telegram user ID whose premium access should be revoked.")
        await c.answer()

    @dp.callback_query(F.data=="admin_premium_list")
    async def admin_premium_list_button(c:CallbackQuery):
        if c.from_user.id!=cfg.owner_id: return await c.answer("Not authorized.",show_alert=True)
        await show_premium_page(c.message,0); await c.answer()

    @dp.callback_query(F.data=="admin_publish_review")
    async def admin_publish_review_button(c:CallbackQuery):
        if c.from_user.id!=cfg.owner_id: return await c.answer("Not authorized.",show_alert=True)
        rows=await db.indexing_jobs.find({"$or":[{"status":"Publish Review Required"},{"partial_message_ids":{"$exists":True,"$ne":[]}}]}).sort("updated_at",-1).limit(50).to_list(50)
        if not rows: return await c.answer("No publishing reviews are pending.",show_alert=True)
        lines=["<b>Publishing reviews required</b>"]
        for row in rows:
            ids=row.get("published_message_ids") or row.get("partial_message_ids") or [x.get("message_id") for x in row.get("publish_chunks",[]) if x.get("message_id")]
            lines.append(f"User <code>{row['user_id']}</code> · {html.escape(row.get('channel_title',''))} · Job <code>{row['job_id']}</code> · IDs <code>{ids}</code>")
        await c.message.answer("\\n".join(lines)[:3900]); await c.answer()
    @dp.message(Command("grant"))
    async def grant(m:Message):
        if m.from_user.id!=cfg.owner_id: return
        try: _,uid,days=m.text.split(); expiry=await premium.grant(int(uid),int(days)); await m.answer(f"Premium granted to <code>{uid}</code> until {expiry.isoformat()}.")
        except Exception: await m.answer("Usage: /grant USER_ID DAYS")
    @dp.message(Command("revoke"))
    async def revoke(m:Message):
        if m.from_user.id!=cfg.owner_id: return
        try: uid=int(m.text.split()[1]); await premium.revoke(uid); await m.answer(f"Premium revoked for {uid}.")
        except Exception: await m.answer("Usage: /revoke USER_ID")
    @dp.message(Command("publish_review"))
    async def publish_review(m:Message):
        if m.from_user.id!=cfg.owner_id: return
        rows=await db.indexing_jobs.find({"$or":[{"status":"Publish Review Required"},{"partial_message_ids":{"$exists":True,"$ne":[]}}]}).sort("updated_at",-1).limit(50).to_list(50)
        if not rows: return await m.answer("No publishing reviews are pending.")
        lines=["<b>Publishing reviews required</b>"]
        for row in rows:
            ids=row.get("published_message_ids") or row.get("partial_message_ids") or [chunk.get("message_id") for chunk in row.get("publish_chunks",[]) if chunk.get("message_id")]
            inflight=row.get("publish_inflight_index")
            lines.append(f"User <code>{row['user_id']}</code> · {html.escape(row.get('channel_title',''))} · Channel <code>{row.get('channel_id')}</code> · Job <code>{row['job_id']}</code> · Message IDs: <code>{ids}</code> · Unknown chunk: <code>{inflight if inflight is not None else 'none'}</code>")
        await m.answer("\n".join(lines)[:3900])

    @dp.message(Command("clear_publish_hold"))
    async def clear_publish_hold(m:Message):
        if m.from_user.id!=cfg.owner_id: return
        try:
            _,uid,cid=m.text.split(); uid=int(uid); cid=int(cid)
            result=await db.publish_holds.delete_one({"user_id":uid,"channel_id":cid})
            await db.registered_channels.update_one({"user_id":uid,"channel_id":cid},{"$unset":{"publish_hold":""}})
            await m.answer("Publishing hold cleared. Only do this after manually inspecting/removing any partial index posts." if result.deleted_count else "No publishing hold was found.")
        except Exception:
            await m.answer("Usage: /clear_publish_hold USER_ID CHANNEL_ID")

    async def show_premium_page(target, page=0):
        rows,page,pages,total=await premium.active_page(page,30)
        if not rows: return await target.answer("No active premium subscriptions.")
        text=f"<b>Active premium users</b> · Page {page+1}/{pages} · {total} total\n"+"\n".join(f"<code>{r['user_id']}</code> — {r.get('expires_at')}" for r in rows)
        buttons=[]
        nav=[]
        if page>0: nav.append(("⬅️ Previous",f"premium_page:{page-1}"))
        if page+1<pages: nav.append(("Next ➡️",f"premium_page:{page+1}"))
        if nav: buttons.append(nav)
        if buttons: return await target.answer(text[:3900],reply_markup=K.kb(buttons))
        return await target.answer(text[:3900])

    @dp.message(Command("premium_list"))
    async def premium_list(m:Message):
        if m.from_user.id!=cfg.owner_id: return
        try: page=max(0,int((m.text or "").split()[1])-1) if len((m.text or "").split())>1 else 0
        except ValueError: page=0
        await show_premium_page(m,page)

    @dp.callback_query(F.data.startswith("premium_page:"))
    async def premium_page(c:CallbackQuery):
        if c.from_user.id!=cfg.owner_id: return await c.answer("Not authorized.",show_alert=True)
        page=int(c.data.split(":",1)[1]); rows,page,pages,total=await premium.active_page(page,30)
        if not rows: return await c.answer("No active subscriptions.",show_alert=True)
        text=f"<b>Active premium users</b> · Page {page+1}/{pages} · {total} total\n"+"\n".join(f"<code>{r['user_id']}</code> — {r.get('expires_at')}" for r in rows)
        buttons=[]; nav=[]
        if page>0: nav.append(("⬅️ Previous",f"premium_page:{page-1}"))
        if page+1<pages: nav.append(("Next ➡️",f"premium_page:{page+1}"))
        if nav: buttons.append(nav)
        await c.message.edit_text(text[:3900],reply_markup=K.kb(buttons) if buttons else None); await c.answer()

    @dp.message(Command("premium"))
    async def premium_status(m:Message):
        try: uid=int(m.text.split()[1])
        except Exception: uid=m.from_user.id
        if m.from_user.id not in (cfg.owner_id,uid): return
        row=await db.premium_subscriptions.find_one({"user_id":uid}); await m.answer(f"Premium: {'ACTIVE' if await premium.active(uid) else 'INACTIVE'}\nExpiry: {row.get('expires_at') if row else 'N/A'}")

    @dp.callback_query(F.data.startswith("back:"))
    async def generic_back(c:CallbackQuery,state:FSMContext):
        target=c.data.split(':',1)[1]
        if target in ('home','main'): return await cb_home(c,state)
        if target=='channels': return await channels(c)
        if target=='create': return await create(c)
        await cb_home(c,state)

    @dp.callback_query(F.data=="home")
    async def cb_home(c:CallbackQuery,state:FSMContext): await tg.cancel_login(c.from_user.id); await invalidate_approvals(c.from_user.id); await state.clear(); await c.message.edit_text("Welcome to <b>JARVIS Index Bot</b>.\n\nCreate professional Telegram channel navigation indexes automatically.",reply_markup=K.main()); await c.answer()
    @dp.callback_query(F.data=="connect")
    async def cb_connect(c:CallbackQuery): await tg.cancel_login(c.from_user.id); await c.message.edit_text("<b>Connect Telegram Account</b>",reply_markup=K.connect()); await c.answer()
    @dp.callback_query(F.data=="login")
    async def cb_login(c:CallbackQuery,state:FSMContext):
        if not await require_premium(c.from_user.id): return await c.answer("Premium access required.",show_alert=True)
        row=await db.telegram_sessions.find_one({"user_id":c.from_user.id})
        if row: return await c.answer("Disconnect the currently connected account first.",show_alert=True)
        await state.set_state(Flow.phone); await c.message.answer("Send your phone number with country code (example: +911234567890).\n\nUse /cancel to stop.",reply_markup=K.kb([[('❌ Cancel','cancel_flow')]])); await c.answer()
    @dp.message(Flow.phone)
    async def phone(m:Message,state:FSMContext):
        phone=(m.text or "").strip()
        if not await safe_delete_sensitive(m): await m.answer("For your privacy, delete the phone-number message from this chat after connecting.")
        if not phone.startswith('+') or not phone[1:].isdigit(): return await m.answer("Invalid phone format. Send it with country code, e.g. +911234567890.")
        try: await tg.begin_login(m.from_user.id,phone); await state.set_state(Flow.otp); await m.answer("Telegram login code sent. Send the OTP here.",reply_markup=K.kb([[('❌ Cancel','cancel_flow')]]))
        except Exception as e: log.warning("Telegram login request failed: %s",type(e).__name__); await state.clear(); await m.answer("Could not request Telegram code. Check API credentials and phone number, then try again.")
    @dp.message(Flow.otp)
    async def otp(m:Message,state:FSMContext):
        code=(m.text or "").strip()
        deleted=await safe_delete_sensitive(m)
        if not deleted: await m.answer("For your security, please delete the OTP message from this chat after login.")
        try:
            result=await tg.finish_login(m.from_user.id,code)
            if result=="2FA_REQUIRED": await state.set_state(Flow.twofa); return await m.answer("Two-step verification is enabled. Send your Telegram 2FA password.",reply_markup=K.kb([[('❌ Cancel','cancel_flow')]]))
            await state.clear(); await m.answer("<b>Telegram Account Connected Successfully</b>",reply_markup=K.connect())
        except Exception as e:
            log.warning("Telegram OTP failed: %s",type(e).__name__)
            if m.from_user.id not in tg.pending: await state.clear(); return await m.answer("Login expired or attempts exceeded. Start Connect Account again.")
            await m.answer("OTP was rejected. Try again, or /cancel.")
    @dp.message(Flow.twofa)
    async def twofa(m:Message,state:FSMContext):
        password=m.text or ""
        deleted=await safe_delete_sensitive(m)
        if not deleted: await m.answer("For your security, please delete the password message from this chat after login.")
        try:
            result=await tg.finish_login(m.from_user.id,"",password)
            if result=="2FA_REQUIRED": return await m.answer("Send your Telegram 2FA password to continue.",reply_markup=K.kb([[('❌ Cancel','cancel_flow')]]))
            await state.clear(); await m.answer("<b>Telegram Account Connected Successfully</b>",reply_markup=K.connect())
        except Exception as e:
            log.warning("Telegram 2FA failed: %s",type(e).__name__)
            if m.from_user.id not in tg.pending: await state.clear(); return await m.answer("Login expired or attempts exceeded. Start Connect Account again.")
            await m.answer("2FA password was not accepted. Try again or /cancel.")
    @dp.message(Command("cancel"))
    async def cancel(m:Message,state:FSMContext):
        await tg.cancel_login(m.from_user.id); await invalidate_approvals(m.from_user.id); await state.clear()
        await m.answer("Operation cancelled.",reply_markup=K.main())
    @dp.callback_query(F.data=="cancel_flow")
    async def cancel_flow(c:CallbackQuery,state:FSMContext):
        await tg.cancel_login(c.from_user.id); await invalidate_approvals(c.from_user.id); await state.clear()
        await c.message.edit_text("Operation cancelled.",reply_markup=K.main()); await c.answer()

    @dp.callback_query(F.data=="status")
    async def status(c:CallbackQuery):
        row=await db.telegram_sessions.find_one({"user_id":c.from_user.id})
        if not row: text="Status: Not connected"
        else:
            verification_error=False
            try: client=await tg.client_for(c.from_user.id)
            except Exception as exc:
                log.warning("Session status check failed: %s",type(exc).__name__); client=None; verification_error=True
            status_text="Could not verify (temporary connection error)" if verification_error else ("Connected" if client else "Session invalid or revoked")
            validity="Not verified" if verification_error else ("Authorized" if client else "Invalid or revoked")
            text=f"Status: {status_text}\nSession validity: {validity}\nTelegram ID: <code>{row.get('telegram_id')}</code>\nUsername: @{row.get('username') or 'not set'}\nConnected: {row.get('connected_at')}"
        await c.message.edit_text(text,reply_markup=K.status()); await c.answer()
    @dp.callback_query(F.data=="disconnect")
    async def disconnect(c:CallbackQuery): await c.message.edit_text("Are you sure you want to disconnect your Telegram account?",reply_markup=K.kb([[('✅ Yes, Disconnect','disconnect:yes')],[('❌ Cancel','connect')]])); await c.answer()
    @dp.callback_query(F.data=="disconnect:yes")
    async def disconnect_yes(c:CallbackQuery):
        revoked=await tg.disconnect_user(c.from_user.id)
        text="Telegram account disconnected and its authorization revoked." if revoked else "Stored session removed from this bot, but Telegram could not confirm remote revocation. Also revoke it in Telegram Settings → Devices."
        await c.message.edit_text(text,reply_markup=K.connect()); await c.answer()

    async def show_channel_picker(target, uid, page=0, prefix="choose"):
        rows,page,pages,total=await channels_for(uid,page)
        if not rows:
            text="No registered channels yet."
            markup=K.kb([[('➕ Add Channel','add_channel')],[('⬅️ Back','home')]])
        else:
            buttons=[[(('📚 ' if prefix=='choose' else '')+x['title'][:48],f"{prefix}:{x['channel_id']}")] for x in rows]
            nav=[]
            if page>0: nav.append(("⬅️ Previous",f"pickpage:{prefix}:{page-1}"))
            if page+1<pages: nav.append(("Next ➡️",f"pickpage:{prefix}:{page+1}"))
            if nav: buttons.append(nav)
            buttons.append([('⬅️ Back to Main Menu','home')])
            text=f"Select a channel · Page {page+1}/{pages} · {total} total"
            markup=K.kb(buttons)
        # Slash commands pass the user's incoming Message; callback flows pass
        # the bot's own message. Only bot-authored messages can be edited.
        sender=getattr(target,"from_user",None)
        if sender and not getattr(sender,"is_bot",False):
            await target.answer(text,reply_markup=markup)
        else:
            await target.edit_text(text,reply_markup=markup)

    @dp.callback_query(F.data.startswith("pickpage:"))
    async def pick_channel_page(c:CallbackQuery):
        _,prefix,page=c.data.split(':')
        if prefix not in {"choose","manage"}: return await c.answer("Invalid page.",show_alert=True)
        await show_channel_picker(c.message,c.from_user.id,int(page),prefix); await c.answer()

    async def show_channels(target, uid, page=0):
        rows,page,pages,total=await channels_for(uid,page)
        if not rows:
            return await target.edit_text("You have not registered any channels yet.",reply_markup=K.kb([[('➕ Add Channel','add_channel')],[('⬅️ Back','home')]]))
        buttons=[[(f"{page*8+i+1}. {x['title'][:45]}",f"manage:{x['channel_id']}")] for i,x in enumerate(rows)]
        nav=[]
        if page>0: nav.append(("⬅️ Previous",f"channels_page:{page-1}"))
        if page+1<pages: nav.append(("Next ➡️",f"channels_page:{page+1}"))
        if nav: buttons.append(nav)
        buttons += [[('➕ Add Channel','add_channel')],[('🗑 Manage Channels','manage')],[('⬅️ Back to Main Menu','home')]]
        await target.edit_text(f"<b>My Channels</b> · Page {page+1}/{pages} · {total} total\nSelect a channel to manage:",reply_markup=K.kb(buttons))

    @dp.callback_query(F.data=="channels")
    async def channels(c:CallbackQuery):
        await show_channels(c.message,c.from_user.id,0); await c.answer()

    @dp.callback_query(F.data.startswith("channels_page:"))
    async def channels_page(c:CallbackQuery):
        await show_channels(c.message,c.from_user.id,int(c.data.split(':',1)[1])); await c.answer()
    @dp.callback_query(F.data=="add_channel")
    async def add_channel(c:CallbackQuery,state:FSMContext):
        if not await require_premium(c.from_user.id): return await c.answer("Premium access required.",show_alert=True)
        try: connected=await tg.client_for(c.from_user.id)
        except Exception: return await c.answer("Could not verify Telegram account connection. Refresh Account Status and try again.",show_alert=True)
        if not connected: return await c.message.edit_text("Connect a Telegram account first.",reply_markup=K.connect())
        await state.set_state(Flow.channel); await c.message.answer("Send the public username, channel ID, or private channel reference.",reply_markup=K.kb([[('❌ Cancel','cancel_flow')]])); await c.answer()
    @dp.message(Flow.channel)
    async def receive_channel(m:Message,state:FSMContext):
        if not await require_premium(m.from_user.id): await state.clear(); return await m.answer("Premium access required.")
        try:
            entity=await tg.resolve_channel(m.from_user.id,(m.text or "").strip()); title=getattr(entity,"title",str(entity.id))
            marked_id=int(f"-100{entity.id}")
            await state.update_data(pending_channel={"channel_id":marked_id,"title":title,"username":getattr(entity,"username",None)})
            await state.set_state(None)
            await m.answer(f"Channel validated:\n<b>{html.escape(title)}</b>\nID: <code>{marked_id}</code>\nUsername: @{getattr(entity,'username',None) or 'private'}",reply_markup=K.kb([[('✅ Register Channel','register_channel')],[('❌ Cancel','home')]]))
        except Exception:
            await m.answer("Channel could not be accessed through the connected account. Check the reference and account access.")
    @dp.callback_query(F.data=="register_channel")
    async def register_channel(c:CallbackQuery,state:FSMContext):
        if not await require_premium(c.from_user.id): return await c.answer("Premium access required.",show_alert=True)
        data=await state.get_data(); ch=data.get("pending_channel")
        if not ch: return await c.answer("Registration expired. Try again.",show_alert=True)
        try: channel_access=await tg.can_access_channel(c.from_user.id,ch["channel_id"])
        except Exception: return await c.answer("Could not verify channel access. Refresh Account Status and try again.",show_alert=True)
        if not channel_access:
            await state.clear(); return await c.answer("Connected Telegram account no longer has channel access.",show_alert=True)
        ch.update(user_id=c.from_user.id,registered_at=datetime.now(timezone.utc)); await db.registered_channels.update_one({"user_id":c.from_user.id,"channel_id":ch['channel_id']},{"$set":ch},upsert=True)
        await state.update_data(pending_channel=None); await c.message.edit_text("Channel registered successfully.",reply_markup=K.main()); await c.answer()
    @dp.callback_query(F.data=="manage")
    async def manage(c:CallbackQuery):
        await show_channel_picker(c.message,c.from_user.id,0,"manage"); await c.answer()
    @dp.callback_query(F.data.startswith("manage:"))
    async def manage_one(c:CallbackQuery):
        cid=int(c.data.split(':')[1]); ch=await db.registered_channels.find_one({"user_id":c.from_user.id,"channel_id":cid})
        if not ch: return await c.answer("Channel not found.",show_alert=True)
        await c.message.edit_text(f"<b>{html.escape(ch['title'])}</b>",reply_markup=K.kb([[('📋 Create Index',f'choose:{cid}')],[('🗑 Remove Channel',f'remove:{cid}')],[('⬅️ Back to My Channels','channels')]])); await c.answer()
    @dp.callback_query(F.data.startswith("remove:"))
    async def remove(c:CallbackQuery):
        cid=int(c.data.split(':')[1]); await c.message.edit_text("Remove this registered channel?",reply_markup=K.kb([[('✅ Yes, Remove',f'remove_yes:{cid}')],[('❌ Cancel','manage')]])); await c.answer()
    @dp.callback_query(F.data.startswith("remove_yes:"))
    async def remove_yes(c:CallbackQuery):
        cid=int(c.data.split(':')[1])
        removed=await db.registered_channels.delete_one({"user_id":c.from_user.id,"channel_id":cid})
        if not removed.deleted_count: return await c.answer("Channel is no longer registered.",show_alert=True)
        await db.indexing_jobs.update_many({"user_id":c.from_user.id,"channel_id":cid,"status":{"$in":["Queued","Scanning","Preview Ready","Waiting for Resolution"]}},
            {"$set":{"status":"Cancelled","cancel_requested":True,"cancel_reason":"channel_removed"},"$unset":{"active_key":"","approval_token":"","approval_expires_at":""}})
        await db.previews.delete_many({"user_id":c.from_user.id,"channel_id":cid})
        await c.message.edit_text("Channel removed. Active scans and unpublished previews for this channel were cancelled.",reply_markup=K.main()); await c.answer()

    async def show_source_target_pairs(target, uid, page=0):
        total=await db.source_target_pairs.count_documents({"user_id":uid}); pages=max(1,(total+7)//8); page=max(0,min(int(page),pages-1))
        pairs=await db.source_target_pairs.find({"user_id":uid}).sort("name",1).skip(page*8).limit(8).to_list(8)
        buttons=[[(f"🔁 {p['name'][:48]}",f"st_pair:{p['pair_id']}")] for p in pairs]
        nav=[]
        if page>0: nav.append(("⬅️ Previous",f"st_page:{page-1}"))
        if page+1<pages: nav.append(("Next ➡️",f"st_page:{page+1}"))
        if nav: buttons.append(nav)
        buttons.append([("➕ Add New Channel Pair","st_add")]); buttons.append([("⬅️ Main Menu","home")])
        await target.edit_text(f"<b>Source-to-Target Index</b>\nSaved pairs · Page {page+1}/{pages} · {total} total\nChoose a pair or add a new one.",reply_markup=K.kb(buttons))

    @dp.callback_query(F.data == "st_home")
    async def source_target_home(c:CallbackQuery):
        if not await require_premium(c.from_user.id): return await c.answer("Premium access required.",show_alert=True)
        await show_source_target_pairs(c.message,c.from_user.id,0); await c.answer()

    @dp.callback_query(F.data.startswith("st_page:"))
    async def source_target_page(c:CallbackQuery):
        await show_source_target_pairs(c.message,c.from_user.id,int(c.data.split(':',1)[1])); await c.answer()

    @dp.callback_query(F.data == "st_add")
    async def source_target_add(c:CallbackQuery,state:FSMContext):
        if not await require_premium(c.from_user.id): return await c.answer("Premium access required.",show_alert=True)
        await state.clear(); await state.set_state(Flow.source_pair)
        await c.message.answer(
            "<b>Add a saved channel pair</b>\nSend these 9 lines in order:\n"
            "1. Pair name (e.g. Parmar railway → Parmar backup)\n"
            "2. Source channel ID or @username\n3. Target channel ID or @username\n"
            "4. Source first file message link\n5. Source last file message link\n6. Source index message link\n"
            "7. Target first file message link\n8. Target last file message link\n9. Existing target index message link (required).\n\n"
            "Your connected Telegram account must be able to read the source channel and edit the existing target index message (own message or channel admin with Edit Messages permission). The bot itself does not need to be an admin in the source or target channel for this mode. Channels will be added to My Channels automatically.",
            reply_markup=K.kb([[('❌ Cancel','cancel_flow')]]))
        await c.answer()

    @dp.message(Flow.source_pair)
    async def source_target_save_pair(m:Message,state:FSMContext):
        if not await require_premium(m.from_user.id): await state.clear(); return await m.answer("Premium access required.")
        lines=[x.strip() for x in (m.text or '').splitlines() if x.strip()]
        if len(lines)!=9:
            return await m.answer("Please send exactly 9 non-empty lines as instructed. No pair was saved.")
        try:
            name,src_ref,dst_ref,src_first,src_last,src_index,dst_first,dst_last,dst_index=lines
            src_entity=await tg.resolve_channel(m.from_user.id,src_ref); dst_entity=await tg.resolve_channel(m.from_user.id,dst_ref)
            src_id=tg.marked_channel_id(src_entity); dst_id=tg.marked_channel_id(dst_entity)
            # Resolve and validate both channels directly from this flow. No separate
            # Add Channel round-trip is required for Source-to-Target mode.
            if not await tg.can_access_channel(m.from_user.id,src_id):
                raise ValueError("Connected Telegram account cannot read the source channel.")
            if not await tg.can_access_channel(m.from_user.id,dst_id):
                raise ValueError("Connected Telegram account cannot access the target channel.")
            if dst_index == '-':
                raise ValueError("An existing target index message link is required. Paste the copied topic list in the target channel first, then send its message link here.")
            now_registered=datetime.now(timezone.utc)
            for entity,cid in ((src_entity,src_id),(dst_entity,dst_id)):
                await db.registered_channels.update_one(
                    {"user_id":m.from_user.id,"channel_id":cid},
                    {"$set":{"user_id":m.from_user.id,"channel_id":cid,
                             "title":entity.title,"username":getattr(entity,"username",None),
                             "registered_at":now_registered,"source_target_auto_added":True}},
                    upsert=True)
            target=await db.registered_channels.find_one({"user_id":m.from_user.id,"channel_id":dst_id})
            def link_id(value, expected_channel):
                parsed=parse_tme_message_url(value)
                if not parsed:
                    raise ValueError("Invalid Telegram message link.")
                # Public t.me/username/message links parse to a username; the
                # selected channel is independently verified by get_message below.
                if isinstance(parsed[0], int) and int(parsed[0]) != int(expected_channel):
                    raise ValueError("A message link does not belong to its selected channel.")
                return int(parsed[1])
            sf=link_id(src_first,src_id); sl=link_id(src_last,src_id); si=link_id(src_index,src_id)
            tf=link_id(dst_first,dst_id); tl=link_id(dst_last,dst_id)
            ti=None if dst_index=='-' else link_id(dst_index,dst_id)
            if sf>sl or tf>tl: raise ValueError("First file link must precede the last file link.")
            # Verify all boundary and index messages are readable by this Telegram account.
            for cid,mid in ((src_id,sf),(src_id,sl),(src_id,si),(dst_id,tf),(dst_id,tl)):
                await tg.get_message(m.from_user.id,cid,mid)
            if ti:
                target_index_message=await tg.get_message(m.from_user.id,dst_id,ti)
                source_index_message=await tg.get_message(m.from_user.id,src_id,si)
                if normalize_index_text(getattr(source_index_message,"message", "")) != normalize_index_text(getattr(target_index_message,"message", "")):
                    raise ValueError("Source and target index text do not match. Copy the same topic list to the target channel without changing its wording/order, then save the pair again.")
                if not await tg.can_edit_channel_message(m.from_user.id,dst_id,ti):
                    raise ValueError("Your connected Telegram account cannot edit this target index message. Make the account the message author or give it Edit Messages admin permission in the target channel.")
            pair_id=uuid.uuid4().hex[:12]; now=datetime.now(timezone.utc)
            doc={"user_id":m.from_user.id,"pair_id":pair_id,"name":name[:80],"source_channel_id":src_id,"source_title":src_entity.title,
                 "target_channel_id":dst_id,"target_title":dst_entity.title,"source_first_id":sf,"source_last_id":sl,"source_index_id":si,
                 "target_first_id":tf,"target_last_id":tl,"target_index_id":ti,"created_at":now,"updated_at":now}
            update_pair_id=(await state.get_data()).get("update_pair_id")
            if update_pair_id:
                await db.source_target_pairs.update_one({"user_id":m.from_user.id,"pair_id":update_pair_id},{"$set":{k:v for k,v in doc.items() if k not in {"pair_id","user_id","created_at"}}})
                pair_id=update_pair_id
            else:
                await db.source_target_pairs.insert_one(doc)
            await state.clear()
            await m.answer(f"<b>Channel pair saved successfully!</b>\n{name}\nSource: {html.escape(src_entity.title)}\nTarget: {html.escape(dst_entity.title)}",reply_markup=K.kb([[('🔁 Open Saved Pair',f'st_pair:{pair_id}')],[('📂 All Saved Pairs','st_home')]]))
        except Exception as exc:
            await m.answer(f"Could not save this pair: {html.escape(str(exc)[:250])}\nPlease check the channel references and message links. No pair was saved.")

    @dp.callback_query(F.data.startswith("st_pair:"))
    async def source_target_pair(c:CallbackQuery):
        pair_id=c.data.split(':',1)[1]; p=await db.source_target_pairs.find_one({"user_id":c.from_user.id,"pair_id":pair_id})
        if not p: return await c.answer("Saved pair not found.",show_alert=True)
        await c.message.edit_text(f"<b>{html.escape(p['name'])}</b>\nSource: {html.escape(p['source_title'])}\nTarget: {html.escape(p['target_title'])}\n\nChoose whether to update saved links/settings or create hyperlinks using the current settings.",reply_markup=K.kb([[('🔄 Update Existing Links / Settings',f'st_update:{pair_id}')],[('🔗 Create Hyperlinks',f'st_create:{pair_id}')],[('🗑 Delete Pair',f'st_delete:{pair_id}')],[('⬅️ Back to Saved Pairs','st_home')]])); await c.answer()

    @dp.callback_query(F.data.startswith("st_update:"))
    async def source_target_update(c:CallbackQuery,state:FSMContext):
        pair_id=c.data.split(':',1)[1]; p=await db.source_target_pairs.find_one({"user_id":c.from_user.id,"pair_id":pair_id})
        if not p: return await c.answer("Pair not found.",show_alert=True)
        await state.clear(); await state.set_state(Flow.source_pair); await state.update_data(update_pair_id=pair_id)
        await c.message.answer("Send the same 9 lines again with updated information. Keep the same pair name if desired. The existing pair will be updated, not duplicated.",reply_markup=K.kb([[('❌ Cancel','cancel_flow')]])); await c.answer()

    @dp.callback_query(F.data.startswith("st_delete:"))
    async def source_target_delete(c:CallbackQuery):
        pair_id=c.data.split(':',1)[1]
        result=await db.source_target_pairs.delete_one({"user_id":c.from_user.id,"pair_id":pair_id})
        await c.message.edit_text("Saved channel pair deleted. Telegram channels and messages were not changed.",reply_markup=K.kb([[('📂 Saved Pairs','st_home')],[('🏠 Main Menu','home')]])); await c.answer()

    @dp.callback_query(F.data.startswith("st_create:"))
    async def source_target_create(c:CallbackQuery):
        if not await require_premium(c.from_user.id): return await c.answer("Premium access required.",show_alert=True)
        pair_id=c.data.split(':',1)[1]; p=await db.source_target_pairs.find_one({"user_id":c.from_user.id,"pair_id":pair_id})
        if not p: return await c.answer("Saved pair not found.",show_alert=True)
        await c.answer("Source-to-Target indexing started.")
        async def update_stage(stage, detail=""):
            try:
                await c.message.edit_text(
                    f"<b>🔁 Source-to-Target Indexing</b>\nCurrent step: {html.escape(stage)}"
                    + (f"\n{html.escape(detail)}" if detail else ""),
                    reply_markup=K.kb([[("⬅️ Saved Pairs","st_home")]]))
            except Exception as edit_exc:
                if "message is not modified" not in str(edit_exc).lower():
                    log.debug("Source-target progress update skipped: %s",type(edit_exc).__name__)
        try:
            await update_stage("Reading source index and its clickable topic links…")
            source_index=await tg.get_message(c.from_user.id,p['source_channel_id'],p['source_index_id'])
            target_index=await tg.get_message(c.from_user.id,p['target_channel_id'],p['target_index_id'])
            if normalize_index_text(getattr(source_index,"message", "")) != normalize_index_text(getattr(target_index,"message", "")):
                await c.message.answer("The source and target index text no longer match. No changes were made. Make the target index text identical to the source index and retry.")
                return
            links=source_links(source_index)
            if not links:
                await c.message.answer("No clickable Telegram message links were found in the source index. Check that the topic rows really have Telegram message hyperlinks.")
                return
            await update_stage("Reading target file range…",f"Source index links found: {len(links)}")
            async def target_range_progress(scanned,last_id):
                await update_stage("Scanning target channel messages…",f"Target messages checked: {scanned} · Latest message ID: {last_id}")
            dst_range=await tg.get_message_range(c.from_user.id,p['target_channel_id'],p['target_first_id'],p['target_last_id'],progress=target_range_progress)
            source_messages=[]; source_ids=[]; source_first=int(p['source_first_id']); source_last=int(p['source_last_id'])
            for link_no,(_,_,url) in enumerate(links,1):
                parsed=parse_tme_message_url(url)
                if not parsed:
                    continue
                source_id=int(parsed[1])
                if source_id < source_first or source_id > source_last:
                    await c.message.answer(f"Source index link #{link_no} points outside the saved source file range (message {source_id}). Update the source first/last file links.")
                    return
                if source_id in source_ids:
                    continue
                linked_message=await tg.get_message(c.from_user.id,p['source_channel_id'],source_id)
                source_messages.append(linked_message); source_ids.append(source_id)
                if link_no==1 or link_no%5==0 or link_no==len(links):
                    await update_stage("Following source index hyperlinks and reading file captions…",f"Opened {link_no}/{len(links)} links · Target files loaded: {len(dst_range)}")
            if not source_messages:
                await c.message.answer("Could not open any source file links using your connected Telegram account.")
                return
            await update_stage("Matching each linked source file to target files…",f"Linked source files: {len(source_messages)} · Target files: {len(dst_range)}")
            mapping,warnings=match_messages(source_messages,dst_range)
            target_channel=await db.registered_channels.find_one({"user_id":c.from_user.id,"channel_id":p['target_channel_id']}) or {}
            rendered,linked,missing=render_linked_index(source_index,mapping,p['target_channel_id'],target_channel.get('username'))
            if linked==0:
                await c.message.answer("No source hyperlinks could be mapped to target files. Check channel ranges and matching captions.")
                return
            await update_stage("Building review preview…",f"Mapped links: {linked} · Unmatched: {len(missing)}")
            # Create a normal preview/job so the existing preview, approval and safe publisher are reused.
            jid=uuid.uuid4().hex[:12]; now=datetime.now(timezone.utc)
            entry_rows=[]
            for start,end,url in links:
                parsed=parse_tme_message_url(url); sid=int(parsed[1]) if parsed else 0
                label=(source_index.message or '')[start:end]
                entry_rows.append({"label":label,"key":label,"source_message_id":sid,"message_id":mapping.get(sid),"resolved":sid in mapping})
            missing_labels=[x['label'] for x in entry_rows if not x.get('message_id')]
            await db.indexing_jobs.insert_one({"user_id":c.from_user.id,"job_id":jid,"channel_id":p['target_channel_id'],"channel_title":p['target_title'],"mode":"source_target","template":"","status":"Preview Ready","created_at":now,"updated_at":now,"messages_scanned":len(source_messages)+len(dst_range),"categories_found":len(entry_rows),"unresolved":len(missing_labels),"source_target_pair_id":pair_id})
            await db.previews.insert_one({"user_id":c.from_user.id,"job_id":jid,"channel_id":p['target_channel_id'],"channel_title":p['target_title'],"channel_username":target_channel.get('username'),"mode":"source_target","html":rendered,"entries":entry_rows,"missing":missing_labels,"uncertain_captions":warnings,"target_index_id":p['target_index_id'],"total_scanned":len(source_messages)+len(dst_range),"created_at":now,"expires_at":now+timedelta(days=7),"approved":False,"empty":not bool(entry_rows),"source_target_pair_id":pair_id})
            await db.source_target_pairs.update_one({"user_id":c.from_user.id,"pair_id":pair_id},{"$set":{"last_used_at":now,"updated_at":now}})
            await c.message.answer(f"<b>✅ Source-to-Target matching complete</b>\nSource files opened from index links: {len(source_messages)}\nTarget files: {len(dst_range)}\nIndex links: {len(links)}\nMapped links: {linked}\nUnmatched: {len(missing_labels)}\nMatching/review notes: {len(warnings)}\n\nReview the preview and correct any unmatched entries before publishing.",reply_markup=K.kb([[('👁 View Index Preview',f'preview:{jid}')],[('✏️ Correct / Edit Links',f'st_edit_menu:{jid}')],[('📂 Saved Channel Pairs','st_home')]]))
        except Exception as exc:
            log.exception("Source-target generation failed for user=%s pair=%s",c.from_user.id,pair_id)
            await c.message.answer(f"❌ Could not create preview.\nReason: <code>{html.escape(type(exc).__name__)}</code>\nDetails: {redact_error(exc)[:500]}")

    @dp.callback_query(F.data.startswith("quick:"))
    async def quick_mode(c:CallbackQuery,state:FSMContext):
        mode=c.data.split(":",1)[1]
        if mode not in {"auto","manual"}: return await c.answer("Invalid mode.",show_alert=True)
        if not await require_premium(c.from_user.id): return await c.answer("Premium access required.",show_alert=True)
        rows,_,_,_=await channels_for(c.from_user.id)
        if not rows: return await c.message.edit_text("Register a channel first.",reply_markup=K.kb([[('➕ Add Channel','add_channel')],[('🏠 Main Menu','home')]]))
        await state.clear(); await state.update_data(preferred_mode="automatic" if mode=="auto" else "manual")
        await show_channel_picker(c.message,c.from_user.id,0,"choose")
        await c.answer()

    @dp.callback_query(F.data=="create")
    async def create(c:CallbackQuery):
        if not await require_premium(c.from_user.id): return await c.answer("Premium access required.",show_alert=True)
        rows,_,_,_=await channels_for(c.from_user.id)
        if not rows: return await c.message.edit_text("Register a channel first.",reply_markup=K.kb([[('➕ Add Channel','add_channel')],[('⬅️ Back','home')]]))
        await show_channel_picker(c.message,c.from_user.id,0,"choose"); await c.answer()
    @dp.callback_query(F.data.startswith("choose:"))
    async def choose(c:CallbackQuery,state:FSMContext):
        if not await require_premium(c.from_user.id): return await c.answer("Premium access required.",show_alert=True)
        cid=int(c.data.split(':')[1]); ch=await db.registered_channels.find_one({"user_id":c.from_user.id,"channel_id":cid})
        if not ch: return await c.answer("Channel not found.",show_alert=True)
        data=await state.get_data()
        preferred=data.get("preferred_mode")
        await state.update_data(channel_id=cid,channel_title=ch['title'])
        if preferred=="manual":
            await state.set_state(Flow.template)
            await c.message.edit_text("<b>📝 Manual Template Mode</b>\nSend your formatted navigation index template. The original layout and font will be preserved.",reply_markup=K.kb([[('❌ Cancel','home')]]))
        elif preferred=="automatic":
            await c.message.edit_text("<b>🤖 Automatic Topic Mode</b>\nThe scanner will classify captions and document filenames, then build a reviewable index.",reply_markup=K.kb([[('🚀 Start Scanning','scan_auto')],[('⬅️ Back','create')]]))
        else:
            await c.message.edit_text(f"<b>{small_caps('Choose Indexing Mode')}</b>",reply_markup=K.modes())
        await c.answer()
    @dp.callback_query(F.data=="mode:manual")
    async def mode_manual(c:CallbackQuery,state:FSMContext):
        if not await require_premium(c.from_user.id): return await c.answer("Premium access required.",show_alert=True)
        data=await state.get_data()
        if not data.get("channel_id") or not await db.registered_channels.find_one({"user_id":c.from_user.id,"channel_id":data.get("channel_id")}): return await c.answer("Select a registered channel again.",show_alert=True)
        await state.set_state(Flow.template); await c.message.edit_text("<b>Manual Template Mode</b>\n\nSend your formatted navigation index template here. The bot preserves the original text and matches the oldest suitable message.",reply_markup=K.kb([[('❌ Cancel','home')]])); await c.answer()
    @dp.message(Flow.template)
    async def template(m:Message,state:FSMContext):
        if not await require_premium(m.from_user.id): await state.clear(); return await m.answer("Premium access required.",reply_markup=K.main())
        data=await state.get_data()
        if not data.get("channel_id") or not await db.registered_channels.find_one({"user_id":m.from_user.id,"channel_id":data.get("channel_id")}): await state.clear(); return await m.answer("Selected channel is no longer registered. Start again.",reply_markup=K.main())
        await state.update_data(pending_mode="manual",pending_template=m.text or "",pending_chat_id=m.chat.id)
        await m.answer("<b>Choose scan scope</b>\\nFull history rebuilds the index from the beginning. Incremental scans only messages added since your last completed scan and reuses its saved index.",
            reply_markup=K.kb([[("🔎 Full Channel History","scan_scope:full")],[("➕ New Messages Only","scan_scope:incremental")],[("❌ Cancel","cancel_flow")]]))
    @dp.callback_query(F.data=="mode:auto")
    async def mode_auto(c:CallbackQuery,state:FSMContext):
        if not await require_premium(c.from_user.id): return await c.answer("Premium access required.",show_alert=True)
        data=await state.get_data()
        if not data.get("channel_id") or not await db.registered_channels.find_one({"user_id":c.from_user.id,"channel_id":data.get("channel_id")}): return await c.answer("Select a registered channel again.",show_alert=True)
        await c.message.edit_text("<b>Automatic Topic Mode</b>\n\nThe bot scans the channel and groups related materials. It will not create separate entries for individual lectures.",reply_markup=K.kb([[('🚀 Start Scanning','scan_auto')],[('⬅️ Back','create')]])); await c.answer()
    @dp.callback_query(F.data=="scan_auto")
    async def scan_auto(c:CallbackQuery,state:FSMContext):
        if not await require_premium(c.from_user.id): return await c.answer("Premium access required.",show_alert=True)
        data=await state.get_data()
        if not data.get('channel_id') or not await db.registered_channels.find_one({"user_id":c.from_user.id,"channel_id":data.get("channel_id")}): return await c.answer("Select a channel again.",show_alert=True)
        await state.update_data(pending_mode="automatic",pending_template="",pending_chat_id=c.message.chat.id)
        await c.message.edit_text("<b>Choose scan scope</b>\\nFull history rebuilds the index. Incremental scans reuse the last completed index and inspect only newer messages.",
            reply_markup=K.kb([[("🔎 Full Channel History","scan_scope:full")],[("➕ New Messages Only","scan_scope:incremental")],[("❌ Cancel","cancel_flow")]]))
        await c.answer()

    @dp.callback_query(F.data.startswith("scan_scope:"))
    async def scan_scope(c:CallbackQuery,state:FSMContext):
        scope=c.data.split(":",1)[1]
        data=await state.get_data()
        mode=data.get("pending_mode")
        if scope not in {"full","incremental"} or mode not in {"manual","automatic"}:
            return await c.answer("Scan setup expired. Start again.",show_alert=True)
        uid=c.from_user.id; cid=int(data.get("channel_id",0)); title=data.get("channel_title","")
        if not cid or not await db.registered_channels.find_one({"user_id":uid,"channel_id":cid}):
            await state.clear(); return await c.answer("Selected channel is no longer registered.",show_alert=True)
        template_text=data.get("pending_template","")
        chat_id=data.get("pending_chat_id",c.message.chat.id)
        try:
            jid = await create_job(
                uid, cid, title, mode, template_text, chat_id, scope
            )
        except RuntimeError as e:
            log.warning(
                "Scan enqueue rejected for user=%s channel=%s: %s",
                uid, cid, e,
            )
            return await c.answer(str(e), show_alert=True)
        except Exception:
            log.exception(
                "Could not enqueue scan for user=%s channel=%s",
                uid, cid,
            )
            return await c.answer(
                "Could not create the scan. The actual error has been logged.",
                show_alert=True,
            )
        await state.clear()
        # Acknowledge the Telegram callback before launching work so the client
        # never leaves the Start Indexing button spinning until the scan ends.
        await c.answer("Indexing job queued.")
        await c.message.edit_text(
            f"<b>🚀 Indexing started</b>\nChannel: {html.escape(title)}\n"
            f"Mode: {html.escape(mode.title())}\nScope: {html.escape(scope.title())}\n"
            "Status: Preparing Telegram connection…\nProgress: 0%",
            reply_markup=K.kb([[("❌ Cancel Scan",f"cancel_scan:{jid}")]]))
        spawn_background(do_scan(c.message,uid,cid,title,mode,template_text,jid))

    async def create_job(uid,cid,title,mode,template,chat_id=None,scan_scope="full"):
        if not await require_premium(uid): raise PermissionError("Premium access required")
        channel=await db.registered_channels.find_one({"user_id":uid,"channel_id":int(cid)})
        if not channel: raise PermissionError("Channel is not registered to this user")
        job_id=uuid.uuid4().hex[:12]
        if mode=="manual":
            await db.index_templates.update_one({"user_id":uid,"channel_id":int(cid)}, {"$set":{"user_id":uid,"channel_id":int(cid),"template":template,"updated_at":datetime.now(timezone.utc)}},upsert=True)
        await job_store.enqueue(user_id=uid,job_id=job_id,channel_id=int(cid),channel_title=title,mode=mode,template=template,chat_id=chat_id,scan_scope=scan_scope)
        return job_id

    async def do_scan(message,uid,cid,title,mode,template_text,job_id):
        if not await require_premium(uid):
            await job_store.finish(uid,job_id,"Failed",error="premium_inactive")
            return await message.answer("Premium access is no longer active. The scan was not started.")
        channel=await db.registered_channels.find_one({"user_id":uid,"channel_id":int(cid)})
        if not channel:
            await job_store.finish(uid,job_id,"Failed",error="channel_not_registered")
            return await message.answer("Selected channel is no longer registered. The scan was not started.")
        job=await job_store.claim(job_id=job_id,user_id=uid,worker_id=secrets.token_hex(8))
        if not job:
            return await message.answer("This indexing job is no longer queued or was cancelled.")
        checkpoint=int(job.get("checkpoint_last_id",0)); base_scanned=int(job.get("messages_scanned",0)); scanned=base_scanned
        try:
            async def show_stage(stage, detail="", percent=None):
                suffix=f"\nProgress: {percent}%" if percent is not None else ""
                body=(f"<b>⚙️ Indexing in progress</b>\n📡 Channel: {html.escape(title)}"
                      f"\n⚙️ Mode: {html.escape(mode.title())}\n🔄 Current step: {html.escape(stage)}")
                if detail: body += f"\n{html.escape(detail)}"
                body += suffix
                try:
                    await message.edit_text(body,reply_markup=K.kb([[("❌ Cancel Scan",f"cancel_scan:{job_id}")]]))
                except Exception as edit_exc:
                    # Telegram may reject an identical edit; that must not stop indexing.
                    if "message is not modified" not in str(edit_exc).lower():
                        log.debug("Progress message update skipped: %s",type(edit_exc).__name__)
            await show_stage("Connecting to Telegram and preparing scanner", "The scan will update here as messages are processed.", 0)
            builder=StreamingIndexBuilder(mode,template_text).restore(job.get("builder_state"))
            if checkpoint == 0 and job.get("scan_scope") == "incremental" and not job.get("builder_state"):
                saved=await db.channel_scan_state.find_one({"user_id":uid,"channel_id":int(cid)})
                if saved and saved.get("mode")==mode and saved.get("template","")==template_text and saved.get("builder_state"):
                    checkpoint=int(saved.get("last_message_id",0) or 0)
                    base_scanned=int(saved.get("messages_scanned",0) or 0)
                    builder.restore(saved["builder_state"])
            last_progress_update=0
            async def progress(batch_count,last_id):
                nonlocal scanned,last_progress_update
                current=await db.indexing_jobs.find_one({"user_id":uid,"job_id":job_id})
                if not current or current.get('status')=='Cancelled' or current.get('cancel_requested'):
                    raise RuntimeError("Scan cancelled by user")
                if not await require_premium(uid):
                    raise PermissionError("Premium entitlement expired during scan")
                scanned=base_scanned+batch_count
                await job_store.checkpoint(uid,job_id,last_id,scanned,builder_state=builder.snapshot())
                if batch_count-last_progress_update>=25 or batch_count<=1:
                    last_progress_update=batch_count
                    found,_,_=builder.result()
                    await show_stage("Reading channel messages",
                        f"Messages scanned: {scanned:,}\nLast message ID: {last_id}\nCategories detected so far: {len(found)}")
            async for record in tg.iter_channel_messages(uid,cid,min_id=checkpoint,progress=progress):
                builder.add(record)
            current=await owned_job(uid,job_id)
            if not current or current.get("status")=="Cancelled" or current.get("cancel_requested"):
                raise RuntimeError("Scan cancelled by user")
            if not await require_premium(uid):
                raise PermissionError("Premium entitlement expired during scan")
            await show_stage("Matching captions and building index", "Applying saved manual resolutions and checking message links…", 94)
            entries,groups,uncertain=builder.result()
            # Apply only saved resolutions that still point to an accessible message in this channel.
            resolutions={}
            async for saved in db.manual_resolutions.find({"user_id":uid,"channel_id":cid}):
                mid=int(saved.get("message_id",0) or 0)
                if mid:
                    try: valid_saved=await tg.validate_message(uid,cid,mid)
                    except Exception as exc:
                        log.warning("Saved resolution verification temporarily failed: %s",type(exc).__name__); valid_saved=None
                    if valid_saved:
                        resolutions[saved.get("entry_key")]=mid
                    elif valid_saved is False:
                        await db.manual_resolutions.update_one({"_id":saved["_id"]},{"$set":{"valid":False,"invalidated_at":datetime.now(timezone.utc)}})
            builder.apply_resolutions(resolutions)
            entries,groups,uncertain=builder.result()
            await db.channel_scan_state.update_one(
                {"user_id":uid,"channel_id":int(cid)},
                {"$set":{"user_id":uid,"channel_id":int(cid),"mode":mode,"template":template_text,
                         "last_message_id":max(checkpoint,int(current.get("checkpoint_last_id",0) or 0)),
                         "messages_scanned":scanned,"builder_state":builder.snapshot(),
                         "completed_at":datetime.now(timezone.utc)}},upsert=True)
            await show_stage("Rendering preview", "Formatting clickable Telegram links and preparing review…", 97)
            if mode=='manual':
                rendered,missing=render_manual_template(template_text,entries,cid,channel.get("username"))
            else:
                rendered,missing=render_automatic_index(entries,cid,username=channel.get("username"),channel_title=title)
            if missing:
                rendered += "\n\n<b>Unresolved entries</b>\n" + "\n".join(f"• {html.escape(label)}" for label in missing)
            if uncertain:
                rendered += "\n\n<b>Captions needing review (not auto-categorized)</b>\n" + "\n".join(f"• {html.escape(caption)}" for caption in uncertain[:10])
            if not entries:
                rendered += "\n\n<b>No categories were detected.</b> No empty index can be published. Try Manual Template Mode or review channel captions."
            now=datetime.now(timezone.utc)
            await db.previews.update_one({"user_id":uid,"job_id":job_id},{"$set":{"user_id":uid,"job_id":job_id,"channel_id":cid,
                "channel_title":title,"channel_username":channel.get("username"),"mode":mode,"html":rendered,"entries":[e.__dict__ for e in entries],"missing":missing,
                "uncertain_captions":uncertain,"total_scanned":scanned,"created_at":now,"expires_at":now+timedelta(days=7),
                "approved":False,"empty":not bool(entries)}},upsert=True)
            final_status="Preview Ready" if entries else "Waiting for Resolution"
            result=await job_store.finish(uid,job_id,final_status,total_scanned=scanned,categories_found=len(entries),
                unresolved=len(missing),builder_state=None)
            if not result.modified_count:
                await db.previews.delete_one({"user_id":uid,"job_id":job_id})
                return await message.answer("Scan was cancelled before preview creation. No index was published.")
            try:
                await message.edit_text(f"<b>✅ Indexing completed</b>\nMessages scanned: {scanned:,}\nCategories found: {len(entries)}\nUnresolved: {len(missing)}",
                    reply_markup=K.kb([[('👁 View Index Preview',f'preview:{job_id}')],[('📂 My Indexing Jobs','jobs')]]))
            except Exception:
                pass
            await message.answer(f"<b>{small_caps('Scan Results')}</b>\n📡 Channel: {html.escape(small_caps(title))}\n📨 Messages scanned: {scanned:,}\n📚 Categories found: {len(entries)}\n⚠️ Unresolved: {len(missing)}\n🔎 Items needing review: {len(uncertain)}",
                reply_markup=K.kb([[('👁 View Index Preview',f'preview:{job_id}')],[('🔧 Resolve Missing Links',f'resolve:{job_id}')],[('🔄 Regenerate Index',f'regenerate:{job_id}')],[('❌ Cancel',f'cancel_job:{job_id}')]]))
        except Exception as e:
            log.warning("Scan failed for job %s: %s",job_id,type(e).__name__)
            current=await owned_job(uid,job_id)
            if current and current.get('status')!='Cancelled':
                await job_store.finish(uid,job_id,"Failed",error=type(e).__name__)
            if current and current.get('status')=='Cancelled':
                await message.answer("Scan cancelled. No index was published.")
            else:
                await message.answer(
                    f"❌ Indexing failed.\nReason: <code>{html.escape(type(e).__name__)}</code>\n"
                    f"Details: {redact_error(e)[:700]}\nNo index was published. Check the connected Telegram account and channel access.",
                    reply_markup=K.main())

    @dp.callback_query(F.data.startswith('cancel_scan:'))
    async def cancel_scan(c:CallbackQuery):
        jid=c.data.split(':',1)[1]
        result=await job_store.request_cancel(c.from_user.id,jid)
        if result.modified_count:
            await c.message.answer("Scan cancellation requested. The current scan will stop at its next checkpoint.")
            await c.answer()
        else:
            await c.answer("This scan is no longer active.",show_alert=True)

    @dp.callback_query(F.data.startswith('cancel_job:'))
    async def cancel_job(c:CallbackQuery):
        jid=c.data.split(':',1)[1]
        result=await db.indexing_jobs.update_one({"user_id":c.from_user.id,"job_id":jid,"status":{"$in":["Queued","Scanning","Preview Ready","Waiting for Resolution"]}},
            {"$set":{"status":"Cancelled","cancel_requested":True,"updated_at":datetime.now(timezone.utc)},
             "$unset":{"active_key":"","approval_token":"","approval_expires_at":""}})
        if not result.modified_count: return await c.answer("This job can no longer be cancelled.",show_alert=True)
        await db.previews.delete_one({"user_id":c.from_user.id,"job_id":jid})
        await c.message.edit_text("Indexing job cancelled. No index was published.",reply_markup=K.main()); await c.answer()

    @dp.callback_query(F.data.startswith('st_edit:'))
    async def source_target_edit_entry(c:CallbackQuery,state:FSMContext):
        parts=c.data.split(':')
        if len(parts)!=3: return await c.answer("Invalid edit request.",show_alert=True)
        jid,index=parts[1],int(parts[2]); p=await db.previews.find_one({"user_id":c.from_user.id,"job_id":jid})
        if not p or p.get('mode')!='source_target' or index<0 or index>=len(p.get('entries',[])):
            return await c.answer("Index entry not found.",show_alert=True)
        entry=p['entries'][index]
        await state.clear(); await state.set_state(Flow.source_edit)
        await state.update_data(source_edit_job=jid,source_edit_index=index)
        await c.message.answer(f"Send the correct target-channel Telegram message link for:\n<b>{html.escape(entry['label'])}</b>",reply_markup=K.kb([[('❌ Cancel','cancel_flow')]])); await c.answer()

    @dp.message(Flow.source_edit)
    async def source_target_edit_receive(m:Message,state:FSMContext):
        data=await state.get_data(); jid=data.get('source_edit_job'); idx=int(data.get('source_edit_index',-1))
        p=await db.previews.find_one({"user_id":m.from_user.id,"job_id":jid}) if jid else None
        if not p or idx<0 or idx>=len(p.get('entries',[])):
            await state.clear(); return await m.answer("This edit request expired. Reopen the preview.")
        parsed=parse_tme_message_url(m.text or '')
        if not parsed or int(parsed[0])!=int(p['channel_id']):
            return await m.answer("That link is not from this target channel. Send a valid target-channel message link.")
        mid=int(parsed[1])
        if not await tg.validate_message(m.from_user.id,p['channel_id'],mid):
            return await m.answer("The target message could not be verified. Check the link and try again.")
        entries=p.get('entries',[]); label=entries[idx]['label']; entries[idx]['message_id']=mid; entries[idx]['resolved']=True
        missing=[x['label'] for x in entries if not x.get('message_id')]
        # Replace this exact occurrence; duplicate visible labels are not conflated.
        target=await db.registered_channels.find_one({"user_id":m.from_user.id,"channel_id":p['channel_id']}) or {}
        dest=(f"https://t.me/{target['username']}/{mid}" if target.get('username') else f"https://t.me/c/{str(p['channel_id']).removeprefix('-100')}/{mid}")
        body=p.get('html',''); escaped_label=html.escape(label)
        pattern=re.compile(r'<a href="[^"]+">'+re.escape(escaped_label)+r'</a>')
        occurrence=sum(1 for item in entries[:idx] if item.get('label')==label)
        matches=list(pattern.finditer(body))
        replacement=f'<a href="{html.escape(dest,quote=True)}">{escaped_label}</a>'
        if occurrence < len(matches):
            match=matches[occurrence]
            body2=body[:match.start()]+replacement+body[match.end():]
        else:
            # Unmatched rows are deliberately plain text in the preview; manual
            # correction must turn that exact row back into a target hyperlink.
            plain_matches=list(re.finditer(re.escape(escaped_label),body))
            if occurrence < len(plain_matches):
                match=plain_matches[occurrence]
                body2=body[:match.start()]+replacement+body[match.end():]
            else:
                body2=body
        await db.previews.update_one({"user_id":m.from_user.id,"job_id":jid},{"$set":{"entries":entries,"missing":missing,"html":body2,"empty":not bool(entries)}})
        await db.indexing_jobs.update_one({"user_id":m.from_user.id,"job_id":jid},{"$set":{"unresolved":len(missing),"status":"Preview Ready" if entries else "Waiting for Resolution"},"$unset":{"approval_token":"","approval_expires_at":""}})
        await state.clear()
        await m.answer("Target link updated and saved. It will remain unchanged during this preview.",reply_markup=K.kb([[('👁 Back to Preview',f'preview:{jid}')],[('🔧 Edit Another Link',f'st_edit_menu:{jid}')]]))

    @dp.callback_query(F.data.startswith('st_edit_menu:'))
    async def source_target_edit_menu(c:CallbackQuery):
        jid=c.data.split(':',1)[1]; p=await db.previews.find_one({"user_id":c.from_user.id,"job_id":jid})
        if not p or p.get('mode')!='source_target': return await c.answer("Preview not found.",show_alert=True)
        buttons=[]
        for i,e in enumerate(p.get('entries',[])[:40]):
            status='✓' if e.get('message_id') else '⚠'
            buttons.append([(f"{status} {e.get('label','Entry')[:42]}",f"st_edit:{jid}:{i}")])
        buttons.append([('⬅️ Back to Preview',f'preview:{jid}')])
        await c.message.answer("Select an index hyperlink to change its target:",reply_markup=K.kb(buttons)); await c.answer()

    @dp.callback_query(F.data.startswith('preview:'))
    async def preview(c:CallbackQuery):
        jid=c.data.split(':',1)[1]; p=await db.previews.find_one({"user_id":c.from_user.id,"job_id":jid}); job=await owned_job(c.from_user.id,jid)
        if not p or not job or preview_expired(p): return await c.answer("Preview not found or expired.",show_alert=True)
        if not await require_premium(c.from_user.id): return await c.answer("Premium is not active.",show_alert=True)
        # Split into safe Telegram-sized chunks.
        body=p['html']
        for chunk in split_html(body,3900):
            await c.message.answer(chunk,disable_web_page_preview=True)
        if p.get("mode") == "source_target":
            review_notes=[]
            if p.get("missing"):
                review_notes.append("Unmatched index rows: " + ", ".join(p.get("missing",[])[:15]))
            if p.get("uncertain_captions"):
                review_notes.extend(p.get("uncertain_captions",[])[:10])
            if review_notes:
                await c.message.answer("<b>Matching notes — review before updating:</b>\n" + "\n".join("• " + html.escape(x) for x in review_notes))
        buttons=[]
        if job.get("status") in {"Preview Ready", "Waiting for Resolution"} and not p.get("empty") and p.get("entries"):
            label = '⚠️ Publish Available Links' if p.get("missing") else '✅ Approve & Publish'
            buttons.append([(label,f'approve:{jid}')])
        if p.get("missing") and p.get('mode')!='source_target': buttons.append([('🔧 Resolve Missing Links',f'resolve:{jid}')])
        if p.get('mode')=='source_target': buttons.append([('✏️ Correct / Edit Links',f'st_edit_menu:{jid}')])
        if job.get("status") not in {"Published","Publish Review Required"}: buttons.append([('🔄 Regenerate',f'regenerate:{jid}')])
        if job.get("status") in {"Preview Ready","Waiting for Resolution","Queued","Scanning"}: buttons.append([('❌ Cancel',f'cancel_job:{jid}')])
        await c.message.answer("Review the preview. Publishing requires a second confirmation.",reply_markup=K.kb(buttons) if buttons else K.main()); await c.answer()
    @dp.callback_query(F.data.startswith('resolve:'))
    async def resolve(c:CallbackQuery):
        jid=c.data.split(':',1)[1]; p=await db.previews.find_one({"user_id":c.from_user.id,"job_id":jid})
        if not p or preview_expired(p): return await c.answer("Preview not found or expired.",show_alert=True)
        if not await require_premium(c.from_user.id): return await c.answer("Premium is not active.",show_alert=True)
        missing=p.get('missing',[])
        if not missing: return await c.answer("There are no unresolved entries.",show_alert=True)
        buttons=[[(f'🔗 Resolve: {x[:35]}',f'resolve_entry:{jid}:{i}')] for i,x in enumerate(missing)]; buttons.append([('⬅️ Back to Preview',f'preview:{jid}')])
        await c.message.answer("Select an unresolved entry:",reply_markup=K.kb(buttons)); await c.answer()
    @dp.callback_query(F.data.startswith('resolve_entry:'))
    async def resolve_entry(c:CallbackQuery,state:FSMContext):
        _,jid,idx=c.data.split(':'); p=await db.previews.find_one({"user_id":c.from_user.id,"job_id":jid})
        if not p or preview_expired(p) or not await require_premium(c.from_user.id) or int(idx)<0 or int(idx)>=len(p.get('missing',[])): return await c.answer("Entry unavailable or premium inactive.",show_alert=True)
        label=p['missing'][int(idx)]; await state.update_data(resolve_job=jid,resolve_label=label,resolve_channel=p['channel_id']); await c.message.answer(f"How would you like to resolve <b>{html.escape(label)}</b>?",reply_markup=K.kb([[('🔗 Send Telegram Message Link','resolve_method:link')],[('📨 Forward Target Message','resolve_method:forward')],[('⬅️ Back','resolve:'+jid)]])); await c.answer()
    @dp.callback_query(F.data.startswith('resolve_method:'))
    async def resolve_method(c:CallbackQuery,state:FSMContext):
        method=c.data.split(':',1)[1]
        data=await state.get_data()
        p=await db.previews.find_one({"user_id":c.from_user.id,"job_id":data.get("resolve_job")}) if data.get("resolve_job") else None
        if not data.get("resolve_job") or not p or preview_expired(p) or data.get("resolve_label") not in p.get("missing",[]) or method not in {"link","forward"} or not await require_premium(c.from_user.id):
            await state.clear(); return await c.answer("Resolution request expired. Start again.",show_alert=True)
        if method=='link': await state.set_state(Flow.resolve_link); await c.message.answer("Send a Telegram message link from the selected channel.",reply_markup=K.kb([[('❌ Cancel','cancel_flow')]]))
        else: await state.set_state(Flow.resolve_forward); await c.message.answer("Forward the target message from the selected channel into this chat.",reply_markup=K.kb([[('❌ Cancel','cancel_flow')]]))
        await c.answer()
    @dp.message(Flow.resolve_forward)
    async def resolve_forward(m:Message,state:FSMContext):
        if not await require_premium(m.from_user.id): await state.clear(); return await m.answer("Premium access required.")
        data=await state.get_data(); p=await db.previews.find_one({"user_id":m.from_user.id,"job_id":data.get("resolve_job")}) if data.get("resolve_job") else None
        if not p or preview_expired(p) or data.get("resolve_label") not in p.get("missing",[]) or int(p.get("channel_id",0))!=int(data.get("resolve_channel",0)):
            await state.clear(); return await m.answer("This resolution request expired. Open the preview and try again.",reply_markup=K.main())
        origin=getattr(m,"forward_origin",None)
        chat=getattr(origin,"chat",None); forwarded_id=getattr(origin,"message_id",None)
        source_id=getattr(chat,"id",None) if chat else None
        if not source_id or not forwarded_id or int(source_id)!=int(data['resolve_channel']):
            return await m.answer("Forwarded message could not be verified as belonging to the selected channel. Please forward the original channel message.")
        if not await tg.validate_message(m.from_user.id,data['resolve_channel'],int(forwarded_id)):
            return await m.answer("The original forwarded message is inaccessible or deleted; it cannot be used.")
        jid=data["resolve_job"]
        await save_resolution(m.from_user.id,data,int(forwarded_id)); await state.clear()
        updated=await db.previews.find_one({"user_id":m.from_user.id,"job_id":jid}) or {}
        buttons=[]
        if updated.get("missing"): buttons.append([('🔧 Resolve Another',f'resolve:{jid}')])
        buttons.extend([[('👁 Back to Preview',f'preview:{jid}')],[('🏠 Main Menu','home')]])
        await m.answer("Link resolved successfully.",reply_markup=K.kb(buttons))
    @dp.message(Flow.resolve_link)
    async def resolve_link(m:Message,state:FSMContext):
        if not await require_premium(m.from_user.id): await state.clear(); return await m.answer("Premium access required.")
        data=await state.get_data(); p=await db.previews.find_one({"user_id":m.from_user.id,"job_id":data.get("resolve_job")}) if data.get("resolve_job") else None
        if not p or preview_expired(p) or data.get("resolve_label") not in p.get("missing",[]) or int(p.get("channel_id",0))!=int(data.get("resolve_channel",0)):
            await state.clear(); return await m.answer("This resolution request expired. Open the preview and try again.",reply_markup=K.main())
        text=m.text or ''; import re
        msgid=await tg.validate_message_link(m.from_user.id,data['resolve_channel'],text)
        if not msgid: return await m.answer("Could not verify this message link in the selected channel. Wrong-channel or inaccessible links are rejected.")
        jid=data["resolve_job"]
        await save_resolution(m.from_user.id,data,msgid); await state.clear()
        updated=await db.previews.find_one({"user_id":m.from_user.id,"job_id":jid}) or {}
        buttons=[]
        if updated.get("missing"): buttons.append([('🔧 Resolve Another',f'resolve:{jid}')])
        buttons.extend([[('👁 Back to Preview',f'preview:{jid}')],[('🏠 Main Menu','home')]])
        await m.answer("Link resolved successfully.",reply_markup=K.kb(buttons))
    async def save_resolution(uid,data,msgid):
        label=data["resolve_label"]; cid=int(data["resolve_channel"]); now=datetime.now(timezone.utc)
        await db.manual_resolutions.update_one({"user_id":uid,"channel_id":cid,"entry_key":label},{"$set":{"user_id":uid,"channel_id":cid,"entry_key":label,"message_id":int(msgid),"valid":True,"updated_at":now}},upsert=True)
        async for p in db.previews.find({"user_id":uid,"channel_id":cid,"job_id":data.get("resolve_job")}):
            entries=p.get("entries",[]); changed=False
            for entry in entries:
                if entry.get("label")==label:
                    entry["message_id"]=int(msgid); entry["resolved"]=True; changed=True
            if not changed: continue
            objects=[IndexEntry(e["label"],e.get("key", ""),e.get("message_id"),e.get("resolved",False)) for e in entries]
            missing=[e.label for e in objects if not e.message_id]
            registered=await db.registered_channels.find_one({"user_id":uid,"channel_id":cid}) or {}
            username=p.get("channel_username") or registered.get("username")
            if p.get("mode")=="manual":
                job=await owned_job(uid,p["job_id"]) or {}
                rendered,_=render_manual_template(job.get("template", ""),objects,cid,username)
            else:
                rendered,_=render_automatic_index(objects,cid,username=username)
            if missing: rendered += "\n\n<b>Unresolved entries</b>\n"+"\n".join(f"• {html.escape(x)}" for x in missing)
            uncertain=p.get("uncertain_captions",[])
            if uncertain: rendered += "\n\n<b>Captions needing review</b>\n"+"\n".join(f"• {html.escape(x)}" for x in uncertain[:10])
            await db.previews.update_one({"_id":p["_id"]},{"$set":{"entries":[e.__dict__ for e in objects],"missing":missing,"html":rendered,"empty":not bool(objects)}})
            await db.indexing_jobs.update_one({"user_id":uid,"job_id":p["job_id"]},{"$set":{"unresolved":len(missing),"categories_found":len(objects),"status":"Preview Ready" if objects else "Waiting for Resolution"},"$unset":{"approval_token":"","approval_expires_at":""}})

    @dp.callback_query(F.data.startswith('regenerate:'))
    async def regenerate(c:CallbackQuery):
        jid=c.data.split(':',1)[1]; job=await owned_job(c.from_user.id,jid)
        if not job or not await require_premium(c.from_user.id): return await c.answer("Job not found or premium inactive.",show_alert=True)
        if job.get("status") in {"Scanning","Queued","Publishing"}: return await c.answer("This job is currently active.",show_alert=True)
        if job.get("status")=="Publish Review Required" or job.get("partial_message_ids"):
            return await c.answer("This channel has an unresolved partial publish. Ask the owner to inspect it before regenerating or publishing again.",show_alert=True)
        if not await db.registered_channels.find_one({"user_id":c.from_user.id,"channel_id":job["channel_id"]}): return await c.answer("Channel is no longer registered.",show_alert=True)
        try:
            new_jid = await create_job(
                c.from_user.id,
                job["channel_id"],
                job["channel_title"],
                job["mode"],
                job.get("template", ""),
                c.message.chat.id,
            )
        except RuntimeError as e:
            log.warning(
                "Regeneration enqueue rejected for user=%s channel=%s: %s",
                c.from_user.id, job.get("channel_id"), e,
            )
            return await c.answer(str(e), show_alert=True)
        except Exception:
            log.exception(
                "Could not enqueue regeneration for user=%s channel=%s",
                c.from_user.id, job.get("channel_id"),
            )
            return await c.answer(
                "Could not create the regeneration job. The actual error has been logged.",
                show_alert=True,
            )
        if job.get("status") in {"Preview Ready","Waiting for Resolution"}:
            await db.indexing_jobs.update_one({"user_id":c.from_user.id,"job_id":jid,"status":job.get("status")},
                {"$set":{"status":"Cancelled","cancel_reason":"superseded_by_regeneration","superseded_by":new_jid},
                 "$unset":{"approval_token":"","approval_expires_at":""}})
            await db.previews.delete_one({"user_id":c.from_user.id,"job_id":jid})
        else:
            await db.indexing_jobs.update_one({"user_id":c.from_user.id,"job_id":jid},{"$unset":{"approval_token":"","approval_expires_at":""}})
        await c.message.answer("Regenerating index using the original template and saved resolutions…")
        spawn_background(do_scan(c.message,c.from_user.id,job['channel_id'],job['channel_title'],job['mode'],job.get('template',''),new_jid)); await c.answer()
    @dp.callback_query(F.data.startswith('approve:'))
    async def approve(c:CallbackQuery):
        jid=c.data.split(':',1)[1]
        p=await db.previews.find_one({"user_id":c.from_user.id,"job_id":jid})
        job=await owned_job(c.from_user.id,jid)
        if not p or not job or preview_expired(p): return await c.answer("Preview expired or unavailable.",show_alert=True)
        if not await require_premium(c.from_user.id): return await c.answer("Premium is not active.",show_alert=True)
        if job.get("status") not in {"Preview Ready", "Waiting for Resolution"} or p.get("empty") or not p.get("entries"): return await c.answer("Only a non-empty ready preview can be published.",show_alert=True)
        if p.get("mode") == "source_target":
            if not p.get("target_index_id"):
                return await c.answer("Existing target index message is missing. Recreate the pair with its target index link.",show_alert=True)
            await c.message.answer(
                "<b>Update Existing Target Index</b>\nThe bot will edit the target index message you selected. It will not create a duplicate index post.",
                reply_markup=K.kb([[ ("✏️ Update Existing Target Index",f"approve_dest:{jid}:target_index")],
                                   [("⬅️ Back to Preview",f"preview:{jid}")]]))
        else:
            await c.message.answer("<b>Choose publishing destination</b>\nYour links will still point to the original channel messages.",
                reply_markup=K.kb([[ ("📢 Publish in Source Channel · Default",f"approve_dest:{jid}:channel")],
                                   [("📩 Send to My Private Chat",f"approve_dest:{jid}:private")],
                                   [("⬅️ Back to Preview",f"preview:{jid}")]]))
        await c.answer()

    @dp.callback_query(F.data.startswith('approve_dest:'))
    async def approve_destination(c:CallbackQuery):
        parts=c.data.split(':')
        if len(parts)!=3 or parts[2] not in {"channel","private","target_index"}: return await c.answer("Invalid destination.",show_alert=True)
        _,jid,destination=parts
        p=await db.previews.find_one({"user_id":c.from_user.id,"job_id":jid})
        job=await owned_job(c.from_user.id,jid)
        if not p or not job or preview_expired(p) or job.get("status") not in {"Preview Ready", "Waiting for Resolution"}: return await c.answer("Preview unavailable.",show_alert=True)
        if not await require_premium(c.from_user.id): return await c.answer("Premium is not active.",show_alert=True)
        if p.get("empty") or not p.get("entries"): return await c.answer("An empty index cannot be published.",show_alert=True)
        if destination == "target_index" and (p.get("mode") != "source_target" or not p.get("target_index_id")):
            return await c.answer("Existing target index is not configured for this preview.",show_alert=True)
        ch=await db.registered_channels.find_one({"user_id":c.from_user.id,"channel_id":p["channel_id"]})
        if not ch: return await c.answer("Channel registration missing.",show_alert=True)
        nonce=secrets.token_urlsafe(9); expires_at=datetime.now(timezone.utc)+timedelta(minutes=10)
        await db.indexing_jobs.update_one({"user_id":c.from_user.id,"job_id":jid,"status":{"$in":["Preview Ready","Waiting for Resolution"]}},
            {"$set":{"approval_token":nonce,"approval_expires_at":expires_at,"approval_destination":destination}})
        sig=approval_signature(cfg.app_secret,c.from_user.id,jid,nonce)
        target=("the existing target index message" if destination=="target_index" else "the source channel" if destination=="channel" else "your private chat")
        warning=f"\\n\\n⚠️ {len(p.get('missing',[]))} unresolved row(s) will remain unlinked." if p.get("missing") else ""
        await c.message.answer(f"<b>Confirm Publishing</b>\\nPublish this index to <b>{target}</b>?{warning}\\nConfirmation expires in 10 minutes.",
            reply_markup=K.kb([[("🚀 Yes, Publish Index",f"publish:{jid}:{destination}:{sig}")],[("❌ Cancel","home")]]))
        await c.answer()

    @dp.callback_query(F.data.startswith('publish:'))
    async def publish(c:CallbackQuery):
        parts=c.data.split(':')
        if len(parts)!=4: return await c.answer("Invalid or expired confirmation.",show_alert=True)
        _,jid,destination,sig=parts
        if destination not in {"channel","private","target_index"}: return await c.answer("Invalid destination.",show_alert=True)
        p=await db.previews.find_one({"user_id":c.from_user.id,"job_id":jid}); job=await owned_job(c.from_user.id,jid)
        now=datetime.now(timezone.utc)
        if not p or not job or job.get("status") not in {"Preview Ready", "Waiting for Resolution"} or int(p.get("channel_id",0))!=int(job.get("channel_id",-1)): return await c.answer("Preview unavailable or already processed.",show_alert=True)
        if not await require_premium(c.from_user.id): return await c.answer("Premium is not active.",show_alert=True)
        nonce=job.get("approval_token"); expiry=job.get("approval_expires_at")
        if job.get("approval_destination") != destination: return await c.answer("Publishing destination changed. Approve again.",show_alert=True)
        if not nonce or not expiry or expired(expiry,now) or not confirmation_matches(cfg.app_secret,sig,c.from_user.id,jid,nonce):
            return await c.answer("Confirmation expired. Approve the preview again.",show_alert=True)
        if preview_expired(p,now): return await c.answer("Preview expired. Regenerate the index.",show_alert=True)
        if p.get("empty") or not p.get("entries") or not (p.get("html") or "").strip(): return await c.answer("Preview validation failed; an empty index cannot be published.",show_alert=True)
        ch=await db.registered_channels.find_one({"user_id":c.from_user.id,"channel_id":p["channel_id"]})
        if not ch: return await c.answer("Channel registration missing.",show_alert=True)
        if destination in {"channel", "target_index"}:
            if destination == "channel" and (ch.get("publish_hold") or await db.publish_holds.find_one({"user_id":c.from_user.id,"channel_id":p["channel_id"]})):
                return await c.answer("Publishing is paused for this channel because a previous partial publish needs owner review.",show_alert=True)
            try: channel_access=await tg.can_access_channel(c.from_user.id,p["channel_id"])
            except Exception: return await c.answer("Could not verify connected Telegram account access. Try again.",show_alert=True)
            if not channel_access: return await c.answer("The connected Telegram account can no longer access this channel.",show_alert=True)
            if destination == "channel":
                try:
                    member=await bot.get_chat_member(p['channel_id'],bot_me.id)
                    if member.status not in ('administrator','creator') or (member.status=='administrator' and not getattr(member,'can_post_messages',False)):
                        return await c.answer("Publishing bot must be a channel admin with post permission.",show_alert=True)
                except Exception:
                    return await c.answer("Could not verify publishing permissions. Check bot channel access.",show_alert=True)
        elif destination == "private":
            try: await bot.send_chat_action(c.from_user.id,"typing")
            except Exception: return await c.answer("Open the bot's private chat and press Start before private publishing.",show_alert=True)
        claimed=await claim_publish(db.indexing_jobs,c.from_user.id,jid,nonce,now)
        if not claimed: return await c.answer("This job is already publishing or confirmation is stale.",show_alert=True)
        try: await c.answer("Publishing started.")
        except Exception: pass
        sent_messages=[]; publish_committed=False
        target_chat_id=int(p["channel_id"]) if destination in {"channel", "target_index"} else int(c.from_user.id)
        try:
            if destination == "target_index":
                target_index_id=int(p.get("target_index_id") or 0)
                if not target_index_id:
                    raise ValueError("target_index_id_missing")
                if len(p["html"]) > 12000:
                    raise ValueError("index_html_too_large_for_single_target_message")
                edited=await tg.edit_channel_message(c.from_user.id,p["channel_id"],target_index_id,p["html"])
                if not edited:
                    raise RuntimeError("target_index_edit_failed")
                committed=await commit_published(db.indexing_jobs,c.from_user.id,jid,[target_index_id],datetime.now(timezone.utc))
                if not committed:
                    raise RuntimeError("publish_state_commit_failed")
                publish_committed=True
                target_link=(f"https://t.me/{ch['username']}/{target_index_id}" if ch.get('username') else f"https://t.me/c/{str(p['channel_id']).removeprefix('-100')}/{target_index_id}")
                await c.message.answer(f"<b>Existing Target Index Updated Successfully!</b>\nChannel: {html.escape(ch['title'])}\nUpdated message: <a href=\"{target_link}\">Open updated index</a>",reply_markup=K.main())
                return
            chunks=split_html(p["html"],3900)
            for index,chunk in enumerate(chunks):
                # A crash after send but before the next DB write is treated as unknown,
                # never retried automatically (Bot API has no idempotency key).
                retry_attempt=0
                while True:
                    inflight_result=await mark_chunk_inflight(db.indexing_jobs,c.from_user.id,jid,index)
                    if inflight_result.modified_count!=1: raise RuntimeError("publish_inflight_state_lost")
                    try:
                        sent=await bot.send_message(target_chat_id,chunk,disable_web_page_preview=True)
                        break
                    except TelegramRetryAfter as rate_error:
                        retry_attempt+=1
                        await clear_chunk_inflight(db.indexing_jobs,c.from_user.id,jid)
                        if retry_attempt>=5: raise
                        await asyncio.sleep(max(1,int(rate_error.retry_after)+1))
                sent_messages.append(sent)
                recorded=await record_chunk(db.indexing_jobs,c.from_user.id,jid,index,sent.message_id)
                if recorded.modified_count!=1: raise RuntimeError("publish_chunk_record_failed")
            first=sent_messages[0]; published_at=datetime.now(timezone.utc)
            committed=await commit_published(db.indexing_jobs,c.from_user.id,jid,[x.message_id for x in sent_messages],published_at)
            if not committed: raise RuntimeError("publish_state_commit_failed")
            publish_committed=True
            if destination=="channel":
                published_link=(f"https://t.me/{ch['username']}/{first.message_id}" if ch.get('username') else f"https://t.me/c/{str(p['channel_id']).removeprefix('-100')}/{first.message_id}")
                delivery=f'Channel: {html.escape(ch["title"])}\\nPublished message: <a href="{published_link}">Open index</a>' 
            else:
                delivery=f"Destination: Your private chat\\nPublished messages: {len(sent_messages)}"
            await c.message.answer(f"<b>Navigation Index Published Successfully!</b>\\n{delivery}\\nPublished at: {published_at.isoformat()}",reply_markup=K.main())
        except Exception as e:
            if publish_committed:
                log.warning("Index published for job %s but user notification failed: %s",jid,type(e).__name__)
                try: await c.message.answer("Index was published; open My Indexing Jobs for its published link.")
                except Exception: pass
                return
            current=await owned_job(c.from_user.id,jid) or {}
            # If a send was in flight, delivery may have happened even if its response was lost.
            unknown=current.get("publish_inflight_index") is not None
            partial=bool(current.get("publish_chunks")) or bool(sent_messages) or unknown
            await mark_publish_failure(db.indexing_jobs,c.from_user.id,jid,partial=partial,error=type(e).__name__)
            if partial and destination=="channel":
                hold={"user_id":c.from_user.id,"channel_id":p["channel_id"],"job_id":jid,"created_at":datetime.now(timezone.utc)}
                await db.publish_holds.update_one({"user_id":c.from_user.id,"channel_id":p["channel_id"]},{"$set":hold},upsert=True)
                await db.registered_channels.update_one({"user_id":c.from_user.id,"channel_id":p["channel_id"]},{"$set":{"publish_hold":jid,"publish_hold_at":datetime.now(timezone.utc)}})
            log.warning("Publish failed for job %s: %s",jid,type(e).__name__)
            error_text=redact_error(e)
            await c.message.answer(("Publishing outcome requires review. Some messages may have been posted; automatic retry is blocked to prevent duplicates. Check the channel and contact the owner before creating a new publish job.\nError: " if partial else "Publishing failed before any message was sent. You can approve the preview again after checking channel permissions.\nError: ")+error_text)

    async def show_jobs(target, uid, page=0):
        rows,page,pages,total=await db.user_page("indexing_jobs",uid,page,8,"created_at",True)
        if not rows: return await target.edit_text("No indexing jobs yet.",reply_markup=K.back())
        buttons=[[(f"{r['channel_title'][:24]} · {r['status']}",f'job:{r["job_id"]}')] for r in rows]
        nav=[]
        if page>0: nav.append(("⬅️ Previous",f"jobs_page:{page-1}"))
        if page+1<pages: nav.append(("Next ➡️",f"jobs_page:{page+1}"))
        if nav: buttons.append(nav)
        buttons.append([('⬅️ Back','home')])
        await target.edit_text(f"<b>My Indexing Jobs</b> · Page {page+1}/{pages} · {total} total",reply_markup=K.kb(buttons))

    @dp.callback_query(F.data=="jobs")
    async def jobs(c:CallbackQuery):
        await show_jobs(c.message,c.from_user.id,0); await c.answer()

    @dp.callback_query(F.data.startswith('jobs_page:'))
    async def jobs_page(c:CallbackQuery):
        await show_jobs(c.message,c.from_user.id,int(c.data.split(':',1)[1])); await c.answer()
    @dp.callback_query(F.data.startswith('job:'))
    async def job_detail(c:CallbackQuery):
        jid=c.data.split(':',1)[1]; j=await owned_job(c.from_user.id,jid)
        if not j: return await c.answer("Job not found.",show_alert=True)
        await c.message.answer(f"Channel: {html.escape(j['channel_title'])}\nMode: {j['mode']}\nStatus: {j['status']}\nMessages scanned: {j.get('messages_scanned',j.get('total_scanned',0))}\nCategories: {j.get('categories_found',0)}\nUnresolved: {j.get('unresolved',0)}\nAttempts: {j.get('attempts',0)}\nCreated: {j['created_at']}",reply_markup=K.kb([[('👁 View Details',f'preview:{jid}')],[('🔄 Regenerate',f'regenerate:{jid}')],[('⬅️ Back','jobs')]])); await c.answer()
    @dp.callback_query(F.data=="account")
    async def account(c:CallbackQuery):
        uid=c.from_user.id; user=await db.users.find_one({"user_id":uid}) or {}; sub=await db.premium_subscriptions.find_one({"user_id":uid}); session=await db.telegram_sessions.find_one({"user_id":uid}); cc=await db.registered_channels.count_documents({"user_id":uid}); jc=await db.indexing_jobs.count_documents({"user_id":uid})
        connected_label="No"
        if session:
            try: connected_label="Yes" if await tg.client_for(uid) else "Invalid/revoked"
            except Exception: connected_label="Unable to verify"
        await c.message.edit_text(f"<b>My Account</b>\nTelegram user ID: <code>{uid}</code>\nPremium: {'Active' if await require_premium(uid) else 'Inactive'}\nPremium expiry: {sub.get('expires_at') if sub else 'N/A'}\nConnected account: {connected_label}\nRegistered channels: {cc}\nIndexing jobs: {jc}",reply_markup=K.kb([[('🔗 Connect Telegram Account','connect')],[('📡 My Channels','channels')],[('🔁 Source-to-Target Index','st_home')],
        [('📂 My Indexing Jobs','jobs')],[('🏠 Main Menu','home')]])); await c.answer()
    @dp.callback_query(F.data=="help")
    async def help(c:CallbackQuery):
        await c.message.edit_text("<b>How to use JARVIS Index Bot</b>\n1. Connect Telegram account.\n2. Register a Telegram channel.\n3. Add this bot as channel administrator with posting permission.\n4. Select Create Index.\n5. Choose Manual Template Mode or Automatic Topic Mode.\n6. Wait for scanning.\n7. Resolve missing links.\n8. Review preview.\n9. Approve publishing.",reply_markup=K.kb([[('🔗 Connect Account','connect')],[('➕ Add Channel','add_channel')],[('📋 Create Index','create')],[('🏠 Main Menu','home')]])); await c.answer()

    @dp.message()
    async def fallback_message(m:Message):
        await m.answer("Use the menu buttons to continue, or /start to return to the main menu.")

    # A pending OTP/2FA client exists only in process memory. Reset auth FSM states
    # after restart; the user must start a fresh login rather than submit stale OTP.
    await db.db.fsm_states.update_many({"state":{"$in":["Flow:phone","Flow:otp","Flow:twofa"]}},
        {"$set":{"state":None,"data":{},"expires_at":datetime.now(timezone.utc)+timedelta(days=7)}})
    # Backfill active-job uniqueness for jobs created by v2 before the active_key index.
    async for legacy_active in db.indexing_jobs.find({"status":{"$in":["Queued","Scanning"]},"active_key":{"$exists":False}}).sort("created_at",1):
        try:
            await db.indexing_jobs.update_one({"_id":legacy_active["_id"],"active_key":{"$exists":False}},
                {"$set":{"active_key":f"{legacy_active['user_id']}:{legacy_active['channel_id']}"}})
        except Exception:
            await db.indexing_jobs.update_one({"_id":legacy_active["_id"]},{"$set":{"status":"Failed","error":"duplicate_active_job_migration"},"$unset":{"active_key":""}})
    # Recover interrupted scans. A process crash during a Bot API send is ambiguous
    # because Telegram sendMessage has no idempotency key; never retry it blindly.
    await job_store.recover_interrupted()
    await recover_interrupted_publishes(db.indexing_jobs)
    # Carry forward v2 partial-publish records into the persistent channel hold store.
    async for legacy_partial in db.indexing_jobs.find({"partial_message_ids":{"$exists":True,"$ne":[]}}):
        await db.indexing_jobs.update_one({"_id":legacy_partial["_id"]},{"$set":{"status":"Publish Review Required","publish_review_required":True}})
    async for review_job in db.indexing_jobs.find({"$or":[{"status":"Publish Review Required"},{"partial_message_ids":{"$exists":True,"$ne":[]}}]}):
        hold={"user_id":review_job["user_id"],"channel_id":review_job["channel_id"],"job_id":review_job["job_id"],"created_at":datetime.now(timezone.utc)}
        await db.publish_holds.update_one({"user_id":review_job["user_id"],"channel_id":review_job["channel_id"]},{"$set":hold},upsert=True)
        await db.registered_channels.update_one({"user_id":review_job["user_id"],"channel_id":review_job["channel_id"]},
            {"$set":{"publish_hold":review_job["job_id"],"publish_hold_at":datetime.now(timezone.utc)}})
    await premium.expire_due()
    async for pending_job in db.indexing_jobs.find({"status":"Queued","cancel_requested":{"$ne":True}}).sort("created_at",1):
        uid=pending_job["user_id"]
        if not await require_premium(uid):
            await db.indexing_jobs.update_one({"_id":pending_job["_id"]},{"$set":{"status":"Failed","error":"premium_inactive"},"$unset":{"active_key":""}})
            continue
        try:
            resume_message=await bot.send_message(pending_job["chat_id"],"Resuming your interrupted JARVIS indexing job…")
            spawn_background(do_scan(resume_message,uid,pending_job["channel_id"],pending_job["channel_title"],
                                      pending_job["mode"],pending_job.get("template",""),pending_job["job_id"]))
        except Exception:
            log.exception("Could not notify user about recovered indexing job %s",pending_job.get("job_id"))

    try:
        log.info("JARVIS Index Bot started as @%s",bot_username)
        await dp.start_polling(bot)
    finally:
        for task in list(background_tasks): task.cancel()
        if background_tasks: await asyncio.gather(*background_tasks,return_exceptions=True)
        auth_cleanup_task.cancel()
        try: await auth_cleanup_task
        except asyncio.CancelledError: pass
        await tg.close()
        await dp.storage.close()
        db.close(); await bot.session.close()
        await stop_http_server(http_server, http_task)

if __name__=="__main__": asyncio.run(run())
