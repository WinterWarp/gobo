"""Developer debug CLI. This is the only place the hidden policy is visible —
deliberately outside both Telegram chats.

Usage: uv run python -m gobo.cli <command>
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import time
from datetime import datetime


def _conn(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _ts(epoch: float | None) -> str:
    if epoch is None:
        return "-"
    return datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M:%S")


def cmd_policy(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    if args.history:
        for row in conn.execute("SELECT id, created_at, active FROM policies ORDER BY id"):
            print(f"#{row['id']}  {_ts(row['created_at'])}  {'ACTIVE' if row['active'] else ''}")
        return
    row = conn.execute("SELECT * FROM policies WHERE active = 1").fetchone()
    if row is None:
        print("no active policy")
        return
    print(json.dumps(json.loads(row["json"]), indent=2))


def cmd_tasks(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    for row in conn.execute("SELECT * FROM tasks ORDER BY id"):
        print(
            f"#{row['id']:<4} [{row['status']:<10}] {row['title']}"
            + (f"  (due {row['stated_deadline']})" if row["stated_deadline"] else "")
        )


def cmd_timers(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    where = "" if args.all else "WHERE fired_at IS NULL"
    for row in conn.execute(f"SELECT * FROM timers {where} ORDER BY fire_at"):
        state = f"fired {_ts(row['fired_at'])}" if row["fired_at"] else "pending"
        print(f"#{row['id']:<5} {row['kind']:<12} fire_at={_ts(row['fire_at'])}  "
              f"{state}  {row['payload']}")


def cmd_state(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    for row in conn.execute("SELECT * FROM runtime_state ORDER BY key"):
        print(f"{row['key']:<28} {row['value']}")


def cmd_audit(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    rows = conn.execute("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (args.n,)).fetchall()
    for row in reversed(rows):
        print(f"{_ts(row['ts'])}  {row['actor']:<8} {row['event']:<26} {row['detail']}")


def cmd_messages(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    rows = conn.execute(
        "SELECT * FROM messages WHERE bot = ? ORDER BY id DESC LIMIT ?", (args.bot, args.n)
    ).fetchall()
    for row in reversed(rows):
        print(f"{_ts(row['ts'])}  {row['role']:<9} {row['text']}")


def cmd_memory(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    for row in conn.execute("SELECT * FROM memories ORDER BY category, key"):
        print(
            f"[{row['category']:<10}] {row['key']}: {row['content']}  "
            f"({row['source']}, {_ts(row['updated_at'])})"
        )


def cmd_fire(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    cur = conn.execute(
        "UPDATE timers SET fire_at = ? WHERE id = ? AND fired_at IS NULL",
        (time.time() - 1, args.timer_id),
    )
    conn.commit()
    if cur.rowcount:
        print(f"timer #{args.timer_id} pulled to now; the running app fires it next tick")
    else:
        print(f"timer #{args.timer_id} not found or already fired")


def main() -> None:
    p = argparse.ArgumentParser(prog="gobo-debug", description=__doc__)
    p.add_argument("--db", default="gobo.db")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("policy", help="show the active (hidden) policy")
    sp.add_argument("--history", action="store_true")
    sp.set_defaults(fn=cmd_policy)

    sub.add_parser("tasks", help="list tasks").set_defaults(fn=cmd_tasks)

    sp = sub.add_parser("timers", help="list timers")
    sp.add_argument("--all", action="store_true", help="include fired timers")
    sp.set_defaults(fn=cmd_timers)

    sub.add_parser("state", help="dump runtime state").set_defaults(fn=cmd_state)

    sp = sub.add_parser("audit", help="tail the audit log")
    sp.add_argument("-n", type=int, default=30)
    sp.set_defaults(fn=cmd_audit)

    sub.add_parser("memory", help="list shared memory").set_defaults(fn=cmd_memory)

    sp = sub.add_parser("messages", help="show a bot transcript")
    sp.add_argument("--bot", choices=["planner", "manager"], default="manager")
    sp.add_argument("-n", type=int, default=30)
    sp.set_defaults(fn=cmd_messages)

    sp = sub.add_parser("fire", help="pull a pending timer's fire time to now")
    sp.add_argument("timer_id", type=int)
    sp.set_defaults(fn=cmd_fire)

    args = p.parse_args()
    conn = _conn(args.db)
    try:
        args.fn(conn, args)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
