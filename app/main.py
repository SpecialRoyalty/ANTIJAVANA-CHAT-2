import asyncio, logging
from aiogram import Bot, Dispatcher
from app.config import get_settings
from app.db.session import init_db
from app.services.settings import init_defaults
from app.services import settings as st
from app.services.state import ensure_status_message, cleanup_known_status_duplicates
from app.handlers import admin, callbacks, group
from app.scheduler import start_scheduler

async def main():
    logging.basicConfig(level=logging.INFO)
    s=get_settings()
    await init_db(); await init_defaults()
    bot=Bot(s.bot_token)
    me=await bot.get_me()
    await st.set_value('bot_id', str(me.id))
    dp=Dispatcher()
    dp.include_router(admin.router); dp.include_router(callbacks.router); dp.include_router(group.router)
    start_scheduler(bot)
    try:
        await ensure_status_message(bot,s.main_group_id)
        await cleanup_known_status_duplicates(bot,s.main_group_id)
    except Exception as e:
        logging.exception('status init failed')
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
if __name__=='__main__': asyncio.run(main())
