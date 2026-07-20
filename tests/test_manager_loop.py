"""End-to-end Manager engine tests on a fake clock and scripted LLM — the
compressed-day acceptance run, no network involved."""

from __future__ import annotations

import random

import pytest

from gobo.manager.agent import handle_user_message
from gobo.manager.loop import ManagerEngine
from gobo.models import Policy
from gobo.planner.compile import validate_and_activate

from conftest import FakeLLM, add_task


@pytest.fixture(autouse=True)
def _seed():
    random.seed(1234)


def policy_dict(task_ids: list[int], **overrides) -> dict:
    base = {
        "day_window": {"start": "08:00", "end": "23:00"},
        "escalation": {"max_attempts": 3, "backoff_minutes": [10, 7, 5]},
        "dnd": {"max_grant_minutes": 90, "max_grants_per_day": 2},
        "queue": [
            {
                "task_id": tid,
                "checkin_interval_minutes": {"min": 10, "max": 20},
                "start_confirm_within_minutes": 60,
            }
            for tid in task_ids
        ],
        "tripwires": [],
    }
    base.update(overrides)
    return base


@pytest.fixture
def engine(db, clock, cfg, scheduler):
    llm = FakeLLM()
    sent: list[str] = []

    async def send(text: str) -> None:
        sent.append(text)

    eng = ManagerEngine(db, clock, cfg, llm, scheduler, send)
    eng.register()
    eng.sent = sent
    eng.fake_llm = llm
    return eng


async def activate(db, clock, engine, raw: dict) -> None:
    Policy.model_validate(raw)  # sanity: test fixture policies must be valid
    result = await validate_and_activate(db, clock, "America/Chicago", raw, engine)
    assert result.startswith("ok"), result


async def run_until(engine, clock, scheduler, predicate, max_hours=8, step=60):
    for _ in range(int(max_hours * 3600 / step)):
        if await predicate():
            return
        clock.advance(step)
        await scheduler.tick()
    raise AssertionError("condition never reached")


async def test_assign_checkin_escalate_exhaust_then_resume(db, clock, cfg, scheduler, engine):
    t1 = await add_task(db, clock, "write report")
    t2 = await add_task(db, clock, "email accountant")
    await activate(db, clock, engine, policy_dict([t1, t2]))

    # activation with no in-flight task schedules an assign shortly
    clock.advance(120)
    await scheduler.tick()
    assert await engine.current_task_id() == t1
    task = await engine.task_row(t1)
    assert task["status"] == "active"
    assert len(engine.sent) == 1  # assignment message
    assert "Assign the current task" in engine.fake_llm.directives[0]
    assert await scheduler.pending("checkin")

    # check-in fires within bounds, arms the escalation chain; three unanswered
    # nudges then the task is marked unverified and the Manager goes quiet
    async def exhausted():
        row = await db.fetchone("SELECT status FROM tasks WHERE id = ?", (t1,))
        return row["status"] == "unverified"

    await run_until(engine, clock, scheduler, exhausted, max_hours=2)
    directives = engine.fake_llm.directives
    assert sum("Check in now" in d for d in directives) == 1
    assert sum("not answered" in d for d in directives) == 3
    assert await engine.current_task_id() is None
    # quiet, but a resume assign is queued
    assert await scheduler.pending("assign")

    # resume assigns the next task
    clock.advance(cfg.manager.resume_after_exhaust_minutes * 60 + 60)
    await scheduler.tick()
    assert await engine.current_task_id() == t2


