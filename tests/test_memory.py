"""Shared memory: tools, chat notices, prompt injection, and the task-inbox flow."""

from __future__ import annotations

from gobo.llm import Toolbox
from gobo.manager.agent import build_toolbox, handle_user_message
from gobo.manager.loop import ManagerEngine
from gobo.memory import add_memory_tools, memory_block
from gobo.planner.agent import PlannerAgent

from conftest import FakeLLM


async def test_save_update_delete_cycle_with_notices(db, clock):
    sent: list[str] = []

    async def send(text: str) -> None:
        sent.append(text)

    tb = Toolbox()
    add_memory_tools(tb, db, clock, send, "manager")

    result = await tb.handlers["save_memory"]({"key": "gym", "content": "Tue/Thu 07:00"})
    assert result.startswith("saved")
    assert sent[-1] == "💾 Memory saved — gym: Tue/Thu 07:00"

    result = await tb.handlers["save_memory"]({"key": "gym", "content": "Mon/Wed 07:00"})
    assert result.startswith("updated")
    assert sent[-1].startswith("💾 Memory updated — gym")
    assert "Mon/Wed 07:00" in await memory_block(db)

    assert await tb.handlers["delete_memory"]({"key": "gym"}) == "deleted"
    assert sent[-1] == "🗑️ Memory deleted — gym"
    assert "gym" not in await memory_block(db)
    assert (await tb.handlers["delete_memory"]({"key": "gym"})).startswith("error")


async def test_manager_capture_reaches_planner_system_prompt(db, clock, cfg, scheduler):
    manager_sent: list[str] = []

    async def msend(text: str) -> None:
        manager_sent.append(text)

    engine = ManagerEngine(db, clock, cfg, FakeLLM(), scheduler, msend)
    engine.register()

    # idle phase still carries the memory tools: mentioned work lands in the inbox
    assert "save_memory" in build_toolbox(engine, "idle", None).handlers
    engine.llm.script.append(
        {
            "tools": [
                (
                    "save_memory",
                    {
                        "key": "dentist",
                        "content": "book a dentist appointment",
                        "category": "task_inbox",
                    },
                )
            ],
            "text": "Noted for the Planner.",
        }
    )
    await handle_user_message(engine, "oh, I also need to book a dentist appointment sometime")
    assert any(n.startswith("📥") and "dentist" in n for n in manager_sent)

    async def psend(text: str) -> None:
        pass

    planner = PlannerAgent(db, clock, cfg, FakeLLM(), scheduler, engine, psend)
    system = await planner._build_system()
    assert "Task inbox" in system and "dentist" in system
    assert "save_memory" in planner._toolbox().handlers
