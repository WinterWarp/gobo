"""Dev tool: probe the Manager's prompts against the real model.

Runs the *actual* production code paths — `ManagerEngine.say` for proactive
directives and `manager.agent.handle_user_message` for inbound handling — so
what you see is exactly what ships. Scenario state lives in a throwaway
in-memory DB (or a copy of gobo.db with --from-db); the real gobo.db is never
written.

    uv run python dev/manager_probe.py say assign
    uv run python dev/manager_probe.py say urgent --tone drill_sergeant
    uv run python dev/manager_probe.py say assign --dry-run          # print the prompt, no call
    uv run python dev/manager_probe.py chat
    uv run python dev/manager_probe.py chat --from-db --show-prompt

Needs only OPENROUTER_API_KEY (from .env). Anything but --dry-run hits the
network and spends tokens.
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import sys
import tempfile
from pathlib import Path

# Runnable as a bare script (dev/ is outside the shipped package).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from gobo.config import load_config  # noqa: E402
from gobo.db import Database  # noqa: E402
from gobo.llm import LLM, Toolbox  # noqa: E402
from gobo.manager import prompts  # noqa: E402
from gobo.manager.agent import handle_user_message as manager_inbound  # noqa: E402
from gobo.manager.loop import ManagerEngine  # noqa: E402
from gobo.memory import memory_block  # noqa: E402
from gobo.models import (  # noqa: E402
    CheckinBounds,
    DayWindow,
    ManagerStyle,
    Policy,
    QueueEntry,
    TaskWindow,
    fmt_stamp,
)
from gobo.scheduler import Scheduler, clock_from_env  # noqa: E402

DIRECTIVE_KINDS = list(prompts.DIRECTIVES.keys())


# --- output helpers -------------------------------------------------------


def _printer():
    async def send(text: str) -> None:
        print(f"[Manager] {text}")

    return send


def _fmt_call(name: str, args: dict) -> str:
    inner = ", ".join(f"{k}={v!r}" for k, v in args.items())
    return f"  · {name}({inner})"


def _install_tool_tracer() -> None:
    """Wrap every Toolbox handler so each tool call the model makes is printed.
    Process-local monkeypatch — the probe only ever runs the Manager."""
    if getattr(Toolbox, "_probe_traced", False):
        return
    orig_add = Toolbox.add

    def traced_add(self, name, description, parameters, handler):
        async def wrapped(a: dict) -> str:
            print(_fmt_call(name, a))
            return await handler(a)

        orig_add(self, name, description, parameters, wrapped)

    Toolbox.add = traced_add  # type: ignore[method-assign]
    Toolbox._probe_traced = True  # type: ignore[attr-defined]


def _install_prompt_tap(llm: LLM) -> None:
    """Print the exact system prompt + message tail sent on every LLM call."""
    orig_text, orig_loop = llm.text, llm.tool_loop

    def dump(system: str, messages: list[dict]) -> None:
        print("\n┌─ system prompt " + "─" * 40)
        print(system)
        print("├─ messages " + "─" * 44)
        for m in messages:
            content = m.get("content", "")
            print(f"[{m['role']}] {content}")
            for tc in m.get("tool_calls", []) or []:
                print(f"    tool_call: {tc}")
        print("└" + "─" * 54 + "\n")

    async def text_tap(cfg, system, messages):
        dump(system, messages)
        return await orig_text(cfg, system, messages)

    async def loop_tap(cfg, system, messages, toolbox, max_rounds=8):
        dump(system, messages)
        return await orig_loop(cfg, system, messages, toolbox, max_rounds)

    llm.text = text_tap  # type: ignore[method-assign]
    llm.tool_loop = loop_tap  # type: ignore[method-assign]


def _die(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)
    raise SystemExit(1)


# --- scenario setup -------------------------------------------------------


def _iso_at(now_dt, hhmm: str) -> str:
    h, m = (int(x) for x in hhmm.split(":"))
    return now_dt.replace(hour=h, minute=m, second=0, microsecond=0, tzinfo=None).isoformat(
        timespec="minutes"
    )


async def _seed_synthetic(engine: ManagerEngine, args: argparse.Namespace) -> int:
    """Insert one task + an active policy that references it, and mark it the
    Manager's current task. Returns the task id."""
    db, clock, cfg = engine.db, engine.clock, engine.cfg
    now, now_dt = clock.now(), clock.dt(cfg.tz)
    cur = await db.execute(
        "INSERT INTO tasks (title, notes, stated_deadline, est_minutes, status, created_at) "
        "VALUES (?, ?, ?, ?, 'active', ?)",
        (
            args.title,
            args.notes,
            _iso_at(now_dt, args.stated_deadline) if args.stated_deadline else None,
            args.est_minutes,
            now,
        ),
    )
    task_id = cur.lastrowid

    entry = QueueEntry(
        task_id=task_id,
        window=TaskWindow(),
        internal_deadline=_iso_at(now_dt, args.internal_deadline)
        if args.internal_deadline
        else None,
        stated_deadline=_iso_at(now_dt, args.stated_deadline) if args.stated_deadline else None,
        checkin_interval_minutes=CheckinBounds(min=args.checkin_min, max=args.checkin_max),
        verify_hint=args.verify_hint,
        guidance=args.guidance,
    )
    policy = Policy(
        day_window=DayWindow(start="06:00", end="23:00"),
        queue=[entry],
        manager_style=ManagerStyle(tone=args.tone or cfg.manager.tone),
    )
    await db.activate_policy(policy.model_dump_json(by_alias=True), now)

    # A couple of memories so the shared-memory block isn't empty in the prompt.
    await db.memory_upsert(
        "focus-pattern", "note",
        "Drifts into email/Slack when a task feels ambiguous; naming the next concrete "
        "step out loud usually unsticks them.",
        "seed", now,
    )
    await db.memory_upsert(
        "board-deck", "task_inbox", "Wants to redo the pipeline chart before the board deck ships.",
        "seed", now,
    )

    await engine._set("current_task_id", task_id)
    await engine._set("phase", args.phase)
    await engine._set("started", False)
    await engine._set("awaiting", False)
    return task_id