async def test_failed_assignment_delivery_rolls_back_and_retries(
    db, clock, cfg, scheduler, engine
):
    t1 = await add_task(db, clock, "write report")
    original_text = engine.fake_llm.text
    attempts = 0

    async def fail_once(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("temporary LLM failure")
        return await original_text(*args, **kwargs)

    engine.fake_llm.text = fail_once
    await activate(db, clock, engine, policy_dict([t1]))

    clock.advance(120)
    await scheduler.tick()
    row = await db.fetchone("SELECT status FROM tasks WHERE id = ?", (t1,))
    assert row["status"] == "pending"
    assert await engine.current_task_id() is None
    assert await scheduler.pending("assign")

    clock.advance(60)
    await scheduler.tick()
    row = await db.fetchone("SELECT status FROM tasks WHERE id = ?", (t1,))
    assert row["status"] == "active"
    assert await engine.current_task_id() == t1
    assert attempts == 2


async def test_reply_clears_escalation_and_reschedules_checkin(db, clock, cfg, scheduler, engine):
    t1 = await add_task(db, clock, "write report")
    await activate(db, clock, engine, policy_dict([t1]))
    clock.advance(120)
    await scheduler.tick()

    async def awaiting():
        return bool(await engine._get("awaiting", False))

    await run_until(engine, clock, scheduler, awaiting, max_hours=1)
    engine.fake_llm.script.append({"text": "Keep going."})
    await handle_user_message(engine, "still on the report, halfway through")
    assert not await engine._get("awaiting", False)
    assert not await scheduler.pending("escalation")
    assert await scheduler.pending("checkin")  # rhythm restored


async def test_done_claim_verify_confirm_advances_queue(db, clock, cfg, scheduler, engine):
    t1 = await add_task(db, clock, "write report")
    t2 = await add_task(db, clock, "email accountant")
    await activate(db, clock, engine, policy_dict([t1, t2]))
    clock.advance(120)
    await scheduler.tick()
    assert await engine.current_task_id() == t1

    engine.fake_llm.script.append({"tools": [("mark_done", {})], "text": "What's in the final doc?"})
    await handle_user_message(engine, "done with the report")
    assert await engine.phase() == "verifying"

    engine.fake_llm.script.append({"tools": [("confirm_done", {})], "text": "Good."})
    await handle_user_message(engine, "three sections plus the appendix, sent to Jim")
    row = await db.fetchone("SELECT status, completed_at FROM tasks WHERE id = ?", (t1,))
    assert row["status"] == "done" and row["completed_at"] is not None

    clock.advance(200)
    await scheduler.tick()
    assert await engine.current_task_id() == t2


async def test_dnd_grants_are_bounded(db, clock, cfg, scheduler, engine):
    t1 = await add_task(db, clock, "write report")
    await activate(db, clock, engine, policy_dict([t1]))
    clock.advance(120)
    await scheduler.tick()

    assert (await engine.grant_dnd(200)).startswith("DENIED")  # over per-grant cap
    assert (await engine.grant_dnd(60)).startswith("GRANTED")
    assert (await engine.grant_dnd(30)).startswith("GRANTED")
    assert (await engine.grant_dnd(10)).startswith("DENIED")   # daily grant count spent

    # while DND is active the check-in gate defers instead of pinging
    sent_before = len(engine.sent)
    await engine.on_checkin({"task_id": t1}, 0)
    assert len(engine.sent) == sent_before
    assert await scheduler.pending("checkin")


async def test_escalation_waits_for_silence_window_to_end(db, clock, cfg, scheduler, engine):
    t1 = await add_task(db, clock, "write report")
    raw = policy_dict(
        [t1],
        silence=[{"start": "09:05", "end": "10:30", "reason": "meeting"}],
    )
    await activate(db, clock, engine, raw)
    clock.advance(120)
    await scheduler.tick()

    # A check-in immediately before the meeting arms an escalation due during silence.
    clock.advance(120)
    await engine.on_checkin({"task_id": t1}, 0)
    for _ in range(35):
        clock.advance(60)
        await scheduler.tick()

    row = await db.fetchone("SELECT status FROM tasks WHERE id = ?", (t1,))
    assert row["status"] == "active"
    assert await engine.current_task_id() == t1
    assert await engine._get("escalation_attempt", 0) == 0
    assert await scheduler.pending("escalation")


async def test_silence_window_defers_assignment(db, clock, cfg, scheduler, engine):
    t1 = await add_task(db, clock, "write report")
    raw = policy_dict([t1])
    raw["queue"][0]["window"] = {"not_before": "10:00"}
    await activate(db, clock, engine, policy_dict([t1], **{"queue": raw["queue"]}))
    clock.advance(120)
    await scheduler.tick()
    # 09:02, task not eligible until 10:00 — nothing assigned, wake-up queued
    assert await engine.current_task_id() is None
    assert await scheduler.pending("assign")
    clock.advance(3600)  # ~10:02
    await scheduler.tick()
    assert await engine.current_task_id() == t1


async def test_replan_keeps_in_flight_task(db, clock, cfg, scheduler, engine):
    t1 = await add_task(db, clock, "write report")
    t2 = await add_task(db, clock, "email accountant")
    await activate(db, clock, engine, policy_dict([t1]))
    clock.advance(120)
    await scheduler.tick()
    assert await engine.current_task_id() == t1

    await activate(db, clock, engine, policy_dict([t1, t2]))
    assert await engine.current_task_id() == t1  # seamless
    row = await db.fetchone("SELECT status FROM tasks WHERE id = ?", (t1,))
    assert row["status"] == "active"
    assert await scheduler.pending("checkin")


async def test_replan_resets_escalation_and_chat_context(db, clock, cfg, scheduler, engine):
    t1 = await add_task(db, clock, "write report")
    await activate(db, clock, engine, policy_dict([t1]))
    clock.advance(120)
    await scheduler.tick()
    await engine.on_checkin({"task_id": t1}, 0)
    assert await engine._get("awaiting", False)
    assert await engine._chat_tail(20)  # assign + checkin messages are in context

    await activate(db, clock, engine, policy_dict([t1]))

    # the user just replanned: pre-replan asks must not read as ignored, so the
    # escalation chain is dropped and a fresh check-in rhythm starts
    assert not await engine._get("awaiting", False)
    assert await engine._get("escalation_attempt", 0) == 0
    assert not await scheduler.pending("escalation")
    assert await scheduler.pending("checkin")
    assert await engine._chat_tail(20) == []  # context restarts at the reset marker


async def test_unrelated_message_does_not_confirm_task_started(
    db, clock, cfg, scheduler, engine
):
    t1 = await add_task(db, clock, "write report")
    await activate(db, clock, engine, policy_dict([t1]))
    clock.advance(120)
    await scheduler.tick()

    engine.fake_llm.script.append(
        {"tools": [("defer_to_planner", {"text": "swap tasks"})], "text": "Ask Planner."}
    )
    await handle_user_message(engine, "Can I swap this for the email task?")
    assert not await engine._get("started", False)

    engine.fake_llm.script.append(
        {"tools": [("note_progress", {"text": "started outlining"})], "text": "Good."}
    )
    await handle_user_message(engine, "I've started outlining it.")
    assert await engine._get("started", False)


async def test_replan_drops_in_flight_task(db, clock, cfg, scheduler, engine):
    t1 = await add_task(db, clock, "write report")
    t2 = await add_task(db, clock, "email accountant")
    await activate(db, clock, engine, policy_dict([t1]))
    clock.advance(120)
    await scheduler.tick()
    assert await engine.current_task_id() == t1

    await activate(db, clock, engine, policy_dict([t2]))
    assert await engine.current_task_id() is None
    row = await db.fetchone("SELECT status FROM tasks WHERE id = ?", (t1,))
    assert row["status"] == "pending"  # not lost, just deprioritized by the Planner
    clock.advance(120)
    await scheduler.tick()
    assert await engine.current_task_id() == t2


async def test_compile_rejects_bad_queue_refs(db, clock, cfg, scheduler, engine):
    t1 = await add_task(db, clock, "write report")
    await db.execute("UPDATE tasks SET status = 'unverified' WHERE id = ?", (t1,))
    result = await validate_and_activate(
        db, clock, "America/Chicago", policy_dict([t1, 999]), engine
    )
    assert result.startswith("error") and "999" in result
    row = await db.fetchone("SELECT status FROM tasks WHERE id = ?", (t1,))
    assert row["status"] == "unverified"  # rejected drafts have no side effects

    await db.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (t1,))
    result = await validate_and_activate(db, clock, "America/Chicago", policy_dict([t1]), engine)
    assert result.startswith("error") and "done" in result


