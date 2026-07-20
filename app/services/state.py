import logging
from sqlalchemy import select, func
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from app.config import get_settings
from app.db.session import SessionLocal
from app.db.models import Vote, TrackedMessage, ErrorLog
from app.services import settings as st
from app.utils.time import day_key, countdown_text, in_slot, slot_times
from app.keyboards.common import vote_kb

async def log_error(area,msg):
    logging.exception('%s: %s',area,msg) if isinstance(msg,Exception) else logging.error('%s: %s',area,msg)
    try:
        async with SessionLocal() as db:
            db.add(ErrorLog(area=area,message=str(msg)[:2000])); await db.commit()
    except Exception: pass

async def vote_count(chat_id:int):
    s=get_settings()
    async with SessionLocal() as db:
        res=await db.execute(select(func.count(Vote.id)).where(Vote.chat_id==chat_id, Vote.day_key==day_key(s.timezone)))
        return int(res.scalar() or 0)

async def add_vote(chat_id:int,user_id:int):
    s=get_settings()
    async with SessionLocal() as db:
        exists=await db.execute(select(Vote).where(Vote.chat_id==chat_id,Vote.user_id==user_id,Vote.day_key==day_key(s.timezone)))
        if exists.scalar_one_or_none(): return False
        db.add(Vote(chat_id=chat_id,user_id=user_id,day_key=day_key(s.timezone))); await db.commit(); return True

async def status_text(chat_id:int):
    goal=await st.vote_goal(); votes=await vote_count(chat_id); slot=await st.time_slot(); s=get_settings()
    opening=slot.split('-')[0]; closing=slot.split('-')[1]
    if not await st.auto_enabled():
        if await st.is_open():
            return '🟢 GROUPE OUVERT\n\nVous pouvez envoyer vos médias <3\n\nMode manuel : fermeture de sécurité active.'
        return '🔴 MAINTENANCE\n\nLe système est en maintenance ce soir.\n\nAucune ouverture prévue.'
    if await st.is_open():
        return f'🟢 GROUPE OUVERT\n\nObjectif atteint : {votes} / {goal} ✅\n\nVous pouvez envoyer vos médias <3\n\nFermeture prévue à {closing}.'
    missing=max(goal-votes,0)
    achieved=votes>=goal
    if achieved:
        if in_slot(slot,s.timezone):
            return f'🟢 OBJECTIF ATTEINT\n\nLe groupe est maintenant ouvert.\n\nFermeture prévue à {closing}.\n\nVous pouvez envoyer vos médias <3'
        remaining=countdown_text(slot,s.timezone,achieved=True)
        if remaining == 'maintenant':
            return '🟢 OBJECTIF ATTEINT\n\nOuverture en cours...'
        return f'🟡 OBJECTIF ATTEINT\n\nLe groupe ouvrira automatiquement à {opening}.\n\nOuverture dans : {remaining}\n\nObjectif :\n{votes} / {goal} votes ✅\n\nPréparez vos médias.'
    remaining=countdown_text(slot,s.timezone,achieved=False)
    return f'🔴 GROUPE FERMÉ\n\nOuverture prévue à {opening}.\nTemps restant : {remaining}\n\nObjectif :\n{votes} / {goal} votes\n\nIl manque encore {missing} votes.'

async def track(chat_id:int,message_id:int,user_id:int|None,kind='message',is_media=False):
    async with SessionLocal() as db:
        sid=int(await st.get_value('active_session_id','0') or '0')
        existing=(await db.execute(select(TrackedMessage).where(TrackedMessage.chat_id==chat_id,TrackedMessage.message_id==message_id))).scalar_one_or_none()
        if not existing:
            db.add(TrackedMessage(chat_id=chat_id,message_id=message_id,user_id=user_id,session_id=sid,kind=kind,is_media=is_media))
            if sid and kind!='status':
                from app.db.models import SessionLog
                sess=await db.get(SessionLog,sid)
                if sess:
                    sess.messages_seen += 1
                    if is_media: sess.media_seen += 1
        await db.commit()

async def ensure_status_message(bot:Bot, chat_id:int, recreate_on_change:bool=False):
    """Maintient le message principal.

    - Par défaut: édite le message existant, utile pour les votes instantanés.
    - recreate_on_change=True: si le texte a changé depuis la dernière version,
      supprime l'ancien message et en publie un nouveau. C'est utilisé par le
      scheduler aux paliers de compte à rebours pour que l'heure Telegram visible
      du message change vraiment.
    """
    text=await status_text(chat_id)
    mid=await st.get_value('status_message_id','')
    last_text=await st.get_value('status_last_text','')
    kb=None if await st.is_open() or not await st.auto_enabled() else vote_kb()

    if mid and recreate_on_change and last_text and text != last_text:
        try:
            await bot.delete_message(chat_id,int(mid))
            async with SessionLocal() as db:
                res=await db.execute(select(TrackedMessage).where(TrackedMessage.chat_id==chat_id,TrackedMessage.message_id==int(mid)))
                tm=res.scalar_one_or_none()
                if tm: tm.deleted=True
                await db.commit()
        except Exception as e:
            await log_error('delete_old_status',e)
        mid=''

    if mid:
        try:
            await bot.edit_message_text(text, chat_id=chat_id, message_id=int(mid), reply_markup=kb)
            from datetime import datetime
            await st.set_value('last_status_update_at', datetime.utcnow().isoformat(timespec='seconds'))
            await st.set_value('status_last_text', text)
            return int(mid)
        except TelegramBadRequest as e:
            low=str(e).lower()
            if 'message is not modified' in low:
                return int(mid)
            if 'message to edit not found' not in low:
                await log_error('edit_status',e)
        except Exception as e:
            await log_error('edit_status',e)

    m=await bot.send_message(chat_id,text,reply_markup=kb)
    await st.set_value('status_message_id',str(m.message_id))
    await st.set_value('status_last_text', text)
    from datetime import datetime
    await st.set_value('last_status_update_at', datetime.utcnow().isoformat(timespec='seconds'))
    await track(chat_id,m.message_id,None,'status',False)
    await cleanup_known_status_duplicates(bot, chat_id)
    return m.message_id

async def cleanup_known_status_duplicates(bot:Bot, chat_id:int):
    keep=int(await st.get_value('status_message_id','0') or '0')
    async with SessionLocal() as db:
        res=await db.execute(select(TrackedMessage).where(TrackedMessage.chat_id==chat_id,TrackedMessage.kind=='status',TrackedMessage.deleted==False))
        for tm in res.scalars().all():
            if tm.message_id!=keep:
                try: await bot.delete_message(chat_id,tm.message_id); tm.deleted=True
                except Exception: pass
        await db.commit()
