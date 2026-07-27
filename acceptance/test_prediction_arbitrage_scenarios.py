from __future__ import annotations

import pytest

from open_trader.prediction_arbitrage_acceptance import SCENARIO_IDS, scenario_results, validate_registry


def test_fixed_prediction_scenario_registry_has_no_gaps() -> None:
    results = scenario_results()
    assert len(results) == 54
    assert validate_registry(results) == []


@pytest.mark.parametrize("scenario_id", [pytest.param(value, id=value) for value in SCENARIO_IDS])
def test_prediction_scenario(scenario_id: str) -> None:
    result = scenario_results()[SCENARIO_IDS.index(scenario_id)]
    if result.status == "BLOCKED":
        pytest.skip(result.detail)
    assert result.status == "PASS"
    assert result.detail
