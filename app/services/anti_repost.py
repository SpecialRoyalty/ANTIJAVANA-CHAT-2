from __future__ import annotations

from datetime import datetime

from aiogram import Bot
from aiogram.types import Message
from sqlalchemy import select

from app.config import get_settings
from app.db.models import MediaHash
from app.db.session import SessionLocal
from app.services import settings as st
from app.services.hashban import file_sha256, media_file_entries, related_media_messages
from app.services.state import log_error, track
from app.services.users import display_name, protected


async def enabled() -> bool:
    return (await st.get_value('anti_repost_enabled', 'false')) == 'true'


async def _key_exists_non_banned(key: str | None) -> bool:
    if not key:
        return False
    async with SessionLocal() as db:
        found = (
            await db.execute(
                select(MediaHash.id)
                .where(
                    MediaHash.file_unique_id == key,
                    MediaHash.banned.is_(False),
                )
                .limit(1)
            )
        ).first()
    return found is not None


async def find_repost(bot: Bot, msg: Message) -> tuple[bool, str]:
    """Détecte un média déjà envoyé, sans confondre avec le hash-ban.

    Ordre : file_unique_id Telegram puis SHA256 exact. Le fingerprint perceptuel
    n'est volontairement pas utilisé ici afin de ne pas bloquer des contenus
    simplement similaires. Le hash-ban perceptuel reste, lui, plus strict.
    """
    entries = media_file_entries(msg)
    if not entries:
        return False, ''

    for unique, _file_id, _media_type in entries:
        if await _key_exists_non_banned(unique):
            return True, 'file_unique_id'

    for _unique, file_id, _media_type in entries:
        sha = await file_sha256(bot, file_id)
        if sha and await _key_exists_non_banned(sha):
            return True, 'sha256'

    return False, ''


async def _delete_album_or_message(bot: Bot, msg: Message) -> tuple[int, int]:
    targets = related_media_messages(msg) if msg.media_group_id else [msg]
    # Déduplication défensive par chat/message_id.
    seen: set[tuple[int, int]] = set()
    deleted = 0
    failed = 0
    for item in targets:
        key = (item.chat.id, item.message_id)
        if key in seen:
            continue
        seen.add(key)
        try:
            await bot.delete_message(item.chat.id, item.message_id)
            deleted += 1
        except Exception as exc:
            failed += 1
            await log_error('anti_repost_delete', exc)
    return deleted, failed


async def enforce(bot: Bot, msg: Message) -> bool:
    """Supprime un repost et arrête le pipeline. Aucune sanction utilisateur."""
    if not msg.from_user or msg.chat.id != get_settings().main_group_id:
        return False
    if await protected(msg.from_user.id) or not await enabled():
        return False
    if not media_file_entries(msg):
        return False

    matched, method = await find_repost(bot, msg)
    if not matched:
        return False

    deleted, failed = await _delete_album_or_message(bot, msg)

    warning = None
    try:
        warning = await bot.send_message(
            msg.chat.id,
            f'{display_name(msg.from_user)}, média déjà envoyé : repost interdit.',
        )
        await track(msg.chat.id, warning.message_id, None, 'temp', False)
    except Exception as exc:
        await log_error('anti_repost_warning', exc)

    now = datetime.utcnow().isoformat(timespec='seconds')
    await st.set_value('anti_repost_last_at', now)
    await st.set_value('anti_repost_last_method', method)
    await st.set_value('anti_repost_last_deleted', str(deleted))
    await st.set_value('anti_repost_last_failed', str(failed))
    await st.set_value('anti_repost_last_user_id', str(msg.from_user.id))
    total = int(await st.get_value('anti_repost_blocks', '0') or '0') + 1
    await st.set_value('anti_repost_blocks', str(total))
    return True


async def health_text() -> str:
    return (
        '♻️ Anti repost\n'
        f"Statut : {'✅ ON' if await enabled() else '⛔ OFF'}\n"
        f"Reposts bloqués : {await st.get_value('anti_repost_blocks','0')}\n"
        f"Dernier blocage : {await st.get_value('anti_repost_last_at','jamais')}\n"
        f"Méthode : {await st.get_value('anti_repost_last_method','-')}\n"
        f"Dernier nettoyage : {await st.get_value('anti_repost_last_deleted','0')} supprimé(s), "
        f"{await st.get_value('anti_repost_last_failed','0')} échec(s)"
    )
