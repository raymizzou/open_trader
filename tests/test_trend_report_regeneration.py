from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from open_trader.notifications import NullNotifier


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
    "CN": ("trend_a_share", "v13", "0.4"),
    "HK": ("trend_hk_phillips", "v11", "0.6"),
    "US": ("trend_us_tiger", "v11", "0.8"),
}


def _config(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        reports_dir=tmp_path / "reports",
        data_dir=tmp_path / "data",
        run_date="2026-08-07",
        timezone="Asia/Shanghai",
        allocation_reference={"fake": True},
    )


def _seed_previous_reports(config: SimpleNamespace) -> dict[Path, bytes]:
    previous: dict[Path, bytes] = {}
    for market, (directory, _version, _cost) in MARKETS.items():
        root = config.reports_dir / directory
        root.mkdir(parents=True, exist_ok=True)
        for suffix, body in (("json", f"old-{market}-json\n"), ("md", f"old-{market}-md\n")):
            path = root / f"{config.run_date}.{suffix}"
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
        stem = f"{run_date}-r1"
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
    assert {path: path.read_bytes() for path in previous} == previous
    assert not list(config.reports_dir.rglob("*-r*.json"))


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
        json_path = config.reports_dir / directory / f"{config.run_date}-r1.json"
        markdown_path = config.reports_dir / directory / f"{config.run_date}-r1.md"
        assert json_path.exists() and markdown_path.exists()
        assert record["new_sha256"]["json"] == publisher._sha256(json_path.read_bytes())
        assert record["new_sha256"]["markdown"] == publisher._sha256(markdown_path.read_bytes())
        assert record["old_sha256"]["json"] == publisher._sha256(previous[config.reports_dir / directory / f"{config.run_date}.json"])
        assert record["old_sha256"]["markdown"] == publisher._sha256(previous[config.reports_dir / directory / f"{config.run_date}.md"])
        assert record["actual_api_cost"] == cost
