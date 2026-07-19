"""aiogram wiring for the Manager bot."""

from __future__ import annotations

import logging

from aiogram import Dispatcher
from aiogram.types import Message

from .agent import handle_user_message
from .loop import ManagerEngine

log = logging.getLogger(__name__)


def build_dispatcher(engine: ManagerEngine, allowed_user_id: int) -> Dispatcher:
    dp = Dispatcher()

    @dp.message()
    async def on_message(message: Message) -> None:
        if message.from_user is None or message.from_user.id != allowed_user_id:
            return
        if not message.text:
            await message.answer("Text only.")
            return
        try:
            await handle_user_message(engine, message.text)
        except Exception:
            log.exception("manager failed to handle message")
            await message.answer("(internal error — logged)")

    return dp
