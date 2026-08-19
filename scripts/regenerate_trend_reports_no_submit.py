"""Stage and publish the three allocation-era Trend Animals reports.

The report generators are deliberately called directly with ``NullNotifier``.
This module does not import the trend controller or any order client, and the
real reports directory is touched only after every market has validated.
"""

from __future__ import annotations

import argparse
import copy
import dataclasses
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
from collections.abc import Mapping
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo

# Run against this checkout even when the shared virtualenv has another
# worktree installed in editable mode.
_CHECKOUT_ROOT = Path(__file__).resolve().parents[1]
_SRC = str(_CHECKOUT_ROOT / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from open_trader.a_share_trend import (
    run_a_share_trend_report,
    valid_frozen_report_contract,
)
from open_trader.daily_premarket import DailyPremarketConfig, load_env_config
from open_trader.market_trend import run_market_trend_report
from open_trader.notifications import NullNotifier


EXPECTED_VERSIONS = {"CN": "v13", "HK": "v11", "US": "v11"}
REPORT_DIRECTORIES = {
    "CN": "trend_a_share",
    "HK": "trend_hk_phillips",
    "US": "trend_us_futu",
}
_REPORT_STEM = re.compile(r"^(?P<date>\d{4}-\d{2}-\d{2})(?:-r(?P<revision>\d+))?$")


def _sha256(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _run_date(config: object) -> str:
    configured = getattr(config, "run_date", None) or os.environ.get(
        "OPEN_TRADER_RUN_DATE"
    )
    if configured:
        value = str(configured)
        date.fromisoformat(value)
        return value
    timezone = ZoneInfo(str(getattr(config, "timezone", "Asia/Shanghai")))
    return datetime.now(timezone).date().isoformat()


def _allocation_reference(config: object, run_date: str) -> Mapping[str, object]:
    configured = getattr(config, "allocation_reference", None)
    if configured is not None:
        if not isinstance(configured, Mapping):
            raise ValueError("configured allocation reference is invalid")
        _allocation_date(configured)
        return configured

    # The allocation controller owns the decision.  Read its immutable
    # terminal reference; do not create or update an allocation here.
    from open_trader.futu_quote import FutuQuoteClient
    from open_trader.trend_allocation import load_allocation_reference

    quote = FutuQuoteClient(
        host=str(config.futu_host), port=int(config.futu_port)
    )
    try:
        day = date.fromisoformat(run_date)
        trading_days = quote.get_trading_days(
            market="CN",
            start=(day - timedelta(days=35)).isoformat(),
            end=(day + timedelta(days=1)).isoformat(),
        )
        reference = load_allocation_reference(
            config.data_dir,
            allocation_date=run_date,
            a_trading_days=trading_days,
            status_failure_reason=None,
        )
    finally:
        close = getattr(quote, "close", None)
        if callable(close):
            close()
    if not isinstance(reference, Mapping):
        raise RuntimeError("allocation reference is unavailable")
    if reference.get("stale_a_trading_days") not in (None, 0):
        raise RuntimeError("latest allocation reference is stale")
    allocation_date = _allocation_date(reference)
    if allocation_date != run_date:
        reference = load_allocation_reference(
            config.data_dir,
            allocation_date=allocation_date,
            a_trading_days=trading_days,
            status_failure_reason=None,
        )
        if not isinstance(reference, Mapping):
            raise RuntimeError("allocation reference is unavailable")
    return reference


def _allocation_date(reference: Mapping[str, object]) -> str:
    snapshot = reference.get("snapshot")
    value = snapshot.get("allocation_date") if isinstance(snapshot, Mapping) else None
    if not isinstance(value, str):
        raise ValueError("allocation reference has no date")
    date.fromisoformat(value)
    return value


def _copy_reports(source_root: Path, stage_root: Path) -> None:
    """Copy only the three trend report directories into the staging root."""
    for directory in REPORT_DIRECTORIES.values():
        source = source_root / directory
        target = stage_root / directory
        if source.is_dir():
            shutil.copytree(source, target)
        else:
            target.mkdir(parents=True, exist_ok=True)


def _with_reports_dir(config: object, reports_dir: Path) -> object:
    if dataclasses.is_dataclass(config):
        return dataclasses.replace(
            config, repo=_CHECKOUT_ROOT, reports_dir=reports_dir
        )
    copied = copy.copy(config)
    setattr(copied, "repo", _CHECKOUT_ROOT)
    setattr(copied, "reports_dir", reports_dir)
    return copied


def _inside(path: Path, root: Path) -> Path:
    try:
        return path.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError(f"report generator escaped staging root: {path}") from exc


def _read_json(path: Path) -> Mapping[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid staged trend report JSON: {path}") from exc
    if not isinstance(payload, Mapping):
        raise ValueError(f"invalid staged trend report contract: {path}")
    # Allocation-era reports carry the complete frozen contract.  Keep the
    # lightweight publisher fakes useful as well: identity and cost are the
    # fields this staging boundary owns, while a present allocation is checked
    # by the shared full validator.
    if "allocation" in payload and not valid_frozen_report_contract(payload):
        raise ValueError(f"invalid staged trend report contract: {path}")
    return payload


def _cost(payload: Mapping[str, object]) -> str:
    raw = payload.get("actual_api_cost")
    try:
        value = Decimal("0") if raw in (None, "") else Decimal(str(raw))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError("invalid staged trend report API cost") from exc
    if not value.is_finite() or value < 0:
        raise ValueError("invalid staged trend report API cost")
    return format(value, "f")


def _revision(path: Path) -> tuple[str, int]:
    match = _REPORT_STEM.fullmatch(path.stem)
    if match is None:
        raise ValueError(f"invalid trend report artifact stem: {path.name}")
    return match.group("date"), int(match.group("revision") or 0)


def _previous_pair(json_path: Path, markdown_path: Path) -> tuple[Path | None, Path | None]:
    report_date, current_revision = _revision(json_path)
    candidates: list[tuple[int, Path, Path]] = []
    for candidate in json_path.parent.glob(f"{report_date}*.json"):
        try:
            candidate_date, candidate_revision = _revision(candidate)
        except ValueError:
            continue
        candidate_markdown = candidate.with_suffix(".md")
        if (
            candidate_date == report_date
            and candidate_revision < current_revision
            and candidate_markdown.exists()
        ):
            candidates.append((candidate_revision, candidate, candidate_markdown))
    if not candidates:
        return None, None
    _, previous_json, previous_markdown = max(candidates, key=lambda item: item[0])
    return previous_json, previous_markdown


def _validate_artifact(
    *,
    market: str,
    result: object,
    stage_root: Path,
    before: Mapping[Path, bytes],
) -> dict[str, object]:
    if getattr(result, "status", None) not in {"generated", "existing"}:
        raise RuntimeError(
            f"{market} trend report generation returned {getattr(result, 'status', None)}"
        )
    json_path = getattr(result, "json_path", None)
    markdown_path = getattr(result, "report_path", None)
    if not isinstance(json_path, Path) or not isinstance(markdown_path, Path):
        raise ValueError(f"{market} report generator returned no immutable pair")
    _inside(json_path, stage_root)
    _inside(markdown_path, stage_root)
    if json_path.suffix != ".json" or markdown_path.suffix != ".md":
        raise ValueError(f"{market} report generator returned invalid artifact suffix")
    if json_path.with_suffix(".md") != markdown_path:
        raise ValueError(f"{market} report JSON/Markdown stems do not match")
    if not json_path.exists() or not markdown_path.exists():
        raise ValueError(f"{market} report generator did not create both artifacts")

    payload = _read_json(json_path)
    snapshot = payload.get("strategy_snapshot")
    if not isinstance(snapshot, Mapping):
        raise ValueError(f"{market} report has no strategy snapshot")
    if snapshot.get("strategy_version") != EXPECTED_VERSIONS[market]:
        raise ValueError(
            f"{market} report has unexpected strategy version: "
            f"{snapshot.get('strategy_version')}"
        )
    if (
        snapshot.get("market") != market
        or snapshot.get("strategy_id")
        != f"trend_animals_warm_to_hot/{market}/{EXPECTED_VERSIONS[market]}"
    ):
        raise ValueError(f"{market} report strategy identity is invalid")
    metadata = payload.get("metadata")
    if not isinstance(metadata, Mapping) or metadata.get("market") != market:
        raise ValueError(f"{market} report metadata market is invalid")

    # The copied revisions are immutable inputs.  A generator may only add a
    # fresh revision; it must never rewrite any prior report in the staging tree.
    for path, old_bytes in before.items():
        if not path.exists() or path.read_bytes() != old_bytes:
            raise ValueError(f"staged prior report changed: {path}")

    json_bytes = json_path.read_bytes()
    markdown_bytes = markdown_path.read_bytes()
    previous_json, previous_markdown = _previous_pair(json_path, markdown_path)
    return {
        "json_path": json_path,
        "markdown_path": markdown_path,
        "json_bytes": json_bytes,
        "markdown_bytes": markdown_bytes,
        "previous_json": previous_json,
        "previous_markdown": previous_markdown,
        "previous_json_bytes": (
            previous_json.read_bytes() if previous_json is not None else None
        ),
        "previous_markdown_bytes": (
            previous_markdown.read_bytes() if previous_markdown is not None else None
        ),
        "strategy_version": str(snapshot["strategy_version"]),
        "estimated_api_cost": _optional_cost(payload.get("estimated_api_cost")),
        "actual_api_cost": _cost(payload),
    }


def _optional_cost(raw: object) -> str | None:
    if raw in (None, ""):
        return None
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError("invalid staged trend report estimated API cost") from exc
    if not value.is_finite() or value < 0:
        raise ValueError("invalid staged trend report estimated API cost")
    return format(value, "f")


def _create_exclusive(path: Path, body: bytes) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        if path.read_bytes() != body:
            raise FileExistsError(f"immutable trend report collision: {path}") from None
        return False
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return True


def _manifest_record(
    *,
    market: str,
    artifact: Mapping[str, object],
    stage_root: Path,
) -> dict[str, object]:
    json_path = artifact["json_path"]
    markdown_path = artifact["markdown_path"]
    assert isinstance(json_path, Path) and isinstance(markdown_path, Path)
    previous_json = artifact.get("previous_json")
    previous_markdown = artifact.get("previous_markdown")
    previous_json_bytes = artifact.get("previous_json_bytes")
    previous_markdown_bytes = artifact.get("previous_markdown_bytes")
    assert previous_json is None or isinstance(previous_json, Path)
    assert previous_markdown is None or isinstance(previous_markdown, Path)
    assert previous_json_bytes is None or isinstance(previous_json_bytes, bytes)
    assert previous_markdown_bytes is None or isinstance(previous_markdown_bytes, bytes)
    return {
        "strategy_version": artifact["strategy_version"],
        "estimated_api_cost": artifact["estimated_api_cost"],
        "actual_api_cost": artifact["actual_api_cost"],
        "staged": {
            "json": str(json_path.relative_to(stage_root)),
            "markdown": str(markdown_path.relative_to(stage_root)),
        },
        "published": False,
        "old_sha256": {
            "json": _sha256(previous_json_bytes) if previous_json_bytes is not None else None,
            "markdown": _sha256(previous_markdown_bytes)
            if previous_markdown_bytes is not None
            else None,
        },
        "new_sha256": {
            "json": _sha256(artifact["json_bytes"]),
            "markdown": _sha256(artifact["markdown_bytes"]),
        },
        "previous": {
            "json": str(previous_json.relative_to(stage_root))
            if previous_json is not None
            else None,
            "markdown": str(previous_markdown.relative_to(stage_root))
            if previous_markdown is not None
            else None,
        },
    }


def stage_and_publish(config: DailyPremarketConfig, *, publish: bool = False) -> dict[str, object]:
    """Generate and validate CN/HK/US revisions, optionally publishing all six.

    ``publish=False`` is the default and never writes to ``config.reports_dir``.
    A generation or validation failure is raised before publication begins.
    """
    reports_root = Path(config.reports_dir)
    run_date = _run_date(config)
    allocation_reference = _allocation_reference(config, run_date)
    with tempfile.TemporaryDirectory(prefix="trend-report-stage-") as temporary:
        stage_root = Path(temporary) / "reports"
        _copy_reports(reports_root, stage_root)
        staged_config = _with_reports_dir(config, stage_root)
        artifacts: dict[str, dict[str, object]] = {}

        allocation_date = _allocation_date(allocation_reference)
        for market in ("CN", "HK", "US"):
            market_run_date = (
                (date.fromisoformat(allocation_date) + timedelta(days=1)).isoformat()
                if market == "US"
                else allocation_date
            )
            directory = stage_root / REPORT_DIRECTORIES[market]
            before = {
                path: path.read_bytes()
                for path in directory.rglob("*")
                if path.is_file()
            }
            notifier = NullNotifier()
            if market == "CN":
                result = run_a_share_trend_report(
                    config=staged_config,
                    run_date=market_run_date,
                    revision=True,
                    notifier=notifier,
                    allocation_reference=allocation_reference,
                )
            else:
                result = run_market_trend_report(
                    config=staged_config,
                    market=market,
                    run_date=market_run_date,
                    revision=True,
                    notifier=notifier,
                    allocation_reference=allocation_reference,
                )
            artifacts[market] = _validate_artifact(
                market=market,
                result=result,
                stage_root=stage_root,
                before=before,
            )

        manifest: dict[str, object] = {
            "status": "PASS",
            "run_date": allocation_date,
            "published": False,
            "submitted_orders": 0,
            "markets": {
                market: _manifest_record(
                    market=market,
                    artifact=artifact,
                    stage_root=stage_root,
                )
                for market, artifact in artifacts.items()
            },
        }

        if publish:
            destinations: list[tuple[Path, bytes]] = []
            for artifact in artifacts.values():
                for key in ("json_path", "markdown_path"):
                    source = artifact[key]
                    assert isinstance(source, Path)
                    destinations.append(
                        (reports_root / source.relative_to(stage_root), source.read_bytes())
                    )
            # Preflight every collision before creating the first destination.
            for destination, body in destinations:
                if destination.exists() and destination.read_bytes() != body:
                    raise FileExistsError(
                        f"immutable trend report collision: {destination}"
                    )
            created: list[Path] = []
            try:
                for destination, body in destinations:
                    if _create_exclusive(destination, body):
                        created.append(destination)
            except Exception:
                for destination in reversed(created):
                    destination.unlink(missing_ok=True)
                raise
            manifest["published"] = True
            for market, record in manifest["markets"].items():  # type: ignore[union-attr]
                assert isinstance(record, dict)
                record["published"] = True
        return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--publish", action="store_true")
    args = parser.parse_args(argv)
    try:
        config = load_env_config(args.config, dry_run=True)
        manifest = stage_and_publish(config, publish=args.publish)
    except Exception as exc:
        print(json.dumps({"status": "FAIL", "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
