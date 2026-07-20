from datetime import datetime, timedelta
from sqlalchemy import select, func, update
from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from app.config import get_settings
from app.db.session import SessionLocal
from app.db.models import SessionLog, TrackedMessage, TrustedAction, User, ErrorLog
from app.services import settings as st
from app.services.state import ensure_status_message, log_error

OPEN_PERMS={'can_send_messages':True,'can_send_audios':True,'can_send_documents':True,'can_send_photos':True,'can_send_videos':True,'can_send_video_notes':False,'can_send_voice_notes':True,'can_send_polls':False,'can_send_other_messages':False,'can_add_web_page_previews':False}
CLOSED_PERMS={'can_send_messages':False}

async def set_group_open(bot:Bot, open_:bool, kind='auto'):
    s=get_settings()
    try: await bot.set_chat_permissions(s.main_group_id, permissions=OPEN_PERMS if open_ else CLOSED_PERMS)
    except Exception as e: await log_error('permissions',e)
    if open_:
        # éviter de recréer une session si déjà ouvert
        if await st.is_open():
            await ensure_status_message(bot,s.main_group_id); return
        async with SessionLocal() as db:
            sess=SessionLog(chat_id=s.main_group_id,kind=kind,status='open'); db.add(sess); await db.flush()
            await st.set_value('active_session_id',str(sess.id))
            # Justice/inactivité : une ouverture accessible compte comme une session
            # pour les membres connus, sauf admins/trusted/bannis. Telegram ne permet pas
            # de lister tous les membres, donc on suit les profils déjà connus par le bot.
            await db.execute(update(User).where(User.is_admin==False, User.is_trusted==False, User.is_banned==False).values(sessions_present=User.sessions_present+1))
            await db.commit()
        await st.set_open(True)
        await st.set_value('manual_opened_at', datetime.utcnow().isoformat() if kind=='manual' else '')
    else:
        if not await st.is_open():
            await ensure_status_message(bot,s.main_group_id); return
        sid=int(await st.get_value('active_session_id','0') or '0')
        await cleanup_session(bot, all_known=False)
        await close_active_session()
        await st.set_open(False)
        await send_report(bot, kind, sid=sid)
    await ensure_status_message(bot,s.main_group_id)

async def close_active_session():
    async with SessionLocal() as db:
        sid=int(await st.get_value('active_session_id','0') or '0')
        if sid:
            sess=await db.get(SessionLog,sid)
            if sess: sess.status='closed'; sess.closed_at=datetime.utcnow()
        await st.set_value('active_session_id','0')
        await st.set_value('manual_opened_at','')
        await db.commit()

async def cleanup_session(bot:Bot, all_known:bool=False):
    s=get_settings(); sid=int(await st.get_value('active_session_id','0') or '0')
    deleted=0; failed=0; media_failed=0
    async with SessionLocal() as db:
        q=select(TrackedMessage).where(TrackedMessage.chat_id==s.main_group_id,TrackedMessage.deleted==False,TrackedMessage.kind!='status')
        if sid and not all_known: q=q.where(TrackedMessage.session_id==sid)
        res=await db.execute(q)
        items=list(res.scalars().all())
        for tm in items:
            try:
                await bot.delete_message(tm.chat_id,tm.message_id); tm.deleted=True; deleted+=1
            except Exception as e:
                failed+=1
                if tm.is_media: media_failed+=1
                await log_error('cleanup_delete',f'{tm.chat_id}/{tm.message_id}: {e}')
        if sid:
            sess=await db.get(SessionLog,sid)
            if sess: sess.messages_deleted+=deleted
        await db.commit()
    if failed:
        await notify_admins(bot,f'🚨 ERREUR NETTOYAGE\n\nMessages non supprimés : {failed}\nMédias non supprimés : {media_failed}\n\nVérifie que le bot est admin avec droit “Supprimer les messages”, puis relance 🧹 Nettoyage.')
    return deleted, failed

async def notify_admins(bot:Bot,text:str, reply_markup=None):
    for aid in get_settings().admin_ids:
        try: await bot.send_message(aid,text,reply_markup=reply_markup)
        except Exception: pass

