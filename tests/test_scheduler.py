import pytest


async def test_fires_due_timers_once(scheduler, clock):
    fired = []
    scheduler.on("ping", lambda payload, overdue: _record(fired, payload, overdue))
    await scheduler.schedule_in("ping", 60, {"n": 1})
    await scheduler.schedule_in("ping", 120, {"n": 2})

    assert await scheduler.tick() == 0
    clock.advance(61)
    await scheduler.tick()
    assert [p["n"] for p, _ in fired] == [1]
    clock.advance(60)
    await scheduler.tick()
    await scheduler.tick()  # already fired — must not repeat
    assert [p["n"] for p, _ in fired] == [1, 2]


async def test_cancel_with_payload_match(scheduler, clock):
    await scheduler.schedule_in("tripwire", 60, {"trip": "silent", "task_id": 1})
    await scheduler.schedule_in("tripwire", 60, {"trip": "deadline", "task_id": 1})
    removed = await scheduler.cancel(["tripwire"], match={"trip": "silent"})
    assert removed == 1
    assert await scheduler.pending("tripwire", match={"trip": "deadline"})
    assert not await scheduler.pending("tripwire", match={"trip": "silent"})


async def test_handler_error_is_retried_and_does_not_stop_tick(scheduler, clock):
    fired = []
    attempts = 0

    async def flaky(payload, overdue):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("boom")
        fired.append((payload, overdue))

    scheduler.on("bad", flaky)
    scheduler.on("good", lambda p, o: _record(fired, p, o))
    await scheduler.schedule_in("bad", 10)
    await scheduler.schedule_in("good", 20)
    clock.advance(30)
    await scheduler.tick()
    assert len(fired) == 1
    rows = await scheduler.db.fetchall("SELECT * FROM audit WHERE event='timer_handler_error'")
    assert len(rows) == 1
    failed = await scheduler.db.fetchone("SELECT * FROM timers WHERE kind = 'bad'")
    assert failed["fired_at"] is None
    assert failed["fire_at"] == clock.now() + 60

    clock.advance(60)
    await scheduler.tick()
    retried = await scheduler.db.fetchone("SELECT * FROM timers WHERE kind = 'bad'")
    assert retried["fired_at"] is not None
    assert attempts == 2


async def test_overdue_reported(scheduler, clock):
    seen = []
    scheduler.on("late", lambda p, o: _record(seen, p, o))
    await scheduler.schedule_in("late", 60)
    clock.advance(2000)
    await scheduler.tick()
    assert seen[0][1] == pytest.approx(1940)


async def _record(acc, payload, overdue):
    acc.append((payload, overdue))
