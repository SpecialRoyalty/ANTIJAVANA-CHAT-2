from __future__ import annotations
from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo
from sqlalchemy import select, func
from sqlalchemy.exc import IntegrityError
from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from app.config import get_settings
from app.db.session import SessionLocal
from app.db.models import FreePassReservation, User, VipAccess
from app.services import settings as st
from app.services.state import track, log_error
from app.services.users import anon_name, is_gibberish
from app.services.vip import _soiree_send_now, _soiree_expire_utc_for_current_or_next, _send_access_link


def _now_local():
    return datetime.now(ZoneInfo(get_settings().timezone))

def session_key_now() -> str:
    # Clé locale de session, pas UTC, pour éviter les bascules autour de minuit.
    return _now_local().strftime('%Y%m%d')

def in_admin_config_window() -> bool:
    """Création/config autorisée de 05h00 à 22h59 inclus.
    Après 23h et avant 05h, l'offre du jour est verrouillée.
    """
    n=_now_local()
    return 5 <= n.hour < 23

async def enabled() -> bool:
    return (await st.get_value('free_pass_enabled','false')) == 'true'
async def places() -> int:
    return int(await st.get_value('free_pass_places','20') or '20')
async def cooldown_days() -> int:
    return int(await st.get_value('free_pass_cooldown_days','30') or '30')
async def min_media() -> int:
    return int(await st.get_value('free_pass_min_media','3') or '3')
async def min_invites() -> int:
    return int(await st.get_value('free_pass_min_invites','0') or '0')
async def published_session_key() -> str:
    return await st.get_value('free_pass_published_session','')
async def is_published_this_session() -> bool:
    return (await published_session_key()) == session_key_now()
async def is_locked() -> bool:
    # Une fois publiée, la campagne ne peut plus être modifiée.
    # Elle reste désactivable/supprimable.
    return await is_published_this_session()

async def reserved_count(session_key: str|None=None) -> int:
    sk=session_key or session_key_now()
    async with SessionLocal() as db:
        res=await db.execute(select(func.count(FreePassReservation.id)).where(FreePassReservation.session_key==sk, FreePassReservation.status.in_(['reserved','sent'])))
        return int(res.scalar() or 0)

async def remaining_places() -> int:
    return max(await places() - await reserved_count(), 0)

def _bot_deeplink(param: str) -> str | None:
    username = get_settings().public_bot_username.strip().lstrip('@')
    if not username:
        return None
    return f'https://t.me/{username}?start={param}'

async def user_eligible(user_id:int, username:str='', full_name:str='') -> tuple[bool,str]:
    if not await enabled(): return False, 'Offre inactive.'
    if not await is_published_this_session(): return False, 'Aucune offre Pass gratuit publiée pour cette session.'
    if await remaining_places() <= 0: return False, 'Toutes les places gratuites sont déjà réservées.'
    cd=await cooldown_days(); mm=await min_media(); mi=await min_invites()
    async with SessionLocal() as db:
        u=await db.get(User,user_id)
        if not u:
            score=0
            if not username: score+=10
            if is_gibberish(full_name or username): score+=20
            u=User(id=user_id, username=username or None, full_name=full_name or username or '', suspect_score=score)
            db.add(u)
            await db.flush()
        else:
            if username: u.username=username
            if full_name: u.full_name=full_name
        if u.is_banned or u.suspect_score>=100: return False, 'Compte non éligible.'

        superior = await db.execute(
            select(VipAccess).where(
                VipAccess.user_id == user_id,
                VipAccess.offer.in_(['total', 'javana']),
                VipAccess.status.in_(['pending', 'active'])
            ).limit(1)
        )
        sup = superior.scalar_one_or_none()
        if sup:
            if sup.offer == 'total':
                return False, '🎟 Offre non disponible : tu disposes déjà du Pass Total.'
            return False, '🎟 Offre non disponible : tu disposes déjà d’un accès VIP.'

        current_exp = _soiree_expire_utc_for_current_or_next()
        paid_soiree = await db.execute(
            select(VipAccess).where(
                VipAccess.user_id == user_id,
                VipAccess.offer == 'soiree',
                VipAccess.status.in_(['pending', 'active']),
                VipAccess.expires_at == current_exp
            ).limit(1)
        )
        if paid_soiree.scalar_one_or_none():
            return False, '🎟 Offre non disponible : tu disposes déjà d’un accès Pass Soirée pour cette session.'

        if u.media_count < mm and u.total_invites < mi:
            req=[]
            if mm>0: req.append(f'{mm} médias publiés')
            if mi>0: req.append(f'{mi} invité validé')
            return False, 'Réservé aux membres actifs : ' + ' OU '.join(req) + '.'
        cutoff=datetime.utcnow()-timedelta(days=cd)
        old=await db.execute(select(FreePassReservation).where(FreePassReservation.user_id==user_id, FreePassReservation.created_at>=cutoff, FreePassReservation.status.in_(['reserved','sent','expired'])).limit(1))
        if old.scalar_one_or_none(): return False, f'Tu as déjà profité d’un Pass gratuit récemment. Prochaine possibilité après {cd} jours.'
    return True,'OK'

