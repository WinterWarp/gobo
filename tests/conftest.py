from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio

from gobo.config import Config, LLMConfig
from gobo.db import Database
from gobo.scheduler import Clock, Scheduler

TZ = ZoneInfo("America/Chicago")
# Monday 09:00 local — inside the default day window, outside sleep.
START = datetime(2026, 7, 20, 9, 0, tzinfo=TZ).timestamp()


class FakeClock(Clock):
    def __init__(self, start: float = START):
        super().__init__(scale=1.0, start=start)
        self._t = start

    def now(self) -> float:
        return self._t

    def advance(self, seconds: float) -> None:
        self._t += seconds


class FakeLLM:
    """Scripted stand-in for gobo.llm.LLM.

    text(): logs the directive it was asked to speak and returns a canned line.
    tool_loop(): pops a script step {"tools": [(name, args), ...], "text": "..."},
    invokes those tools against the real toolbox, returns the text.
    """

    def __init__(self):
        self.directives: list[str] = []
        self.script: list[dict] = []

    async def text(self, cfg, system, messages) -> str:
        directive = messages[-1]["content"]
        self.directives.append(directive)
        return f"<out {len(self.directives)}>"

    async def tool_loop(self, cfg, system, messages, toolbox, max_rounds=8) -> str:
        step = self.script.pop(0)
        for name, args in step.get("tools", []):
            result = await toolbox.handlers[name](args)
            step.setdefault("results", []).append(result)
        return step.get("text", "ok")


@pytest_asyncio.fixture
async def db():
    d = await Database.open(":memory:")
    yield d
    await d.close()


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def cfg():
    return Config(planner_llm=LLMConfig(model="planner"), manager_llm=LLMConfig(model="manager"))


@pytest.fixture
def scheduler(db, clock):
    return Scheduler(db, clock)


async def add_task(db, clock, title: str, **kw) -> int:
    cur = await db.execute(
        "INSERT INTO tasks (title, notes, stated_deadline, est_minutes, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (title, kw.get("notes", ""), kw.get("stated_deadline"), kw.get("est_minutes"),
         clock.now()),
    )
    return cur.lastrowid
