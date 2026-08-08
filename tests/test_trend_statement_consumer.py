from __future__ import annotations

import json
from pathlib import Path

import pytest

from open_trader.trend_api_stats import build_trend_api_stats_payload, write_trend_api_stats


GENERATION_A = "sha256:" + "a" * 64
GENERATION_B = "sha256:" + "b" * 64
ACCOUNT_GENERATION = "sha256:" + "c" * 64
SNAPSHOT_GENERATION = "sha256:" + "d" * 64


def _snapshot(
    statement_generation: str,
    *,
    snapshot_generation: str = SNAPSHOT_GENERATION,
    account_generation: str = ACCOUNT_GENERATION,
) -> dict[str, object]:
    return {
        "snapshot_generation": snapshot_generation,
        "account_generation": account_generation,
        "accepted_statement_generation": {
            "phillips": statement_generation,
            "eastmoney": "",
        },
    }


def _facts(generation: str, *, cutoff: str = "2026-08-04T11:00:00+08:00") -> dict[str, object]:
    return {
        "statement_generation": generation,
        "statement_period": "2026-08-04",
        "trade_facts_cutoff_at": cutoff,
        "facts": [],
    }


def _consume(
    tmp_path: Path, *, generated_at: str = "2026-08-04T12:00:00+08:00"
) -> dict[str, object]:
    from open_trader.trend_statement_consumer import (
        consume_accepted_statement_facts,
    )

    return consume_accepted_statement_facts(
        data_dir=tmp_path / "data",
        reports_dir=tmp_path / "reports",
        broker="phillips",
        generated_at=generated_at,
        account_url="http://account",
    )


def test_trend_consumes_one_http_snapshot_and_its_accepted_facts_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import open_trader.trend_statement_consumer as consumer

    snapshots: list[tuple[str, float]] = []
    facts_calls: list[tuple[str, str, str, float]] = []
    monkeypatch.setattr(
        consumer,
        "fetch_account_snapshot",
        lambda url, timeout: (snapshots.append((url, timeout)) or _snapshot(GENERATION_A)),
        raising=False,
    )
    monkeypatch.setattr(
        consumer,
        "fetch_statement_trade_facts",
        lambda url, broker, generation, timeout: (
            facts_calls.append((url, broker, generation, timeout)) or _facts(generation)
        ),
        raising=False,
    )

    result = _consume(tmp_path)

    assert result["status"] == "consumed"
    assert result["snapshot_generation"] == SNAPSHOT_GENERATION
    assert result["account_generation"] == ACCOUNT_GENERATION
    assert result["statement_generation"] == GENERATION_A
    assert snapshots == [("http://account", 5.0)]
    assert facts_calls == [("http://account", "phillips", GENERATION_A, 5.0)]


def test_trend_waiting_status_keeps_snapshot_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import open_trader.trend_statement_consumer as consumer

    monkeypatch.setattr(
        consumer,
        "fetch_account_snapshot",
        lambda *_args: _snapshot(""),
        raising=False,
    )
    monkeypatch.setattr(
        consumer,
        "fetch_statement_trade_facts",
        lambda *_args: pytest.fail("facts must not be fetched before promotion"),
        raising=False,
    )

    result = _consume(tmp_path)

    assert result == {
        "schema_version": "open_trader.trend.statement_consumption.v1",
        "status": "waiting_for_promotion",
        "broker": "phillips",
        "snapshot_generation": SNAPSHOT_GENERATION,
        "statement_generation": "",
        "account_generation": ACCOUNT_GENERATION,
    }


def test_trend_waits_for_same_day_market_close_cutoff_from_http_facts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import open_trader.trend_statement_consumer as consumer

    monkeypatch.setattr(
        consumer,
        "fetch_account_snapshot",
        lambda *_args: _snapshot(GENERATION_A),
        raising=False,
    )
    monkeypatch.setattr(
        consumer,
        "fetch_statement_trade_facts",
        lambda *_args: _facts(GENERATION_A, cutoff="2026-08-04T16:00:00+08:00"),
        raising=False,
    )

    result = _consume(tmp_path, generated_at="2026-08-04T13:00:00+08:00")

    assert result["status"] == "waiting_for_statement_cutoff"
    assert result["retry_after"] == "2026-08-04T16:00:00+08:00"
    assert result["snapshot_generation"] == SNAPSHOT_GENERATION
    assert result["account_generation"] == ACCOUNT_GENERATION
    assert result["statement_generation"] == GENERATION_A
    assert not (tmp_path / "data/latest/trend_api_stats.json").exists()


