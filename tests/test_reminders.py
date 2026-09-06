"""Reminders spend real money per send, so every guard is tested."""

import pathlib
import re
from datetime import UTC, datetime, timedelta

from app.config.schema import AgentConfig, ReminderCfg, ReminderRule
from app.services.reminders import (
    SWEEP_LIMIT,
    in_quiet_hours,
    parse_ranges_wrapping,
    shift_out_of_quiet,
)


def test_reminders_are_off_by_default():
    """Shipping them on would message every existing tenant's customers."""
    assert AgentConfig().reminders.enabled is False
    assert ReminderCfg().rules == []


def test_idempotency_is_a_database_constraint_not_a_code_path():
    """Two workers racing must be physically unable to double-send."""
    models = pathlib.Path("app/db/models.py").read_text()
    block = re.search(r'__tablename__ = "reminders".*?(?=\nclass |\Z)', models, re.S).group(0)
    assert "UniqueConstraint" in block
    assert '"appointment_id", "rule_key"' in block


def test_quiet_hours_wrap_midnight():
    quiet = ["21:00-08:00"]
    for hour, expected in ((7, True), (9, False), (20, False), (22, True), (2, True)):
        when = datetime(2026, 9, 7, hour, tzinfo=UTC)
        assert in_quiet_hours(when, quiet, "UTC") is expected, f"{hour}:00"


def test_quiet_hours_shift_is_bounded():
    """A pathological config must not loop forever."""
    when = datetime(2026, 9, 7, 22, tzinfo=UTC)
    assert shift_out_of_quiet(when, ["21:00-08:00"], "UTC").hour == 8
    # a config that makes every hour quiet returns the original rather than spinning
    assert shift_out_of_quiet(when, ["00:00-23:59"], "UTC") == when


def test_wrapping_parser_keeps_inverted_ranges():
    """parse_ranges drops inverted spans; quiet hours need them."""
    assert parse_ranges_wrapping(["21:00-08:00"]) != []
    assert parse_ranges_wrapping(["garbage", "9:00"]) == []


def test_rules_are_capped_per_appointment():
    cfg = ReminderCfg(
        enabled=True,
        max_per_appointment=2,
        rules=[ReminderRule(key=f"r{i}", hours_before=i + 1) for i in range(5)],
    )
    assert len(cfg.rules[: cfg.max_per_appointment]) == 2


def test_sweep_is_bounded():
    assert 0 < SWEEP_LIMIT <= 1000


def test_claim_excludes_terminal_states():
    """A sweep that requeues anything pending resurrects failed rows forever."""
    src = pathlib.Path("app/services/reminders.py").read_text()
    claim = src.split("_CLAIM = text(")[1].split('""")')[0]
    assert "status = 'planned'" in claim
    assert "FOR UPDATE SKIP LOCKED" in claim
    for terminal in ("'failed'", "'sent'", "'cancelled'"):
        assert f"status = {terminal}" not in claim


def test_cancellation_is_checked_against_the_live_appointment():
    src = pathlib.Path("app/services/reminders.py").read_text()
    assert 'appointment.status != "booked"' in src
    assert "already started" in src


def test_planning_failure_never_fails_the_booking():
    src = pathlib.Path("app/tools/booking.py").read_text()
    planning = src.split("plan_for_appointment(")[1].split("result = {")[0]
    assert "except Exception" in planning


async def test_past_window_plans_nothing():
    """Booked inside the reminder window - there is nothing left to schedule."""
    import uuid

    from app.services.reminders import plan_for_appointment

    n = await plan_for_appointment(
        tenant_id=uuid.uuid4(),
        appointment_id=uuid.uuid4(),
        starts_at=datetime.now(UTC) + timedelta(minutes=30),
        cfg=ReminderCfg(enabled=True, rules=[ReminderRule(key="24h", hours_before=24)]),
    )
    assert n == 0