async def test_user_messages_and_directives_are_timestamped(db, clock, cfg, scheduler, engine):
    t1 = await add_task(db, clock, "write report")
    await activate(db, clock, engine, policy_dict([t1]))
    clock.advance(120)
    await scheduler.tick()

    # the assign directive carries the wall clock (Mon 2026-07-20 09:02 local)
    assert engine.fake_llm.directives[0].startswith("[Mon 2026-07-20 09:02]")

    engine.fake_llm.script.append({"text": "Noted."})
    await handle_user_message(engine, "on it")
    user_msgs = [m for m in await engine._chat_tail(10) if m["role"] == "user"]
    assert user_msgs[-1]["content"] == "[Mon 2026-07-20 09:02] on it"


async def test_set_next_checkin_reschedules_and_clamps(db, clock, cfg, scheduler, engine):
    t1 = await add_task(db, clock, "write report")
    await activate(db, clock, engine, policy_dict([t1]))
    clock.advance(120)
    await scheduler.tick()

    result = await engine.set_next_checkin(40, "says done in 40")
    assert "~40" in result
    rows = await db.fetchall("SELECT * FROM timers WHERE kind='checkin' AND fired_at IS NULL")
    assert len(rows) == 1  # the policy-drawn check-in was replaced, not stacked
    assert rows[0]["fire_at"] == pytest.approx(clock.now() + 40 * 60)

    assert "~120" in await engine.set_next_checkin(500, "way too long")

    await engine._clear_current()
    assert (await engine.set_next_checkin(30)).startswith("DENIED")


async def test_task_slice_carries_policy_terms(db, clock, cfg, scheduler, engine):
    t1 = await add_task(db, clock, "write report", notes="quarterly numbers")
    raw = policy_dict([t1])
    raw["queue"][0].update(
        internal_deadline="2026-07-20T11:00",
        stated_deadline="2026-07-20T17:00",
        verify_hint="ask for the doc link",
        guidance="outline before inbox",
    )
    await activate(db, clock, engine, raw)
    policy = await engine.policy()
    task = await engine.task_row(t1)

    s = engine._task_slice(task, policy.entry_for(t1))
    assert "Deadline: 2026-07-20T11:00" in s
    assert "2026-07-20T17:00" in s and "never acknowledge" in s
    assert "ask for the doc link" in s
    assert "outline before inbox" in s
    assert "10–20 min" in s


async def test_internal_deadline_tripwire_fires_urgent_nudge(db, clock, cfg, scheduler, engine):
    t1 = await add_task(db, clock, "write report")
    raw = policy_dict([t1])
    raw["queue"][0]["internal_deadline"] = "2026-07-20T09:30"
    await activate(db, clock, engine, raw)
    clock.advance(120)
    await scheduler.tick()
    assert await engine.current_task_id() == t1

    clock.advance(35 * 60)  # past 09:30
    await scheduler.tick()
    assert any("real urgency" in d for d in engine.fake_llm.directives)
    rows = await db.fetchall("SELECT * FROM audit WHERE event='internal_deadline_passed'")
    assert len(rows) == 1
