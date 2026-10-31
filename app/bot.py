import asyncio, logging, uuid, html, secrets
from datetime import datetime, timezone, timedelta
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery
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
from .indexer import render_automatic_index
from .jobs import JobStore
from .publishing import approval_signature, confirmation_matches, claim_publish, mark_chunk_inflight, clear_chunk_inflight, record_chunk, commit_published, mark_publish_failure, recover_interrupted_publishes

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log=logging.getLogger("jarvis")

class Flow(StatesGroup):
    phone=State(); otp=State(); twofa=State(); channel=State(); template=State(); resolve_link=State(); resolve_forward=State(); grant=State()

async def run():
    cfg=Settings.from_env(); db=Database(cfg.mongo_uri,cfg.mongo_db_name); await db.initialize()
    bot=Bot(cfg.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML)); dp=Dispatcher(storage=MongoFSMStorage(db.db.fsm_states))
    cipher=SessionCipher(cfg.session_encryption_key); tg=UserTelegram(cfg.api_id,cfg.api_hash,cipher,db); premium=Premium(db); job_store=JobStore(db.indexing_jobs)
    bot_me=await bot.get_me(); bot_username=bot_me.username

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
    async def home_message(target, text="Welcome to <b>JARVIS Index Bot</b>.\n\nCreate professional Telegram channel navigation indexes automatically."):
        await target.answer(text, reply_markup=K.main())

    @dp.message(CommandStart())
    async def start(m:Message, state:FSMContext):
        await tg.cancel_login(m.from_user.id); await invalidate_approvals(m.from_user.id)
        await state.clear(); await db.users.update_one({"user_id":m.from_user.id},{"$set":{"user_id":m.from_user.id,"username":m.from_user.username,"last_seen":datetime.now(timezone.utc)}},upsert=True)
        await home_message(m)

    @dp.message(Command("admin"))
    async def admin(m:Message):
        if m.from_user.id!=cfg.owner_id: return await m.answer("Not authorized.")
        await m.answer("Admin commands:\n/grant USER_ID DAYS\n/revoke USER_ID\n/premium USER_ID\n/premium_list [PAGE]\n/publish_review\n/clear_publish_hold USER_ID CHANNEL_ID")
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
            return await target.edit_text("No registered channels yet.",reply_markup=K.kb([[('➕ Add Channel','add_channel')],[('⬅️ Back','home')]]))
        buttons=[[(('📚 ' if prefix=='choose' else '')+x['title'][:48],f"{prefix}:{x['channel_id']}")] for x in rows]
        nav=[]
        if page>0: nav.append(("⬅️ Previous",f"pickpage:{prefix}:{page-1}"))
        if page+1<pages: nav.append(("Next ➡️",f"pickpage:{prefix}:{page+1}"))
        if nav: buttons.append(nav)
        buttons.append([('⬅️ Back to Main Menu','home')])
        await target.edit_text(f"Select a channel · Page {page+1}/{pages} · {total} total",reply_markup=K.kb(buttons))

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
        await state.update_data(channel_id=cid,channel_title=ch['title']); await c.message.edit_text("<b>Choose Indexing Mode</b>",reply_markup=K.modes()); await c.answer()
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
        await state.clear()
        try: jid=await create_job(m.from_user.id,data['channel_id'],data['channel_title'],'manual',m.text or '',m.chat.id)
        except Exception:
            return await m.answer("A scan for this channel is already queued or running, or the job could not be created. Check My Indexing Jobs.")
        progress_message=await m.answer("Template received. Scanning channel history…")
        spawn_background(do_scan(progress_message,m.from_user.id,data['channel_id'],data['channel_title'],'manual',m.text or '',jid))
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
        try: jid=await create_job(c.from_user.id,data['channel_id'],data['channel_title'],'automatic','',c.message.chat.id)
        except Exception: return await c.answer("A scan for this channel is already queued or running.",show_alert=True)
        await c.message.edit_text("Scanning started. This may take a while for large channels…")
        spawn_background(do_scan(c.message,c.from_user.id,data['channel_id'],data['channel_title'],'automatic','',jid))
        await c.answer()

    async def create_job(uid,cid,title,mode,template,chat_id=None):
        if not await require_premium(uid): raise PermissionError("Premium access required")
        channel=await db.registered_channels.find_one({"user_id":uid,"channel_id":int(cid)})
        if not channel: raise PermissionError("Channel is not registered to this user")
        job_id=uuid.uuid4().hex[:12]
        if mode=="manual":
            await db.index_templates.update_one({"user_id":uid,"channel_id":int(cid)}, {"$set":{"user_id":uid,"channel_id":int(cid),"template":template,"updated_at":datetime.now(timezone.utc)}},upsert=True)
        await job_store.enqueue(user_id=uid,job_id=job_id,channel_id=int(cid),channel_title=title,mode=mode,template=template,chat_id=chat_id)
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
            builder=StreamingIndexBuilder(mode,template_text).restore(job.get("builder_state"))
            async def progress(batch_count,last_id):
                nonlocal scanned
                current=await db.indexing_jobs.find_one({"user_id":uid,"job_id":job_id})
                if not current or current.get('status')=='Cancelled' or current.get('cancel_requested'):
                    raise RuntimeError("Scan cancelled by user")
                if not await require_premium(uid):
                    raise PermissionError("Premium entitlement expired during scan")
                scanned=base_scanned+batch_count
                await job_store.checkpoint(uid,job_id,last_id,scanned,builder_state=builder.snapshot())
                try:
                    found,_,_=builder.result()
                    await message.edit_text(f"<b>Scanning Progress</b>\nChannel: {html.escape(title)}\nMode: {mode}\nMessages scanned: {scanned}\nCategories detected: {len(found)}\nUnresolved: {sum(1 for e in found if not e.message_id)}",
                        reply_markup=K.kb([[('❌ Cancel Scan',f'cancel_scan:{job_id}')]]))
                except Exception:
                    pass
            async for record in tg.iter_channel_messages(uid,cid,min_id=checkpoint,progress=progress):
                builder.add(record)
            current=await owned_job(uid,job_id)
            if not current or current.get("status")=="Cancelled" or current.get("cancel_requested"):
                raise RuntimeError("Scan cancelled by user")
            if not await require_premium(uid):
                raise PermissionError("Premium entitlement expired during scan")
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
            if mode=='manual':
                rendered,missing=render_manual_template(template_text,entries,cid,channel.get("username"))
            else:
                rendered,missing=render_automatic_index(entries,cid,username=channel.get("username"))
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
            final_status="Waiting for Resolution" if missing else "Preview Ready"
            result=await job_store.finish(uid,job_id,final_status,total_scanned=scanned,categories_found=len(entries),
                unresolved=len(missing),builder_state=None)
            if not result.modified_count:
                await db.previews.delete_one({"user_id":uid,"job_id":job_id})
                return await message.answer("Scan was cancelled before preview creation. No index was published.")
            await message.answer(f"<b>Scan Results</b>\nChannel: {html.escape(title)}\nMessages scanned: {scanned}\nCategories found: {len(entries)}\nUnresolved: {len(missing)}\nCaptions needing review: {len(uncertain)}",
                reply_markup=K.kb([[('👁 View Index Preview',f'preview:{job_id}')],[('🔧 Resolve Missing Links',f'resolve:{job_id}')],[('🔄 Regenerate Index',f'regenerate:{job_id}')],[('❌ Cancel',f'cancel_job:{job_id}')]]))
        except Exception as e:
            log.warning("Scan failed for job %s: %s",job_id,type(e).__name__)
            current=await owned_job(uid,job_id)
            if current and current.get('status')!='Cancelled':
                await job_store.finish(uid,job_id,"Failed",error=type(e).__name__)
            if current and current.get('status')=='Cancelled':
                await message.answer("Scan cancelled. No index was published.")
            else:
                await message.answer("Scanning failed. Check Telegram account access, channel permissions, premium status, and logs; no index was published.")

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

    @dp.callback_query(F.data.startswith('preview:'))
    async def preview(c:CallbackQuery):
        jid=c.data.split(':',1)[1]; p=await db.previews.find_one({"user_id":c.from_user.id,"job_id":jid}); job=await owned_job(c.from_user.id,jid)
        if not p or not job or preview_expired(p): return await c.answer("Preview not found or expired.",show_alert=True)
        if not await require_premium(c.from_user.id): return await c.answer("Premium is not active.",show_alert=True)
        # Split into safe Telegram-sized chunks.
        body=p['html']
        for chunk in split_html(body,3900):
            await c.message.answer(chunk,disable_web_page_preview=True)
        buttons=[]
        if job.get("status")=="Preview Ready" and not p.get("missing") and not p.get("empty") and p.get("entries"):
            buttons.append([('✅ Approve & Publish',f'approve:{jid}')])
        if p.get("missing"): buttons.append([('🔧 Resolve Missing Links',f'resolve:{jid}')])
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
            await db.indexing_jobs.update_one({"user_id":uid,"job_id":p["job_id"]},{"$set":{"unresolved":len(missing),"categories_found":len(objects),"status":"Waiting for Resolution" if missing else "Preview Ready"},"$unset":{"approval_token":"","approval_expires_at":""}})

    @dp.callback_query(F.data.startswith('regenerate:'))
    async def regenerate(c:CallbackQuery):
        jid=c.data.split(':',1)[1]; job=await owned_job(c.from_user.id,jid)
        if not job or not await require_premium(c.from_user.id): return await c.answer("Job not found or premium inactive.",show_alert=True)
        if job.get("status") in {"Scanning","Queued","Publishing"}: return await c.answer("This job is currently active.",show_alert=True)
        if job.get("status")=="Publish Review Required" or job.get("partial_message_ids"):
            return await c.answer("This channel has an unresolved partial publish. Ask the owner to inspect it before regenerating or publishing again.",show_alert=True)
        if not await db.registered_channels.find_one({"user_id":c.from_user.id,"channel_id":job["channel_id"]}): return await c.answer("Channel is no longer registered.",show_alert=True)
        try: new_jid=await create_job(c.from_user.id,job['channel_id'],job['channel_title'],job['mode'],job.get('template',''),c.message.chat.id)
        except Exception: return await c.answer("A scan for this channel is already queued or running.",show_alert=True)
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
        jid=c.data.split(':',1)[1]; p=await db.previews.find_one({"user_id":c.from_user.id,"job_id":jid}); job=await owned_job(c.from_user.id,jid)
        if not p or not job or preview_expired(p): return await c.answer("Preview expired or unavailable.",show_alert=True)
        if not await require_premium(c.from_user.id): return await c.answer("Premium is not active.",show_alert=True)
        if job.get("status")!="Preview Ready": return await c.answer("Only a ready preview can be approved.",show_alert=True)
        if p.get('missing'): return await c.answer("Resolve all missing links before publishing.",show_alert=True)
        if p.get("empty") or not p.get("entries") or not (p.get("html") or "").strip(): return await c.answer("An empty index cannot be published.",show_alert=True)
        ch=await db.registered_channels.find_one({"user_id":c.from_user.id,"channel_id":p['channel_id']})
        if not ch: return await c.answer("Channel registration missing.",show_alert=True)
        nonce=secrets.token_urlsafe(9); expires=datetime.now(timezone.utc)+timedelta(minutes=10)
        await db.indexing_jobs.update_one({"user_id":c.from_user.id,"job_id":jid,"status":"Preview Ready"},{"$set":{"approval_token":nonce,"approval_expires_at":expires}})
        sig=approval_signature(cfg.app_secret,c.from_user.id,jid,nonce)
        await c.message.answer(f"<b>Confirm Publishing</b>\nAre you sure you want to publish this navigation index in <b>{html.escape(ch['title'])}</b>?\nConfirmation expires in 10 minutes.",reply_markup=K.kb([[('🚀 Yes, Publish Index',f'publish:{jid}:{sig}')],[('❌ Cancel','home')]])); await c.answer()

    @dp.callback_query(F.data.startswith('publish:'))
    async def publish(c:CallbackQuery):
        parts=c.data.split(':')
        if len(parts)!=3: return await c.answer("Invalid or expired confirmation.",show_alert=True)
        _,jid,sig=parts; p=await db.previews.find_one({"user_id":c.from_user.id,"job_id":jid}); job=await owned_job(c.from_user.id,jid)
        now=datetime.now(timezone.utc)
        if not p or not job or job.get("status")!="Preview Ready" or int(p.get("channel_id",0))!=int(job.get("channel_id",-1)): return await c.answer("Preview unavailable or already processed.",show_alert=True)
        if not await require_premium(c.from_user.id): return await c.answer("Premium is not active.",show_alert=True)
        nonce=job.get("approval_token"); expiry=job.get("approval_expires_at")
        if not nonce or not expiry or expired(expiry,now) or not confirmation_matches(cfg.app_secret,sig,c.from_user.id,jid,nonce):
            return await c.answer("Confirmation expired. Approve the preview again.",show_alert=True)
        if preview_expired(p,now): return await c.answer("Preview expired. Regenerate the index.",show_alert=True)
        if p.get("missing") or p.get("empty") or not p.get("entries") or not (p.get("html") or "").strip(): return await c.answer("Preview validation failed; empty or unresolved indexes cannot be published.",show_alert=True)
        ch=await db.registered_channels.find_one({"user_id":c.from_user.id,"channel_id":p["channel_id"]})
        if not ch: return await c.answer("Channel registration missing.",show_alert=True)
        if ch.get("publish_hold") or await db.publish_holds.find_one({"user_id":c.from_user.id,"channel_id":p["channel_id"]}):
            return await c.answer("Publishing is paused for this channel because a previous partial publish needs owner review.",show_alert=True)
        # Validate Bot API permission before claiming the one-time publish transition.
        try: channel_access=await tg.can_access_channel(c.from_user.id,p["channel_id"])
        except Exception: return await c.answer("Could not verify connected Telegram account access. Try again.",show_alert=True)
        if not channel_access:
            return await c.answer("The connected Telegram account can no longer access this channel.",show_alert=True)
        try:
            member=await bot.get_chat_member(p['channel_id'],bot_me.id)
            if member.status not in ('administrator','creator') or (member.status=='administrator' and not getattr(member,'can_post_messages',False)):
                return await c.answer("Publishing bot must be a channel admin with post permission.",show_alert=True)
        except Exception:
            return await c.answer("Could not verify publishing permissions. Check bot channel access.",show_alert=True)
        claimed=await claim_publish(db.indexing_jobs,c.from_user.id,jid,nonce,now)
        if not claimed: return await c.answer("This job is already publishing or confirmation is stale.",show_alert=True)
        try: await c.answer("Publishing started.")
        except Exception: pass
        sent_messages=[]; publish_committed=False
        try:
            chunks=split_html(p["html"],3900)
            for index,chunk in enumerate(chunks):
                # A crash after send but before the next DB write is treated as unknown,
                # never retried automatically (Bot API has no idempotency key).
                retry_attempt=0
                while True:
                    inflight_result=await mark_chunk_inflight(db.indexing_jobs,c.from_user.id,jid,index)
                    if inflight_result.modified_count!=1: raise RuntimeError("publish_inflight_state_lost")
                    try:
                        sent=await bot.send_message(p['channel_id'],chunk,disable_web_page_preview=True)
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
            published_link=(f"https://t.me/{ch['username']}/{first.message_id}" if ch.get('username') else f"https://t.me/c/{str(p['channel_id']).removeprefix('-100')}/{first.message_id}")
            await c.message.answer(f"<b>Navigation Index Published Successfully!</b>\nChannel: {html.escape(ch['title'])}\nPublished message: <a href=\"{published_link}\">Open index</a>\nPublished at: {published_at.isoformat()}",reply_markup=K.main())
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
            if partial:
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
        await c.message.edit_text(f"<b>My Account</b>\nTelegram user ID: <code>{uid}</code>\nPremium: {'Active' if await require_premium(uid) else 'Inactive'}\nPremium expiry: {sub.get('expires_at') if sub else 'N/A'}\nConnected account: {connected_label}\nRegistered channels: {cc}\nIndexing jobs: {jc}",reply_markup=K.kb([[('🔗 Connect Telegram Account','connect')],[('📡 My Channels','channels')],[('📂 My Indexing Jobs','jobs')],[('🏠 Main Menu','home')]])); await c.answer()
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

if __name__=="__main__": asyncio.run(run())
