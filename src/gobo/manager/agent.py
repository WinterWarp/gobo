"""Inbound message handling for the Manager: a tool loop on the cheap model,
with only the tools valid for the current phase."""

from __future__ import annotations

from ..llm import Toolbox
from ..memory import add_memory_tools, memory_block
from ..models import fmt_stamp
from . import prompts
from .loop import ManagerEngine

_MINUTES_PARAM = {
    "type": "object",
    "properties": {"minutes": {"type": "integer", "minimum": 1}},
    "required": ["minutes"],
}
_TEXT_PARAM = {
    "type": "object",
    "properties": {"text": {"type": "string"}},
    "required": ["text"],
}
_NO_PARAMS = {"type": "object", "properties": {}}


def build_toolbox(engine: ManagerEngine, phase: str, verify_hint: str | None) -> Toolbox:
    tb = Toolbox()

    async def defer_to_planner(args: dict) -> str:
        return (
            "This is Planner territory — you cannot change the schedule. "
            "Tell them to take it to the Planner chat."
        )

    tb.add(
        "defer_to_planner",
        "The user is trying to negotiate scheduling: swap tasks, move deadlines, add or "
        "remove work, or get more silence than you can grant.",
        _TEXT_PARAM,
        defer_to_planner,
    )

    if phase == "active":

        async def mark_done(args: dict) -> str:
            await engine.mark_done_claimed()
            hint = f" Verification hint from the plan: {verify_hint}." if verify_hint else ""
            return (
                "Completion claim recorded, not yet confirmed. Ask exactly ONE concrete "
                "verification question about the work — conversational, not forensic."
                + hint
            )

        async def note_progress(args: dict) -> str:
            await engine.mark_started()
            await engine.db.audit(
                engine.clock.now(), "manager", "progress_note", note=args.get("text", "")
            )
            return "Noted. Acknowledge briefly; don't pile on questions."

        async def flag_blocker(args: dict) -> str:
            task_id = await engine.current_task_id()
            desc = args.get("text", "")
            if task_id is not None:
                await engine.db.execute(
                    "UPDATE tasks SET notes = notes || ? WHERE id = ?",
                    (f"\n[blocker] {desc}", task_id),
                )
            await engine.db.audit(engine.clock.now(), "manager", "blocker", note=desc)
            return (
                "Blocker recorded. If it fully stops this task, tell them to raise it with "
                "the Planner; otherwise ask what they can still move on."
            )

        async def note_not_started(args: dict) -> str:
            await engine.db.audit(
                engine.clock.now(), "manager", "not_started", note=args.get("text", "")
            )
            return "They have not started. Tell them plainly to begin now; do not mark progress."

        tb.add("mark_done", "The user claims the current task is complete.", _NO_PARAMS, mark_done)
        tb.add(
            "note_progress",
            "The user reports meaningful progress or status on the current task.",
            _TEXT_PARAM,
            note_progress,
        )
        tb.add(
            "flag_blocker",
            "The user reports something blocking the current task.",
            _TEXT_PARAM,
            flag_blocker,
        )
        tb.add(
            "note_not_started",
            "The user explicitly says they have not started or have been drifting. This is not "
            "a progress report.",
            _TEXT_PARAM,
            note_not_started,
        )

    if phase == "verifying":

        async def confirm_done(args: dict) -> str:
            await engine.confirm_done()
            return (
                "Confirmed and recorded. Acknowledge tersely. Do not say what is next — "
                "you'll be in touch."
            )

        async def reopen_task(args: dict) -> str:
            await engine.reopen()
            return "Claim rejected; task is active again. Tell them plainly to finish it."

        tb.add(
            "confirm_done",
            "The user's answer plausibly reflects having done the work. Accept it.",
            _NO_PARAMS,
            confirm_done,
        )
        tb.add(
            "reopen_task",
            "The user's answer clearly shows the task was NOT actually done.",
            _NO_PARAMS,
            reopen_task,
        )

    if phase in ("active", "verifying"):

        async def grant_dnd(args: dict) -> str:
            return await engine.grant_dnd(int(args.get("minutes", 0) or 0))

        async def set_next_checkin(args: dict) -> str:
            return await engine.set_next_checkin(
                int(args.get("minutes", 0) or 0), args.get("reason", "")
            )

        tb.add(
            "grant_dnd",
            "The user asks to be left alone for a while (meeting, focus block). "
            "Pass the requested duration in minutes.",
            _MINUTES_PARAM,
            grant_dnd,
        )
        tb.add(
            "set_next_checkin",
            "Move the next status check earlier or later when the conversation warrants it — "
            "e.g. they say they'll be done in 40 minutes, or they clearly need a tighter "
            "leash. Not a silence grant: 'leave me alone' goes through grant_dnd.",
            {
                "type": "object",
                "properties": {
                    "minutes": {"type": "integer", "minimum": 1},
                    "reason": {"type": "string"},
                },
                "required": ["minutes"],
            },
            set_next_checkin,
        )

    add_memory_tools(tb, engine.db, engine.clock, engine._send, "manager")
    return tb


async def handle_user_message(engine: ManagerEngine, text: str) -> None:
    await engine.db.add_message("manager", "user", text, engine.clock.now())
    await engine.note_user_activity()

    phase = await engine.phase()
    task_id = await engine.current_task_id()
    task = await engine.task_row(task_id) if task_id else None
    policy = await engine.policy()
    entry = policy.entry_for(task_id) if (policy and task_id) else None
    verify_hint = entry.verify_hint if entry else None

    toolbox = build_toolbox(engine, phase, verify_hint)
    system = prompts.inbound_system(
        await engine._tone(),
        engine._task_slice(task, entry) if task else "",
        phase,
        now=fmt_stamp(engine.clock.dt(engine.cfg.tz)),
        memory=await memory_block(engine.db),
    )
    tail = await engine._chat_tail(14)
    reply = await engine.llm.tool_loop(engine.cfg.manager_llm, system, tail, toolbox)
    if reply:
        await engine._deliver(reply)
