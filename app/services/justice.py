from sqlalchemy import select, func, and_, or_
from aiogram import Bot
from app.config import get_settings
from app.db.session import SessionLocal
from app.db.models import User
from app.services import settings as st
from app.services.session_ops import CLOSED_PERMS, OPEN_PERMS, notify_admins
from app.services.state import track, log_error

DEFAULT_JUSTICE_REMOVALS = 20

async def justice_limit() -> int:
    # Configurable depuis le panel admin. Défaut: 20 personnes/session.
    return await st.justice_limit()

async def _current_session_key() -> str:
    sid = await st.get_value('active_session_id','0')
    return sid if sid and sid != '0' else await st.get_value('current_day_key','manual')

async def justice_already_done() -> bool:
    key = await _current_session_key()
    return await st.get_value(f'justice_done_session:{key}','false') == 'true'

async def mark_justice_done():
    key = await _current_session_key()
    await st.set_value(f'justice_done_session:{key}','true')

def _candidate_filter(sid:int):
    # Justice = uniquement les membres qui ont eu une vraie chance de participer.
    # 1) jamais média après au moins 3 sessions accessibles
    # 2) ancien actif mais plus aucun média depuis 14 sessions ouvertes
    return or_(
        and_(User.sessions_present >= 3, User.media_count == 0),
        and_(User.sessions_present >= 14, User.last_media_session < max(sid-14, 0))
    )

async def _protected_ids() -> set[int]:
    s=get_settings()
    bot_id = int(await st.get_value('bot_id','0') or '0')
    protected_ids = set(s.admin_ids) | set(s.trusted_ids)
    if bot_id:
        protected_ids.add(bot_id)
    return protected_ids

def _base_query(protected_ids:set[int], sid:int):
    q = select(User).where(User.is_admin==False, User.is_trusted==False, User.is_banned==False)
    if protected_ids:
        q = q.where(User.id.not_in(list(protected_ids)))
    q = q.where(_candidate_filter(sid))
    return q



def _display_name(u: User) -> str:
    """Nom public sans jamais afficher l'ID Telegram."""
    if getattr(u, 'username', None):
        return '@' + u.username
    name = (getattr(u, 'full_name', None) or 'membre').strip()
    return name[:80] or 'membre'

async def _send_visible_justice_removal(bot: Bot, user: User):
    """Fallback visible pour la justice populaire.

    Telegram ne garantit pas l'affichage d'une notification système quand un bot
    retire un membre via l'API (ban/unban = seul mécanisme Bot API fiable pour
    "kick"). Pour conserver l'effet public demandé, on publie une notification
    bot courte, suivie en base et supprimée à la fermeture.
    """
    s = get_settings()
    try:
        m = await bot.send_message(s.main_group_id, f'ANTIJAVANA CHAT removed {_display_name(user)}')
        await track(s.main_group_id, m.message_id, getattr(user, 'id', None), 'justice_removed_notification', False)
    except Exception as e:
        await log_error('justice_visible_remove_notice', e)

async def candidate_count() -> int:
    sid = int(await st.get_value('active_session_id','0') or '0')
    protected_ids = await _protected_ids()
    async with SessionLocal() as db:
        q = select(func.count(User.id)).where(User.is_admin==False, User.is_trusted==False, User.is_banned==False)
        if protected_ids:
            q = q.where(User.id.not_in(list(protected_ids)))
        q = q.where(_candidate_filter(sid))
        res = await db.execute(q)
        return int(res.scalar() or 0)

async def candidates(limit:int|None=None):
    if limit is None:
        limit = await justice_limit()
    sid = int(await st.get_value('active_session_id','0') or '0')
    protected_ids = await _protected_ids()
    async with SessionLocal() as db:
        q = _base_query(protected_ids, sid)
        q = q.order_by(User.media_count.asc(), User.last_media_session.asc(), User.sessions_present.desc(), User.suspect_score.desc()).limit(limit)
        res = await db.execute(q)
        return [u for u in res.scalars().all() if u.id not in protected_ids]