async def _resolve_from_db(engine: ManagerEngine, args: argparse.Namespace) -> int:
    """Point the engine at the real active policy + current task (from a copy)."""
    task_id = await engine.current_task_id()
    if task_id is None:
        policy = await engine.policy()
        if policy is None or not policy.queue:
            _die("gobo.db has no active policy / current task to probe (--from-db)")
        task_id = policy.queue[0].task_id
        await engine.db.execute("UPDATE tasks SET status = 'active' WHERE id = ?", (task_id,))
        await engine._set("current_task_id", task_id)
        await engine._set("phase", args.phase)
        print(f"(no in-flight task in db; probing queue head task #{task_id})")
    if args.tone:
        await _set_tone(engine, args.tone)
    if args.phase_given:
        await engine._set("phase", args.phase)
    return task_id


async def _set_tone(engine: ManagerEngine, tone: str) -> None:
    raw = await engine.db.active_policy_json()
    if not raw:
        return
    policy = Policy.model_validate_json(raw)
    policy.manager_style.tone = tone
    await engine.db.execute(
        "UPDATE policies SET json = ? WHERE active = 1",
        (policy.model_dump_json(by_alias=True),),
    )


async def _open_db(args: argparse.Namespace) -> Database:
    if not args.from_db:
        return await Database.open(":memory:")
    src = Path(args.db)
    if not src.exists():
        _die(f"--from-db: {src} does not exist")
    tmp = Path(tempfile.mkdtemp(prefix="gobo-probe-")) / "probe.db"
    for suffix in ("", "-wal", "-shm"):  # copy WAL sidecars so uncommitted state comes along
        p = Path(str(src) + suffix)
        if p.exists():
            shutil.copy(p, str(tmp) + suffix)
    print(f"(working on a throwaway copy of {src}; the real db is untouched)")
    return await Database.open(str(tmp))


