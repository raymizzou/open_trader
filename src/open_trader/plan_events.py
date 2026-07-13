from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Literal


PlanEventType = Literal[
    "plan_activated",
    "condition_triggered",
    "plan_completed",
    "plan_invalidated",
    "plan_expired",
    "plan_missed",
]


@dataclass(frozen=True)
class PlanEvent:
    event_id: str
    plan_id: str
    event_type: PlanEventType
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
    return [
        PlanEvent(**json.loads(line))
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def replay_plan_status(events: Iterable[PlanEvent], plan_id: str) -> str:
    statuses = {
        "plan_activated": "active",
        "condition_triggered": "triggered",
        "plan_completed": "completed",
        "plan_invalidated": "invalidated",
        "plan_expired": "expired",
        "plan_missed": "missed",
    }
    status = "missing"
    for event in events:
        if event.plan_id == plan_id:
            status = statuses[event.event_type]
    return status
