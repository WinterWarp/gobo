"""The Manager engine: executes the active policy off the timers table.

Timer kinds owned here: assign, checkin, escalation, tripwire, dnd_end.
State keys are namespaced "manager.*" in runtime_state."""

from __future__ import annotations

import logging
import random
from datetime import datetime
from typing import Awaitable, Callable

from ..config import Config
from ..db import Database
from ..llm import LLM
from ..memory import memory_block
from ..models import (
    Policy,
    QueueEntry,
    fmt_stamp,
    hhmm_at,
    is_silent,
    next_allowed,
    parse_local_dt,
)
from ..scheduler import Clock, Scheduler
from . import prompts

log = logging.getLogger(__name__)

STALE_CHECKIN_SECONDS = 900  # a check-in overdue past this (e.g. downtime) is redrawn, not fired

MANAGER_TIMERS = ["assign", "checkin", "escalation", "tripwire"]

# Bounds for the Manager's own set_next_checkin overrides, minutes.
CHECKIN_OVERRIDE_MIN = 2
CHECKIN_OVERRIDE_MAX = 120


class ManagerEngine:
    def __init__(
        self,
        db: Database,
        clock: Clock,
        cfg: Config,
        llm: LLM,
        scheduler: Scheduler,
        send: Callable[[str], Awaitable[None]],
    ):
        self.db = db
        self.clock = clock
        self.cfg = cfg
        self.llm = llm
        self.scheduler = scheduler
        self._send = send

    def register(self) -> None:
        self.scheduler.on("assign", self.on_assign)
        self.scheduler.on("checkin", self.on_checkin)
        self.scheduler.on("escalation", self.on_escalation)
        self.scheduler.on("tripwire", self.on_tripwire)
        self.scheduler.on("dnd_end", self.on_dnd_end)

    # --- state helpers ---

    async def policy(self) -> Policy | None:
        raw = await self.db.active_policy_json()
        return Policy.model_validate_json(raw) if raw else None

    async def current_task_id(self) -> int | None:
        return await self.db.state_get("manager.current_task_id")

    async def phase(self) -> str:
        return await self.db.state_get("manager.phase", "idle")

    async def _set(self, key: str, value: object) -> None:
        await self.db.state_set(f"manager.{key}", value)

    async def _get(self, key: str, default: object = None) -> object:
        return await self.db.state_get(f"manager.{key}", default)

    async def task_row(self, task_id: int) -> dict | None:
        row = await self.db.fetchone("SELECT * FROM tasks WHERE id = ?", (task_id,))
        return dict(row) if row else None

    def _task_slice(self, task: dict, entry: QueueEntry | None = None) -> str:
        parts = [f"Title: {task['title']}"]
        if task.get("notes"):
            parts.append(f"Notes: {task['notes']}")
        if entry is not None:
            if entry.internal_deadline:
                parts.append(f"Deadline: {entry.internal_deadline}")
                if entry.stated_deadline and entry.stated_deadline != entry.internal_deadline:
                    parts.append(
                        f"(The user may believe the deadline is {entry.stated_deadline}. "
                        f"It is not. Enforce {entry.internal_deadline} and never acknowledge "
                        "any other date.)"
                    )
            if entry.window.not_before or entry.window.not_after:
                parts.append(
                    f"Work window: {entry.window.not_before or 'any'} → "
                    f"{entry.window.not_after or 'any'}"
                )
            b = entry.checkin_interval_minutes
            parts.append(
                f"Default check-in rhythm: every {b.min}–{b.max} min (never reveal this)"
            )
            if entry.verify_hint:
                parts.append(f"Verify hint: {entry.verify_hint}")
            if entry.guidance:
                parts.append(f"Planner guidance (fine to share): {entry.guidance}")
        return "\n".join(parts)

    # --- speech gating (silence windows, day window, DND) ---

    async def speech_gate(self, policy: Policy) -> float | None:
        """None if the Manager may speak now, else the epoch when it next may."""
        now = self.clock.now()
        gate: float | None = None
        dnd_until = await self._get("dnd_until", 0) or 0
        if float(dnd_until) > now:
            gate = float(dnd_until)
        now_dt = self.clock.dt(self.cfg.tz)
        if is_silent(policy, now_dt):
            allowed = next_allowed(policy, now_dt).timestamp()
            gate = max(gate or 0, allowed)
        return gate

    async def _gated_defer(self, policy: Policy, kind: str, payload: dict) -> bool:
        """If speech is gated, requeue this event for when it opens. True if deferred."""
        gate = await self.speech_gate(policy)
        if gate is None:
            return False
        await self.scheduler.schedule(kind, gate + random.uniform(30, 120), payload)
        return True

    # --- outbound ---

    async def say(self, directive_kind: str, task: dict | None, **fmt: object) -> None:
        policy = await self.policy()
        tone = policy.manager_style.tone if policy else self.cfg.manager.tone
        entry = policy.entry_for(task["id"]) if (policy and task) else None
        task_slice = self._task_slice(task, entry) if task else "(none)"
        now = fmt_stamp(self.clock.dt(self.cfg.tz))
        tail = await self._chat_tail(8)
        directive = f"[{now}] {prompts.directive(directive_kind, **fmt)}"
        text = await self.llm.text(
            self.cfg.manager_llm,
            prompts.outbound_system(tone, task_slice, now, await memory_block(self.db)),
            tail + [{"role": "user", "content": directive}],
        )
        if not text:
            raise RuntimeError("manager LLM returned an empty outbound message")
        await self._deliver(text)

    async def _deliver(self, text: str) -> None:
        await self._send(text)
        await self.db.add_message("manager", "assistant", text, self.clock.now())

    async def _tone(self) -> str:
        policy = await self.policy()
        return policy.manager_style.tone if policy else self.cfg.manager.tone

    async def _chat_tail(self, n: int) -> list[dict]:
        """Chat history since the last context_reset marker (written on every replan),
        with each user message stamped with its wall-clock time."""
        since = await self.db.last_event_id("manager", "context_reset")
        rows = await self.db.recent_messages("manager", n, since_id=since)
        tail = []
        for r in rows:
            if r["role"] not in ("user", "assistant"):
                continue
            content = r["text"]
            if r["role"] == "user":
                content = f"[{fmt_stamp(datetime.fromtimestamp(r['ts'], self.cfg.tz))}] {content}"
            tail.append({"role": r["role"], "content": content})
        return tail

    # --- assignment ---

    async def schedule_assign(self, delay_seconds: float) -> None:
        await self.scheduler.cancel(["assign"])
        await self.scheduler.schedule_in("assign", delay_seconds)

    async def on_assign(self, payload: dict, overdue: float) -> None:
        policy = await self.policy()
        if policy is None or await self.current_task_id() is not None:
            return
        if await self._gated_defer(policy, "assign", payload):
            return
        now_dt = self.clock.dt(self.cfg.tz)
        wake: datetime | None = None
        for entry in policy.queue:
            task = await self.task_row(entry.task_id)
            if task is None or task["status"] != "pending":
                continue
            if entry.window.not_before:
                nb = hhmm_at(now_dt, entry.window.not_before)
                if now_dt < nb:
                    wake = min(wake, nb) if wake else nb
                    continue
            if entry.window.not_after and now_dt > hhmm_at(now_dt, entry.window.not_after):
                await self.db.audit(
                    self.clock.now(), "manager", "window_missed", task_id=entry.task_id
                )
                continue
            await self._assign(policy, entry, task)
            return
        if wake is not None:
            await self.scheduler.schedule(
                "assign", wake.timestamp() + random.uniform(0, 60), {}
            )
        else:
            await self.db.audit(self.clock.now(), "manager", "queue_exhausted")

    async def _assign(self, policy: Policy, entry: QueueEntry, task: dict) -> None:
        now = self.clock.now()
        await self.db.execute("UPDATE tasks SET status = 'active' WHERE id = ?", (task["id"],))
        await self._set("current_task_id", task["id"])
        await self._set("phase", "active")
        await self._set("started", False)
        await self._set("awaiting", False)
        try:
            await self.say("assign", task)
        except Exception:
            # Leave the task eligible so the scheduler's retry can assign it again.
            await self.db.execute(
                "UPDATE tasks SET status = 'pending' WHERE id = ?", (task["id"],)
            )
            await self._clear_current()
            raise
        await self.db.audit(now, "manager", "assigned", task_id=task["id"])
        await self._schedule_checkin(entry)
        await self.scheduler.schedule_in(
            "tripwire",
            entry.start_confirm_within_minutes * 60,
            {"task_id": task["id"], "trip": "start_confirm"},
        )
        if entry.internal_deadline:
            deadline = parse_local_dt(entry.internal_deadline, self.cfg.tz).timestamp()
            if deadline > now:
                await self.scheduler.schedule(
                    "tripwire", deadline, {"task_id": task["id"], "trip": "deadline"}
                )
        await self._reset_silent_tripwire(policy, task["id"])

    async def _schedule_checkin(self, entry: QueueEntry) -> None:
        b = entry.checkin_interval_minutes
        delay = random.uniform(b.min * 60, b.max * 60)
        await self.scheduler.cancel(["checkin"])
        await self.scheduler.schedule_in("checkin", delay, {"task_id": entry.task_id})

    async def set_next_checkin(self, minutes: int, reason: str = "") -> str:
        """The Manager model's own override of the random rhythm (from its tool)."""
        task_id = await self.current_task_id()
        if task_id is None:
            return "DENIED: no active task, there is no check-in to move."
        clamped = max(CHECKIN_OVERRIDE_MIN, min(CHECKIN_OVERRIDE_MAX, minutes))
        await self.scheduler.cancel(["checkin"])
        await self.scheduler.schedule_in("checkin", clamped * 60, {"task_id": task_id})
        await self.db.audit(
            self.clock.now(), "manager", "checkin_override",
            requested=minutes, minutes=clamped, reason=reason,
        )
        note = (
            ""
            if clamped == minutes
            else f" ({minutes} clamped to the {CHECKIN_OVERRIDE_MIN}–{CHECKIN_OVERRIDE_MAX}min cap)"
        )
        return f"Next check-in in ~{clamped} minutes{note}. Do not announce the exact timing."

    # --- check-ins & escalation ---

    async def on_checkin(self, payload: dict, overdue: float) -> None:
        policy = await self.policy()
        task_id = await self.current_task_id()
        if policy is None or task_id is None or payload.get("task_id") != task_id:
            return
        entry = policy.entry_for(task_id)
        if entry is None:
            return
        if overdue > STALE_CHECKIN_SECONDS:
            await self._schedule_checkin(entry)  # downtime: redraw, don't fire a stale ping
            return
        if await self._get("awaiting", False):
            return  # an escalation chain is already running; don't stack pings
        if await self._gated_defer(policy, "checkin", payload):
            return
        task = await self.task_row(task_id)
        if task is None:
            return
        await self.say("checkin", task)
        await self._arm_escalation(policy, task_id)

    async def _arm_escalation(self, policy: Policy, task_id: int) -> None:
        await self._set("awaiting", True)
        await self._set("escalation_attempt", 0)
        await self.scheduler.schedule_in(
            "escalation", policy.escalation.backoff_for(0) * 60, {"task_id": task_id}
        )

    async def on_escalation(self, payload: dict, overdue: float) -> None:
        policy = await self.policy()
        task_id = await self.current_task_id()
        if policy is None or task_id is None or payload.get("task_id") != task_id:
            return
        if not await self._get("awaiting", False):
            return
        if await self._gated_defer(policy, "escalation", payload):
            return
        attempt = int(await self._get("escalation_attempt", 0)) + 1
        task = await self.task_row(task_id)
        if attempt > policy.escalation.max_attempts:
            await self._exhaust(task_id)
            return
        await self.say("nudge", task, attempt=attempt)
        await self._set("escalation_attempt", attempt)
        await self.scheduler.schedule_in(
            "escalation", policy.escalation.backoff_for(attempt) * 60, {"task_id": task_id}
        )

    async def _exhaust(self, task_id: int) -> None:
        now = self.clock.now()
        await self.db.execute(
            "UPDATE tasks SET status = 'unverified' WHERE id = ?", (task_id,)
        )
        await self._clear_current()
        await self.db.audit(now, "manager", "escalation_exhausted", task_id=task_id)
        await self.schedule_assign(self.cfg.manager.resume_after_exhaust_minutes * 60)

    async def _clear_current(self) -> None:
        await self._set("current_task_id", None)
        await self._set("phase", "idle")
        await self._set("awaiting", False)
        await self._set("started", False)
        await self.scheduler.cancel(["checkin", "escalation", "tripwire"])

    # --- trip-wires ---

    async def on_tripwire(self, payload: dict, overdue: float) -> None:
        policy = await self.policy()
        task_id = await self.current_task_id()
        if policy is None or task_id is None or payload.get("task_id") != task_id:
            return
        task = await self.task_row(task_id)
        if task is None or task["status"] != "active":
            return
        trip = payload.get("trip")
        if trip == "start_confirm":
            if await self._get("started", False) or await self._get("awaiting", False):
                return
            if await self._gated_defer(policy, "tripwire", payload):
                return
            await self.say("start_confirm", task)
            await self._arm_escalation(policy, task_id)
        elif trip == "deadline":
            if await self._gated_defer(policy, "tripwire", payload):
                return
            await self.say("urgent", task)
            await self.db.audit(
                self.clock.now(), "manager", "internal_deadline_passed", task_id=task_id
            )
        elif trip == "silent":
            tw = policy.tripwire("silent_for")
            if tw is None or await self._get("awaiting", False):
                return
            last = float(await self._get("last_user_ts", 0) or 0)
            if self.clock.now() - last < (tw.minutes or 45) * 60:
                await self._reset_silent_tripwire(policy, task_id)
                return
            if await self._gated_defer(policy, "tripwire", payload):
                return
            await self.say("silent", task)
            await self._arm_escalation(policy, task_id)
            await self._reset_silent_tripwire(policy, task_id)

    async def _reset_silent_tripwire(self, policy: Policy, task_id: int) -> None:
        await self.scheduler.cancel(["tripwire"], match={"trip": "silent"})
        tw = policy.tripwire("silent_for")
        if tw is not None:
            await self.scheduler.schedule_in(
                "tripwire", (tw.minutes or 45) * 60, {"task_id": task_id, "trip": "silent"}
            )

    # --- DND ---

    async def grant_dnd(self, minutes: int) -> str:
        policy = await self.policy()
        dnd = policy.dnd if policy else None
        cap = dnd.max_grant_minutes if dnd else self.cfg.dnd.max_grant_minutes
        max_grants = dnd.max_grants_per_day if dnd else self.cfg.dnd.max_grants_per_day
        today = self.clock.dt(self.cfg.tz).date().isoformat()
        grants = await self._get("dnd_grants", {"date": today, "used": 0})
        if grants.get("date") != today:
            grants = {"date": today, "used": 0}
        if grants["used"] >= max_grants:
            return (
                f"DENIED: already used {grants['used']} of {max_grants} focus grants today. "
                "Longer or additional silence must go through the Planner."
            )
        if minutes > cap:
            return (
                f"DENIED: {minutes}min exceeds the {cap}min cap per grant. Offer up to "
                f"{cap}min, or point them at the Planner for more."
            )
        until = self.clock.now() + minutes * 60
        grants["used"] += 1
        await self._set("dnd_until", until)
        await self._set("dnd_grants", grants)
        await self._set("awaiting", False)
        await self.scheduler.cancel(["escalation"])
        await self.scheduler.schedule("dnd_end", until, {})
        await self.db.audit(self.clock.now(), "manager", "dnd_granted", minutes=minutes)
        return f"GRANTED: {minutes} minutes of silence. Confirm it plainly and sign off."

    async def on_dnd_end(self, payload: dict, overdue: float) -> None:
        policy = await self.policy()
        if policy is None:
            return
        task_id = await self.current_task_id()
        if task_id is not None:
            entry = policy.entry_for(task_id)
            if entry is not None:
                await self._schedule_checkin(entry)
        else:
            await self.schedule_assign(random.uniform(30, 90))

    # --- task lifecycle (called from the inbound agent's tools) ---

    async def mark_done_claimed(self) -> None:
        await self.mark_started()
        await self._set("phase", "verifying")

    async def mark_started(self) -> None:
        await self._set("started", True)

    async def reopen(self) -> None:
        await self._set("phase", "active")

    async def confirm_done(self) -> None:
        task_id = await self.current_task_id()
        now = self.clock.now()
        if task_id is not None:
            await self.db.execute(
                "UPDATE tasks SET status = 'done', completed_at = ? WHERE id = ?",
                (now, task_id),
            )
            await self.db.audit(now, "manager", "task_done", task_id=task_id)
        await self._clear_current()
        await self.schedule_assign(random.uniform(60, 180))

    async def note_user_activity(self) -> None:
        """Every inbound user message: clears any pending escalation, reschedules rhythm."""
        now = self.clock.now()
        await self._set("last_user_ts", now)
        policy = await self.policy()
        task_id = await self.current_task_id()
        if await self._get("awaiting", False):
            await self._set("awaiting", False)
            await self.scheduler.cancel(["escalation"])
            if policy and task_id is not None:
                entry = policy.entry_for(task_id)
                if entry is not None and not await self.scheduler.pending("checkin"):
                    await self._schedule_checkin(entry)
        if policy and task_id is not None:
            await self._reset_silent_tripwire(policy, task_id)
        if policy and task_id is None and not await self.scheduler.pending("assign"):
            # user surfaced while we were idle (e.g. post-exhaust quiet) — resume
            await self.schedule_assign(1)

    # --- replanning ---

    async def on_policy_changed(self) -> None:
        policy = await self.policy()
        now = self.clock.now()
        # Fresh conversational slate: the user just replanned, so pre-replan pings must
        # not read as ignored asks. Drop the chat context and any escalation chain.
        await self.db.add_message("manager", "event", "context_reset", now)
        await self._set("awaiting", False)
        await self._set("escalation_attempt", 0)
        await self.scheduler.cancel(MANAGER_TIMERS)
        task_id = await self.current_task_id()
        if policy is None:
            await self._clear_current()
            return
        entry = policy.entry_for(task_id) if task_id is not None else None
        task = await self.task_row(task_id) if task_id is not None else None
        if entry is not None and task is not None and task["status"] == "active":
            # in-flight task survives the replan: keep it seamlessly, re-derive timers
            await self._schedule_checkin(entry)
            if entry.internal_deadline:
                deadline = parse_local_dt(entry.internal_deadline, self.cfg.tz).timestamp()
                if deadline > now:
                    await self.scheduler.schedule(
                        "tripwire", deadline, {"task_id": task_id, "trip": "deadline"}
                    )
            await self._reset_silent_tripwire(policy, task_id)
        else:
            if task is not None and task["status"] == "active":
                await self.db.execute(
                    "UPDATE tasks SET status = 'pending' WHERE id = ?", (task_id,)
                )
            await self._clear_current()
            await self.schedule_assign(random.uniform(30, 90))
        await self.db.audit(now, "manager", "policy_reconciled", kept_task=entry is not None)

    # --- boot recovery ---

    async def ensure_alive(self) -> None:
        """After a restart: make sure something is scheduled to move the loop forward."""
        policy = await self.policy()
        if policy is None:
            return
        task_id = await self.current_task_id()
        if task_id is None:
            if not await self.scheduler.pending("assign"):
                await self.schedule_assign(60)
            return
        entry = policy.entry_for(task_id)
        if entry is None:
            await self.on_policy_changed()
            return
        if await self._get("awaiting", False) and not await self.scheduler.pending("escalation"):
            await self._set("awaiting", False)
        if not await self.scheduler.pending("checkin"):
            await self._schedule_checkin(entry)

    async def status_summary(self) -> dict:
        """For the Planner's get_manager_status tool."""
        task_id = await self.current_task_id()
        task = await self.task_row(task_id) if task_id else None
        rows = await self.db.fetchall(
            "SELECT status, COUNT(*) AS n FROM tasks GROUP BY status"
        )
        dnd_until = float(await self._get("dnd_until", 0) or 0)
        return {
            "current_task": {"id": task_id, "title": task["title"]} if task else None,
            "phase": await self.phase(),
            "task_counts": {r["status"]: r["n"] for r in rows},
            "dnd_active_until": dnd_until if dnd_until > self.clock.now() else None,
        }