def test_trend_restarts_once_after_accepted_generation_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import open_trader.trend_statement_consumer as consumer
    from open_trader.account_http import AccountHttpError

    snapshots = iter([_snapshot(GENERATION_A), _snapshot(GENERATION_B)])
    fact_generations: list[str] = []
    second_attempt_seen = False
    monkeypatch.setattr(
        consumer,
        "fetch_account_snapshot",
        lambda *_args: next(snapshots),
        raising=False,
    )

    def fetch_facts(*_args: object) -> dict[str, object]:
        nonlocal second_attempt_seen
        generation = str(_args[2])
        fact_generations.append(generation)
        if generation == GENERATION_A:
            raise AccountHttpError("accepted_statement_generation_changed")
        second_attempt_seen = True
        assert not (tmp_path / "data/latest/trend_api_stats.json").exists()
        assert not (
            tmp_path / "data/trend_statement_consumption/phillips.json"
        ).exists()
        return _facts(generation)

    monkeypatch.setattr(
        consumer, "fetch_statement_trade_facts", fetch_facts, raising=False)

    result = _consume(tmp_path)

    assert result["status"] == "consumed"
    assert result["statement_generation"] == GENERATION_B
    assert fact_generations == [GENERATION_A, GENERATION_B]
    assert second_attempt_seen
    status = json.loads(
        (tmp_path / "data/trend_statement_consumption/phillips.json").read_text(
            encoding="utf-8"
        )
    )
    assert status["statement_generation"] == GENERATION_B


def test_trend_blocks_after_second_accepted_generation_change_without_promotion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import open_trader.trend_statement_consumer as consumer
    from open_trader.account_http import AccountHttpError

    snapshots = iter([_snapshot(GENERATION_A), _snapshot(GENERATION_B)])
    facts_calls: list[str] = []
    monkeypatch.setattr(
        consumer,
        "fetch_account_snapshot",
        lambda *_args: next(snapshots),
        raising=False,
    )

    def fetch_facts(*args: object) -> dict[str, object]:
        facts_calls.append(str(args[2]))
        raise AccountHttpError("accepted_statement_generation_changed")

    monkeypatch.setattr(consumer, "fetch_statement_trade_facts", fetch_facts, raising=False)

    result = _consume(tmp_path)

    assert result["status"] == "blocked"
    assert result["reason"] == "accepted_statement_generation_changed"
    assert result["snapshot_generation"] == SNAPSHOT_GENERATION
    assert result["statement_generation"] == GENERATION_B
    assert facts_calls == [GENERATION_A, GENERATION_B]
    assert not (tmp_path / "data/latest/trend_api_stats.json").exists()
    assert not (tmp_path / "data/trend_statement_consumption/phillips.json").exists()


@pytest.mark.parametrize(
    "code",
    [
        pytest.param("account_unavailable", id="transport"),
        pytest.param("account_unavailable", id="timeout"),
        pytest.param("account_contract_invalid", id="invalid_contract"),
        pytest.param("account_unavailable", id="http_503"),
    ],
)
def test_trend_blocks_sanitized_snapshot_failures_after_one_http_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: str
) -> None:
    import open_trader.trend_statement_consumer as consumer
    from open_trader.account_http import AccountHttpError

    calls = 0

    def fetch_snapshot(*_args: object) -> dict[str, object]:
        nonlocal calls
        calls += 1
        raise AccountHttpError(code)

    monkeypatch.setattr(consumer, "fetch_account_snapshot", fetch_snapshot, raising=False)

    result = _consume(tmp_path)

    assert result["status"] == "blocked"
    assert result["reason"] == code
    assert calls == 1
    assert "error_type" not in result


