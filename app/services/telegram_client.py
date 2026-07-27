from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Any

from aiogram import Bot
from aiogram.exceptions import (
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)

logger = logging.getLogger(__name__)


class ResilientBot(Bot):
    """Bot Telegram avec limitation globale et reprises sur erreurs temporaires.

    Tous les appels de l'API Aiogram passent par ``Bot.__call__``. Cette classe
    protège donc également les services et les handlers existants sans devoir
    remplacer chaque ``bot.send_message`` individuellement.
    """

    def __init__(
        self,
        *args: Any,
        max_concurrent_requests: int = 4,
        min_request_interval: float = 0.08,
        max_retries: int = 5,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._api_semaphore = asyncio.Semaphore(max(1, max_concurrent_requests))
        self._spacing_lock = asyncio.Lock()
        self._last_request_started = 0.0
        self._min_request_interval = max(0.0, min_request_interval)
        self._max_retries = max(1, max_retries)

    async def _wait_for_spacing(self) -> None:
        async with self._spacing_lock:
            now = time.monotonic()
            wait = self._min_request_interval - (now - self._last_request_started)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request_started = time.monotonic()

    async def __call__(
        self,
        method: Any,
        request_timeout: int | None = None,
    ) -> Any:
        method_name = method.__class__.__name__

        for attempt in range(1, self._max_retries + 1):
            try:
                async with self._api_semaphore:
                    await self._wait_for_spacing()
                    return await super().__call__(
                        method,
                        request_timeout=request_timeout,
                    )
            except TelegramRetryAfter as exc:
                # Telegram donne lui-même le délai à respecter.
                delay = max(float(exc.retry_after), 1.0) + 0.25
                logger.warning(
                    "Limite Telegram sur %s. Nouvelle tentative dans %.2f s (%s/%s).",
                    method_name,
                    delay,
                    attempt,
                    self._max_retries,
                )
            except (TelegramNetworkError, TelegramServerError, asyncio.TimeoutError) as exc:
                if attempt >= self._max_retries:
                    logger.error(
                        "Échec Telegram définitif sur %s après %s tentative(s) : %s",
                        method_name,
                        attempt,
                        exc,
                    )
                    raise

                # Reprise progressive avec un léger jitter pour éviter que tous
                # les jobs recommencent exactement au même instant.
                delay = min(2 ** (attempt - 1), 20) + random.uniform(0.1, 0.8)
                logger.warning(
                    "Erreur Telegram temporaire sur %s (%s). "
                    "Nouvelle tentative dans %.2f s (%s/%s).",
                    method_name,
                    exc,
                    delay,
                    attempt,
                    self._max_retries,
                )

            await asyncio.sleep(delay)

        raise RuntimeError("Boucle de reprise Telegram terminée sans résultat")
