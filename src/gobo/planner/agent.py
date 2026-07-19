"""Planner agent: conversation loop on the strong model, task CRUD tools, and the
proactive daily session. Timer kind owned here: daily_plan."""

from __future__ import annotations

import json
import logging
from datetime import timedelta
from typing import Awaitable, Callable

from ..config import Config
from ..db import Database
from ..llm import LLM, Toolbox
from ..manager.loop import ManagerEngine
from ..models import hhmm_at
from ..scheduler import Clock, Scheduler
from . import prompts
from .compile import validate_and_activate

log = logging.getLogger(__name__)

SESSION_CONTEXT_CAP = 80


class PlannerAgent:
    def __init__(
        self,
        db: Database,
        clock: Clock,
        cfg: Config,
        llm: LLM,
        scheduler: Scheduler,
        manager: ManagerEngine,
        send: Callable[[str], Awaitable[None]],
    ):
        self.db = db
        self.clock = clock
        self.cfg = cfg
        self.llm = llm
        self.scheduler = scheduler
        self.manager = manager
        self._send = send

    def register(self) -> None:
        self.scheduler.on("daily_plan", self.on_daily_plan)

    # --- daily proactive session ---

    async def ensure_daily_timer(self, *, future_only: bool = False) -> None:
        if future_only:
            pending = await self.db.fetchone(
                "SELECT 1 FROM timers WHERE kind = 'daily_plan' AND fired_at IS NULL "
                "AND fire_at > ? LIMIT 1",
                (self.clock.now(),),
            )
            if pending is not None:
                return
        elif await self.scheduler.pending("daily_plan"):
            return
        now_dt = self.clock.dt(self.cfg.tz)
        at = hhmm_at(now_dt, self.cfg.daily_plan_time)
        if at <= now_dt:
            at += timedelta(days=1)
        await self.scheduler.schedule("daily_plan", at.timestamp())

    async def on_daily_plan(self, payload: dict, overdue: float) -> None:
        # The scheduler marks this timer fired only after the handler succeeds, so
        # ignore the currently executing due row while checking that tomorrow is armed.
        await self.ensure_daily_timer(future_only=True)
        if overdue > 2 * 3600:
            return  # woke up long after the slot (downtime) — skip today's opener
        text = await self._converse(prompts.DAILY_OPENER_DIRECTIVE, record_user=False)
        if text:
            await self._deliver(text)
        await self.db.audit(self.clock.now(), "planner", "daily_session_opened")

    # --- conversation ---

    async def handle_user_message(self, text: str) -> None:
        reply = await self._converse(text, record_user=True)
        if reply:
            await self._deliver(reply)

    async def _converse(self, user_text: str, record_user: bool) -> str:
        now = self.clock.now()
        if record_user:
            await self.db.add_message("planner", "user", user_text, now)
        system = await self._build_system()
        history = await self._session_context()
        if not record_user:
            history = history + [{"role": "user", "content": user_text}]
        return await self.llm.tool_loop(
            self.cfg.planner_llm, system, history, self._toolbox(), max_rounds=12
        )

    async def _deliver(self, text: str) -> None:
        await self._send(text)
        await self.db.add_message("planner", "assistant", text, self.clock.now())

    async def _build_system(self) -> str:
        tasks = [
            dict(r)
            for r in await self.db.fetchall(
                "SELECT id, title, notes, stated_deadline, est_minutes, status FROM tasks "
                "WHERE status != 'dropped' ORDER BY id"
            )
        ]
        return prompts.build_system(
            now_str=self.clock.dt(self.cfg.tz).isoformat(timespec="minutes"),
            timezone=self.cfg.timezone,
            tone_default=self.cfg.manager.tone,
            tasks=tasks,
            has_policy=await self.db.active_policy_json() is not None,
            manager_status=await self.manager.status_summary(),
        )

    async def _session_context(self) -> list[dict]:
        """Planner transcript since the last end_session marker."""
        marker = await self.db.fetchone(
            "SELECT MAX(id) AS id FROM messages "
            "WHERE bot = 'planner' AND role = 'event' AND text = 'session_end'"
        )
        since = marker["id"] if marker and marker["id"] else 0
        rows = await self.db.fetchall(
            "SELECT role, text FROM messages WHERE bot = 'planner' AND id > ? "
            "AND role IN ('user','assistant') ORDER BY id DESC LIMIT ?",
            (since, SESSION_CONTEXT_CAP),
        )
        return [{"role": r["role"], "content": r["text"]} for r in reversed(rows)]

    # --- tools ---

    def _toolbox(self) -> Toolbox:
        tb = Toolbox()

        async def list_tasks(args: dict) -> str:
            rows = await self.db.fetchall(
                "SELECT id, title, notes, stated_deadline, est_minutes, status FROM tasks "
                "ORDER BY id"
            )
            return json.dumps([dict(r) for r in rows])

        async def upsert_task(args: dict) -> str:
            task_id = args.get("id")
            if task_id is None:
                if not args.get("title"):
                    return "error: title is required to create a task"
                cur = await self.db.execute(
                    "INSERT INTO tasks (title, notes, stated_deadline, est_minutes, created_at)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (
                        args["title"],
                        args.get("notes", ""),
                        args.get("stated_deadline"),
                        args.get("est_minutes"),
                        self.clock.now(),
                    ),
                )
                return json.dumps({"id": cur.lastrowid, "created": True})
            sets, vals = [], []
            for col in ("title", "notes", "stated_deadline", "est_minutes", "status"):
                if col in args:
                    sets.append(f"{col} = ?")
                    vals.append(args[col])
            if not sets:
                return "error: nothing to update"
            cur = await self.db.execute(
                f"UPDATE tasks SET {', '.join(sets)} WHERE id = ?", (*vals, task_id)
            )
            return json.dumps({"id": task_id, "updated": cur.rowcount == 1})

        async def drop_task(args: dict) -> str:
            await self.db.execute(
                "UPDATE tasks SET status = 'dropped' WHERE id = ?", (args["id"],)
            )
            return "dropped"

        async def get_current_policy(args: dict) -> str:
            return await self.db.active_policy_json() or "none"

        async def get_manager_status(args: dict) -> str:
            return json.dumps(await self.manager.status_summary())

        async def submit_policy(args: dict) -> str:
            raw = args.get("policy")
            if not isinstance(raw, dict):
                return "error: pass the policy as a JSON object in the 'policy' parameter"
            return await validate_and_activate(
                self.db, self.clock, self.cfg.timezone, raw, self.manager
            )

        async def end_session(args: dict) -> str:
            await self.db.add_message("planner", "event", "session_end", self.clock.now())
            return "session closed"

        obj = {"type": "object"}
        tb.add("list_tasks", "List all tasks with ids and statuses.", obj, list_tasks)
        tb.add(
            "upsert_task",
            "Create a task (no id) or update fields on an existing one (with id). "
            "Fields: title, notes, stated_deadline (ISO), est_minutes, status "
            "(pending|done|dropped).",
            {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "title": {"type": "string"},
                    "notes": {"type": "string"},
                    "stated_deadline": {"type": ["string", "null"]},
                    "est_minutes": {"type": ["integer", "null"]},
                    "status": {"type": "string"},
                },
            },
            upsert_task,
        )
        tb.add(
            "drop_task",
            "Drop a task that is no longer wanted.",
            {"type": "object", "properties": {"id": {"type": "integer"}}, "required": ["id"]},
            drop_task,
        )
        tb.add(
            "get_current_policy",
            "Read the currently active policy (your own prior compilation).",
            obj,
            get_current_policy,
        )
        tb.add(
            "get_manager_status",
            "Snapshot of the Manager: current task, phase, task counts, DND.",
            obj,
            get_manager_status,
        )
        tb.add(
            "submit_policy",
            "Validate and activate a new enforcement policy. Replaces the current one; "
            "the Manager reconciles seamlessly.",
            {
                "type": "object",
                "properties": {"policy": {"type": "object"}},
                "required": ["policy"],
            },
            submit_policy,
        )
        tb.add(
            "end_session",
            "Close this planning session once the plan is handed off.",
            obj,
            end_session,
        )
        return tb
