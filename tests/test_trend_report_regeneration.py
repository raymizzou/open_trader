from __future__ import annotations

import importlib.util
import json
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from open_trader import a_share_trend as trend_module
from open_trader import trend_review
from open_trader.a_share_trend import AccountSnapshot, CandidateInput
from open_trader.notifications import NullNotifier
from open_trader.trend_allocation import build_allocation_snapshot


SCRIPT_PATH = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "regenerate_trend_reports_no_submit.py"
)
SPEC = importlib.util.spec_from_file_location("trend_report_regeneration", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
publisher = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(publisher)


MARKETS = {
    "CN": ("trend_a_share", "v17", "0.4"),
    "HK": ("trend_hk_phillips", "v14", "0.6"),
    "US": ("trend_us_futu", "v14", "0.8"),
}


def _config(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        repo=tmp_path / "old-checkout",
        reports_dir=tmp_path / "reports",
        data_dir=tmp_path / "data",
        run_date="2026-08-07",
        timezone="Asia/Shanghai",
        futu_host="127.0.0.1",
        futu_port=11111,
        allocation_reference={"snapshot": {"allocation_date": "2026-08-07"}},
    )


def _seed_previous_reports(config: SimpleNamespace) -> dict[Path, bytes]:
    previous: dict[Path, bytes] = {}
    for market, (directory, _version, _cost) in MARKETS.items():
        root = config.reports_dir / directory
        root.mkdir(parents=True, exist_ok=True)
        report_date = (
            (date.fromisoformat(config.run_date) - timedelta(days=1)).isoformat()
            if market == "US"
            else config.run_date
        )
        for suffix, body in (("json", f"old-{market}-json\n"), ("md", f"old-{market}-md\n")):
            path = root / f"{report_date}.{suffix}"
            path.write_text(body, encoding="utf-8")
            previous[path] = path.read_bytes()
    return previous


def _fake_generator(calls: list[dict[str, object]], market: str):
    directory, version, cost = MARKETS[market]

    def generate(*, config, run_date, revision, notifier, **_kwargs):
        calls.append(
            {
                "market": market,
                "config": config,
                "run_date": run_date,
                "revision": revision,
                "notifier": notifier,
            }
        )
        assert revision is True
        assert isinstance(notifier, NullNotifier)
        root = config.reports_dir / directory
        root.mkdir(parents=True, exist_ok=True)
        report_date = (
            (date.fromisoformat(run_date) - timedelta(days=1)).isoformat()
            if market == "US"
            else run_date
        )
        stem = f"{report_date}-r1"
        json_path = root / f"{stem}.json"
        markdown_path = root / f"{stem}.md"
        payload = {
            "metadata": {"market": market},
            "strategy_snapshot": {
                "market": market,
                "strategy_id": f"trend_animals_warm_to_hot/{market}/{version}",
                "strategy_version": version,
            },
            "actual_api_cost": cost,
        }
        json_path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
        markdown_path.write_text(f"# {market} revision\n", encoding="utf-8")
        return SimpleNamespace(
            status="generated", report_path=markdown_path, json_path=json_path
        )

    return generate


def _current_cn_v2_payload(tmp_path: Path) -> dict[str, object]:
    roots = {
        "CN": {
            "stock": {"asset": "A股", "tm_id": 10, "as_of_date": "2026-08-07", "global_strength": "70"},
            "etf": {"asset": "ETF基金", "tm_id": 11, "as_of_date": "2026-08-07", "global_strength": "60"},
        },
        "HK": {
            "stock": {"asset": "港股", "tm_id": 20, "as_of_date": "2026-08-07", "global_strength": "90"},
            "etf": {"asset": "香港ETF", "tm_id": 21, "as_of_date": "2026-08-07", "global_strength": "80"},
        },
        "US": {
            "stock": {"asset": "美股", "tm_id": 30, "as_of_date": "2026-08-07", "global_strength": "50"},
            "etf": {"asset": "美国ETF", "tm_id": 31, "as_of_date": "2026-08-07", "global_strength": "40"},
        },
    }
    allocation_snapshot = build_allocation_snapshot(
        allocation_date="2026-08-07",
        generated_at="2026-08-07T16:18:00+08:00",
        git_sha="a" * 40,
        roots=roots,
        previous=None,
        version=2,
    )
    allocation = {
        "daily_path": "data/trend_allocation/daily/2026-08-07.json",
        "sha256": "b" * 64,
        "snapshot": allocation_snapshot,
    }
    strategy = trend_module.live_trend_strategy_snapshot(
        "CN", "abc123", (10, 11, 12), allocation=allocation
    )
    candidate = CandidateInput(
        tm_id=600001,
        symbol="600001",
        exchange="SH",
        name="股票600001",
        asset="A股",
        industry="电力",
        as_of_date="2026-08-07",
        tradable=True,
        amount=Decimal("2"),
        right_side=True,
        days=3,
        strength=Decimal("96"),
        danger=False,
        close=Decimal("10"),
        atr=Decimal("0.5"),
        industry_tm_id=700001,
        industry_temperature="热",
        filter_price=Decimal("10"),
        market_cap=Decimal("100"),
        temperature_prev="温",
        temperature_curr="热",
        phase="立夏",
        global_strength=Decimal("100"),
    )
    report = trend_module.build_report(
        as_of_date="2026-08-07",
        execution_date="2026-08-08",
        market="CN",
        account=AccountSnapshot(
            source_date="2026-08-07",
            fresh=True,
            net_value=Decimal("100000"),
            available_cash=Decimal("100000"),
            positions=(),
            exceptions=(),
        ),
        candidates=(candidate,),
        holding_snapshots={},
        bars_by_symbol={},
        metadata={"market": "CN"},
        strategy_snapshot=strategy,
        allocation_reference=allocation,
        account_input={
            "snapshot_generation": "sha256:" + "a" * 64,
            "account_generation": "sha256:" + "b" * 64,
            "status": "healthy",
        },
    )
    payload = trend_module._report_payload(report)
    judgments = payload["strategy_judgments"]
    assert isinstance(judgments, dict)
    judgments["simulated_buy_fifo"] = trend_review.freeze_simulated_buy_fifo(
        data_dir=tmp_path / "fifo",
        report=payload,
        market="CN",
        execution_date="2026-08-08",
        persist=False,
    )
    judgments["planned_new_seats"] = 1
    return payload


def test_stage_rejects_current_report_that_controller_cannot_execute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    previous = _seed_previous_reports(config)

    def invalid_cn_generator(*, config, run_date, **_kwargs):
        payload = _current_cn_v2_payload(tmp_path)
        judgments = payload["strategy_judgments"]
        assert isinstance(judgments, dict)
        del judgments["simulated_buy_fifo"]
        del judgments["planned_new_seats"]
        root = config.reports_dir / "trend_a_share"
        root.mkdir(parents=True, exist_ok=True)
        json_path = root / f"{run_date}-r1.json"
        markdown_path = root / f"{run_date}-r1.md"
        json_path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
        markdown_path.write_text("# CN revision\n", encoding="utf-8")
        return SimpleNamespace(
            status="generated", report_path=markdown_path, json_path=json_path
        )

    monkeypatch.setattr(publisher, "run_a_share_trend_report", invalid_cn_generator)
    monkeypatch.setattr(
        publisher,
        "run_market_trend_report",
        lambda **kwargs: _fake_generator([], kwargs["market"])(**kwargs),
    )

    with pytest.raises(ValueError, match="invalid staged trend report contract"):
        publisher.stage_and_publish(config, publish=False)

    assert (
        {path: path.read_bytes() for path in previous},
        list(config.reports_dir.rglob("*-r*.json")),
    ) == (previous, [])


def test_stage_calls_all_markets_with_revision_and_does_not_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    previous = _seed_previous_reports(config)
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        publisher,
        "run_a_share_trend_report",
        _fake_generator(calls, "CN"),
    )
    monkeypatch.setattr(
        publisher,
        "run_market_trend_report",
        lambda **kwargs: _fake_generator(calls, kwargs["market"])(**kwargs),
    )

    manifest = publisher.stage_and_publish(config, publish=False)

    assert manifest["status"] == "PASS"
    assert manifest["published"] is False
    assert manifest["submitted_orders"] == 0
    assert [call["market"] for call in calls] == ["CN", "HK", "US"]
    assert all(call["revision"] is True for call in calls)
    assert all(isinstance(call["notifier"], NullNotifier) for call in calls)
    assert all(call["config"].repo == SCRIPT_PATH.parents[1] for call in calls)
    assert {path: path.read_bytes() for path in previous} == previous
    assert not list(config.reports_dir.rglob("*-r*.json"))