async def send_report(bot:Bot, kind='auto', sid:int|None=None):
    async with SessionLocal() as db:
        sess=None
        if sid: sess=await db.get(SessionLog,sid)
        if not sess:
            res=await db.execute(select(SessionLog).order_by(SessionLog.id.desc()).limit(1)); sess=res.scalar_one_or_none()
        actions=await db.execute(select(TrustedAction.trusted_username,TrustedAction.command,func.count(TrustedAction.id)).group_by(TrustedAction.trusted_username,TrustedAction.command))
        action_lines=[f'@{name or "trusted"} {cmd}: {c}' for name,cmd,c in actions.all()]
        inactive=await db.execute(select(func.count(User.id)).where(User.media_count==0)); inactive_count=inactive.scalar() or 0
        trusted_inactive=await db.execute(select(User).where(User.is_trusted==True).limit(20))
        trusted_lines=[]
        for u in trusted_inactive.scalars().all():
            last=(u.last_seen.strftime('%d/%m %H:%M') if u.last_seen else 'jamais')
            trusted_lines.append(f'@{u.username or u.full_name or "trusted"} — dernière activité {last}')
        errors=await db.execute(select(func.count(ErrorLog.id)).where(ErrorLog.created_at>=datetime.utcnow()-timedelta(hours=24))); err=errors.scalar() or 0
        remain=0
        if sid:
            remain=(await db.execute(select(func.count(TrackedMessage.id)).where(TrackedMessage.session_id==sid,TrackedMessage.deleted==False,TrackedMessage.kind!='status'))).scalar() or 0
    text=(f'📊 RAPPORT DE SESSION\n\nType : {kind}\n'
          f'Messages vus : {sess.messages_seen if sess else 0}\nMédias vus : {sess.media_seen if sess else 0}\n'
          f'Messages supprimés : {sess.messages_deleted if sess else 0}\nMessages restants suivis : {remain}\n\n'
          f'Inactifs jamais média : {inactive_count}\n\nActions trusted :\n' + ('\n'.join(action_lines[-20:]) or 'Aucune') +
          '\n\nTrusted inactifs / peu actifs :\n' + ('\n'.join(trusted_lines[:10]) or 'Aucun') + f'\n\nErreurs 24h : {err}')
    await notify_admins(bot,text)

async def security_close_if_manual(bot:Bot):
    if not await st.is_open() or await st.auto_enabled(): return
    opened=await st.get_value('manual_opened_at','')
    if not opened: return
    try: dt=datetime.fromisoformat(opened)
    except Exception: return
    if datetime.utcnow()-dt < timedelta(hours=2): return
    warned=await st.get_value('manual_security_warned_at','')
    if not warned:
        kb=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='✅ Maintenir ouvert',callback_data='manual_keep_open'),InlineKeyboardButton(text='🔒 Fermer',callback_data='manual_security_close')]])
        await st.set_value('manual_security_warned_at',datetime.utcnow().isoformat())
        await notify_admins(bot,'⚠️ FERMETURE DE SÉCURITÉ\n\nLe groupe est ouvert manuellement depuis 2h.\nRépondez OUI pour maintenir ouvert. Sans réponse sous 5 minutes : fermeture.',kb)
        return
    try: wdt=datetime.fromisoformat(warned)
    except Exception: wdt=datetime.utcnow()-timedelta(minutes=10)
    if datetime.utcnow()-wdt >= timedelta(minutes=5):
        await set_group_open(bot,False,'security')

async def count_known_bans_and_restrictions():
    async with SessionLocal() as db:
        banned=(await db.execute(select(func.count(User.id)).where(User.is_banned==True))).scalar() or 0
        restricted=(await db.execute(select(func.count(User.id)).where(User.is_restricted==True))).scalar() or 0
        return int(banned), int(restricted)

async def presidential_pardon(bot:Bot):
    async with SessionLocal() as db:
        res=await db.execute(select(User).where(User.is_banned==True))
        users=list(res.scalars().all()); count=len(users)
        for u in users:
            try: await bot.unban_chat_member(get_settings().main_group_id,u.id,only_if_banned=True)
            except Exception as e: await log_error('unban',e)
            u.is_banned=False
        await db.commit()
    if await st.is_open():
        await bot.send_message(get_settings().main_group_id,f'👑 GRÂCE PRÉSIDENTIELLE\n\n{count} bannissement(s) levé(s).\n\nNe confondez pas pardon et oubli.')
    return count

async def ministerial_pardon(bot:Bot):
    async with SessionLocal() as db:
        res=await db.execute(select(User).where(User.is_restricted==True))
        users=list(res.scalars().all()); count=len(users)
        for u in users:
            try: await bot.restrict_chat_member(get_settings().main_group_id,u.id,permissions=OPEN_PERMS)
            except Exception as e: await log_error('unrestrict',e)
            u.is_restricted=False
        await db.commit()
    if await st.is_open():
        await bot.send_message(get_settings().main_group_id,f'⚖️ GRÂCE MINISTÉRIELLE\n\n{count} restriction(s) levée(s).\n\nLa prochaine faute comptera double.')
    return count
