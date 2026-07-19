"""aiogram wiring for the Planner bot."""

from __future__ import annotations

import logging

from aiogram import Dispatcher
from aiogram.types import Message

from .agent import PlannerAgent

log = logging.getLogger(__name__)


def build_dispatcher(agent: PlannerAgent, allowed_user_id: int) -> Dispatcher:
    dp = Dispatcher()

    @dp.message()
    async def on_message(message: Message) -> None:
        if message.from_user is None or message.from_user.id != allowed_user_id:
            return
        if not message.text:
            await message.answer("Text only.")
            return
        try:
            await agent.handle_user_message(message.text)
        except Exception:
            log.exception("planner failed to handle message")
            await message.answer("(internal error — logged)")

    return dp
