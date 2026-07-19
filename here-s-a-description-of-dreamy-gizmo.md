# Gobo — Two-Agent Attention Management System

## Context

Gobo is a personal productivity system built on one core bet: **you can't rules-lawyer a schedule you can't see.** A Planner ("Good Cop", strong model) talks with you over Telegram, gathers intentions/tasks, and compiles a **hidden enforcement policy** — time-windows, trip-wires, silence rules, quietly-compressed deadlines. A Manager ("Bad Cop", cheap model) executes it: one task at a time from an unreadable queue, check-ins at task-scaled random intervals, conversational completion verification. It is **not a lie detector** — its leverage is the interrupt itself: "what are you doing right now?" forces the conscious naming that drift survives by avoiding.

**Locked decisions:** Python · two separate Telegram bots · OpenRouter for all inference, with per-agent model + thinking level configurable · internal-only task store in v1 (no Todoist/GCal; design so they can bolt on later) · DigitalOcean droplet provisioned via Pulumi · NixOS dev machine, so a flake.nix dev shell · single user · Planner available 24/7 for replanning plus a proactive daily session · escalation persistent-but-capped · DND grants bounded by policy · Manager tone configurable (default terse-professional; drill-sergeant and neutral presets).

The repo (`/home/r4/Documents/gobo`) is empty — greenfield.

## Stack

| Concern | Choice | Why |
|---|---|---|
| Packaging | **uv**, `src/` layout | Fast, lockfile, standard |
| Telegram | **aiogram 3.x** | Asyncio-native; two `Bot`+`Dispatcher` pairs poll cleanly in one process |
| LLM | **openai SDK** → `https://openrouter.ai/api/v1` | Tool-calling support for free; OpenRouter is OpenAI-compatible |
| Storage | **SQLite via aiosqlite**, hand-written SQL + migrations | Single user; no ORM ceremony |
| Scheduler | **Hand-rolled: DB-backed `timers` table + asyncio ticker** | APScheduler's persistence story is awkward; a table is transparent, crash-safe, and testable |
| Validation | **pydantic v2** for the policy schema | Compile-time guardrail on the Planner's output |

Default models in config (both easily changed at deploy time): planner `anthropic/claude-opus-4.5`, manager `anthropic/claude-haiku-4.5`.

## Repo layout

```
pyproject.toml  .env.example  config.toml  README.md
flake.nix  flake.lock          # NixOS dev shell (see Dev environment)
deploy/gobo.service            # systemd unit installed on the droplet
deploy/pulumi/                 # DigitalOcean IaC: Pulumi.yaml, __main__.py, cloud-init.yaml
src/gobo/
  __main__.py                  # python -m gobo
  app.py                       # asyncio supervisor: 2 bots + ticker
  config.py  db.py  models.py  llm.py  scheduler.py  cli.py
  planner/{bot,agent,compile,prompts}.py
  manager/{bot,agent,loop,prompts}.py
tests/{test_policy,test_scheduler,test_manager_loop}.py
```

## Data model (SQLite)

- **`tasks`** — `id, title, notes, stated_deadline, est_minutes, status(pending|active|done|dropped|unverified), created_at, completed_at`
- **`policies`** — `id, created_at, active(bool), json` — full compiled policy JSON; exactly one active; history kept
- **`runtime_state`** — key/value: current task id, queue cursor, `dnd_until`, awaiting-reply flag, escalation counter, DND grants used today
- **`timers`** — `id, kind(checkin|tripwire|escalation|daily_plan|dnd_end|deadline), fire_at, payload_json, fired_at(NULL until fired)` — **this table IS the scheduler**
- **`messages`** — `bot(planner|manager), role, text, ts` — both transcripts; doubles as LLM conversation context
- **`audit`** — every Manager/Planner/system action with detail JSON; feeds the debug CLI

## Dev environment (NixOS)

`flake.nix` provides a devShell with `python312`, `uv`, `pulumi-bin`, and `sqlite`, and sets `UV_PYTHON_DOWNLOADS=never` so uv uses the nix-provided interpreter — uv's own downloaded CPython builds don't run on NixOS (dynamic-linker paths). `uv sync` then manages the venv normally inside the shell. `nix develop` (or direnv `use flake`) is the entry point for all dev work.

