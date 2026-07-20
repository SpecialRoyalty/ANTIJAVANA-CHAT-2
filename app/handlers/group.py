from aiogram import Bot, Router
from aiogram.types import ChatMemberUpdated, Message

from app.config import get_settings
from app.services import settings as st
from app.services.actions import trusted_command
from app.services.crowdfunding import handle_crowd_proof, handle_crowd_text
from app.services.invites import on_join
from app.services.moderation import moderate_message
from app.services.state import track
from app.services.users import upsert_user
from app.services.vip import copy_media_to_vip, handle_vip_proof

router = Router()


@router.my_chat_member()
async def bot_added(event: ChatMemberUpdated, bot: Bot):
    settings = get_settings()
    allowed_groups = [
        settings.main_group_id,
        settings.pass_soiree_group_id,
        settings.pass_total_group_id,
        settings.vip_javana_group_id,
        settings.log_group_id,
    ]
    if event.chat.id not in allowed_groups:
        for admin_id in settings.admin_ids:
            try:
                await bot.send_message(
                    admin_id,
                    f"🚨 Tentative de raccordement pirate\nGroupe: {event.chat.title} ({event.chat.id})",
                )
            except Exception:
                pass
        try:
            await bot.send_message(event.chat.id, "Tentative de raccordement pirate détectée 😭")
        except Exception:
            pass
        try:
            await bot.leave_chat(event.chat.id)
        except Exception:
            pass


@router.chat_member()
async def member_update(event: ChatMemberUpdated, bot: Bot):
    await on_join(event, bot)


@router.message()
async def all_messages(msg: Message, bot: Bot):
    if msg.from_user:
        await upsert_user(msg.from_user)

    # Notifications entrée/sortie : supprimées, sauf pendant la justice populaire.
    if msg.chat.id == get_settings().main_group_id and (msg.new_chat_members or msg.left_chat_member):
        keep_removed = bool(
            msg.left_chat_member
            and await st.get_value("justice_running", "false") == "true"
        )
        if keep_removed:
            await track(
                msg.chat.id,
                msg.message_id,
                getattr(msg.left_chat_member, "id", None),
                "justice_removed_notification",
                False,
            )
        else:
            try:
                await bot.delete_message(msg.chat.id, msg.message_id)
            except Exception:
                pass
        return

    if msg.chat.type == "private":
        if await handle_crowd_text(msg):
            return
        if await handle_crowd_proof(bot, msg):
            return
        if await handle_vip_proof(bot, msg):
            return
        return

    if msg.text and await trusted_command(bot, msg):
        return

    # Point critique : aucun traitement ni copie VIP après un refus de modération.
    allowed = await moderate_message(bot, msg)
    if not allowed:
        return

    if msg.chat.id == get_settings().main_group_id:
        await copy_media_to_vip(bot, msg)
