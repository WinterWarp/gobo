"""Configuration: .env for secrets, config.toml for behavior."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from pydantic import BaseModel, Field

ThinkingLevel = Literal["none", "low", "medium", "high", "xhigh", "max"]
Tone = Literal["terse_professional", "drill_sergeant", "neutral", "persuasive"]


class LLMConfig(BaseModel):
    model: str
    thinking_level: ThinkingLevel = "none"


class SleepConfig(BaseModel):
    start: str = "23:30"
    end: str = "07:30"


class EscalationConfig(BaseModel):
    max_attempts: int = 3
    backoff_minutes: list[int] = Field(default_factory=lambda: [10, 7, 5])


class DndConfig(BaseModel):
    max_grant_minutes: int = 90
    max_grants_per_day: int = 2


class ManagerConfig(BaseModel):
    tone: Tone = "terse_professional"
    resume_after_exhaust_minutes: int = 45


class Config(BaseModel):
    timezone: str = "America/Chicago"
    db_path: str = "gobo.db"
    daily_plan_time: str = "08:30"
    sleep: SleepConfig = Field(default_factory=SleepConfig)
    planner_llm: LLMConfig
    manager_llm: LLMConfig
    manager: ManagerConfig = Field(default_factory=ManagerConfig)
    escalation: EscalationConfig = Field(default_factory=EscalationConfig)
    dnd: DndConfig = Field(default_factory=DndConfig)

    # secrets, from env
    planner_bot_token: str = ""
    manager_bot_token: str = ""
    openrouter_api_key: str = ""
    allowed_user_id: int = 0

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


def load_config(path: str | None = None, require_secrets: bool = True) -> Config:
    load_dotenv()
    toml_path = Path(path or os.environ.get("GOBO_CONFIG", "config.toml"))
    raw = tomllib.loads(toml_path.read_text()) if toml_path.exists() else {}

    planner = raw.pop("planner", {})
    manager_raw = raw.pop("manager", {})
    manager_llm = manager_raw.pop("llm", {"model": "anthropic/claude-haiku-4.5"})

    cfg = Config(
        planner_llm=LLMConfig(**planner.get("llm", {"model": "anthropic/claude-opus-4.5"})),
        manager_llm=LLMConfig(**manager_llm),
        manager=ManagerConfig(**manager_raw),
        **raw,
        planner_bot_token=os.environ.get("PLANNER_BOT_TOKEN", ""),
        manager_bot_token=os.environ.get("MANAGER_BOT_TOKEN", ""),
        openrouter_api_key=os.environ.get("OPENROUTER_API_KEY", ""),
        allowed_user_id=int(os.environ.get("ALLOWED_TELEGRAM_USER_ID", "0")),
    )
    if require_secrets:
        missing = [
            name
            for name, val in [
                ("PLANNER_BOT_TOKEN", cfg.planner_bot_token),
                ("MANAGER_BOT_TOKEN", cfg.manager_bot_token),
                ("OPENROUTER_API_KEY", cfg.openrouter_api_key),
                ("ALLOWED_TELEGRAM_USER_ID", cfg.allowed_user_id),
            ]
            if not val
        ]
        if missing:
            raise SystemExit(f"Missing required env vars: {', '.join(missing)} (see .env.example)")
    return cfg