@pytest.mark.parametrize(
    "code",
    [
        pytest.param("account_unavailable", id="transport"),
        pytest.param("account_unavailable", id="timeout"),
        pytest.param("account_contract_invalid", id="invalid_contract"),
        pytest.param("account_unavailable", id="http_503"),
    ],
)
def test_trend_blocks_sanitized_facts_failures_after_one_snapshot_and_facts_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: str
) -> None:
    import open_trader.trend_statement_consumer as consumer
    from open_trader.account_http import AccountHttpError

    snapshot_calls = 0
    facts_calls = 0

    def fetch_snapshot(*_args: object) -> dict[str, object]:
        nonlocal snapshot_calls
        snapshot_calls += 1
        return _snapshot(GENERATION_A)

    def fetch_facts(*_args: object) -> dict[str, object]:
        nonlocal facts_calls
        facts_calls += 1
        raise AccountHttpError(code)

    monkeypatch.setattr(consumer, "fetch_account_snapshot", fetch_snapshot, raising=False)
    monkeypatch.setattr(
        consumer, "fetch_statement_trade_facts", fetch_facts, raising=False
    )

    result = _consume(tmp_path)

    assert result["status"] == "blocked"
    assert result["reason"] == code
    assert result["snapshot_generation"] == SNAPSHOT_GENERATION
    assert result["account_generation"] == ACCOUNT_GENERATION
    assert result["statement_generation"] == GENERATION_A
    assert snapshot_calls == 1
    assert facts_calls == 1
    assert "error_type" not in result


def test_trend_failed_facts_processing_keeps_snapshot_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import open_trader.trend_statement_consumer as consumer

    monkeypatch.setattr(
        consumer,
        "fetch_account_snapshot",
        lambda *_args: _snapshot(GENERATION_A),
        raising=False,
    )
    monkeypatch.setattr(
        consumer,
        "fetch_statement_trade_facts",
        lambda *_args: {"statement_generation": GENERATION_A},
        raising=False,
    )

    result = _consume(tmp_path)

    assert result["status"] == "failed"
    assert result["reason"] == "statement_facts_processing_failed"
    assert result["account_generation"] == ACCOUNT_GENERATION
    assert result["statement_generation"] == GENERATION_A


def test_trend_failed_statistics_keeps_prior_artifact_and_retries_accepted_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import open_trader.trend_statement_consumer as consumer

    write_trend_api_stats(
        tmp_path / "data",
        build_trend_api_stats_payload(
            [],
            strategy_versions=[],
            generated_at="2026-08-04T12:00:00+08:00",
            statistics_cutoff_at="2026-08-04T11:00:00+08:00",
        ),
    )
    artifact_path = tmp_path / "data/latest/trend_api_stats.json"
    before = artifact_path.read_bytes()
    monkeypatch.setattr(
        consumer,
        "fetch_account_snapshot",
        lambda *_args: _snapshot(GENERATION_A),
        raising=False,
    )
    monkeypatch.setattr(
        consumer,
        "fetch_statement_trade_facts",
        lambda *_args: _facts(GENERATION_A),
        raising=False,
    )
    real_build = consumer.build_statement_actual_stats_payload
    attempts = 0

    def fail_once(**kwargs: object) -> dict[str, object]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ValueError("statistics validation failed")
        return real_build(**kwargs)

    monkeypatch.setattr(
        consumer, "build_statement_actual_stats_payload", fail_once, raising=False
    )

    failed = _consume(tmp_path)

    assert failed == {
        "schema_version": "open_trader.trend.statement_consumption.v1",
        "status": "failed",
        "broker": "phillips",
        "statement_generation": GENERATION_A,
        "account_generation": ACCOUNT_GENERATION,
        "attempted_at": "2026-08-04T12:00:00+08:00",
        "reason": "statement_facts_processing_failed",
    }
    assert artifact_path.read_bytes() == before
    assert _consume(tmp_path)["status"] == "consumed"
    assert attempts == 2


def test_trend_already_consumed_preserves_http_snapshot_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import open_trader.trend_statement_consumer as consumer

    monkeypatch.setattr(
        consumer,
        "fetch_account_snapshot",
        lambda *_args: _snapshot(GENERATION_A),
        raising=False,
    )
    monkeypatch.setattr(
        consumer,
        "fetch_statement_trade_facts",
        lambda *_args: _facts(GENERATION_A),
        raising=False,
    )

    assert _consume(tmp_path)["status"] == "consumed"
    repeated = _consume(tmp_path)

    assert repeated["status"] == "already_consumed"
    assert repeated["snapshot_generation"] == SNAPSHOT_GENERATION
    assert repeated["account_generation"] == ACCOUNT_GENERATION
    assert repeated["statement_generation"] == GENERATION_A