def test_stage_uses_allocation_date_for_each_market_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import open_trader.futu_quote as futu_quote
    import open_trader.trend_allocation as trend_allocation

    config = _config(tmp_path)
    config.run_date = "2026-08-09"
    del config.allocation_reference
    _seed_previous_reports(config)
    calls: list[dict[str, object]] = []
    reference = {"snapshot": {"allocation_date": "2026-08-07"}}

    class Quote:
        def __init__(self, **_kwargs):
            pass

        def get_trading_days(self, **_kwargs):
            return ["2026-08-07"]

        def close(self):
            pass

    monkeypatch.setattr(futu_quote, "FutuQuoteClient", Quote)
    monkeypatch.setattr(
        trend_allocation,
        "load_allocation_reference",
        lambda *_args, **_kwargs: reference,
    )
    monkeypatch.setattr(
        trend_allocation,
        "allocation_reference_for_report",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("revision publisher must not require the current cycle")
        ),
    )
    monkeypatch.setattr(
        publisher,
        "run_a_share_trend_report",
        _fake_generator(calls, "CN"),
    )
    monkeypatch.setattr(
        publisher,
        "run_market_trend_report",
        lambda **kwargs: _fake_generator(calls, kwargs["market"])(**kwargs),
    )

    manifest = publisher.stage_and_publish(config, publish=False)

    assert manifest["run_date"] == "2026-08-07"
    assert [(call["market"], call["run_date"]) for call in calls] == [
        ("CN", "2026-08-07"),
        ("HK", "2026-08-07"),
        ("US", "2026-08-07"),
    ]