async def _build_engine(args: argparse.Namespace) -> tuple[ManagerEngine, int]:
    cfg = load_config(require_secrets=False)
    if not cfg.openrouter_api_key and not args.dry_run:
        _die("OPENROUTER_API_KEY is not set (see .env). Use --dry-run to print prompts offline.")
    if args.model:
        cfg.manager_llm.model = args.model
    if args.thinking:
        cfg.manager_llm.thinking_level = args.thinking

    clock = clock_from_env()
    db = await _open_db(args)
    llm = LLM(cfg.openrouter_api_key or "unused")
    if args.show_prompt:
        _install_prompt_tap(llm)
    engine = ManagerEngine(db, clock, cfg, llm, Scheduler(db, clock), _printer())

    task_id = await (_resolve_from_db if args.from_db else _seed_synthetic)(engine, args)
    return engine, task_id


# --- modes ----------------------------------------------------------------


async def _dump_outbound_prompt(engine: ManagerEngine, task: dict, kind: str, **fmt) -> None:
    policy = await engine.policy()
    tone = policy.manager_style.tone if policy else engine.cfg.manager.tone
    entry = policy.entry_for(task["id"]) if policy else None
    now = fmt_stamp(engine.clock.dt(engine.cfg.tz))
    system = prompts.outbound_system(
        tone, engine._task_slice(task, entry), now, await memory_block(engine.db)
    )
    print("\n┌─ system prompt " + "─" * 40)
    print(system)
    print("├─ directive " + "─" * 44)
    print(f"[{now}] {prompts.directive(kind, **fmt)}")
    print("└" + "─" * 54)


async def do_say(engine: ManagerEngine, task_id: int, args: argparse.Namespace) -> None:
    task = await engine.task_row(task_id)
    assert task is not None
    fmt = {"attempt": args.attempt} if args.kind == "nudge" else {}
    suffix = f" (attempt {args.attempt})" if args.kind == "nudge" else ""
    print(f"\n# directive: {args.kind}{suffix}\n")
    if args.dry_run:
        await _dump_outbound_prompt(engine, task, args.kind, **fmt)
        return
    try:
        await engine.say(args.kind, task, **fmt)
    except Exception as e:  # noqa: BLE001 - dev tool, surface anything
        _die(f"LLM call failed: {e}")


HELP = """\
Type a message as the user, or a meta-command:
  :say <kind> [attempt]  fire a proactive directive (assign|checkin|nudge|start_confirm|urgent|silent|resume)
  :phase <active|verifying|idle>   switch the Manager's phase (which tools it may use)
  :tone <terse_professional|drill_sergeant|neutral>   re-tone the active policy
  :model <id>            swap the manager model for the next turn
  :reset                 drop the chat context (fresh conversational slate)
  :state                 show phase, current task, recent audit
  :help                  this
  :q                     quit\
"""


async def _print_state(engine: ManagerEngine) -> None:
    task_id = await engine.current_task_id()
    task = await engine.task_row(task_id) if task_id else None
    task_desc = f"#{task_id} {task['title']}" if task else "none"
    print(f"  phase={await engine.phase()}  task={task_desc}  model={engine.cfg.manager_llm.model}")
    rows = await engine.db.fetchall("SELECT actor, event, detail FROM audit ORDER BY id DESC LIMIT 6")
    for r in reversed(rows):
        print(f"    audit: {r['actor']:<8} {r['event']:<22} {r['detail']}")


async def _handle_meta(engine: ManagerEngine, line: str) -> bool:
    """Returns True if the line was a meta-command."""
    if not line.startswith(":"):
        return False
    parts = line[1:].split()
    cmd, rest = (parts[0] if parts else ""), parts[1:]
    if cmd in ("q", "quit", "exit"):
        raise EOFError
    elif cmd == "help":
        print(HELP)
    elif cmd == "state":
        await _print_state(engine)
    elif cmd == "reset":
        await engine.db.add_message("manager", "event", "context_reset", engine.clock.now())
        print("  (chat context cleared)")
    elif cmd == "say" and rest:
        kind = rest[0]
        if kind not in DIRECTIVE_KINDS:
            print(f"  unknown directive {kind!r}; one of {DIRECTIVE_KINDS}")
        else:
            task = await engine.task_row(await engine.current_task_id())
            fmt = {"attempt": int(rest[1]) if len(rest) > 1 else 2} if kind == "nudge" else {}
            try:
                await engine.say(kind, task, **fmt)
            except Exception as e:  # noqa: BLE001
                print(f"  say failed: {e}")
    elif cmd == "phase" and rest:
        await engine._set("phase", rest[0])
        print(f"  (phase set to {rest[0]})")
    elif cmd == "tone" and rest:
        await _set_tone(engine, rest[0])
        print(f"  (tone set to {rest[0]})")
    elif cmd == "model" and rest:
        engine.cfg.manager_llm.model = rest[0]
        print(f"  (model set to {rest[0]})")
    else:
        print(f"  unrecognized meta-command {line!r} — try :help")
    return True


