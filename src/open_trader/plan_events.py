from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Literal


PlanEventType = Literal[
    "condition_triggered",
    "condition_reset",
    "notification_sent",
    "notification_failed",
    "plan_expired",
]


@dataclass(frozen=True)
class PlanEvent:
    event_id: str
    plan_id: str
    event_type: PlanEventType
    condition_id: str
    occurred_at: str
    payload: dict[str, Any]


def append_plan_event(path: Path, event: PlanEvent) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(asdict(event), ensure_ascii=False, sort_keys=True))
        handle.write("\n")


def load_plan_events(path: Path) -> list[PlanEvent]:
    if not path.exists():
        return []
    events: list[PlanEvent] = []
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1,
    ):
        if not line.strip():
            continue
        try:
            events.append(PlanEvent(**json.loads(line)))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError(f"plan_events.jsonl 第 {line_number} 行无效") from exc
    return events


def replay_plan_status(events: Iterable[PlanEvent], plan_id: str) -> str:
    statuses = {
        "condition_triggered": "triggered",
        "condition_reset": "waiting",
        "plan_expired": "expired",
    }
    status = "waiting"
    for event in events:
        if event.plan_id == plan_id and event.event_type in statuses:
            status = statuses[event.event_type]
    return status