## The policy schema (heart of the system)

Compiled by the Planner via a validated tool call; **never rendered into any user-facing chat**:

```json
{
  "version": 1, "compiled_at": "...", "planner_notes": "hidden rationale",
  "day_window": {"start": "08:30", "end": "23:00"},
  "silence": [{"start": "00:00", "end": "08:30", "reason": "sleep"}],
  "dnd": {"max_grant_minutes": 90, "max_grants_per_day": 2},
  "escalation": {"max_attempts": 3, "backoff_minutes": [10, 7, 5],
                 "on_exhaust": "mark_unverified_and_wait"},
  "queue": [{
    "task_id": 12,
    "window": {"not_before": "09:00", "not_after": "11:30"},
    "internal_deadline": "11:00",
    "stated_deadline": "17:00",
    "checkin_interval_minutes": {"min": 12, "max": 25},
    "start_confirm_within_minutes": 15,
    "verify_hint": "ask what the diff looks like"
  }],
  "tripwires": [
    {"if": "not_started_within", "minutes": 15, "then": "ping_now"},
    {"if": "internal_deadline_passed", "then": "urgent_nudge"},
    {"if": "silent_for", "minutes": 45, "then": "whatcha_doing_ping"}
  ],
  "manager_style": {"tone": "terse_professional"},
  "disclosure": {"never": ["queue_order", "internal_deadlines", "queue_length"],
                 "may": ["current_task", "that_a_next_task_exists"]}
}
```

`internal_deadline` vs `stated_deadline` is where deadline compression lives. `checkin_interval_minutes` is where task-scaling lives (Planner sets tighter bounds for drift-prone tasks). Pydantic models in `models.py` validate on submit; validation errors are fed back to the Planner model to fix.

## Planner agent (`planner/`)

