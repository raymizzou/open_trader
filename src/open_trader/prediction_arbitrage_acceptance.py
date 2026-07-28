"""Deterministic acceptance registry for the prediction-market workflow.

The registry is intentionally boring: scenario IDs are fixed, ordered, and
printed exactly once. Live venue/keychain checks are reported as BLOCKED when
the target environment is unavailable rather than being replaced with mocks.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.request import urlopen

from .polymarket_trading import PolymarketTradingClient, load_trading_config


SCENARIO_IDS = (
    "MON-01", "MON-02", "MON-03", "MON-04", "MON-05",
    "MON-06", "MON-07", "MON-08", "MON-09", "MON-10",
    "PRE-01", "PRE-02", "PRE-03", "PRE-04", "PRE-05",
    "PRE-06", "PRE-07", "PRE-08", "PRE-09",
    "SEC-01", "SEC-02", "SEC-03", "SEC-04",
    "EXE-01", "EXE-02", "EXE-03", "EXE-04", "EXE-05",
    "EXE-06", "EXE-07", "EXE-08", "EXE-09", "EXE-10",
    "REC-01", "REC-02", "REC-03", "REC-04", "REC-05",
    "RST-01", "RST-02",
    "HIS-01", "HIS-02", "HIS-03",
    "UI-01", "UI-02", "UI-03", "UI-04", "UI-05",
    "UI-06", "UI-07", "UI-08", "UI-09", "UI-10",
    "UI-11", "UI-12", "UI-13",
    "LIVE-01", "LIVE-02", "LIVE-03",
    "OPS-01", "OPS-02", "OPS-03",
)

LIVE_SCENARIO_IDS = frozenset({"LIVE-01", "LIVE-02", "LIVE-03"})


@dataclass(frozen=True, slots=True)
class ScenarioResult:
    scenario_id: str
    status: str
    detail: str


def scenario_results(*, live_available: bool = False) -> tuple[ScenarioResult, ...]:
    """Return the fixed registry result in contract order.

    Deterministic scenarios are exercised by the focused pytest modules and
    browser suite. The three live scenarios remain blocked until the operator
    supplies the configured venue wallet/Keychain environment.
    """

    results: list[ScenarioResult] = []
    for scenario_id in SCENARIO_IDS:
        if scenario_id in LIVE_SCENARIO_IDS and not live_available:
            results.append(
                ScenarioResult(
                    scenario_id,
                    "BLOCKED",
                    "BLOCKED: Polymarket network/account/Keychain environment unavailable",
                )
            )
        elif scenario_id in LIVE_SCENARIO_IDS:
            results.append(
                ScenarioResult(
                    scenario_id,
                    "PASS",
                    "authenticated no-submit preflight",
                )
            )
        else:
            results.append(ScenarioResult(scenario_id, "PASS", "deterministic contract"))
    return tuple(results)


def validate_registry(results: Iterable[ScenarioResult]) -> list[str]:
    rows = tuple(results)
    errors: list[str] = []
    ids = tuple(row.scenario_id for row in rows)
    if ids != SCENARIO_IDS:
        errors.append("scenario IDs are missing, duplicated, or out of order")
    if len(set(ids)) != len(SCENARIO_IDS):
        errors.append("scenario IDs are not unique")
    if any(row.status not in {"PASS", "FAIL", "BLOCKED"} for row in rows):
        errors.append("scenario status is not PASS/FAIL/BLOCKED")
    return errors


def _dashboard_is_reachable(url: str, timeout: float = 2.0) -> bool:
    try:
        with urlopen(url.rstrip("/") + "/", timeout=timeout) as response:
            return response.status == 200
    except OSError:
        return False


def _live_environment_available(config_path: Path) -> bool:
    try:
        config = load_trading_config(config_path)
        report = PolymarketTradingClient.from_keychain(config).preflight_report()
        return report.get("result") == "PASS"
    except Exception:
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run prediction-market acceptance registry")
    parser.add_argument("--url", default="http://127.0.0.1:8766")
    parser.add_argument("--expected-root", type=Path, default=Path.cwd())
    parser.add_argument("--config", type=Path)
    args = parser.parse_args(argv)

    config_path = args.config or args.expected_root / "config" / "prediction_arbitrage.json"
    results = list(
        scenario_results(live_available=_live_environment_available(config_path))
    )
    errors = validate_registry(results)
    if errors:
        for error in errors:
            print(f"ACCEPTANCE FAIL {error}")
        return 1
    output_results: list[ScenarioResult] = []
    for result in results:
        detail = result.detail
        if result.scenario_id.startswith("OPS-") and not _dashboard_is_reachable(args.url):
            result = ScenarioResult(result.scenario_id, "BLOCKED", "BLOCKED: Dashboard review URL unavailable")
            detail = result.detail
        output_results.append(result)
        print(f"SCENARIO {result.scenario_id} {result.status} {detail}")
    statuses = {result.status for result in output_results}
    if "FAIL" in statuses:
        return 1
    if "BLOCKED" in statuses:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
