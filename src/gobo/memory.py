"""Shared memory: durable notes plus a task inbox, read and written by both agents.

The full contents are injected into both system prompts (single user, small scale),
and every save/update/delete is announced in the chat that caused it."""

from __future__ import annotations

from typing import Awaitable, Callable

from .db import Database
from .llm import Toolbox
from .scheduler import Clock

CATEGORIES = ("note", "task_inbox")
NOTICE_MAX_CHARS = 300

_SAVE_PARAMS = {
    "type": "object",
    "properties": {
        "key": {"type": "string", "description": "short-stable-slug identifying the memory"},
        "content": {"type": "string"},
        "category": {"type": "string", "enum": list(CATEGORIES)},
    },
    "required": ["key", "content"],
}
_KEY_PARAM = {
    "type": "object",
    "properties": {"key": {"type": "string"}},
    "required": ["key"],
}


async def memory_block(db: Database) -> str:
    """Render the whole memory for a system prompt."""
    rows = await db.memories_all()
    notes = [r for r in rows if r["category"] == "note"]
    inbox = [r for r in rows if r["category"] == "task_inbox"]
    lines = ["## Shared memory (visible to both agents)"]
    lines += [f"- {r['key']}: {r['content']}" for r in notes] or ["(empty)"]
    lines.append("## Task inbox — mentioned by the user, not yet scheduled")
    lines += [f"- {r['key']}: {r['content']}" for r in inbox] or ["(empty)"]
    return "\n".join(lines)


def add_memory_tools(
    tb: Toolbox,
    db: Database,
    clock: Clock,
    send: Callable[[str], Awaitable[None]],
    source: str,
) -> None:
    async def save_memory(args: dict) -> str:
        key = (args.get("key") or "").strip()
        content = (args.get("content") or "").strip()
        category = args.get("category", "note")
        if not key or not content:
            return "error: both key and content are required"
        if category not in CATEGORIES:
            return f"error: category must be one of {CATEGORIES}"
        created = await db.memory_upsert(key, category, content, source, clock.now())
        verb = "saved" if created else "updated"
        icon = "📥" if category == "task_inbox" else "💾"
        await send(f"{icon} Memory {verb} — {key}: {content}"[:NOTICE_MAX_CHARS])
        await db.audit(clock.now(), source, "memory_saved", key=key, category=category)
        return f"{verb} — this is now in the shared memory both agents see"

    async def delete_memory(args: dict) -> str:
        key = (args.get("key") or "").strip()
        if not await db.memory_delete(key):
            return f"error: no memory with key {key!r}"
        await send(f"🗑️ Memory deleted — {key}")
        await db.audit(clock.now(), source, "memory_deleted", key=key)
        return "deleted"

    tb.add(
        "save_memory",
        "Save or update a durable entry in the shared memory that both the Planner and the "
        "Manager see in every conversation. Use it when the user shares a lasting preference, "
        "fact, or constraint worth keeping. Use category 'task_inbox' for work the user "
        "mentions that is not scheduled yet.",
        _SAVE_PARAMS,
        save_memory,
    )
    tb.add(
        "delete_memory",
        "Remove a shared memory entry by key — a task_inbox item once it has been scheduled "
        "or dropped, or a note that is stale or wrong.",
        _KEY_PARAM,
        delete_memory,
    )