async def reserve_free_pass(bot:Bot, user_id:int, username:str='') -> tuple[bool,str]:
    ok,reason=await user_eligible(user_id, username=username, full_name=username)
    if not ok: return False, reason
    sk=session_key_now()
    async with SessionLocal() as db:
        r=FreePassReservation(user_id=user_id, username=username or '', session_key=sk, status='reserved')
        db.add(r)
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback(); return False, 'Tu as déjà réservé une place gratuite pour cette session.'
    # si on est déjà entre 23h et 05h, envoi immédiat
    if _soiree_send_now():
        await send_due_free_pass_links(bot, force=True, only_user_id=user_id)
        msg=('🎟 PASS SOIRÉE GRATUIT\n\n'
             'Ta place est réservée.\n\n'
             'Ton lien unique vient d’être envoyé.\n\n'
             'À 05h00, l’accès expire automatiquement.')
    else:
        msg=('🎟 PASS SOIRÉE GRATUIT\n\n'
             'Ta place est réservée.\n\n'
             'Ton lien unique te sera envoyé automatiquement à 23h00.\n\n'
             'À 05h00, l’accès expire automatiquement.')
    # actualise le message groupe, pour retirer le bouton si complet.
    try: await refresh_free_pass_message(bot)
    except Exception: pass
    return True, msg

async def free_pass_text() -> str:
    p=await places(); r=await reserved_count(); rem=max(p-r,0); mm=await min_media(); mi=await min_invites(); cd=await cooldown_days()
    if rem <= 0:
        return ('🔥 PASS SOIRÉE OFFERT\n\n'
                'Offre complète pour ce soir.\n\n'
                'Rendez-vous à la prochaine session.')
    urgency=''
    if rem<=1: urgency='\n🔥 Dernière place.'
    elif rem<=3: urgency='\n🔥 Plus que 3 places.'
    elif rem<=5: urgency='\n🔥 Plus que 5 places.'
    cond=[]
    if mm>0: cond.append(f'au moins {mm} médias publiés')
    if mi>0: cond.append(f'au moins {mi} invité validé')
    cond_txt=' OU '.join(cond) if cond else 'membres actifs'
    return (f'🔥 PASS SOIRÉE OFFERT\n\n'
            f'{p} accès disponibles ce soir.\n\n'
            f'Réservé aux membres actifs :\n✓ {cond_txt}\n\n'
            f'Places restantes :\n{rem} / {p}{urgency}\n\n'
            f'Limite : 1 utilisation tous les {cd} jours.')

async def free_pass_kb():
    if await remaining_places() <= 0:
        return None
    url=_bot_deeplink('freepass')
    if url:
        return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='🎟 Réserver gratuitement', url=url)]])
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='🎟 Réserver gratuitement', callback_data='freepass_reserve')]])

def _freepass_admin_rows(locked: bool):
    if locked:
        return [
            [InlineKeyboardButton(text='👁 Voir campagne', callback_data='freepass_beneficiaries')],
            [InlineKeyboardButton(text='⛔ Désactiver', callback_data='freepass_toggle'), InlineKeyboardButton(text='🗑 Supprimer', callback_data='freepass_delete')],
            [InlineKeyboardButton(text='⬅️ Retour panel', callback_data='adm_dashboard')]
        ]
    return [
        [InlineKeyboardButton(text='✅/⛔ ON/OFF', callback_data='freepass_toggle'), InlineKeyboardButton(text='📤 Publier', callback_data='freepass_publish')],
        [InlineKeyboardButton(text='➕ Modifier places', callback_data='await:freepass_places'), InlineKeyboardButton(text='📅 Modifier cooldown', callback_data='await:freepass_cooldown')],
        [InlineKeyboardButton(text='🎯 Condition médias', callback_data='await:freepass_media'), InlineKeyboardButton(text='🎯 Condition invités', callback_data='await:freepass_invites')],
        [InlineKeyboardButton(text='📊 Bénéficiaires', callback_data='freepass_beneficiaries'), InlineKeyboardButton(text='🔄 Reset session', callback_data='freepass_reset')],
        [InlineKeyboardButton(text='⬅️ Retour panel', callback_data='adm_dashboard')]
    ]

async def free_pass_admin_kb_async():
    return InlineKeyboardMarkup(inline_keyboard=_freepass_admin_rows(await is_locked()))

