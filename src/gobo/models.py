"""The policy schema — the artifact the Planner compiles and the user never sees —
plus the time-window math the Manager uses to honor it."""

from __future__ import annotations

import re
from datetime import datetime, time, timedelta
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, field_validator, model_validator

HHMM_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

HHMM = Annotated[str, Field(description="24h local time, HH:MM")]


def _check_hhmm(value: str) -> str:
    if not HHMM_RE.match(value):
        raise ValueError(f"not a HH:MM time: {value!r}")
    return value


def _check_local_dt(value: str) -> str:
    try:
        datetime.fromisoformat(value)
    except ValueError as e:
        raise ValueError(f"not an ISO datetime: {value!r}") from e
    return value


class DayWindow(BaseModel):
    start: HHMM
    end: HHMM
    _v = field_validator("start", "end")(_check_hhmm)


class SilenceWindow(BaseModel):
    start: HHMM
    end: HHMM
    reason: str = ""
    _v = field_validator("start", "end")(_check_hhmm)


class DndPolicy(BaseModel):
    max_grant_minutes: int = Field(90, ge=0)
    max_grants_per_day: int = Field(2, ge=0)


class EscalationPolicy(BaseModel):
    max_attempts: int = Field(3, ge=1, le=10)
    backoff_minutes: list[int] = Field(default_factory=lambda: [10, 7, 5])
    on_exhaust: Literal["mark_unverified_and_wait"] = "mark_unverified_and_wait"

    @field_validator("backoff_minutes")
    @classmethod
    def _positive(cls, v: list[int]) -> list[int]:
        if not v or any(m < 1 for m in v):
            raise ValueError("backoff_minutes must be non-empty positive minutes")
        return v

    def backoff_for(self, attempt: int) -> int:
        return self.backoff_minutes[min(attempt, len(self.backoff_minutes) - 1)]


class CheckinBounds(BaseModel):
    min: int = Field(ge=2)
    max: int = Field(ge=2)

    @model_validator(mode="after")
    def _ordered(self) -> "CheckinBounds":
        if self.min > self.max:
            raise ValueError("checkin_interval_minutes.min must be <= max")
        return self


class TaskWindow(BaseModel):
    not_before: HHMM | None = None
    not_after: HHMM | None = None

    @field_validator("not_before", "not_after")
    @classmethod
    def _v(cls, v: str | None) -> str | None:
        return None if v is None else _check_hhmm(v)


class QueueEntry(BaseModel):
    task_id: int
    window: TaskWindow = Field(default_factory=TaskWindow)
    internal_deadline: str | None = None
    stated_deadline: str | None = None
    checkin_interval_minutes: CheckinBounds = Field(
        default_factory=lambda: CheckinBounds(min=15, max=30)
    )
    start_confirm_within_minutes: int = Field(15, ge=3)
    verify_hint: str | None = None
    guidance: str | None = None  # shareable coaching note the Manager may pass on

    @field_validator("internal_deadline", "stated_deadline")
    @classmethod
    def _dt(cls, v: str | None) -> str | None:
        return None if v is None else _check_local_dt(v)


class Tripwire(BaseModel):
    if_: Literal["not_started_within", "internal_deadline_passed", "silent_for"] = Field(alias="if")
    minutes: int | None = Field(None, ge=1)
    then: Literal["ping_now", "urgent_nudge", "whatcha_doing_ping"]

    model_config = {"populate_by_name": True}


class ManagerStyle(BaseModel):
    tone: Literal["terse_professional", "drill_sergeant", "neutral", "persuasive"] = (
        "terse_professional"
    )


class Disclosure(BaseModel):
    never: list[str] = Field(
        default_factory=lambda: ["queue_order", "queue_length", "deadline_compression"]
    )
    may: list[str] = Field(
        default_factory=lambda: [
            "current_task",
            "that_a_next_task_exists",
            "current_deadline",
        ]
    )


class Policy(BaseModel):
    version: int = 1
    compiled_at: str = ""
    planner_notes: str = ""
    day_window: DayWindow
    silence: list[SilenceWindow] = Field(default_factory=list)
    dnd: DndPolicy = Field(default_factory=DndPolicy)
    escalation: EscalationPolicy = Field(default_factory=EscalationPolicy)
    queue: list[QueueEntry] = Field(default_factory=list)
    tripwires: list[Tripwire] = Field(default_factory=list)
    manager_style: ManagerStyle = Field(default_factory=ManagerStyle)
    disclosure: Disclosure = Field(default_factory=Disclosure)

    @model_validator(mode="after")
    def _unique_tasks(self) -> "Policy":
        ids = [e.task_id for e in self.queue]
        if len(ids) != len(set(ids)):
            raise ValueError("queue contains duplicate task_ids")
        return self

    def entry_for(self, task_id: int) -> QueueEntry | None:
        return next((e for e in self.queue if e.task_id == task_id), None)

    def tripwire(self, kind: str) -> Tripwire | None:
        return next((t for t in self.tripwires if t.if_ == kind), None)


# --- time-window math ---


def fmt_stamp(dt: datetime) -> str:
    """Timestamp prefixed to every user message, so the models always know when."""
    return dt.strftime("%a %Y-%m-%d %H:%M")


def hhmm_at(day: datetime, hhmm: str) -> datetime:
    h, m = (int(p) for p in hhmm.split(":"))
    return day.replace(hour=h, minute=m, second=0, microsecond=0)


def window_contains(start: str, end: str, now: datetime) -> bool:
    """True if `now` falls in [start, end); windows where start > end wrap midnight."""
    t = now.time()
    s = time(*(int(p) for p in start.split(":")))
    e = time(*(int(p) for p in end.split(":")))
    if s <= e:
        return s <= t < e
    return t >= s or t < e


def parse_local_dt(value: str, tz: ZoneInfo) -> datetime:
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=tz)


def is_silent(policy: Policy, now: datetime) -> bool:
    """Outside the day window, or inside any silence window."""
    if not window_contains(policy.day_window.start, policy.day_window.end, now):
        return True
    return any(window_contains(w.start, w.end, now) for w in policy.silence)


def next_allowed(policy: Policy, now: datetime, step_minutes: int = 5) -> datetime:
    """First moment at/after `now` when the Manager may speak. Steps in small
    increments — robust against wrapped/overlapping windows, cheap at this scale."""
    probe = now
    for _ in range(int(36 * 60 / step_minutes)):
        if not is_silent(policy, probe):
            return probe
        probe += timedelta(minutes=step_minutes)
    return probe
