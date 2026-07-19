"""DB-backed scheduler: a `timers` table plus an asyncio ticker.

Fire times are drawn once and persisted, so restarts neither lose nor duplicate
pings. The Clock supports acceleration (GOBO_TIME_SCALE) so a simulated day can
run in minutes during development."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time as _time
from datetime import datetime
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo

from .db import Database

log = logging.getLogger(__name__)

Handler = Callable[[dict, float], Awaitable[None]]  # (payload, overdue_seconds)
HANDLER_RETRY_SECONDS = 60


class Clock:
    """Virtual time = start + (real elapsed) * scale."""

    def __init__(self, scale: float = 1.0, start: float | None = None):
        self.scale = scale
        self._real0 = _time.time()
        self._virt0 = start if start is not None else self._real0

    def now(self) -> float:
        return self._virt0 + (_time.time() - self._real0) * self.scale

    def dt(self, tz: ZoneInfo) -> datetime:
        return datetime.fromtimestamp(self.now(), tz)

    def real_seconds(self, virtual_seconds: float) -> float:
        return virtual_seconds / self.scale


def clock_from_env() -> Clock:
    scale = float(os.environ.get("GOBO_TIME_SCALE", "1"))
    start_iso = os.environ.get("GOBO_TIME_START")
    start = datetime.fromisoformat(start_iso).timestamp() if start_iso else None
    return Clock(scale=scale, start=start)


class Scheduler:
    def __init__(self, db: Database, clock: Clock, tick_seconds: float = 5.0):
        self.db = db
        self.clock = clock
        self.tick_seconds = tick_seconds
        self.handlers: dict[str, Handler] = {}
        self._stop = asyncio.Event()

    def on(self, kind: str, handler: Handler) -> None:
        self.handlers[kind] = handler

    async def schedule(self, kind: str, fire_at: float, payload: dict | None = None) -> int:
        cur = await self.db.execute(
            "INSERT INTO timers (kind, fire_at, payload, created_at) VALUES (?, ?, ?, ?)",
            (kind, fire_at, json.dumps(payload or {}), self.clock.now()),
        )
        assert cur.lastrowid is not None
        return cur.lastrowid

    async def schedule_in(self, kind: str, seconds: float, payload: dict | None = None) -> int:
        return await self.schedule(kind, self.clock.now() + seconds, payload)

    async def cancel(self, kinds: list[str], match: dict[str, Any] | None = None) -> int:
        """Delete unfired timers of the given kinds whose payload contains `match`."""
        qmarks = ",".join("?" for _ in kinds)
        rows = await self.db.fetchall(
            f"SELECT id, payload FROM timers WHERE fired_at IS NULL AND kind IN ({qmarks})",
            tuple(kinds),
        )
        doomed = []
        for row in rows:
            payload = json.loads(row["payload"])
            if match is None or all(payload.get(k) == v for k, v in match.items()):
                doomed.append(row["id"])
        for tid in doomed:
            await self.db.execute("DELETE FROM timers WHERE id = ? AND fired_at IS NULL", (tid,))
        return len(doomed)

    async def pending(self, kind: str, match: dict[str, Any] | None = None) -> bool:
        rows = await self.db.fetchall(
            "SELECT payload FROM timers WHERE fired_at IS NULL AND kind = ?", (kind,)
        )
        for row in rows:
            payload = json.loads(row["payload"])
            if match is None or all(payload.get(k) == v for k, v in match.items()):
                return True
        return False

    async def tick(self) -> int:
        """Run all due timers and mark only successful handlers as fired.

        A failed handler is moved slightly into the future so transient API errors
        do not lose the event or create a tight retry loop.
        """
        now = self.clock.now()
        due = await self.db.fetchall(
            "SELECT * FROM timers WHERE fired_at IS NULL AND fire_at <= ? ORDER BY fire_at",
            (now,),
        )
        fired = 0
        for row in due:
            handler = self.handlers.get(row["kind"])
            if handler is None:
                log.warning("no handler for timer kind %r", row["kind"])
                await self.db.execute(
                    "UPDATE timers SET fired_at = ? WHERE id = ? AND fired_at IS NULL",
                    (now, row["id"]),
                )
                fired += 1
                continue
            overdue = now - row["fire_at"]
            try:
                await handler(json.loads(row["payload"]), overdue)
            except Exception:
                log.exception("timer handler %r failed", row["kind"])
                await self.db.execute(
                    "UPDATE timers SET fire_at = ? WHERE id = ? AND fired_at IS NULL",
                    (now + HANDLER_RETRY_SECONDS, row["id"]),
                )
                await self.db.audit(
                    now, "system", "timer_handler_error", kind=row["kind"], timer_id=row["id"]
                )
                continue
            cur = await self.db.execute(
                "UPDATE timers SET fired_at = ? WHERE id = ? AND fired_at IS NULL",
                (now, row["id"]),
            )
            fired += cur.rowcount
        return fired

    async def run(self) -> None:
        while not self._stop.is_set():
            await self.tick()
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=max(self.tick_seconds / max(self.clock.scale, 1), 0.2)
                )
            except asyncio.TimeoutError:
                pass

    def stop(self) -> None:
        self._stop.set()
