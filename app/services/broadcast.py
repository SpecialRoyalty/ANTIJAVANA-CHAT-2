from __future__ import annotations

import asyncio
from datetime import datetime

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import Message, User as TelegramUser
from sqlalchemy import select

from app.config import get_settings
from app.db.models import PrivateSubscriber
from app.db.session import SessionLocal
from app.services.state import log_error


async def register_private_start(user: TelegramUser) -> None:
    """Enregistre uniquement les personnes ayant réellement lancé /start en privé."""
    now = datetime.utcnow()
    async with SessionLocal() as db:
        subscriber = await db.get(PrivateSubscriber, user.id)
        if subscriber is None:
            subscriber = PrivateSubscriber(
                user_id=user.id,
                username=user.username,
                full_name=user.full_name or "",
                active=True,
                started_at=now,
                last_start_at=now,
            )
            db.add(subscriber)
        else:
            subscriber.username = user.username
            subscriber.full_name = user.full_name or ""
            subscriber.active = True
            subscriber.last_start_at = now
        await db.commit()


def supported_broadcast_message(message: Message) -> bool:
    """Formats autorisés : texte, photo seule, ou photo avec légende."""
    return bool(message.text or message.photo)


async def broadcast_to_main_group(bot: Bot, source: Message) -> int:
    """Copie le message admin tel quel dans le groupe principal."""
    copied = await bot.copy_message(
        chat_id=get_settings().main_group_id,
        from_chat_id=source.chat.id,
        message_id=source.message_id,
    )
    return copied.message_id


async def _copy_with_retry(bot: Bot, chat_id: int, source: Message) -> None:
    try:
        await bot.copy_message(
            chat_id=chat_id,
            from_chat_id=source.chat.id,
            message_id=source.message_id,
        )
    except TelegramRetryAfter as exc:
        await asyncio.sleep(float(exc.retry_after) + 0.5)
        await bot.copy_message(
            chat_id=chat_id,
            from_chat_id=source.chat.id,
            message_id=source.message_id,
        )


async def broadcast_to_private_starters(bot: Bot, source: Message) -> dict[str, int]:
    """Envoie le message à tous les utilisateurs actifs ayant fait /start en privé."""
    async with SessionLocal() as db:
        result = await db.execute(
            select(PrivateSubscriber).where(PrivateSubscriber.active.is_(True))
        )
        subscribers = list(result.scalars().all())

    sent = 0
    blocked = 0
    errors = 0
    now = datetime.utcnow()

    for subscriber in subscribers:
        try:
            await _copy_with_retry(bot, subscriber.user_id, source)
            sent += 1
            async with SessionLocal() as db:
                current = await db.get(PrivateSubscriber, subscriber.user_id)
                if current:
                    current.last_broadcast_at = now
                    current.active = True
                    await db.commit()
        except TelegramForbiddenError:
            blocked += 1
            async with SessionLocal() as db:
                current = await db.get(PrivateSubscriber, subscriber.user_id)
                if current:
                    current.active = False
                    await db.commit()
        except TelegramBadRequest as exc:
            # Chat introuvable, compte supprimé, ou autre destinataire devenu invalide.
            errors += 1
            await log_error('broadcast_private_bad_request', exc)
        except Exception as exc:
            errors += 1
            await log_error('broadcast_private', exc)

        # Limite volontaire sous la limite Telegram pour réduire les FloodWait.
        await asyncio.sleep(0.045)

    return {
        'total': len(subscribers),
        'sent': sent,
        'blocked': blocked,
        'errors': errors,
    }


async def private_subscriber_count() -> int:
    async with SessionLocal() as db:
        result = await db.execute(
            select(PrivateSubscriber.user_id).where(PrivateSubscriber.active.is_(True))
        )
        return len(result.scalars().all())