async def justice_preview_text():
    limit = await justice_limit()
    if await justice_already_done():
        return f'⚖️ Justice populaire\n\nJustice déjà exécutée pour cette session.\n\nMaximum : 1 fois par session, manuel ou auto.\nLimite configurée : {limit} personnes/session.'
    total = await candidate_count()
    cs = await candidates(limit)
    if not cs:
        return f'⚖️ Justice populaire\n\nAucun membre justifiable détecté pour le moment.\n\nLimite configurée : {limit} personnes/session.\nRien ne sera envoyé dans le groupe.'
    selected = min(total, limit)
    postponed = max(total - selected, 0)
    lines = [
        '⚖️ Justice populaire',
        '',
        f'Membres justifiables détectés : {total}',
        f'Limite de cette session : {limit}',
        f'{selected} seront supprimés.',
    ]
    if postponed:
        lines.append(f'{postponed} seront reportés aux prochaines sessions.')
    lines += ['', 'Aperçu :']
    for u in cs[:10]:
        name = ('@'+u.username) if u.username else (u.full_name or 'membre')
        shown = name[:3] + '****' if len(name)>3 else name+'****'
        lines.append(f'- {shown} — médias: {u.media_count}, sessions: {u.sessions_present}, score: {u.suspect_score}')
    lines.append('\nValider le lancement ?')
    return '\n'.join(lines)

async def execute_justice(bot:Bot, manual:bool=False):
    s=get_settings()
    limit = await justice_limit()
    if await justice_already_done():
        return 0, 'Justice déjà exécutée pour cette session.'
    total = await candidate_count()
    cs=await candidates(limit)
    if not cs:
        return 0, 'Aucun membre justifiable détecté.'
    await mark_justice_done()
    await st.set_value('justice_running','true')
    try: await bot.set_chat_permissions(s.main_group_id, permissions=CLOSED_PERMS)
    except Exception as e: await log_error('justice_permissions_close',e)
    try:
        m = await bot.send_message(s.main_group_id, f'⚖️ JUSTICE POPULAIRE\n\nLe groupe est bloqué pendant 5 minutes.\n\nMembres justifiables : {total}\nLimite session : {limit}\nSuppression prévue : {len(cs)}\nLes plus inactifs sont supprimés.')
        await track(s.main_group_id, m.message_id, None, 'justice', False)
    except Exception as e:
        await log_error('justice_message', e)
    removed=0
    async with SessionLocal() as db:
        for u in cs:
            if u.id == int(await st.get_value('bot_id','0') or '0'):
                continue
            try:
                # Bot API n'a pas de méthode "kick visible" séparée : le retrait
                # fiable se fait par ban puis unban. Certaines configurations Telegram
                # n'affichent pas la notification système de retrait ; on publie donc
                # une notification visible dédiée juste après le retrait.
                await bot.ban_chat_member(s.main_group_id, u.id, revoke_messages=False)
                await bot.unban_chat_member(s.main_group_id, u.id, only_if_banned=True)
                await _send_visible_justice_removal(bot, u)
                u2=await db.get(User,u.id)
                if u2: u2.is_banned=True
                removed+=1
            except Exception as e:
                await log_error('justice_remove',f'{u.id}: {e}')
        await db.commit()
    import asyncio
    await asyncio.sleep(300)
    try: await bot.set_chat_permissions(s.main_group_id, permissions=OPEN_PERMS)
    except Exception as e: await log_error('justice_permissions_open',e)
    postponed=max(total-removed,0)
    try:
        m2=await bot.send_message(s.main_group_id, f'🟢 JUSTICE TERMINÉE\n\nMembres supprimés : {removed}\nReportés : {postponed}\nLe groupe est de nouveau ouvert.')
        await track(s.main_group_id, m2.message_id, None, 'justice', False)
    except Exception as e:
        await log_error('justice_end_message', e)
    await st.set_value('justice_running','false')
    await notify_admins(bot, f'⚖️ Justice terminée. Éligibles : {total} — Supprimés : {removed} — Reportés : {postponed} — Limite : {limit}')
    return removed, f'Justice lancée. Membres supprimés : {removed}'