def test_market_failure_blocks_every_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    previous = _seed_previous_reports(config)
    calls: list[str] = []

    monkeypatch.setattr(
        publisher,
        "run_a_share_trend_report",
        lambda **kwargs: (_fake_generator([], "CN")(**kwargs)),
    )

    def fail_hk(**kwargs):
        calls.append(kwargs["market"])
        raise RuntimeError("HK unavailable")

    monkeypatch.setattr(publisher, "run_market_trend_report", fail_hk)

    with pytest.raises(RuntimeError, match="HK unavailable"):
        publisher.stage_and_publish(config, publish=True)

    assert calls == ["HK"]
    assert {path: path.read_bytes() for path in previous} == previous
    assert not list(config.reports_dir.rglob("*-r*.json"))


def test_publish_uses_immutable_pairs_and_records_hashes_and_costs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    previous = _seed_previous_reports(config)
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        publisher,
        "run_a_share_trend_report",
        _fake_generator(calls, "CN"),
    )
    monkeypatch.setattr(
        publisher,
        "run_market_trend_report",
        lambda **kwargs: _fake_generator(calls, kwargs["market"])(**kwargs),
    )

    manifest = publisher.stage_and_publish(config, publish=True)

    assert manifest["status"] == "PASS"
    assert manifest["published"] is True
    assert manifest["submitted_orders"] == 0
    assert {path: path.read_bytes() for path in previous} == previous
    assert len(manifest["markets"]) == 3
    for market, record in manifest["markets"].items():
        directory, _version, cost = MARKETS[market]
        report_date = "2026-08-06" if market == "US" else config.run_date
        json_path = config.reports_dir / directory / f"{report_date}-r1.json"
        markdown_path = config.reports_dir / directory / f"{report_date}-r1.md"
        assert json_path.exists() and markdown_path.exists()
        assert record["new_sha256"]["json"] == publisher._sha256(json_path.read_bytes())
        assert record["new_sha256"]["markdown"] == publisher._sha256(markdown_path.read_bytes())
        assert record["old_sha256"]["json"] == publisher._sha256(previous[config.reports_dir / directory / f"{report_date}.json"])
        assert record["old_sha256"]["markdown"] == publisher._sha256(previous[config.reports_dir / directory / f"{report_date}.md"])
        assert record["actual_api_cost"] == cost


@pytest.mark.parametrize(
    ("market", "version", "strategy_id", "metadata_market"),
    [
        ("CN", "v14", "trend_animals_warm_to_hot/CN/v14", "CN"),
        ("HK", "v12", "trend_animals_warm_to_hot/HK/v12", "HK"),
        ("US", "v13", "trend_animals_warm_to_hot/US/v12", "US"),
        ("CN", "v15", "trend_animals_warm_to_hot/CN/v15", "HK"),
    ],
)
def test_validate_artifact_rejects_old_and_hybrid_strategy_identity(
    tmp_path: Path,
    market: str,
    version: str,
    strategy_id: str,
    metadata_market: str,
) -> None:
    directory = MARKETS[market][0]
    stage_root = tmp_path / "reports"
    root = stage_root / directory
    root.mkdir(parents=True)
    json_path = root / "2026-08-07-r1.json"
    markdown_path = root / "2026-08-07-r1.md"
    json_path.write_text(
        json.dumps({
            "metadata": {"market": metadata_market},
            "strategy_snapshot": {
                "market": market,
                "strategy_id": strategy_id,
                "strategy_version": version,
            },
        }),
        encoding="utf-8",
    )
    markdown_path.write_text("# report\n", encoding="utf-8")

    with pytest.raises(ValueError):
        publisher._validate_artifact(
            market=market,
            result=SimpleNamespace(
                status="generated", json_path=json_path, report_path=markdown_path,
            ),
            stage_root=stage_root,
            before={},
        )
