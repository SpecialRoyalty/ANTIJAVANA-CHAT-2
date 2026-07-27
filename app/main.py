from __future__ import annotations

import asyncio
import logging
from contextlib import suppress

from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramNetworkError, TelegramServerError

from app.config import get_settings
from app.db.session import init_db
from app.handlers import admin, callbacks, group
from app.scheduler import start_scheduler
from app.services import settings as st
from app.services.settings import init_defaults
from app.services.state import cleanup_known_status_duplicates, ensure_status_message
from app.services.telegram_client import ResilientBot

logger = logging.getLogger(__name__)


async def wait_for_telegram(bot: Bot) -> object:
    """Attend que l'API Telegram réponde avant de poursuivre le démarrage.

    Les erreurs réseau temporaires et les réponses 5xx de Telegram ne doivent pas
    faire redémarrer le conteneur en boucle. Le délai augmente progressivement,
    avec un maximum de 60 secondes entre deux tentatives.
    """
    attempt = 0

    while True:
        attempt += 1
        try:
            me = await bot.get_me(request_timeout=30)
            logger.info(
                "Connexion Telegram établie : @%s (id=%s)",
                me.username or "sans_username",
                me.id,
            )
            return me
        except (TelegramServerError, TelegramNetworkError, asyncio.TimeoutError) as exc:
            delay = min(5 * attempt, 60)
            logger.warning(
                "API Telegram temporairement indisponible (%s). "
                "Nouvelle tentative dans %s s [tentative %s].",
                exc,
                delay,
                attempt,
            )
            await asyncio.sleep(delay)


async def initialize_status(bot: Bot, chat_id: int) -> None:
    """Initialise le message d'état sans empêcher le bot de démarrer."""
    try:
        await ensure_status_message(bot, chat_id)
        await cleanup_known_status_duplicates(bot, chat_id)
    except (TelegramServerError, TelegramNetworkError, asyncio.TimeoutError):
        logger.exception(
            "Initialisation du message d'état impossible pour le moment. "
            "Le scheduler réessaiera automatiquement."
        )
    except Exception:
        logger.exception("Échec inattendu pendant l'initialisation du message d'état.")


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )

    settings = get_settings()
    scheduler = None
    bot = ResilientBot(
        settings.bot_token,
        max_concurrent_requests=4,
        min_request_interval=0.08,
        max_retries=5,
    )

    try:
        await init_db()
        await init_defaults()

        me = await wait_for_telegram(bot)
        await st.set_value("bot_id", str(me.id))

        dispatcher = Dispatcher()
        dispatcher.include_router(admin.router)
        dispatcher.include_router(callbacks.router)
        dispatcher.include_router(group.router)

        scheduler = start_scheduler(bot)
        await initialize_status(bot, settings.main_group_id)

        logger.info("Démarrage du polling Telegram.")
        await dispatcher.start_polling(
            bot,
            allowed_updates=dispatcher.resolve_used_update_types(),
            close_bot_session=False,
        )
    except asyncio.CancelledError:
        logger.info("Arrêt demandé : fermeture propre du bot.")
        raise
    except Exception:
        logger.exception("Erreur fatale dans le processus principal du bot.")
        raise
    finally:
        if scheduler is not None and scheduler.running:
            with suppress(Exception):
                scheduler.shutdown(wait=False)

        with suppress(Exception):
            await bot.session.close()

        logger.info("Session Telegram fermée proprement.")


if __name__ == "__main__":
    asyncio.run(main())
