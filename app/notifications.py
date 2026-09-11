from __future__ import annotations

import logging

from telegram import Bot
from telegram.error import TelegramError

log = logging.getLogger(__name__)


class TelegramNotifier:
    """Best-effort notifications. Notification failure never changes trade state."""
    def __init__(self, token: str):
        self.bot = Bot(token)

    async def send(self, user_id: int, text: str) -> None:
        try:
            await self.bot.send_message(chat_id=user_id, text=text)
        except TelegramError as exc:
            log.warning("Could not notify user %s: %s", user_id, exc)

    async def close(self) -> None:
        await self.bot.close()
