"""Process supervisor: two Telegram bots + the timer ticker in one asyncio loop."""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot

from .config import Config, load_config
from .db import Database
from .llm import LLM
from .manager.bot import build_dispatcher as build_manager_dp
from .manager.loop import ManagerEngine
from .planner.agent import PlannerAgent
from .planner.bot import build_dispatcher as build_planner_dp
from .scheduler import Scheduler, clock_from_env

log = logging.getLogger(__name__)

TELEGRAM_CHUNK = 4000  # Telegram hard limit is 4096 chars per message


def chunked_sender(bot: Bot, chat_id: int):
    async def send(text: str) -> None:
        for i in range(0, len(text), TELEGRAM_CHUNK):
            await bot.send_message(chat_id, text[i : i + TELEGRAM_CHUNK])

    return send


async def run(cfg: Config | None = None) -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    cfg = cfg or load_config()
    clock = clock_from_env()
    if clock.scale != 1.0:
        log.warning("running with GOBO_TIME_SCALE=%s — virtual time is accelerated", clock.scale)

    db = await Database.open(cfg.db_path)
    llm = LLM(cfg.openrouter_api_key)
    scheduler = Scheduler(db, clock)

    planner_bot = Bot(cfg.planner_bot_token)
    manager_bot = Bot(cfg.manager_bot_token)

    manager = ManagerEngine(
        db, clock, cfg, llm, scheduler, chunked_sender(manager_bot, cfg.allowed_user_id)
    )
    planner = PlannerAgent(
        db, clock, cfg, llm, scheduler, manager, chunked_sender(planner_bot, cfg.allowed_user_id)
    )
    manager.register()
    planner.register()
    await planner.ensure_daily_timer()
    await manager.ensure_alive()

    planner_dp = build_planner_dp(planner, cfg.allowed_user_id)
    manager_dp = build_manager_dp(manager, cfg.allowed_user_id)

    log.info("gobo up: planner + manager polling, ticker running")
    try:
        await asyncio.gather(
            planner_dp.start_polling(planner_bot, handle_signals=False),
            manager_dp.start_polling(manager_bot, handle_signals=False),
            scheduler.run(),
        )
    finally:
        scheduler.stop()
        await planner_bot.session.close()
        await manager_bot.session.close()
        await db.close()
