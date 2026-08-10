"""Selective, read-only seed export for the Prediction shadow store."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile, TemporaryDirectory

from .prediction_arbitrage_store import PredictionArbitrageStore

_SNAPSHOT_RETRIES = 3


def _source_files(source_path: Path) -> tuple[Path, ...]:
    return tuple(Path(f"{source_path}{suffix}") for suffix in ("", "-wal", "-shm"))


def _source_file_state(source_path: Path) -> tuple[tuple[bool, int, int], ...]:
    state: list[tuple[bool, int, int]] = []
    for path in _source_files(source_path):
        try:
            stat = path.stat()
        except FileNotFoundError:
            state.append((False, 0, 0))
        else:
            state.append((True, stat.st_size, stat.st_mtime_ns))
    return tuple(state)


def _copy_source_snapshot(source_path: Path, target_path: Path) -> None:
    target_path.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(_SNAPSHOT_RETRIES):
        before = _source_file_state(source_path)
        try:
            for source_file, target_file in zip(
                _source_files(source_path), _source_files(target_path)
            ):
                if source_file.exists():
                    shutil.copyfile(source_file, target_file)
                elif target_file.exists():
                    target_file.unlink()
            after = _source_file_state(source_path)
        except FileNotFoundError:
            continue
        if before == after:
            return
    raise RuntimeError("source database changed while taking shadow snapshot")


def seed_shadow_store(
    *, source_data_dir: Path, shadow_data_dir: Path
) -> dict[str, object]:
    """Copy the relation and LLM cache snapshot without copying operational state."""

    source_path = (
        Path(source_data_dir) / "prediction_arbitrage" / "prediction_arbitrage.sqlite3"
    )
    destination_path = (
        Path(shadow_data_dir) / "prediction_arbitrage" / "prediction_arbitrage.sqlite3"
    )
    if source_path.resolve() == destination_path.resolve() or (
        source_path.exists()
        and destination_path.exists()
        and os.path.samefile(source_path, destination_path)
    ):
        raise ValueError("source and shadow must use different databases")
    destination = PredictionArbitrageStore(Path(shadow_data_dir))

    with TemporaryDirectory(prefix="open-trader-shadow-seed-") as temporary:
        snapshot_path = Path(temporary) / source_path.name
        _copy_source_snapshot(source_path, snapshot_path)
        source_uri = f"{snapshot_path.resolve().as_uri()}?mode=ro"
        source = sqlite3.connect(source_uri, uri=True)
        try:
            source.execute("BEGIN")
            relation_rows = source.execute(
                "SELECT singleton, payload, full_scanned_at, updated_at "
                "FROM relation_state ORDER BY singleton"
            ).fetchall()
            cache_rows = source.execute(
                "SELECT cache_key, payload, created_at FROM llm_cache ORDER BY cache_key"
            ).fetchall()
            source.execute("COMMIT")
        finally:
            source.close()

    canonical_rows = {
        "relation_state": relation_rows,
        "llm_cache": cache_rows,
    }
    digest = hashlib.sha256(
        json.dumps(canonical_rows, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    with destination._transaction() as connection:
        connection.execute("DELETE FROM relation_state")
        connection.execute("DELETE FROM llm_cache")
        connection.executemany(
            "INSERT INTO relation_state(singleton, payload, full_scanned_at, updated_at) "
            "VALUES (?, ?, ?, ?)",
            relation_rows,
        )
        connection.executemany(
            "INSERT INTO llm_cache(cache_key, payload, created_at) VALUES (?, ?, ?)",
            cache_rows,
        )
    return {
        "source_path": str(source_path.resolve()),
        "shadow_path": str(destination.path.resolve()),
        "relation_state_rows": len(relation_rows),
        "llm_cache_rows": len(cache_rows),
        "seeded_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "sha256": digest,
    }


def _write_report(path: Path, report: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, delete=False
    ) as handle:
        json.dump(report, handle, sort_keys=True, indent=2)
        handle.write("\n")
        temporary = Path(handle.name)
    temporary.replace(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-data-dir", type=Path, required=True)
    parser.add_argument("--shadow-data-dir", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args(argv)
    report = seed_shadow_store(
        source_data_dir=args.source_data_dir,
        shadow_data_dir=args.shadow_data_dir,
    )
    _write_report(args.report, report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
