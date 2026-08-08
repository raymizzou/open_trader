#!/usr/bin/env python3
"""Read-only replay comparison for the three-market trend candidate change.

This module deliberately only reads frozen evidence and calls the existing pure
report rebuild function.  It never creates a notifier, market-data client, or
broker object.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import sys
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

# The shared virtualenv may be editable-installed from another worktree.
_SRC = str(Path(__file__).resolve().parents[1] / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from open_trader.a_share_trend import (
    freeze_allocation_reference,
    live_trend_strategy_snapshot,
)
from open_trader.trend_review import rebuild_trend_report_from_evidence


MARKETS = ("CN", "HK", "US")
TARGET_VERSIONS = {"CN": "v13", "HK": "v11", "US": "v11"}
TARGET_RANKS = {"CN": 3, "HK": 2, "US": 1}
TARGET_ENTRY_WEIGHTS = {1: "0.06", 2: "0.04", 3: "0.02"}
TARGET_NOMINAL_WEIGHTS = {1: "0.60", 2: "0.40", 3: "0.20"}
ROOT_ASSETS = {
    "CN": ("A股", "ETF基金"),
    "HK": ("港股", "香港ETF"),
    "US": ("美股", "美国ETF"),
}
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@dataclass(frozen=True)
class ReplayComparison:
    """The frozen-invariant result for one old/new report pair."""

    errors: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.errors


def _canonical(value: object) -> object:
    if isinstance(value, dict):
        return {
            str(key): _canonical(item)
            for key, item in sorted(value.items(), key=lambda item: str(item[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    if isinstance(value, Decimal):
        return str(value)
    return value


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        _canonical(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _rows(value: object) -> list[dict[str, object]] | None:
    if not isinstance(value, list):
        return None
    return [dict(item) for item in value if isinstance(item, dict)]


def _symbol(row: dict[str, object]) -> str:
    for key in ("symbol", "code", "futu_symbol", "ticker"):
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _qualified_symbols(report: object) -> tuple[str, ...] | None:
    if not isinstance(report, dict):
        return None
    signals = report.get("signal_snapshots")
    if not isinstance(signals, dict):
        return None
    candidates = _rows(signals.get("candidates"))
    if candidates is None:
        return None
    result: set[str] = set()
    for row in candidates:
        qualified = (
            row.get("eligible") is True
            or row.get("discipline_qualified") is True
            or row.get("discipline_passed") is True
            or row.get("discipline_status") in {"qualified", "passed"}
        )
        if qualified and _symbol(row):
            result.add(_symbol(row))
    return tuple(sorted(result))


def _judgments(report: object) -> dict[str, object]:
    if not isinstance(report, dict):
        return {}
    value = report.get("strategy_judgments")
    return dict(value) if isinstance(value, dict) else {}


def _holding_rows(report: object) -> list[dict[str, object]] | None:
    judgments = _judgments(report)
    rows = _rows(judgments.get("holding_decisions"))
    if rows is not None:
        return rows
    if isinstance(report, dict):
        rows = _rows(report.get("holdings"))
        if rows is not None:
            return rows
        signals = report.get("signal_snapshots")
        if isinstance(signals, dict):
            return _rows(signals.get("holdings"))
    return None


def _real_holding_rows(report: object) -> list[dict[str, object]] | None:
    rows = _rows(_judgments(report).get("real_holding_decisions"))
    return rows


def _sorted_rows(rows: list[dict[str, object]] | None) -> tuple[object, ...] | None:
    if rows is None:
        return None
    return tuple(sorted((_canonical(row) for row in rows), key=lambda row: _canonical_bytes(row)))


_EXIT_ACTIONS = {"SELL", "SELL_ALL", "SELL_PARTIAL", "EXIT", "EXIT_ALL"}


def _exit_rows(report: object) -> tuple[object, ...] | None:
    judgments = _judgments(report)
    rows = _rows(judgments.get("formal_actions"))
    real_rows = _real_holding_rows(report)
    if rows is None and real_rows is None:
        return None
    rows = (rows or []) + (real_rows or [])
    return _sorted_rows(
        [
            row
            for row in rows
            if str(row.get("action") or "").upper() in _EXIT_ACTIONS
            or str(row.get("side") or "").lower() == "sell"
        ]
    )


_RISK_FIELDS = {
    "risk_formula",
    "risk_limit",
    "portfolio_risk_limit",
    "portfolio_risk_limit_pct",
    "single_entry_risk_limit",
    "single_entry_risk_limit_pct",
    "abnormal_loss_buffer",
    "abnormal_loss_buffer_pct",
    "total_risk_budget_target_pct",
    "drawdown_limit",
    "drawdown_limit_pct",
    "initial_protection_atr_multiple",
    "protection_line_non_decreasing",
}


def _risk_contract(report: object) -> dict[str, object] | None:
    if not isinstance(report, dict):
        return None
    result: dict[str, object] = {}
    for container_name in ("risk_summary", "strategy_snapshot"):
        container = report.get(container_name)
        if not isinstance(container, dict):
            continue
        sources = [container]
        parameters = container.get("parameters")
        if isinstance(parameters, dict):
            sources.append(parameters)
        for source in sources:
            for key, value in source.items():
                key_text = str(key)
                lower = key_text.lower()
                if key_text in _RISK_FIELDS or any(
                    token in lower
                    for token in ("formula", "risk_limit", "risk_buffer")
                ):
                    result[key_text] = _canonical(value)
    return result


def _rotation_contract(report: object) -> dict[str, object] | None:
    if not isinstance(report, dict):
        return None
    result: dict[str, object] = {}
    judgments = _judgments(report)
    for key, value in judgments.items():
        if "rotation" not in str(key).lower():
            continue
        rows = _rows(value)
        if rows is not None:
            selected: list[dict[str, object]] = []
            for row in rows:
                selected.append(
                    {
                        field: _canonical(row.get(field))
                        for field in (
                            "pair_index",
                            "threshold",
                            "rotation_threshold",
                            "strength_basis",
                            "basis",
                            "comparison_basis",
                        )
                        if field in row
                    }
                )
            if selected:
                result[str(key)] = tuple(
                    sorted(selected, key=lambda item: _canonical_bytes(item))
                )
        elif isinstance(value, dict):
            selected = {
                field: _canonical(value.get(field))
                for field in (
                    "threshold",
                    "rotation_threshold",
                    "strength_basis",
                    "basis",
                    "comparison_basis",
                )
                if field in value
            }
            if selected:
                result[str(key)] = selected
    for key in ("rotation_threshold", "rotation_basis", "strength_basis"):
        if key in report:
            result[key] = _canonical(report[key])
    snapshot = report.get("strategy_snapshot")
    if isinstance(snapshot, dict) and isinstance(snapshot.get("parameters"), dict):
        parameters = snapshot["parameters"]
        for key in ("rotation_threshold", "rotation_basis", "strength_basis"):
            if key in parameters:
                result[f"parameters.{key}"] = _canonical(parameters[key])
    return result


def compare_reports(old: dict[str, object], new: dict[str, object]) -> ReplayComparison:
    """Compare only frozen strategy invariants.

    Candidate ordering, BUY priority, final-plan audit rows, strategy identity,
    costs, timestamps, and hashes are intentionally absent from this allowlist.
    """

    errors: list[str] = []
    old_qualified = _qualified_symbols(old)
    new_qualified = _qualified_symbols(new)
    if old_qualified is None or new_qualified is None:
        errors.append("discipline_qualified_set unavailable")
    elif old_qualified != new_qualified:
        errors.append("discipline_qualified_set changed")

    old_holdings = _sorted_rows(_holding_rows(old))
    new_holdings = _sorted_rows(_holding_rows(new))
    if old_holdings is None or new_holdings is None:
        errors.append("holding_decisions unavailable")
    elif old_holdings != new_holdings:
        errors.append("holding_decisions changed")
    old_real_holdings = _sorted_rows(_real_holding_rows(old))
    new_real_holdings = _sorted_rows(_real_holding_rows(new))
    if old_real_holdings is not None or new_real_holdings is not None:
        if old_real_holdings is None or new_real_holdings is None:
            errors.append("holding_decisions unavailable")
        elif old_real_holdings != new_real_holdings:
            errors.append("holding_decisions changed")

    old_protection = old.get("protection_state") if isinstance(old, dict) else None
    new_protection = new.get("protection_state") if isinstance(new, dict) else None
    if _canonical(old_protection) != _canonical(new_protection):
        errors.append("protection_state changed")

    old_exits = _exit_rows(old)
    new_exits = _exit_rows(new)
    if old_exits is None or new_exits is None:
        errors.append("exit unavailable")
    elif old_exits != new_exits:
        errors.append("exit changed")

    old_risk = _risk_contract(old)
    new_risk = _risk_contract(new)
    if old_risk is None or new_risk is None:
        errors.append("risk_formula_or_limit unavailable")
    elif old_risk != new_risk:
        errors.append("risk_formula_or_limit changed")

    old_rotation = _rotation_contract(old)
    new_rotation = _rotation_contract(new)
    if old_rotation is None or new_rotation is None:
        errors.append("rotation_threshold_or_basis unavailable")
    elif old_rotation != new_rotation:
        errors.append("rotation_threshold_or_basis changed")

    return ReplayComparison(tuple(dict.fromkeys(errors)))


def _load_json(path: Path) -> dict[str, object] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _selected_evidence(
    data_dir: Path, market: str, days: int
) -> list[tuple[str, Path, dict[str, object]]]:
    grouped: dict[str, list[tuple[str, Path, dict[str, object]]]] = {}
    root = data_dir / "trend_review" / "evidence" / market
    for path in sorted(root.glob("*.json")):
        payload = _load_json(path)
        if payload is None or payload.get("market") != market:
            continue
        report_id = payload.get("report_id")
        if not isinstance(report_id, str) or not _DATE_RE.fullmatch(report_id):
            continue
        inputs = payload.get("rebuild_inputs")
        if not isinstance(inputs, dict):
            continue
        generated_at = inputs.get("generated_at")
        timestamp = generated_at if isinstance(generated_at, str) else ""
        grouped.setdefault(report_id, []).append((timestamp, path, payload))
    selected: list[tuple[str, Path, dict[str, object]]] = []
    for report_id in sorted(grouped)[-days:]:
        _timestamp, path, payload = max(
            grouped[report_id], key=lambda item: (item[0], str(item[1]))
        )
        selected.append((report_id, path, payload))
    return selected


def _synthetic_allocation(market: str, report_id: str) -> dict[str, object]:
    """Build a valid allocation fact when an old report predates allocation."""
    strengths = {"CN": "70", "HK": "80", "US": "90"}
    roots: dict[str, object] = {}
    markets: dict[str, object] = {}
    tm_id = 900001
    for item_market, assets in ROOT_ASSETS.items():
        score = Decimal(str(strengths[item_market]))
        roots[item_market] = {
            "stock": {
                "asset": assets[0],
                "tm_id": tm_id,
                "as_of_date": report_id,
                "global_strength": str(score),
            },
            "etf": {
                "asset": assets[1],
                "tm_id": tm_id + 1,
                "as_of_date": report_id,
                "global_strength": str(score - 1),
            },
        }
        rank = TARGET_RANKS[item_market]
        markets[item_market] = {
            "rank": rank,
            "score": str(score),
            "score_source": assets[0],
            "entry_weight": TARGET_ENTRY_WEIGHTS[rank],
            "nominal_weight": TARGET_NOMINAL_WEIGHTS[rank],
        }
        tm_id += 2
    snapshot = {
        "version": 1,
        "allocation_date": report_id,
        "generated_at": f"{report_id}T16:20:00+08:00",
        "generator_version": "trend-allocation-v1",
        "git_sha": "0" * 40,
        "roots": roots,
        "markets": markets,
    }
    body = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    return {
        "daily_path": f"data/trend_allocation/daily/{report_id}-r1.json",
        "sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
        "snapshot": snapshot,
        "reused": False,
        "stale_a_trading_days": 0,
        "failure_reason": "",
        "_daily_json": body,
    }


def _allocation_for(
    data_dir: Path, market: str, report_id: str, evidence: dict[str, object]
) -> tuple[dict[str, object], dict[str, object]]:
    inputs = evidence.get("rebuild_inputs")
    frozen = inputs.get("allocation") if isinstance(inputs, dict) else None
    if isinstance(frozen, dict):
        reference = frozen.get("reference")
        daily_json = frozen.get("daily_json")
        if isinstance(reference, dict) and isinstance(daily_json, str):
            try:
                raw = {
                    "daily_path": reference["daily_path"],
                    "sha256": reference["sha256"],
                    "snapshot": json.loads(daily_json),
                    "reused": reference.get("reused", False),
                    "stale_a_trading_days": reference.get("stale_a_trading_days", 0),
                    "failure_reason": reference.get("failure_reason", ""),
                }
                if freeze_allocation_reference(raw) == reference:
                    return raw, {"reference": dict(reference), "daily_json": daily_json}
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                pass
    daily_root = data_dir / "trend_allocation" / "daily"
    for path in sorted(daily_root.glob("*.json"), reverse=True):
        try:
            body = path.read_text(encoding="utf-8")
            snapshot = json.loads(body)
            raw = {
                "daily_path": f"data/trend_allocation/daily/{path.name}",
                "sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
                "snapshot": snapshot,
                "reused": False,
                "stale_a_trading_days": 0,
                "failure_reason": "",
            }
            reference = freeze_allocation_reference(raw)
            return raw, {"reference": reference, "daily_json": body}
        except (OSError, UnicodeError, TypeError, ValueError, json.JSONDecodeError):
            continue
    raw = _synthetic_allocation(market, report_id)
    body = str(raw.pop("_daily_json"))
    reference = freeze_allocation_reference(raw)
    return raw, {"reference": reference, "daily_json": body}


def _target_snapshot(
    evidence: dict[str, object], market: str, allocation: dict[str, object]
) -> dict[str, object]:
    original = evidence.get("strategy_snapshot")
    if not isinstance(original, dict):
        raise ValueError("missing strategy_snapshot")
    inputs = evidence.get("rebuild_inputs")
    if not isinstance(inputs, dict):
        raise ValueError("missing rebuild_inputs")
    parameters = original.get("parameters")
    if not isinstance(parameters, dict):
        raise ValueError("missing strategy parameters")
    pools = parameters.get("candidate_pool_ids", inputs.get("candidate_pool_ids"))
    if not isinstance(pools, list) or any(
        isinstance(item, bool) or not isinstance(item, int) for item in pools
    ):
        raise ValueError("missing candidate_pool_ids")
    cost_rate = Decimal(str(inputs.get("normal_cost_rate") or "0.001"))
    return live_trend_strategy_snapshot(
        market,
        str(evidence.get("process_version") or original.get("process_version") or "replay"),
        tuple(pools),
        normal_cost_rate=cost_rate,
        strategy_version=TARGET_VERSIONS[market],
        allocation=allocation,
    )


def _rewrite_drawdown_identity(
    evidence: dict[str, object], market: str, snapshot: dict[str, object]
) -> None:
    inputs = evidence.get("rebuild_inputs")
    if not isinstance(inputs, dict):
        return
    drawdown = inputs.get("drawdown_summary")
    if not isinstance(drawdown, dict):
        return
    strategy_id = str(snapshot.get("strategy_id") or "")
    version = TARGET_VERSIONS[market]
    drawdown["strategy_id"] = strategy_id
    drawdown["strategy_version"] = version
    drawdown["kelly_sample_key"] = f"{market}|{strategy_id}|{version}"
    for event_name in ("bootstrap_event", "recovery_event", "parameter_compatibility_event"):
        event = drawdown.get(event_name)
        if isinstance(event, dict):
            event["strategy_id"] = strategy_id
            event["strategy_version"] = version


def _normalise_source_evidence(
    evidence: dict[str, object], market: str, data_dir: Path
) -> dict[str, object]:
    """Repair only stale snapshot identity fields before the pure rebuild."""
    modified = copy.deepcopy(evidence)
    original = modified.get("strategy_snapshot")
    inputs = modified.get("rebuild_inputs")
    if not isinstance(original, dict) or not isinstance(inputs, dict):
        return modified
    version = str(original.get("strategy_version") or "")
    if version not in {"v4", "v5", "v6", "v7", "v8", "v9", "v10", "v11", "v12", "v13"}:
        return modified
    parameters = original.get("parameters")
    if not isinstance(parameters, dict):
        return modified
    pools = parameters.get("candidate_pool_ids", inputs.get("candidate_pool_ids"))
    if not isinstance(pools, list) or any(
        isinstance(item, bool) or not isinstance(item, int) for item in pools
    ):
        return modified
    allocation = None
    if isinstance(inputs.get("allocation"), dict):
        try:
            allocation, _payload = _allocation_for(
                data_dir,
                market,
                str(modified.get("report_id") or "1970-01-01"),
                modified,
            )
        except ValueError:
            allocation = None
    try:
        snapshot = live_trend_strategy_snapshot(
            market,
            str(modified.get("process_version") or original.get("process_version") or "replay"),
            tuple(pools),
            normal_cost_rate=Decimal(str(inputs.get("normal_cost_rate") or "0.001")),
            strategy_version=version,
            allocation=allocation,
        )
    except (TypeError, ValueError):
        return modified
    modified["strategy_snapshot"] = snapshot
    _rewrite_drawdown_identity(modified, market, snapshot)
    return modified


def validate_replay(data_dir: Path, *, days: int = 20) -> dict[str, object]:
    errors: list[str] = []
    counts = {market: 0 for market in MARKETS}
    if days <= 0:
        errors.append("days must be positive")
    else:
        for market in MARKETS:
            selected = _selected_evidence(data_dir, market, days)
            for report_id, _path, evidence in selected:
                try:
                    source = evidence
                    try:
                        old_report = rebuild_trend_report_from_evidence(source)
                    except Exception:
                        source = _normalise_source_evidence(evidence, market, data_dir)
                        old_report = rebuild_trend_report_from_evidence(source)
                    modified = copy.deepcopy(source)
                    allocation, allocation_payload = _allocation_for(
                        data_dir,
                        market,
                        report_id,
                        modified,
                    )
                    modified_inputs = modified.get("rebuild_inputs")
                    if isinstance(modified_inputs, dict):
                        modified_inputs["allocation"] = allocation_payload
                    snapshot = _target_snapshot(modified, market, allocation)
                    modified["strategy_snapshot"] = snapshot
                    _rewrite_drawdown_identity(modified, market, snapshot)
                    new_report = rebuild_trend_report_from_evidence(modified)
                    comparison = compare_reports(old_report, new_report)
                    if comparison.errors:
                        errors.extend(
                            f"{market} {report_id} {error}" for error in comparison.errors
                        )
                    else:
                        counts[market] += 1
                except Exception as exc:  # evidence is untrusted input; fail closed per artifact
                    errors.append(f"{market} {report_id}: {type(exc).__name__}: {exc}")
        for market in MARKETS:
            if counts[market] < days:
                errors.append(
                    f"{market}: only {counts[market]} complete replay days; {days} required"
                )
    status = "PASS" if not errors and all(counts[m] == days for m in MARKETS) else "FAIL"
    return {
        "status": status,
        "days_per_market": counts,
        "paid_api_calls": 0,
        "errors": errors,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--days", type=int, default=20)
    args = parser.parse_args(argv)
    payload = validate_replay(args.data_dir, days=args.days)
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0 if payload["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
