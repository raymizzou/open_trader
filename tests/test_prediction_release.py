from __future__ import annotations

import json
from pathlib import Path

import pytest

from open_trader.prediction_release import (
    load_prediction_release_manifest,
    load_prediction_runtime_record,
    write_prediction_runtime_record,
)


def test_tracked_prediction_release_manifest_is_generation_one() -> None:
    root = Path(__file__).resolve().parents[1]
    release = load_prediction_release_manifest(
        root / "ops" / "prediction-service-release.json"
    )

    assert release.schema_version == "open_trader.prediction_service.release.v1"
    assert release.reader_generation == 1
    assert release.contract_generation == 1


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"schema_version": "wrong", "reader_generation": 1, "contract_generation": 1},
        {"schema_version": "open_trader.prediction_service.release.v1", "reader_generation": 0, "contract_generation": 1},
        {"schema_version": "open_trader.prediction_service.release.v1", "reader_generation": True, "contract_generation": 1},
        {"schema_version": "open_trader.prediction_service.release.v1", "reader_generation": 1, "contract_generation": 1, "extra": 1},
    ],
)
def test_release_manifest_rejects_wrong_shape(tmp_path: Path, payload: object) -> None:
    path = tmp_path / "release.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="prediction release manifest"):
        load_prediction_release_manifest(path)


def test_runtime_record_is_atomically_replaced_and_round_trips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import open_trader.prediction_release as release_module

    path = tmp_path / "prediction-service-runtime.json"
    replacements: list[tuple[Path, Path]] = []
    real_replace = release_module.os.replace

    def replace(source: str, destination: Path) -> None:
        replacements.append((Path(source), Path(destination)))
        real_replace(source, destination)

    monkeypatch.setattr(release_module.os, "replace", replace)
    previous = {"git_sha": "old", "reader_generation": 1, "contract_generation": 1}
    write_prediction_runtime_record(path, {
        "state": "maintenance",
        "candidate": {"git_sha": "new", "checkout": "/tmp/new", "source_state": "clean", "reader_generation": 1, "contract_generation": 1},
        "previous_release": previous,
        "transition_started_at": "2026-08-11T10:00:00+08:00",
        "updated_at": "2026-08-11T10:00:00+08:00",
        "failure_reason": "",
    })
    write_prediction_runtime_record(path, {
        "state": "failed",
        "candidate": {"git_sha": "new", "checkout": "/tmp/new", "source_state": "clean", "reader_generation": 1, "contract_generation": 1},
        "previous_release": previous,
        "transition_started_at": "2026-08-11T10:00:00+08:00",
        "updated_at": "2026-08-11T10:01:00+08:00",
        "failure_reason": "candidate_not_ready",
    })

    record = load_prediction_runtime_record(path)
    assert record is not None
    assert record["schema_version"] == "open_trader.prediction_service.runtime.v1"
    assert record["state"] == "failed"
    assert record["previous_release"] == previous
    assert len(replacements) == 2
    assert all(destination == path for _source, destination in replacements)
    assert list(path.parent.glob(f".{path.name}.*.tmp")) == []


def test_runtime_record_rejects_unknown_state_and_schema(tmp_path: Path) -> None:
    path = tmp_path / "prediction-service-runtime.json"
    with pytest.raises(ValueError, match="runtime state"):
        write_prediction_runtime_record(path, {"state": "starting"})
    path.write_text('{"schema_version":"wrong","state":"ready"}', encoding="utf-8")
    with pytest.raises(ValueError, match="runtime record"):
        load_prediction_runtime_record(path)
