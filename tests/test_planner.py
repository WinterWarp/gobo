from gobo.manager.loop import ManagerEngine
from gobo.planner.agent import PlannerAgent

from conftest import FakeLLM


async def test_daily_plan_timer_arms_tomorrow_while_current_timer_is_running(
    db, clock, cfg, scheduler
):
    llm = FakeLLM()
    manager = ManagerEngine(db, clock, cfg, llm, scheduler, lambda text: _ignore(text))
    sent = []

    async def send(text: str) -> None:
        sent.append(text)

    planner = PlannerAgent(db, clock, cfg, llm, scheduler, manager, send)
    planner.register()
    llm.script.append({"text": "Morning. What's on for today?"})
    await scheduler.schedule_in("daily_plan", 60)

    clock.advance(61)
    await scheduler.tick()

    assert sent == ["Morning. What's on for today?"]
    rows = await db.fetchall(
        "SELECT * FROM timers WHERE kind = 'daily_plan' AND fired_at IS NULL"
    )
    assert len(rows) == 1
    assert rows[0]["fire_at"] > clock.now()


async def test_session_context_timestamps_user_messages(db, clock, cfg, scheduler):
    llm = FakeLLM()
    manager = ManagerEngine(db, clock, cfg, llm, scheduler, _ignore)
    planner = PlannerAgent(db, clock, cfg, llm, scheduler, manager, _ignore)

    await db.add_message("planner", "user", "plan my day", clock.now())
    await db.add_message("planner", "assistant", "sure", clock.now())

    ctx = await planner._session_context()
    assert ctx[0]["content"] == "[Mon 2026-07-20 09:00] plan my day"
    assert ctx[1]["content"] == "sure"  # only user messages carry the stamp


async def _ignore(text: str) -> None:
    pass