def free_pass_admin_kb():
    # Compat sync pour anciens appels. Le verrou est surtout appliqué côté callbacks.
    return InlineKeyboardMarkup(inline_keyboard=_freepass_admin_rows(False))

async def admin_text() -> str:
    locked=await is_locked(); window=in_admin_config_window(); pub=await published_session_key()
    return (f'🎟 Pass Soirée Gratuit\n\n'
            f'Statut : {"ON" if await enabled() else "OFF"}\n'
            f'Campagne session : {pub or "non publiée"}\n'
            f'Verrou : {"🔒 VERROUILLÉE" if locked else "modifiable"}\n'
            f'Fenêtre admin : {"ouverte" if window else "fermée (23h-05h)"}\n'
            f'Places : {await places()}\n'
            f'Réservées : {await reserved_count()}\n'
            f'Restantes : {await remaining_places()}\n'
            f'Cooldown : {await cooldown_days()} jours\n'
            f'Condition médias : {await min_media()}\n'
            f'Condition invités : {await min_invites()}')

async def publish_free_pass(bot:Bot):
    if not await enabled(): return None
    if not in_admin_config_window():
        return 'window_closed'
    if await is_published_this_session():
        return 'already_published'
    s=get_settings(); old=await st.get_value('free_pass_message_id','')
    if old:
        try: await bot.delete_message(s.main_group_id,int(old))
        except Exception: pass
    m=await bot.send_message(s.main_group_id, await free_pass_text(), reply_markup=await free_pass_kb())
    await st.set_value('free_pass_message_id',str(m.message_id))
    await st.set_value('free_pass_published_session', session_key_now())
    await track(s.main_group_id,m.message_id,None,'free_pass_ad',False)
    await st.set_value('last_free_pass_sent_at', datetime.utcnow().isoformat(timespec='seconds'))
    return m.message_id

async def refresh_free_pass_message(bot:Bot):
    s=get_settings(); mid=await st.get_value('free_pass_message_id','')
    if not mid: return
    try:
        await bot.edit_message_text(await free_pass_text(), chat_id=s.main_group_id, message_id=int(mid), reply_markup=await free_pass_kb())
    except Exception:
        pass

async def delete_free_pass_campaign(bot:Bot|None=None):
    s=get_settings(); mid=await st.get_value('free_pass_message_id','')
    if bot and mid:
        try: await bot.delete_message(s.main_group_id,int(mid))
        except Exception: pass
    await st.set_value('free_pass_message_id','')
    await st.set_value('free_pass_published_session','')
    await st.set_value('free_pass_enabled','false')
    return True

async def send_due_free_pass_links(bot:Bot, force:bool=False, only_user_id:int|None=None):
    if not force and datetime.now(ZoneInfo(get_settings().timezone)).hour != 23: return 0
    s=get_settings(); gid=s.pass_soiree_group_id
    if not gid:
        await log_error('free_pass','PASS_SOIREE_GROUP_ID non configuré')
        return 0
    async with SessionLocal() as db:
        q=select(FreePassReservation).where(FreePassReservation.status=='reserved')
        if only_user_id: q=q.where(FreePassReservation.user_id==only_user_id)
        res=await db.execute(q)
        rows=list(res.scalars().all())
        ids=[]
        for r in rows:
            access=VipAccess(order_id=0,user_id=r.user_id,username=r.username,offer='soiree',group_id=gid,status='pending',expires_at=_soiree_expire_utc_for_current_or_next())
            db.add(access); await db.flush()
            r.access_id=access.id; ids.append(access.id)
        await db.commit()
    sent=0
    for aid in ids:
        if await _send_access_link(bot, aid):
            sent+=1
            async with SessionLocal() as db:
                res=await db.execute(select(FreePassReservation).where(FreePassReservation.access_id==aid))
                r=res.scalar_one_or_none()
                if r: r.status='sent'; await db.commit()
    return sent

async def beneficiaries_text() -> str:
    async with SessionLocal() as db:
        res=await db.execute(select(FreePassReservation).order_by(FreePassReservation.id.desc()).limit(30))
        rows=list(res.scalars().all())
    if not rows: return 'Aucun bénéficiaire.'
    lines=['🎟 Bénéficiaires Pass gratuit']
    for r in rows:
        name='@'+r.username if r.username else str(r.user_id)
        lines.append(f'- {anon_name(r.username, name)} — {r.status} — {r.session_key}')
    return '\n'.join(lines)

async def reset_current_session():
    sk=session_key_now()
    async with SessionLocal() as db:
        res=await db.execute(select(FreePassReservation).where(FreePassReservation.session_key==sk, FreePassReservation.status=='reserved'))
        n=0
        for r in res.scalars().all():
            r.status='rejected'; n+=1
        await db.commit(); return n