async def do_chat(engine: ManagerEngine, task_id: int, args: argparse.Namespace) -> None:
    task = await engine.task_row(task_id)
    policy = await engine.policy()
    entry = policy.entry_for(task_id) if policy else None
    tone = policy.manager_style.tone if policy else engine.cfg.manager.tone
    print(f"\nScenario: #{task_id} {task['title']!r}")
    if entry:
        print(f"  internal_deadline={entry.internal_deadline}  "
              f"stated_deadline={entry.stated_deadline}")
    print(f"  phase={await engine.phase()}  tone={tone}  model={engine.cfg.manager_llm.model}")
    print("\n" + HELP + "\n")

    while True:
        try:
            line = (await asyncio.to_thread(input, "you> ")).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not line:
            continue
        try:
            if await _handle_meta(engine, line):
                continue
        except EOFError:
            return
        before = (await engine.phase(), await engine.current_task_id())
        try:
            await manager_inbound(engine, line)
        except Exception as e:  # noqa: BLE001
            print(f"  turn failed: {e}")
            continue
        after = (await engine.phase(), await engine.current_task_id())
        if after != before:
            print(f"  (phase {before[0]}→{after[0]}; task {before[1]}→{after[1]})")


# --- cli ------------------------------------------------------------------


def _add_scenario_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument("--from-db", action="store_true", help="use the live active policy + task")
    p.add_argument("--db", default="gobo.db", help="db path for --from-db (default gobo.db)")
    p.add_argument("--title", default="Finish the Q3 board deck")
    p.add_argument(
        "--notes",
        default="8 slides, exec audience. Tends to over-polish slide 1 and stall before the ask.",
    )
    p.add_argument("--internal-deadline", default="15:00", help="HH:MM today (compressed)")
    p.add_argument("--stated-deadline", default="17:00", help="HH:MM today (what the user believes)")
    p.add_argument("--checkin-min", type=int, default=10)
    p.add_argument("--checkin-max", type=int, default=20)
    p.add_argument("--verify-hint", default="Ask what the final slide's call-to-action is.")
    p.add_argument("--guidance", default="Draft every slide's headline before touching design.")
    p.add_argument("--est-minutes", type=int, default=90)
    p.add_argument("--tone", choices=["terse_professional", "drill_sergeant", "neutral"])
    p.add_argument("--model", help="override the manager model (default: config.toml)")
    p.add_argument("--thinking", choices=["none", "low", "medium", "high"])
    p.add_argument("--phase", default="active", choices=["active", "verifying", "idle"])
    p.add_argument("--show-prompt", action="store_true", help="print the full prompt on each call")


def _parse() -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="manager_probe", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="mode", required=True)

    sp = sub.add_parser("say", help="render one proactive directive")
    sp.add_argument("kind", choices=DIRECTIVE_KINDS)
    sp.add_argument("--attempt", type=int, default=2, help="escalation attempt # for 'nudge'")
    sp.add_argument("--dry-run", action="store_true", help="print the prompt, don't call the model")
    _add_scenario_flags(sp)

    cp = sub.add_parser("chat", help="interactive REPL against the Manager")
    cp.add_argument("--dry-run", action="store_true", help=argparse.SUPPRESS)
    _add_scenario_flags(cp)

    args = ap.parse_args()
    # remember whether --phase was explicitly given (matters only for --from-db)
    args.phase_given = "--phase" in sys.argv
    return args


async def _amain(args: argparse.Namespace) -> None:
    _install_tool_tracer()
    engine, task_id = await _build_engine(args)
    try:
        if args.mode == "say":
            await do_say(engine, task_id, args)
        else:
            await do_chat(engine, task_id, args)
    finally:
        await engine.db.close()


def main() -> None:
    args = _parse()
    try:
        asyncio.run(_amain(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
