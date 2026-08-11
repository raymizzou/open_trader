from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Mapping


RELEASE_SCHEMA = "open_trader.prediction_service.release.v1"
RUNTIME_SCHEMA = "open_trader.prediction_service.runtime.v1"
RUNTIME_STATES = {"maintenance", "ready", "failed", "stopped"}


@dataclass(frozen=True)
class PredictionReleaseManifest:
    schema_version: str
    reader_generation: int
    contract_generation: int


def load_prediction_release_manifest(path: Path) -> PredictionReleaseManifest:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"prediction release manifest is unreadable: {path}") from exc
    required = {"schema_version", "reader_generation", "contract_generation"}
    if not isinstance(payload, dict) or set(payload) != required:
        raise ValueError("prediction release manifest has invalid keys")
    if payload["schema_version"] != RELEASE_SCHEMA:
        raise ValueError("prediction release manifest has invalid schema")
    for key in ("reader_generation", "contract_generation"):
        if type(payload[key]) is not int or payload[key] < 1:
            raise ValueError(f"prediction release manifest has invalid {key}")
    return PredictionReleaseManifest(
        schema_version=RELEASE_SCHEMA,
        reader_generation=payload["reader_generation"],
        contract_generation=payload["contract_generation"],
    )


def load_prediction_runtime_record(path: Path) -> dict[str, object] | None:
    path = Path(path)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"prediction runtime record is unreadable: {path}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != RUNTIME_SCHEMA:
        raise ValueError("prediction runtime record has invalid schema")
    if payload.get("state") not in RUNTIME_STATES:
        raise ValueError("prediction runtime record has invalid state")
    return payload


def write_prediction_runtime_record(
    path: Path, payload: Mapping[str, object]
) -> None:
    state = payload.get("state")
    if state not in RUNTIME_STATES:
        raise ValueError("prediction runtime state is invalid")
    record = {"schema_version": RUNTIME_SCHEMA, **dict(payload)}
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = ""
    try:
        with NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent,
            prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as handle:
            temporary = handle.name
            json.dump(record, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = ""
    finally:
        if temporary:
            Path(temporary).unlink(missing_ok=True)