- Any message to the Planner bot opens/continues a session with the strong model — this is the 24/7 replanning path. A `daily_plan` timer additionally makes it **initiate** the morning session at a configured hour.
- **Tools:** `list_tasks`, `upsert_task`, `drop_task`, `get_current_policy`, `get_manager_status`, `submit_policy`, `end_session`.
- **System prompt:** warm and collaborative, but under instruction to *quietly* compress deadlines, order the queue, and set trip-wires — and never to reveal those internals even when asked directly. It gathers intentions, estimates, energy, fixed appointments (conversationally — that's the v1 calendar), then compiles and submits.
- **Handoff:** `submit_policy` validates → inserts row + flips `active` in one transaction → writes audit → reconciles Manager state: in-flight task still in new queue → keep it seamlessly; removed → cancel its timers, Manager reassigns from the new queue on next tick. The user never sees a seam.

## Manager agent (`manager/`)

Event-driven off the `timers` table + incoming messages:

- **Assign:** pop next eligible queue entry (respecting `not_before` + silence windows) → cheap model writes the assignment message → set `start_confirm` trip-wire → schedule first check-in at `uniform(min, max)`.
- **Check-in:** skip/defer if inside silence or DND. Otherwise ping, set awaiting-reply, arm the escalation chain (3 attempts at 10/7/5-min backoff → mark `unverified`, go quiet, note it for the Planner's next session).
- **Replies:** cheap model handles them with tools: `mark_done`, `note_progress`, `grant_dnd(minutes)` (≤ policy cap, ≤ daily grant count; beyond → "take it up with the Planner"), `flag_blocker`, `defer_to_planner`. Completion claim → **one** conversational verification question (`verify_hint`) → done → assign next. Negotiation ("can I swap tasks / move the deadline") always deflects to the Planner chat.
- **Trip-wires** are just timers: e.g. `internal_deadline_passed` → urgency escalates in tone *without ever stating the deadline* ("Where are you at with this?").
- **Invisibility by construction:** the Manager model's per-interaction prompt contains only the current task slice, tone preset, and disclosure rules — never the queue, other tasks, or internal deadlines. It cannot leak what it was never given. (Cheaper per-call too.)

## Runtime & scheduler

- `app.py`: `asyncio.gather(planner_polling, manager_polling, ticker())`.
- Ticker every ~20s: `SELECT … FROM timers WHERE fired_at IS NULL AND fire_at <= now` → dispatch → mark `fired_at` transactionally. Random check-in times are drawn **once, at scheduling time, and persisted** — restarts neither lose nor duplicate pings.
- Boot recovery: reload `runtime_state`; overdue check-in timers older than a staleness threshold are re-drawn fresh instead of firing a stale ping.
- Auth: both bots ignore any Telegram user id ≠ `ALLOWED_TELEGRAM_USER_ID`.

## Config & secrets

- `.env`: `PLANNER_BOT_TOKEN`, `MANAGER_BOT_TOKEN`, `OPENROUTER_API_KEY`, `ALLOWED_TELEGRAM_USER_ID`
- `config.toml`: per-agent LLM sections plus behavior defaults:

  ```toml
  [planner.llm]
  model = "anthropic/claude-opus-4.5"
  thinking_level = "high"        # none | low | medium | high

  [manager.llm]
  model = "anthropic/claude-haiku-4.5"
  thinking_level = "none"
  ```

  `thinking_level` maps to OpenRouter's unified `reasoning: {"effort": ...}` parameter in `llm.py` (`none` omits it), so it works across providers. Remaining keys: timezone, `daily_plan_time`, default sleep hours, tone preset (`terse_professional` | `drill_sergeant` | `neutral`), escalation defaults, DND caps, db path.

## Deployment (Pulumi → DigitalOcean)

- **`deploy/pulumi/`** — Pulumi program in Python (`pulumi-digitalocean` provider) defining: one small Droplet (`s-1vcpu-1gb`, Ubuntu LTS), an SSH key resource, and a firewall allowing inbound SSH only (both bots use Telegram long polling — no inbound app ports needed). Droplet `user_data` is a cloud-init that installs git + uv, clones the repo, and installs/enables `deploy/gobo.service`.
- Secrets (`doToken`, bot tokens, OpenRouter key) via `pulumi config set --secret`; cloud-init templates them into `/opt/gobo/.env`.
- **`deploy/gobo.service`**: `ExecStart=uv run python -m gobo`, `Restart=always`, `EnvironmentFile=/opt/gobo/.env`, logs to journald.
- App updates after initial provision: a small `deploy/update.sh` (ssh: `git pull && uv sync && systemctl restart gobo`) — no re-provisioning needed.
- README runbook: create the two bots via BotFather → `pulumi config set` the secrets → `pulumi up` → done.

## Milestones (each independently testable)

1. **M1 Skeleton** — flake.nix dev shell, uv project, config (incl. per-agent model/thinking_level), DB + migrations, both bots up with auth, replies via their configured OpenRouter models, transcripts persisted. *Works = `nix develop` → `uv run python -m gobo` → both chats answer; rows in `messages`.*
2. **M2 Planner** — session loop, task tools, policy compile/validate/activate. *Works = a brain-dump conversation produces a valid active policy, inspectable via debug CLI.*
3. **M3 Manager core** — assignment, randomized check-ins, verification, queue advance, silence windows. *Works = a full day runs end-to-end under accelerated time.*
4. **M4 Full semantics** — trip-wires, escalation chain, DND grants, negotiation deflection, mid-day replan reconciliation, proactive daily session.
5. **M5 Hardening & deploy** — crash-recovery paths, Pulumi program (`pulumi preview` clean, `pulumi up` provisions the droplet end-to-end), `update.sh`, runbook README. *Works = fresh `pulumi up` yields a droplet where both bots respond.*

## Verification

- **Debug CLI** (`python -m gobo.cli`): `policy show` (the hidden policy — developer-only, deliberately outside both chats), `timers`, `state`, `audit tail`, `fire <timer_id>` (trigger any event now).
- **Time acceleration**: an injectable clock in `scheduler.py` (`GOBO_TIME_SCALE` env) so a simulated day runs in minutes; used by M3/M4 acceptance.
- **Unit tests**: policy validation edge cases, check-in draws stay within bounds, silence-window math across midnight, escalation chain sequencing, queue reconciliation on replan.
- **E2E**: two throwaway BotFather bots + real cheap models, one accelerated day: brain-dump → policy → assignments → check-ins → a completion → a missed-ping escalation → a mid-day replan.
