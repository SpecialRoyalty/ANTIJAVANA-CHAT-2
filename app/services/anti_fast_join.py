from __future__ import annotations

from datetime import datetime, timedelta

from aiogram import Bot
from aiogram.types import ChatMemberUpdated, Message
from sqlalchemy import select, func

from app.config import get_settings
from app.db.models import RapidJoinGuard, TrackedMessage, User
from app.db.session import SessionLocal
from app.services import settings as st
from app.services.state import log_error
from app.services.users import protected


async def enabled() -> bool:
    return (await st.get_value('anti_fast_join_enabled', 'true')) == 'true'


async def window_minutes() -> int:
    raw = await st.get_value('anti_fast_join_minutes', '5')
    try:
        value = int(raw)
    except Exception:
        value = 5
    return max(1, min(value, 60))


async def register_join(event: ChatMemberUpdated) -> None:
    """Enregistre/reinitialise l'heure d'arrivée réelle d'un membre du groupe principal."""
    if event.chat.id != get_settings().main_group_id:
        return
    member = getattr(event.new_chat_member, 'user', None) or event.from_user
    if not member or await protected(member.id):
        return
    if event.new_chat_member.status not in ('member', 'restricted'):
        return

    now = datetime.utcnow()
    async with SessionLocal() as db:
        row = await db.get(RapidJoinGuard, member.id)
        if not row:
            row = RapidJoinGuard(
                user_id=member.id,
                chat_id=event.chat.id,
                joined_at=now,
                last_triggered_at=None,
                trigger_count=0,
            )
            db.add(row)
        else:
            row.chat_id = event.chat.id
            row.joined_at = now
        await db.commit()


async def remaining_seconds(user_id: int) -> int | None:
    if not await enabled():
        return None
    async with SessionLocal() as db:
        row = await db.get(RapidJoinGuard, user_id)
        if not row or row.chat_id != get_settings().main_group_id:
            return None
        limit = timedelta(minutes=await window_minutes())
        remaining = (row.joined_at + limit - datetime.utcnow()).total_seconds()
        return max(0, int(remaining))


async def should_ban_for_fast_media(msg: Message) -> bool:
    """True uniquement pour un média publié dans la fenêtre suivant une arrivée connue."""
    if not msg.from_user or msg.chat.id != get_settings().main_group_id:
        return False
    if await protected(msg.from_user.id):
        return False
    if not await enabled():
        return False

    from app.services.hashban import media_file_entries
    if not media_file_entries(msg):
        return False

    async with SessionLocal() as db:
        row = await db.get(RapidJoinGuard, msg.from_user.id)
        if not row or row.chat_id != msg.chat.id:
            # Donnée absente = aucune sanction automatique.
            return False
        return datetime.utcnow() < row.joined_at + timedelta(minutes=await window_minutes())


async def delete_all_tracked_user_content(bot: Bot, chat_id: int, user_id: int) -> tuple[int, int]:
    """Supprime tous les messages suivis du membre. Retourne (supprimés, échecs)."""
    deleted = 0
    failed = 0
    async with SessionLocal() as db:
        res = await db.execute(
            select(TrackedMessage).where(
                TrackedMessage.chat_id == chat_id,
                TrackedMessage.user_id == user_id,
                TrackedMessage.deleted == False,
            )
        )
        rows = list(res.scalars().all())
        for tm in rows:
            try:
                await bot.delete_message(tm.chat_id, tm.message_id)
                tm.deleted = True
                deleted += 1
            except Exception as exc:
                failed += 1
                await log_error('anti_fast_join_delete', exc)
        await db.commit()
    return deleted, failed


async def enforce(bot: Bot, msg: Message) -> bool:
    """Bannit et nettoie si la règle publication immédiate s'applique.

    Retourne True si une sanction a été exécutée et que le pipeline doit s'arrêter.
    """
    if not await should_ban_for_fast_media(msg):
        return False

    uid = msg.from_user.id
    deleted, failed = await delete_all_tracked_user_content(bot, msg.chat.id, uid)

    # Le message courant est déjà tracké dans moderate_message. Si la suppression
    # globale a échoué ou n'a pas encore vu ce message, on tente explicitement.
    try:
        await bot.delete_message(msg.chat.id, msg.message_id)
    except Exception:
        pass

    banned = False
    try:
        await bot.ban_chat_member(msg.chat.id, uid)
        banned = True
    except Exception as exc:
        await log_error('anti_fast_join_ban', exc)

    now = datetime.utcnow()
    async with SessionLocal() as db:
        row = await db.get(RapidJoinGuard, uid)
        if row:
            row.last_triggered_at = now
            row.trigger_count += 1
        user = await db.get(User, uid)
        if user and banned:
            user.is_banned = True
        await db.commit()

    await st.set_value('anti_fast_join_last_user_id', str(uid))
    await st.set_value('anti_fast_join_last_at', now.isoformat(timespec='seconds'))
    await st.set_value('anti_fast_join_last_deleted', str(deleted))
    await st.set_value('anti_fast_join_last_failed', str(failed))
    total = int(await st.get_value('anti_fast_join_bans', '0') or '0') + (1 if banned else 0)
    await st.set_value('anti_fast_join_bans', str(total))
    return True


async def health_text() -> str:
    async with SessionLocal() as db:
        known = int((await db.execute(select(func.count(RapidJoinGuard.user_id)))).scalar() or 0)
    return (
        '🛡️ Anti publication immédiate\n'
        f"Statut : {'✅ ON' if await enabled() else '⛔ OFF'}\n"
        f'Délai : {await window_minutes()} min\n'
        f'Arrivées connues : {known}\n'
        f"Bans déclenchés : {await st.get_value('anti_fast_join_bans','0')}\n"
        f"Dernier déclenchement : {await st.get_value('anti_fast_join_last_at','jamais')}\n"
        f"Dernier nettoyage : {await st.get_value('anti_fast_join_last_deleted','0')} supprimé(s), {await st.get_value('anti_fast_join_last_failed','0')} échec(s)"
    )
