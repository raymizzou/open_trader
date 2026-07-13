from pathlib import Path

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
            event_type="plan_activated",
            occurred_at="2026-07-13T09:00:00+08:00",
            payload={"target_quantity": "400"},
        ),
    )
    append_plan_event(
        path,
        PlanEvent(
            event_id="event-2",
            plan_id="US.DRAM:2026-07-13:v1",
            event_type="condition_triggered",
            occurred_at="2026-07-13T10:00:00+08:00",
            payload={"condition_id": "trim-at-resistance"},
        ),
    )

    events = load_plan_events(path)

    assert [event.event_id for event in events] == ["event-1", "event-2"]
    assert replay_plan_status(events, "US.DRAM:2026-07-13:v1") == "triggered"
