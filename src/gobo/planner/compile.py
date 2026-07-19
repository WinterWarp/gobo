"""Policy compilation: validate the Planner's draft, activate it, reconcile the Manager."""

from __future__ import annotations

from datetime import datetime

from pydantic import ValidationError

from ..db import Database
from ..manager.loop import ManagerEngine
from ..models import Policy
from ..scheduler import Clock


async def validate_and_activate(
    db: Database, clock: Clock, tz_str: str, raw: dict, manager: ManagerEngine
) -> str:
    """Returns 'ok: ...' or an 'error: ...' string fed back into the Planner's tool loop."""
    try:
        policy = Policy.model_validate(raw)
    except ValidationError as e:
        return f"error: policy failed validation, fix and resubmit:\n{e}"

    in_flight = await manager.current_task_id()
    reset_task_ids: list[int] = []
    for entry in policy.queue:
        task = await db.fetchone("SELECT id, status FROM tasks WHERE id = ?", (entry.task_id,))
        if task is None:
            return f"error: queue references task_id {entry.task_id} which does not exist"
        if task["status"] in ("done", "dropped"):
            return (
                f"error: queue references task_id {entry.task_id} "
                f"which is already {task['status']}"
            )
        if entry.task_id == in_flight:
            continue  # the Manager's in-flight task stays 'active' so the replan is seamless
        if task["status"] in ("active", "unverified"):
            # Apply only after every queue reference has passed validation.
            reset_task_ids.append(entry.task_id)

    now = clock.now()
    policy.compiled_at = datetime.fromtimestamp(now).isoformat(timespec="seconds")
    policy_id = await db.activate_policy(
        policy.model_dump_json(by_alias=True), now, reset_task_ids
    )
    await db.audit(now, "planner", "policy_activated", policy_id=policy_id,
                   queue_len=len(policy.queue))
    await manager.on_policy_changed()
    return f"ok: policy {policy_id} is now active with {len(policy.queue)} queued tasks"
