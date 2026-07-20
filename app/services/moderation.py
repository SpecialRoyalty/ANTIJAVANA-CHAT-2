from __future__ import annotations

import re
from datetime import datetime, timedelta

from aiogram import Bot
from aiogram.types import ChatPermissions, Message
from sqlalchemy import select

from app.config import get_settings
from app.db.models import User, WordRule
from app.db.session import SessionLocal
from app.services import settings as st
from app.services.hashban import (
    find_banned_hash,
    media_file_entries,
    record_repost_verification,
    store_message_hashes,
)
from app.services.state import log_error, track
from app.services.users import display_name, protected


def has_link(text: str) -> bool:
    return bool(re.search(r"(https?://|t\.me/|www\.|\.com\b|\.net\b|\.io\b)", text or "", re.I))


def has_mention(text: str) -> bool:
    return "@" in (text or "")


def has_command(text: str) -> bool:
    return (text or "").strip().startswith("/")


def is_media(msg: Message) -> bool:
    return bool(media_file_entries(msg))


def file_ids(msg: Message):
    """Compatibilité avec les autres modules existants."""
    return media_file_entries(msg)


async def words(kind: str) -> list[str]:
    async with SessionLocal() as db:
        result = await db.execute(select(WordRule).where(WordRule.kind == kind))
        return [row.word.lower() for row in result.scalars().all()]


async def text_has_word(kind: str, text: str) -> bool:
    lowered = (text or "").lower()
    return any(word and word in lowered for word in await words(kind))


async def restrict(bot: Bot, chat_id: int, user_id: int, days: int) -> bool:
    if await protected(user_id):
        return False
    until = datetime.utcnow() + timedelta(days=days)
    try:
        await bot.restrict_chat_member(
            chat_id,
            user_id,
            permissions=ChatPermissions(can_send_messages=False),
            until_date=until,
        )
        async with SessionLocal() as db:
            user = await db.get(User, user_id)
            if user:
                user.is_restricted = True
            await db.commit()
        return True
    except Exception as exc:
        await log_error("restrict", exc)
        return False


async def ban(bot: Bot, chat_id: int, user_id: int) -> bool:
    if await protected(user_id):
        return False
    try:
        await bot.ban_chat_member(chat_id, user_id)
        async with SessionLocal() as db:
            user = await db.get(User, user_id)
            if user:
                user.is_banned = True
            await db.commit()
        return True
    except Exception as exc:
        await log_error("ban", exc)
        return False


async def delete(bot: Bot, msg: Message) -> bool:
    try:
        await bot.delete_message(msg.chat.id, msg.message_id)
        return True
    except Exception as exc:
        await log_error("delete_message", exc)
        return False


async def record_media(msg: Message, bot: Bot | None = None, banned: bool = False) -> int:
    """Enregistre le média. Avec bot, l'ID Telegram et le SHA256 sont stockés."""
    if bot is None:
        # Compatibilité prudente : sans Bot, impossible de calculer le SHA256.
        from app.db.models import MediaHash

        entries = media_file_entries(msg)
        async with SessionLocal() as db:
            for unique, file_id, media_type in entries:
                result = await db.execute(select(MediaHash).where(MediaHash.file_unique_id == unique))
                row = result.scalar_one_or_none()
                if row is None:
                    db.add(MediaHash(
                        user_id=msg.from_user.id if msg.from_user else None,
                        file_unique_id=unique,
                        file_id=file_id,
                        media_type=media_type,
                        banned=banned,
                    ))
                elif banned:
                    row.banned = True
            await db.commit()
        stored = len(entries)
    else:
        stored = await store_message_hashes(msg, bot, banned=banned)

    if msg.from_user and not banned and media_file_entries(msg):
        async with SessionLocal() as db:
            user = await db.get(User, msg.from_user.id)
            if user:
                user.media_count += 1
                user.last_media_session = int(await st.get_value("active_session_id", "0") or "0")
            await db.commit()
    return stored


async def contains_banned_hash(bot: Bot, msg: Message) -> bool:
    """Compatibilité : retourne seulement un booléen."""
    return (await find_banned_hash(bot, msg)).matched


async def moderate_message(bot: Bot, msg: Message) -> bool:
    """Retourne True seulement si le pipeline peut continuer vers la copie VIP."""
    if not msg.from_user:
        return False

    await track(msg.chat.id, msg.message_id, msg.from_user.id, "message", is_media(msg))
    if msg.chat.id != get_settings().main_group_id:
        return True

    uid = msg.from_user.id
    text = msg.text or msg.caption or ""
    trusted = uid in get_settings().trusted_ids
    admin = uid in get_settings().admin_ids

    if not await st.is_open() and not (trusted or admin):
        await delete(bot, msg)
        return False

    if is_media(msg):
        match = await find_banned_hash(bot, msg)
        if match.matched:
            deleted = await delete(bot, msg)
            user_banned = await ban(bot, msg.chat.id, uid)
            await record_repost_verification(
                match=match,
                deleted=deleted,
                user_banned=user_banned,
                pipeline_stopped=True,
                user_id=uid,
                chat_id=msg.chat.id,
                message_id=msg.message_id,
            )
            return False
        await record_media(msg, bot=bot)

    # Liens interdits pour tout le monde sauf admins ; trusted supprimé sans sanction.
    if has_link(text):
        await delete(bot, msg)
        if not (trusted or admin):
            await ban(bot, msg.chat.id, uid)
        return False

    if trusted or admin:
        return True

    if has_command(text):
        await delete(bot, msg)
        await restrict(bot, msg.chat.id, uid, 1)
        return False

    if msg.video_note:
        await delete(bot, msg)
        await restrict(bot, msg.chat.id, uid, 1)
        return False

    if has_mention(text):
        await delete(bot, msg)
        await restrict(bot, msg.chat.id, uid, 2)
        return False

    if await text_has_word("ban", text):
        await delete(bot, msg)
        await ban(bot, msg.chat.id, uid)
        return False

    if await text_has_word("forbidden", text):
        await delete(bot, msg)
        await restrict(bot, msg.chat.id, uid, 1)
        return False

    if text and not is_media(msg):
        async with SessionLocal() as db:
            user = await db.get(User, uid)
            if user and user.media_count <= 0:
                await delete(bot, msg)
                warning = await bot.send_message(
                    msg.chat.id,
                    f"{display_name(msg.from_user)}, envoie d’abord un média avant d’écrire.",
                )
                await track(msg.chat.id, warning.message_id, None, "temp", False)
                return False

    return True
