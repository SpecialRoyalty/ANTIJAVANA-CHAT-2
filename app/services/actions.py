from aiogram import Bot
from aiogram.types import Message
from sqlalchemy import select

from app.config import get_settings
from app.db.models import TrackedMessage, TrustedAction
from app.db.session import SessionLocal
from app.services.hashban import (
    audit_hashes,
    ensure_hashes_banned,
    format_hash_audit,
    split_telegram_text,
)
from app.services.moderation import ban, delete, restrict
from app.services.state import log_error


async def trusted_command(bot: Bot, msg: Message) -> bool:
    if not msg.from_user or msg.from_user.id not in get_settings().all_admin_ids:
        return False

    cmd = (msg.text or "").split()[0].lower()
    if cmd not in ["/supprime", "/mineur", "/pasfr", "/pedo", "/hashdemande", "/clean", "/info"]:
        return False

    try:
        await bot.delete_message(msg.chat.id, msg.message_id)
    except Exception:
        pass

    target = msg.reply_to_message
    async with SessionLocal() as db:
        db.add(
            TrustedAction(
                trusted_user_id=msg.from_user.id,
                trusted_username=msg.from_user.username or msg.from_user.full_name or "",
                command=cmd,
                target_user_id=target.from_user.id if target and target.from_user else None,
            )
        )
        await db.commit()

    if cmd == "/clean":
        count = 50
        parts = (msg.text or "").split()
        if len(parts) > 1 and parts[1].isdigit():
            count = min(int(parts[1]), 300)
        for message_id in range(msg.message_id - 1, max(msg.message_id - count, 0), -1):
            try:
                await bot.delete_message(msg.chat.id, message_id)
            except Exception:
                pass
        return True

    if cmd == "/info" and target and target.from_user:
        await bot.send_message(
            msg.from_user.id,
            f"👤 {target.from_user.full_name}\n"
            f"@{target.from_user.username or 'sans username'}\n"
            "ID interne masqué dans le groupe.",
        )
        return True

    if cmd == "/hashdemande":
        if not target:
            await bot.send_message(msg.from_user.id, "Réponds à un média ou à un élément d’album avec /hashdemande.")
            return True
        entries = await audit_hashes(bot, target)
        report = format_hash_audit(entries, title="🔍 DEMANDE DE VÉRIFICATION HASH")
        for admin_id in get_settings().admin_ids:
            for chunk in split_telegram_text(report):
                try:
                    await bot.send_message(admin_id, chunk)
                except Exception as exc:
                    await log_error("hashdemande_send", exc)
        return True

    if not target:
        return True

    if cmd == "/supprime":
        await delete(bot, target)
    elif cmd == "/mineur":
        await delete(bot, target)
        if target.from_user:
            await restrict(bot, msg.chat.id, target.from_user.id, 1)
    elif cmd == "/pasfr":
        await delete(bot, target)
        if target.from_user:
            await restrict(bot, msg.chat.id, target.from_user.id, 1)
    elif cmd == "/pedo":
        uid = target.from_user.id if target.from_user else None
        if uid:
            # Le média ciblé est blacklisté AVANT sa suppression : ID Telegram + SHA256.
            try:
                fingerprints, audits = await ensure_hashes_banned(bot, target)
                report = format_hash_audit(
                    audits,
                    title=f"🚫 /PEDO — BLACKLIST CONFIRMÉE ({fingerprints} empreinte(s))",
                )
                for chunk in split_telegram_text(report):
                    try:
                        await bot.send_message(msg.from_user.id, chunk)
                    except Exception as send_exc:
                        await log_error("pedo_hash_report", send_exc)
            except Exception as exc:
                await log_error("pedo_hashban", exc)
                try:
                    await bot.send_message(
                        msg.from_user.id,
                        "🔴 /pedo : le bannissement utilisateur continue, mais la mise en blacklist du média a échoué. Consulte les logs avant de considérer le hash comme protégé.",
                    )
                except Exception:
                    pass

            await ban(bot, msg.chat.id, uid)

            async with SessionLocal() as db:
                result = await db.execute(
                    select(TrackedMessage).where(
                        TrackedMessage.chat_id == msg.chat.id,
                        TrackedMessage.user_id == uid,
                        TrackedMessage.deleted.is_(False),
                    )
                )
                for tracked in result.scalars().all():
                    try:
                        await bot.delete_message(tracked.chat_id, tracked.message_id)
                        tracked.deleted = True
                    except Exception:
                        pass
                await db.commit()

    return True
