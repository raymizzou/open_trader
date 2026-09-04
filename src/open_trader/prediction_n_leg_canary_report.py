"""Issue #65: the read-only manual-canary fact report builder.

``build_canary_report`` folds the EXISTING N-leg tables (FIFO queue,
execution batches, audit transitions, partial-fill proofs, opportunity
episodes, unsettled-capital ledger) into one deterministic fact report.
Strictly read-only: the builder opens the store's SQLite file with
``mode=ro`` (the sqlite layer rejects any write), creates no tables and
runs no migrations. ``now`` is injected by the caller so the report is
reproducible byte-for-byte for the same store state.

Facts only: no projection, no advice, no estimated actual profit (the
actual profit stays ``UNSETTLED`` until venue settlement).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Mapping

REPORT_SCHEMA = "open_trader.prediction_n_leg.canary_report.v1"

#: The proven profit lower bound frozen with the batch's own solution is a
#: fact; the realized profit is not knowable before venue settlement.
ACTUAL_PROFIT_UNSETTLED = "UNSETTLED"

#: Trigger source of a manual-confirm batch (issue #65; issue #66's AUTO
#: producer will add its own source on this shared surface).
TRIGGER_SOURCE_MANUAL_CONFIRM = "MANUAL_CONFIRM"
TRIGGER_SOURCE_AUTO = "AUTO"

#: Repair-authorization estimate label (issue #65 ruling): the repair
#: authorization ceiling is a complete-repair end-point estimate, NOT a
#: worst-case bound.
REPAIR_ESTIMATE_LABEL = "完整修复终点估算、非最坏界"

#: The N-leg unit scale shared with the #117 economics pipeline and the
#: dashboard money rendering (``predictionNLegUnitsMoney``).
NLEG_UNITS_PER_DOLLAR = 1_000_000


def _units_money(units: object) -> str:
    return f"{_int(units)} units（${_int(units) / NLEG_UNITS_PER_DOLLAR:.2f}）"


def _signed_money(units: object) -> str:
    value = _int(units)
    sign = "+" if value >= 0 else "-"
    return f"{sign}${abs(value) / NLEG_UNITS_PER_DOLLAR:.2f}"


def _load_payload(raw: object) -> dict[str, object]:
    if not isinstance(raw, str) or not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _int(value: object) -> int:
    return int(value) if isinstance(value, int) and not isinstance(value, bool) else 0


def _connect_ro(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _trigger_source(mode: object) -> str:
    return TRIGGER_SOURCE_MANUAL_CONFIRM if str(mode or "") == "MANUAL" else TRIGGER_SOURCE_AUTO


def _batch_legs(payload: Mapping[str, object]) -> list[dict[str, object]]:
    legs: list[dict[str, object]] = []
    raw_legs = payload.get("legs")
    for leg in raw_legs if isinstance(raw_legs, list) else []:
        if not isinstance(leg, Mapping):
            continue
        receipt = leg.get("receipt") if isinstance(leg.get("receipt"), Mapping) else {}
        legs.append(
            {
                "action_id": str(leg.get("action_id") or ""),
                "client_order_id": str(leg.get("client_order_id") or ""),
                "side": str(leg.get("side") or ""),
                "submitted_quantity": _int(leg.get("submitted_quantity")),
                "filled_quantity": _int(receipt.get("cumulative_filled_quantity")),
                "paid_cash_units": _int(receipt.get("cumulative_cost_units")),
                "paid_fee_units": _int(receipt.get("cumulative_fee_units")),
                "state": str(receipt.get("state") or "UNSUBMITTED"),
            }
        )
    return legs


def _conflict_terminal_units(payload: Mapping[str, object]) -> int:
    ledger = payload.get("conflict_terminal_ledger")
    total = 0
    for item in ledger.values() if isinstance(ledger, dict) else ():
        if isinstance(item, Mapping):
            total += _int(
                item.get("holding_capital_units")
            ) or (_int(item.get("cumulative_cost_units")) + _int(item.get("cumulative_fee_units")))
    return total


def _batch_entry(
    row: sqlite3.Row,
    request_payloads: dict[str, Mapping[str, object]],
) -> dict[str, object]:
    payload = _load_payload(row["payload"])
    legs = _batch_legs(payload)
    paid_cash = sum(leg["paid_cash_units"] for leg in legs)
    paid_fees = sum(leg["paid_fee_units"] for leg in legs)
    reservations = payload.get("reservations")
    reserved = sum(
        _int(item.get("original_units"))
        for item in reservations
        if isinstance(item, Mapping)
    ) if isinstance(reservations, list) else 0
    position = sum(
        _int(item.get("holding_units"))
        for item in reservations
        if isinstance(item, Mapping)
    ) if isinstance(reservations, list) else 0
    position += _conflict_terminal_units(payload)

    request_payload = request_payloads.get(str(row["execution_batch_id"]), {})
    market = (
        request_payload.get("market")
        if isinstance(request_payload.get("market"), Mapping)
        else {}
    )
    batch_id = str(row["execution_batch_id"])
    entry: dict[str, object] = {
        "execution_batch_id": batch_id,
        "state": str(row["state"]),
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
        "opportunity_episode_id": str(payload.get("opportunity_episode_id") or ""),
        "episode_lineage_id": str(payload.get("episode_lineage_id") or ""),
        "trigger_source": _trigger_source(payload.get("mode")),
        "legs": legs,
        "paid_cash_units": paid_cash,
        "paid_fee_units": paid_fees,
        "conservation": {
            "reserved_units": reserved,
            "position_units": position,
            "equal": reserved == position,
        },
        "profit": {
            "guaranteed_profit_units": market.get("guaranteed_profit_units"),
            "paid_cash_units": paid_cash,
            "paid_fee_units": paid_fees,
            "actual_profit": ACTUAL_PROFIT_UNSETTLED,
        },
    }
    incident = payload.get("incident")
    if isinstance(incident, Mapping):
        entry["incident"] = {
            "reason": incident.get("reason"),
            "execution_batch_id": batch_id,
            "repair_status": incident.get("repair_status"),
            "happened_at": str(row["updated_at"]),
            "paid_cash_units": paid_cash,
            "paid_fee_units": paid_fees,
        }
        entry["repair_authorization"] = {
            "max_partial_fill_loss_units": _int(payload.get("max_partial_fill_loss")),
            "max_auto_repair_loss_units": _int(payload.get("max_auto_repair_loss")),
            "repair_status": incident.get("repair_status"),
            "estimate_label": REPAIR_ESTIMATE_LABEL,
        }
    else:
        entry["incident"] = None
    return entry


def build_canary_report(store: object, *, now: datetime | None = None) -> dict[str, object]:
    """Build the deterministic canary fact report from existing tables only.

    Strictly read-only (``mode=ro``): no table is written, created or
    migrated. ``now`` defaults to the current clock; tests inject it for
    determinism.
    """
    return build_canary_report_from_path(Path(store.path), now=now)  # type: ignore[attr-defined]


def build_canary_report_from_path(
    db_path: Path, *, now: datetime | None = None
) -> dict[str, object]:
    """Path-based entry (the read-only CLI seam): same report, opened
    ``mode=ro`` directly on the prediction SQLite file."""
    moment = now or datetime.now(UTC)
    connection = _connect_ro(db_path)
    try:
        control_row = connection.execute(
            "SELECT * FROM n_leg_controls WHERE singleton=1"
        ).fetchone()
        requests = connection.execute(
            "SELECT * FROM n_leg_execution_requests ORDER BY fifo_index"
        ).fetchall()
        batches = connection.execute(
            "SELECT * FROM n_leg_batches ORDER BY created_at, execution_batch_id"
        ).fetchall()
        transitions = connection.execute(
            "SELECT execution_batch_id, kind, idempotency_key, created_at"
            " FROM n_leg_transitions ORDER BY created_at, transition_id"
        ).fetchall()
        proofs = (
            connection.execute(
                "SELECT proof_fingerprint, status, created_at"
                " FROM partial_fill_proofs ORDER BY created_at, proof_fingerprint"
            ).fetchall()
            if _table_exists(connection, "partial_fill_proofs")
            else []
        )
        episodes = (
            connection.execute(
                "SELECT * FROM opportunity_episodes"
                " ORDER BY opportunity_episode_id"
            ).fetchall()
            if _table_exists(connection, "opportunity_episodes")
            else []
        )
    finally:
        connection.close()

    request_payloads: dict[str, Mapping[str, object]] = {}
    request_rows: list[dict[str, object]] = []
    counts: dict[str, int] = {}
    pending: list[dict[str, object]] = []
    for row in requests:
        payload = _load_payload(row["payload"])
        state = str(row["state"])
        counts[state] = counts.get(state, 0) + 1
        batch_id = payload.get("execution_batch_id")
        if isinstance(batch_id, str) and batch_id:
            request_payloads[batch_id] = payload
        entry = {
            "request_id": str(row["request_id"]),
            "fifo_index": int(row["fifo_index"]),
            "component_id": str(row["component_id"]),
            "state": state,
            "abandon_reason": row["abandon_reason"],
            "created_at": str(row["created_at"]),
        }
        if state == "PENDING":
            entry["position"] = len(pending) + 1
            pending.append(entry)
        if state == "ABANDONED":
            entry["abandon_reason"] = str(row["abandon_reason"] or "")
        request_rows.append(entry)

    audit: dict[str, list[dict[str, object]]] = {}
    for row in transitions:
        audit.setdefault(str(row["execution_batch_id"]), []).append(
            {
                "kind": str(row["kind"]),
                "idempotency_key": str(row["idempotency_key"]),
                "created_at": str(row["created_at"]),
            }
        )

    episode_rows = [
        {
            "opportunity_episode_id": str(row["opportunity_episode_id"]),
            "episode_lineage_id": str(row["episode_lineage_id"]),
            "component_id": str(row["component_id"]),
            "status": "CLOSED" if row["closed_at"] is not None else "ONGOING",
            "opened_at": str(row["opened_at"]),
            "closed_at": row["closed_at"],
            "close_reason": row["close_reason"],
            "best_guaranteed_profit": row["best_guaranteed_profit"],
            "worst_guaranteed_profit": row["worst_guaranteed_profit"],
        }
        for row in episodes
    ]

    return {
        "schema": REPORT_SCHEMA,
        "generated_at": moment.isoformat(),
        "ledger": {
            "total_unsettled_capital_units": _int(control_row["total_unsettled_capital_units"]) if control_row is not None else 0,
            "active_execution_batch_id": (
                control_row["active_batch_id"] if control_row is not None else None
            ),
            "mode": str(control_row["mode"]) if control_row is not None else "",
            "breaker_open": bool(control_row["breaker_open"]) if control_row is not None else False,
        },
        "queue": {
            "pending": pending,
            "counts": {key: counts[key] for key in sorted(counts)},
            "requests": request_rows,
        },
        "batches": [_batch_entry(row, request_payloads) for row in batches],
        "audit": {batch_id: audit[batch_id] for batch_id in sorted(audit)},
        "proofs": [
            {
                "proof_fingerprint": str(row["proof_fingerprint"]),
                "status": str(row["status"]),
                "created_at": str(row["created_at"]),
            }
            for row in proofs
        ],
        "episodes": episode_rows,
    }


def _render_batch_markdown(batch: Mapping[str, object]) -> list[str]:
    lines = [f"### 批次 {batch['execution_batch_id']}"]
    lines.append(f"状态：{batch['state']}")
    lines.append(f"触发来源：{batch['trigger_source']}")
    incident = batch.get("incident")
    if isinstance(incident, Mapping):
        lines.append(f"事故原因：{incident.get('reason')}")
        lines.append(f"事故批次：{incident.get('execution_batch_id')}")
        lines.append(f"事故发生时间：{incident.get('happened_at')}")
        lines.append(f"事故已付现金：{_units_money(incident.get('paid_cash_units'))}")
        lines.append(f"事故已付费用：{_units_money(incident.get('paid_fee_units'))}")
        repair = batch.get("repair_authorization")
        if isinstance(repair, Mapping):
            lines.append(
                "修复授权：部分成交损失上限 "
                f"{_units_money(repair.get('max_partial_fill_loss_units'))}；"
                "自动修复上限 "
                f"{_units_money(repair.get('max_auto_repair_loss_units'))}；"
                f"{REPAIR_ESTIMATE_LABEL}"
            )
    for leg in batch.get("legs", []):
        if not isinstance(leg, Mapping):
            continue
        lines.append(
            f"腿 {leg.get('action_id')}：方向 {str(leg.get('side') or '')}，"
            f"提交 {_int(leg.get('submitted_quantity'))} 份，"
            f"成交 {_int(leg.get('filled_quantity'))} 份，"
            f"已付 {_units_money(leg.get('paid_cash_units'))}，"
            f"费用 {_units_money(leg.get('paid_fee_units'))}，"
            f"回执状态 {leg.get('state')}"
        )
    conservation = batch.get("conservation")
    if isinstance(conservation, Mapping):
        lines.append(
            f"守恒：预留 {_units_money(conservation.get('reserved_units'))} = "
            f"仓位 {_units_money(conservation.get('position_units'))}："
            f"{'相等' if conservation.get('equal') else '不相等'}"
        )
    profit = batch.get("profit")
    if isinstance(profit, Mapping):
        bound = profit.get("guaranteed_profit_units")
        if bound is None:
            lines.append("保证利润（已证下界）：-")
        else:
            lines.append(
                f"保证利润（已证下界）：{_signed_money(bound)}（{_int(bound)} units）"
            )
        lines.append(f"已付现金：{_units_money(profit.get('paid_cash_units'))}")
        lines.append(f"已付费用：{_units_money(profit.get('paid_fee_units'))}")
        lines.append(f"实际利润：{profit.get('actual_profit')}")
    return lines


def render_canary_report_markdown(report: Mapping[str, object]) -> str:
    """Render the canary fact report as Chinese Markdown, one fact per line.

    Amounts are humanized on the shared N-leg unit scale (1,000,000 units
    per dollar, matching the dashboard). Facts only: the renderer adds no
    conclusion and no advice.
    """
    lines = ["# N_LEG 人工小单 Canary 事实报告"]
    lines.append(f"报告时间：{report.get('generated_at')}")
    lines.append(f"报表 schema：{report.get('schema')}")

    ledger = report.get("ledger")
    if isinstance(ledger, Mapping):
        lines.append("## 未清资本账本")
        lines.append(
            "账本总未清资本："
            f"{_units_money(ledger.get('total_unsettled_capital_units'))}"
        )
        lines.append(f"活跃批次：{ledger.get('active_execution_batch_id')}")
        lines.append(f"模式：{ledger.get('mode')}")
        lines.append(
            "全局熔断："
            f"{'开启' if ledger.get('breaker_open') else '关闭'}"
        )

    queue = report.get("queue")
    if isinstance(queue, Mapping):
        lines.append("## 下单队列")
        counts = queue.get("counts")
        if isinstance(counts, Mapping):
            for state in sorted(counts):
                lines.append(f"请求 {state} 数：{counts[state]}")
        pending = queue.get("pending")
        for row in pending if isinstance(pending, list) else []:
            if isinstance(row, Mapping):
                lines.append(
                    f"队位 {row.get('position')}：{row.get('component_id')}"
                    f"（{row.get('state')}）"
                )
        requests = queue.get("requests")
        for row in requests if isinstance(requests, list) else []:
            if not isinstance(row, Mapping):
                continue
            if row.get("state") == "ABANDONED":
                lines.append(
                    f"请求行 {row.get('component_id')}：{row.get('state')}"
                    f"（{row.get('abandon_reason')}）——仅监控"
                )

    lines.append("## 批次")
    batches = report.get("batches")
    for batch in batches if isinstance(batches, list) else []:
        if isinstance(batch, Mapping):
            lines.extend(_render_batch_markdown(batch))

    episodes = report.get("episodes")
    if isinstance(episodes, list):
        lines.append("## Episode")
        for episode in episodes:
            if not isinstance(episode, Mapping):
                continue
            lines.append(
                f"Episode {episode.get('opportunity_episode_id')}："
                f"{episode.get('component_id')} 状态 {episode.get('status')}"
                f"（{episode.get('close_reason')}）"
            )

    proofs = report.get("proofs")
    if isinstance(proofs, list):
        lines.append("## 成交证明")
        for proof in proofs:
            if isinstance(proof, Mapping):
                lines.append(
                    f"证明 {proof.get('proof_fingerprint')}：{proof.get('status')}"
                )

    return "\n".join(lines) + "\n"
