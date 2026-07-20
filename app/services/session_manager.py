from __future__ import annotations
from datetime import datetime, timedelta
from aiogram import Bot
from aiogram.types import ChatPermissions
from sqlalchemy import select
from app.config import get_settings
from app.db.session import SessionLocal
from app.db.models import Session, TrackedMessage, GroupState
from app.services.messages import ensure_status_message, safe_delete
from app.services.state import mark_open

settings=get_settings()
OPEN_PERMS=ChatPermissions(can_send_messages=True, can_send_audios=True, can_send_documents=True, can_send_photos=True, can_send_videos=True, can_send_video_notes=False, can_send_voice_notes=True, can_send_polls=False, can_send_other_messages=False, can_add_web_page_previews=False)
CLOSED_PERMS=ChatPermissions(can_send_messages=False)

async def open_group(bot:Bot, chat_id:int, kind:str='auto'):
    async with SessionLocal() as db:
        sess=Session(chat_id=chat_id, opened_at=datetime.utcnow(), kind=kind)
        db.add(sess); await db.commit(); await db.refresh(sess)
    try: await bot.set_chat_permissions(chat_id, OPEN_PERMS)
    except Exception: pass
    await mark_open(chat_id, True, manual=(kind=='manual'), session_id=sess.id)
    await ensure_status_message(bot, chat_id)
    return sess.id

async def close_group(bot:Bot, chat_id:int, reason:str='auto'):
    try: await bot.set_chat_permissions(chat_id, CLOSED_PERMS)
    except Exception: pass
    async with SessionLocal() as db:
        st=await db.get(GroupState, chat_id)
        sid=st.current_session_id if st else None
        if sid:
            sess=await db.get(Session, sid)
            if sess: sess.closed_at=datetime.utcnow()
        await db.commit()
    await cleanup_session(bot, chat_id)
    await mark_open(chat_id, False)
    await ensure_status_message(bot, chat_id)
    await send_session_report(bot, chat_id)

async def cleanup_session(bot:Bot, chat_id:int):
    async with SessionLocal() as db:
        st=await db.get(GroupState, chat_id)
        sid=st.current_session_id if st else None
        q=select(TrackedMessage).where(TrackedMessage.chat_id==chat_id, TrackedMessage.deleted==False)
        if sid: q=q.where(TrackedMessage.session_id==sid)
        rows=(await db.execute(q)).scalars().all()
        deleted=0
        for r in rows:
            ok=await safe_delete(bot, r.chat_id, r.message_id)
            if ok:
                r.deleted=True; deleted+=1
        if sid:
            sess=await db.get(Session, sid)
            if sess: sess.messages_deleted += deleted
        await db.commit()
    # double check: Telegram can't list chat history, so DB-based check + admin alert if failed rows remain.
    async with SessionLocal() as db:
        remain=(await db.execute(select(TrackedMessage).where(TrackedMessage.chat_id==chat_id, TrackedMessage.deleted==False))).scalars().all()
    if remain:
        await notify_admins(bot, f'🚨 ERREUR NETTOYAGE\n\nMessages restants dans la base : {len(remain)}\n\nRelance nettoyage conseillée.')

async def notify_admins(bot:Bot, text:str):
    for aid in settings.admin_ids:
        try: await bot.send_message(aid, text)
        except Exception: pass

async def send_session_report(bot:Bot, chat_id:int):
    async with SessionLocal() as db:
        sess=(await db.execute(select(Session).where(Session.chat_id==chat_id).order_by(Session.id.desc()).limit(1))).scalar_one_or_none()
    if not sess: return
    text=f'📊 RAPPORT DE SESSION\n\nType : {sess.kind}\nMessages suivis : {sess.messages_seen}\nMédias suivis : {sess.media_seen}\nMessages supprimés : {sess.messages_deleted}\nFermeture : OK'
    await notify_admins(bot, text)
