from pathlib import Path

import pytest

from open_trader.plan_events import (
    PlanEvent,
    append_plan_event,
    load_plan_events,
    replay_plan_status,
)


def test_events_append_and_replay_current_plan_status(tmp_path: Path) -> None:
    path = tmp_path / "plan_events.jsonl"
    append_plan_event(
        path,
        PlanEvent(
            event_id="event-1",
            plan_id="US.DRAM:2026-07-13:v1",
            event_type="condition_triggered",
            condition_id="trim-at-resistance",
            occurred_at="2026-07-13T09:00:00+08:00",
            payload={"target_quantity": "400"},
        ),
    )
    append_plan_event(
        path,
        PlanEvent(
            event_id="event-2",
            plan_id="US.DRAM:2026-07-13:v1",
            event_type="condition_reset",
            condition_id="trim-at-resistance",
            occurred_at="2026-07-13T10:00:00+08:00",
            payload={},
        ),
    )

    events = load_plan_events(path)

    assert [event.event_id for event in events] == ["event-1", "event-2"]
    assert replay_plan_status(events, "US.DRAM:2026-07-13:v1") == "waiting"


def test_event_loader_reports_malformed_line_number(tmp_path: Path) -> None:
    path = tmp_path / "plan_events.jsonl"
    path.write_text(
        '{"event_id":"ok","plan_id":"p","event_type":"plan_expired","condition_id":"","occurred_at":"2026-07-13T16:00:00+08:00","payload":{}}\nnot-json\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="第 2 行"):
        load_plan_events(path)
