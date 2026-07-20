from __future__ import annotations
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from app.keyboards.inline import status_keyboard
from app.services.state import get_group_state, save_status_message, status_text

async def ensure_status_message(bot:Bot, chat_id:int) -> None:
    st=await get_group_state(chat_id)
    text=await status_text(chat_id)
    kb = None if st.is_open or (not st.auto_enabled and not st.is_open) else status_keyboard()
    if st.status_message_id:
        try:
            await bot.edit_message_text(text, chat_id=chat_id, message_id=st.status_message_id, reply_markup=kb)
            await save_status_message(chat_id, st.status_message_id, text)
            return
        except TelegramBadRequest as e:
            msg=str(e).lower()
            if 'message is not modified' in msg:
                return
            # if deleted/not found, recreate one
    m=await bot.send_message(chat_id, text, reply_markup=kb)
    await save_status_message(chat_id, m.message_id, text)

async def safe_delete(bot:Bot, chat_id:int, message_id:int) -> bool:
    try:
        await bot.delete_message(chat_id, message_id)
        return True
    except Exception:
        return False

async def temp_reply_delete(bot:Bot, chat_id:int, text:str, seconds:int=8):
    m=await bot.send_message(chat_id, text)
    # deletion scheduled in scheduler would be better; lightweight fire-and-forget not here
    return m.message_id
