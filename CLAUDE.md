# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

Dev shell is Nix (`nix develop` gives python 3.13 + uv + pulumi + sqlite + ruff). `uv` is pinned to the nix interpreter — do not let it download a standalone CPython.

```sh
uv sync                                     # install deps
uv run python -m gobo                       # run both bots + ticker (needs .env)
uv run pytest                               # full offline suite (fake clock, scripted LLM, :memory: db)
uv run pytest tests/test_manager_loop.py::test_assign_checkin_escalate_exhaust_then_resume  # a single test
ruff check . && ruff format .               # lint/format (ruff is from the nix shell, NOT a dep)
uv run python -m gobo.cli <cmd>             # debug CLI: policy | tasks | timers | state | audit | messages | memory | fire <id>
```

`ruff` is intentionally **not** a project dependency — its PyPI wheel is dynamically linked and won't run on NixOS, so it comes from the nix dev shell.

Accelerated-time knobs (dev only): `GOBO_TIME_SCALE=60` runs virtual time 60× faster so a full day of check-ins takes minutes; `GOBO_TIME_START=2026-07-20T08:00` pins the start. Config lives in `config.toml` (behavior) + `.env` (secrets); override the toml path with `GOBO_CONFIG`.

## Architecture

Two agents manage the user's attention over Telegram (two separate bots, one user). The **Planner** (`planner/`, strong model, "Good Cop") talks to the user and compiles a hidden **Policy**; the **Manager** (`manager/`, cheap model, "Bad Cop") executes that policy, handing out one task at a time. `app.py` runs both bots plus the scheduler ticker in a single asyncio process.

**The Policy (`models.py`) is the central artifact.** It's a pydantic model the Planner compiles and *no one else fully sees* — not the user, not even the Manager beyond the current task. Key fields: a `queue` of `QueueEntry` (each with `internal_deadline` vs `stated_deadline`, work windows, per-task check-in bounds, verify hints, shareable guidance), `silence` windows, `tripwires`, `escalation`, and `disclosure` rules. Only one policy row is `active` at a time.

**The invisibility invariant is a correctness property, not just prompt text.** The Manager's LLM calls are only ever *given* the current task and its terms (`_task_slice` in `loop.py`) — never the queue behind it, so it cannot leak what it was never handed. `internal_deadline` is a quietly-compressed deadline the Manager presents as *the* deadline; `stated_deadline` (what the user believes) must never be acknowledged as different. When touching prompts or what data reaches an LLM call, preserve this: don't pass queue contents, future tasks, or the compression gap into Manager context.

**Scheduling is DB-backed (`scheduler.py` + the `timers` table).** Fire times are drawn once and persisted, so restarts neither lose nor duplicate pings. The `Clock` supports acceleration for dev. Handlers are registered by `kind`; a failed handler is pushed slightly into the future (retry) rather than dropped, and a timer is marked fired only after its handler succeeds.

**The Manager is a timer-driven state machine (`manager/loop.py`).** Timer kinds it owns: `assign`, `checkin`, `escalation`, `tripwire`, `dnd_end`. Runtime state is namespaced `manager.*` in the `runtime_state` KV table (`current_task_id`, `phase`, `awaiting`, `started`, `dnd_until`, ...). Every timer handler defensively re-checks current state (task/policy still valid, `payload.task_id` matches the live task, not gated) because a timer fired now may reflect a world that has since changed. `phase` ∈ {idle, active, verifying} gates which tools the inbound handler exposes (`manager/agent.py build_toolbox`).

**Speech gating:** the Manager must stay silent outside the day window, inside silence windows, and during DND grants. `speech_gate`/`_gated_defer` re-queue a blocked event for when speech reopens rather than firing it late — check this whenever adding a new outbound path.

**Replanning is reconciled, not restarted.** `submit_policy` → `planner/compile.py validate_and_activate` → `db.activate_policy` (atomic: resets stale task states + swaps the active policy) → `manager.on_policy_changed`. An in-flight `active` task survives a replan seamlessly (timers re-derived); a `context_reset` marker is written to the Manager transcript so pre-replan pings don't read as ignored asks.

**Shared memory (`memory.py`):** durable notes + a `task_inbox`, read/written by both agents via `save_memory`/`delete_memory` tools, injected whole into both system prompts (single user, small scale). Every save/delete is announced in the causing chat with an icon.

**LLM (`llm.py`):** OpenRouter through the OpenAI SDK. `text()` is a one-shot completion; `tool_loop()` runs a tool-calling loop until the model returns plain text. `thinking_level` maps to OpenRouter's `reasoning.effort`. Per-agent model + thinking level are set in `config.toml`.

**DB (`db.py`):** one shared aiosqlite connection guarded by a write lock; forward-only `MIGRATIONS` list keyed by `schema_version`. `runtime_state` and memory values are JSON. Add a migration by appending to the list — never edit an existing entry.

**CLI (`cli.py`) is the only place the hidden policy is visible** — deliberately outside both Telegram chats. Don't add policy/queue introspection to either bot's surface.

## Conventions

- Every inbound user message is stamped with its wall-clock time before the model sees it (`fmt_stamp`), and the system prompt always states "now" — the models reason about timing from these, so keep them present on any new message path.
- Timer handler signature is `(payload: dict, overdue_seconds: float)`; guard on `overdue` for stale events (e.g. a check-in overdue past `STALE_CHECKIN_SECONDS` after downtime is redrawn, not fired).
- Tests are fully offline: `FakeClock` (manually advanced) + `FakeLLM` (scripted tool calls / canned text) + `:memory:` db, `asyncio_mode=auto`. New behavior should be exercisable on the accelerated clock without network.
