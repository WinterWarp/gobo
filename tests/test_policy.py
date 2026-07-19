from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from gobo.models import (
    CheckinBounds,
    Policy,
    QueueEntry,
    is_silent,
    next_allowed,
    window_contains,
)

TZ = ZoneInfo("America/Chicago")


def make_policy(**overrides) -> Policy:
    base = {
        "day_window": {"start": "08:00", "end": "23:00"},
        "silence": [{"start": "12:30", "end": "13:15", "reason": "lunch"}],
        "queue": [
            {
                "task_id": 1,
                "checkin_interval_minutes": {"min": 10, "max": 20},
                "internal_deadline": "2026-07-20T11:00",
                "stated_deadline": "2026-07-20T17:00",
            }
        ],
        "tripwires": [{"if": "silent_for", "minutes": 45, "then": "whatcha_doing_ping"}],
    }
    base.update(overrides)
    return Policy.model_validate(base)


def test_valid_policy_roundtrips():
    p = make_policy()
    p2 = Policy.model_validate_json(p.model_dump_json(by_alias=True))
    assert p2.queue[0].task_id == 1
    assert p2.tripwire("silent_for").minutes == 45
    assert p2.entry_for(1).internal_deadline == "2026-07-20T11:00"
    assert p2.entry_for(99) is None


def test_bad_hhmm_rejected():
    with pytest.raises(ValidationError):
        make_policy(day_window={"start": "8:00", "end": "23:00"})
    with pytest.raises(ValidationError):
        make_policy(day_window={"start": "08:00", "end": "24:30"})


def test_checkin_bounds_ordered():
    with pytest.raises(ValidationError):
        CheckinBounds(min=30, max=10)


def test_bad_deadline_rejected():
    with pytest.raises(ValidationError):
        QueueEntry(task_id=1, internal_deadline="tomorrow at noon")


def test_duplicate_queue_task_ids_rejected():
    with pytest.raises(ValidationError):
        make_policy(queue=[{"task_id": 1}, {"task_id": 1}])


def test_escalation_backoff_clamps():
    p = make_policy()
    assert p.escalation.backoff_for(0) == 10
    assert p.escalation.backoff_for(2) == 5
    assert p.escalation.backoff_for(99) == 5


def test_window_contains_wraps_midnight():
    def at(h, m):
        return datetime(2026, 7, 20, h, m, tzinfo=TZ)

    assert window_contains("23:30", "07:30", at(23, 45))
    assert window_contains("23:30", "07:30", at(3, 0))
    assert not window_contains("23:30", "07:30", at(12, 0))
    assert window_contains("09:00", "17:00", at(9, 0))
    assert not window_contains("09:00", "17:00", at(17, 0))


def test_is_silent_day_window_and_lunch():
    p = make_policy()

    def at(h, m):
        return datetime(2026, 7, 20, h, m, tzinfo=TZ)

    assert is_silent(p, at(7, 0))       # before day window
    assert not is_silent(p, at(9, 0))
    assert is_silent(p, at(12, 45))     # lunch
    assert is_silent(p, at(23, 30))     # after day window


def test_next_allowed_exits_silence():
    p = make_policy()
    lunch = datetime(2026, 7, 20, 12, 35, tzinfo=TZ)
    out = next_allowed(p, lunch)
    assert out.hour == 13 and out.minute >= 15
    night = datetime(2026, 7, 20, 23, 30, tzinfo=TZ)
    out = next_allowed(p, night)
    assert out.day == 21 and out.hour == 8
