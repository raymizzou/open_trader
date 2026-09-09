from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from open_trader.holding_snapshot_import import (
    HoldingSnapshotImportService,
    load_staged_holding_snapshot,
)


def test_stage_confirmed_snapshot_deduplicates_identical_rows_and_is_idempotent(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    latest = data_dir / "latest"
    report = tmp_path / "reports" / "2026-09-07.md"
    latest.mkdir(parents=True)
    report.parent.mkdir(parents=True)
    (latest / "sentinel").write_bytes(b"latest-before")
    report.write_bytes(b"report-before")

    payload = {
        "data_as_of": "2026-09-07",
        "confirmed": True,
        "complete": True,
        "positions": [
            {
                "symbol": "700",
                "name": "腾讯控股",
                "quantity": "10",
                "cost_price": "400.00",
            },
            {
                "symbol": "00700",
                "name": "腾讯控股",
                "quantity": "10",
                "cost_price": "400.00",
            },
        ],
        "cash": {
            "policy": "replace",
            "currency": "HKD",
            "balance": "1000.00",
            "available_balance": "1000.00",
        },
    }

    service = HoldingSnapshotImportService(data_dir=data_dir)
    first = service.stage_snapshot("phillips", payload)
    second = service.stage_snapshot("phillips", payload)
    assert first["holding_generation"] == second["holding_generation"]
    assert first["holding_generation"] == (
        "sha256:38ccbcff3803be259542adbe7949ecb5a251a873f0f7a7d26562fe19e73612aa"
    )
    assert first["positions"] == [{
        "symbol": "00700",
        "name": "腾讯控股",
        "quantity": "10",
        "cost_price": "400",
    }]
    generations = list((data_dir / "account_holdings/generations/phillips").iterdir())
    assert len(generations) == 1
    assert (latest / "sentinel").read_bytes() == b"latest-before"
    assert report.read_bytes() == b"report-before"


def test_stage_snapshot_rejects_future_date_without_artifacts(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    latest = data_dir / "latest"
    report = tmp_path / "reports" / "2026-09-09.md"
    latest.mkdir(parents=True)
    report.parent.mkdir(parents=True)
    (latest / "sentinel").write_bytes(b"latest-before")
    report.write_bytes(b"report-before")
    payload = {
        "data_as_of": "9999-12-31",
        "confirmed": True,
        "complete": True,
        "positions": [
            {
                "symbol": "00700",
                "name": "腾讯控股",
                "quantity": "10",
                "cost_price": "400",
            }
        ],
        "cash": {
            "policy": "replace",
            "currency": "HKD",
            "balance": "1000",
            "available_balance": "1000",
        },
    }

    service = HoldingSnapshotImportService(data_dir=data_dir)
    with pytest.raises(ValueError, match="future data_as_of"):
        service.stage_snapshot("phillips", payload)

    generations = data_dir / "account_holdings/generations/phillips"
    assert not generations.exists() or not list(generations.iterdir())
    assert (latest / "sentinel").read_bytes() == b"latest-before"
    assert report.read_bytes() == b"report-before"


def test_stage_snapshot_uses_injected_business_date_for_boundaries_and_retry(
    tmp_path: Path,
) -> None:
    for data_as_of in ("2026-09-09", "2026-09-08", "2026-09-10"):
        data_dir = tmp_path / data_as_of
        payload = {
            "data_as_of": data_as_of,
            "confirmed": True,
            "complete": True,
            "positions": [
                {
                    "symbol": "00700",
                    "name": "腾讯控股",
                    "quantity": "10",
                    "cost_price": "400",
                }
            ],
            "cash": {
                "policy": "replace",
                "currency": "HKD",
                "balance": "1000",
                "available_balance": "1000",
            },
        }
        service = HoldingSnapshotImportService(
            data_dir=data_dir,
            business_date=lambda: date(2026, 9, 9),
        )

        if data_as_of == "2026-09-10":
            with pytest.raises(ValueError, match="future data_as_of: 2026-09-10"):
                service.stage_snapshot("phillips", payload)
            generations = data_dir / "account_holdings/generations/phillips"
            assert not generations.exists() or not list(generations.iterdir())
            continue

        first = service.stage_snapshot("phillips", payload)
        retry = HoldingSnapshotImportService(
            data_dir=data_dir,
            business_date=lambda: date(2026, 9, 9),
        )
        second = retry.stage_snapshot("phillips", payload)
        assert first["holding_generation"] == second["holding_generation"]


def test_stage_snapshot_rejects_conflicting_normalized_duplicate_without_artifacts(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    latest = data_dir / "latest"
    report = tmp_path / "reports" / "2026-09-07.md"
    latest.mkdir(parents=True)
    report.parent.mkdir(parents=True)
    (latest / "sentinel").write_bytes(b"latest-before")
    report.write_bytes(b"report-before")
    payload = {
        "data_as_of": "2026-09-07",
        "confirmed": True,
        "complete": True,
        "positions": [
            {
                "symbol": "700",
                "name": "腾讯控股",
                "quantity": "100",
                "cost_price": "400",
            },
            {
                "symbol": "00700",
                "name": "腾讯控股",
                "quantity": "200",
                "cost_price": "400",
            },
        ],
        "cash": {
            "policy": "replace",
            "currency": "HKD",
            "balance": "1000",
            "available_balance": "1000",
        },
    }

    service = HoldingSnapshotImportService(data_dir=data_dir)
    with pytest.raises(ValueError, match="conflicting normalized duplicate position"):
        service.stage_snapshot("phillips", payload)

    generations = data_dir / "account_holdings/generations/phillips"
    assert not generations.exists() or not list(generations.iterdir())
    assert (latest / "sentinel").read_bytes() == b"latest-before"
    assert report.read_bytes() == b"report-before"


def test_stage_snapshot_requires_complete_position_set(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    payload = {
        "data_as_of": "2026-09-07",
        "confirmed": True,
        "complete": False,
        "positions": [
            {
                "symbol": "700",
                "name": "腾讯控股",
                "quantity": "10",
                "cost_price": "400",
            }
        ],
        "cash": {
            "policy": "replace",
            "currency": "HKD",
            "balance": "1000",
            "available_balance": "1000",
        },
    }

    service = HoldingSnapshotImportService(data_dir=data_dir)
    with pytest.raises(ValueError, match="position set is incomplete"):
        service.stage_snapshot("phillips", payload)

    generations = data_dir / "account_holdings/generations/phillips"
    assert not generations.exists() or not list(generations.iterdir())


def test_stage_snapshot_preserves_cash_only_when_explicitly_requested(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    payload = {
        "data_as_of": "2026-09-07",
        "confirmed": True,
        "complete": True,
        "positions": [
            {
                "symbol": "700",
                "name": "腾讯控股",
                "quantity": "10",
                "cost_price": "400",
            }
        ],
    }

    service = HoldingSnapshotImportService(data_dir=data_dir)
    with pytest.raises(ValueError):
        service.stage_snapshot("phillips", payload)
    generations = data_dir / "account_holdings/generations/phillips"
    assert not generations.exists() or not list(generations.iterdir())

    preserved = {
        **payload,
        "cash": {"policy": "preserve"},
    }
    staged = service.stage_snapshot("phillips", preserved)

    assert staged["cash"] == {"policy": "preserve"}


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    [
        ("confirmed", False),
        ("data_as_of", "not-a-date"),
        ("quantity", "NaN"),
        ("quantity", "Infinity"),
        ("quantity", "0"),
        ("quantity", "-1"),
        ("cost_price", "-1"),
    ],
)
def test_stage_snapshot_rejects_invalid_trust_boundary_values_without_artifacts(
    tmp_path: Path, field: str, invalid_value: object
) -> None:
    data_dir = tmp_path / "data"
    payload = {
        "data_as_of": "2026-09-07",
        "confirmed": True,
        "complete": True,
        "positions": [
            {
                "symbol": "700",
                "name": "腾讯控股",
                "quantity": "10",
                "cost_price": "400",
            }
        ],
        "cash": {
            "policy": "replace",
            "currency": "HKD",
            "balance": "1000",
            "available_balance": "1000",
        },
    }
    if field in {"quantity", "cost_price"}:
        payload["positions"][0][field] = invalid_value
    else:
        payload[field] = invalid_value

    service = HoldingSnapshotImportService(data_dir=data_dir)
    with pytest.raises(ValueError):
        service.stage_snapshot("phillips", payload)

    generations = data_dir / "account_holdings/generations/phillips"
    assert not generations.exists() or not list(generations.iterdir())


def test_generation_rejects_tampered_same_date_precedence_metadata_on_load_and_retry(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    payload = {
        "data_as_of": "2026-09-07",
        "confirmed": True,
        "complete": True,
        "positions": [
            {
                "symbol": "700",
                "name": "腾讯控股",
                "quantity": "10",
                "cost_price": "400",
            }
        ],
        "cash": {
            "policy": "replace",
            "currency": "HKD",
            "balance": "1000",
            "available_balance": "1000",
        },
    }

    service = HoldingSnapshotImportService(data_dir=data_dir)
    staged = service.stage_snapshot("phillips", payload)
    generation = staged["holding_generation"]
    assert isinstance(generation, str)
    manifest_path = (
        data_dir
        / "account_holdings/generations/phillips"
        / generation.removeprefix("sha256:")
        / "manifest.json"
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["staged_at"] = "2026-09-07T00:00:00.000001+08:00"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="invalid holding generation"):
        load_staged_holding_snapshot(data_dir, "phillips")
    with pytest.raises(ValueError, match="invalid holding generation"):
        service.stage_snapshot("phillips", payload)
