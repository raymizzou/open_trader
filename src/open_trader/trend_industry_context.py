from __future__ import annotations

import json
import re
from datetime import date, datetime
from dataclasses import dataclass, fields, replace
from decimal import Decimal, InvalidOperation
from pathlib import Path
from tempfile import NamedTemporaryFile

from collections.abc import Mapping, Sequence


KNOWN_TEMPERATURES = ("冻", "寒", "凉", "平", "温", "热", "沸")


@dataclass(frozen=True)
class IndustryContext:
    industry_tm_id: int
    industry: str
    as_of_date: str
    component_count: int
    snapshot_count: int
    tradable_count: int
    valid_count: int
    right_count: int
    snapshot_coverage: Decimal
    right_state_coverage: Decimal
    right_share: Decimal | None
    warm_to_hot_count: int
    temperature: str | None
    strength: Decimal | None
    valid: bool
    invalid_reasons: tuple[str, ...]
    member_breadth_collected: bool = True
    aggregate_right_count_ratio: Decimal | None = None
    aggregate_right_market_cap_ratio: Decimal | None = None
    prior_as_of_date: str | None = None
    prior_temperature: str | None = None
    prior_right_share: Decimal | None = None
    prior_aggregate_right_count_ratio: Decimal | None = None
    prior_aggregate_right_market_cap_ratio: Decimal | None = None
    temperature_direction: str | None = None
    right_share_change_pp: Decimal | None = None


def calculate_industry_context(
    *,
    industry_tm_id: int,
    industry: str,
    expected_date: str,
    component_tm_ids: Sequence[int],
    member_rows: Sequence[Mapping[str, object]],
    industry_row: Mapping[str, object] | None,
    warm_to_hot_count: int,
    member_breadth_collected: bool = True,
    require_strength: bool = True,
) -> IndustryContext:
    normalized_warm_to_hot_count = _nonnegative_int(warm_to_hot_count)
    component_ids = {
        value for value in (_positive_int(item) for item in component_tm_ids) if value
    }
    members: dict[int, Mapping[str, object]] = {}
    for row in member_rows:
        if not isinstance(row, Mapping):
            continue
        tm_id = _positive_int(_row_value(row, "tmId", "tm_id"))
        if tm_id is None or tm_id not in component_ids or tm_id in members:
            continue
        if _row_value(row, "asOfDate", "as_of_date") != expected_date:
            continue
        members[tm_id] = row

    snapshot_count = len(members)
    tradable_rows = [
        row for row in members.values() if _row_value(row, "tradableFlag", "tradable") is True
    ]
    valid_rows = [
        row
        for row in tradable_rows
        if isinstance(_row_value(row, "isTrendRightSide", "right_side"), bool)
    ]
    right_count = sum(
        _row_value(row, "isTrendRightSide", "right_side") is True
        for row in valid_rows
    )
    component_count = len(component_ids)
    tradable_count = len(tradable_rows)
    valid_count = len(valid_rows)
    snapshot_coverage = (
        Decimal(snapshot_count) / Decimal(component_count)
        if component_count
        else Decimal("0")
    )
    right_state_coverage = (
        Decimal(valid_count) / Decimal(tradable_count)
        if tradable_count
        else Decimal("0")
    )
    right_share = (
        Decimal(right_count) / Decimal(valid_count) if valid_count else None
    )

    temperature: str | None = None
    strength: Decimal | None = None
    aggregate_right_count_ratio: Decimal | None = None
    aggregate_right_market_cap_ratio: Decimal | None = None
    if isinstance(industry_row, Mapping) and _industry_row_matches(
        industry_row, industry_tm_id, expected_date
    ):
        raw_temperature = _row_value(
            industry_row, "trendTemperatureCurr", "temperature"
        )
        if raw_temperature in KNOWN_TEMPERATURES:
            temperature = str(raw_temperature)
        strength = _valid_strength(
            _row_value(
                industry_row,
                "trendStrengthLocalCurr",
                "strength",
            )
        )
        aggregate_right_count_ratio = _valid_ratio(
            _row_value(
                industry_row,
                "TrendRightSideCountRatio",
                "trendRightSideCountRatio",
            )
        )
        aggregate_right_market_cap_ratio = _valid_ratio(
            _row_value(
                industry_row,
                "TrendRightSideMktCapRatio",
                "trendRightSideMktCapRatio",
            )
        )

    invalid_reasons: list[str] = []
    if member_breadth_collected:
        if snapshot_coverage < Decimal("0.9"):
            invalid_reasons.append("snapshot_coverage_below_90pct")
        if right_state_coverage < Decimal("0.9"):
            invalid_reasons.append("right_state_coverage_below_90pct")
    if normalized_warm_to_hot_count is None:
        invalid_reasons.append("warm_to_hot_count_invalid")
    if temperature is None:
        invalid_reasons.append("industry_temperature_invalid")
    if require_strength and strength is None:
        invalid_reasons.append("industry_strength_invalid")

    return IndustryContext(
        industry_tm_id=industry_tm_id,
        industry=industry,
        as_of_date=expected_date,
        component_count=component_count,
        snapshot_count=snapshot_count,
        tradable_count=tradable_count,
        valid_count=valid_count,
        right_count=right_count,
        snapshot_coverage=snapshot_coverage,
        right_state_coverage=right_state_coverage,
        right_share=right_share,
        warm_to_hot_count=normalized_warm_to_hot_count or 0,
        temperature=temperature,
        strength=strength,
        valid=not invalid_reasons,
        invalid_reasons=tuple(invalid_reasons),
        member_breadth_collected=member_breadth_collected,
        aggregate_right_count_ratio=aggregate_right_count_ratio,
        aggregate_right_market_cap_ratio=aggregate_right_market_cap_ratio,
    )


def _row_value(row: Mapping[str, object], *keys: str) -> object:
    for key in keys:
        if key in row:
            return row[key]
    return None


def _positive_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _nonnegative_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _industry_row_matches(
    row: Mapping[str, object], industry_tm_id: int, expected_date: str
) -> bool:
    return (
        _row_value(row, "asOfDate", "as_of_date") == expected_date
        and ("tmId" in row or "tm_id" in row)
        and _positive_int(_row_value(row, "tmId", "tm_id")) == industry_tm_id
    )


def _valid_strength(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(str(value).strip())
    except (InvalidOperation, ValueError, AttributeError):
        return None
    return (
        parsed
        if parsed.is_finite() and Decimal("0") <= parsed <= Decimal("100")
        else None
    )


def _valid_ratio(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(str(value).strip())
    except (InvalidOperation, ValueError, AttributeError):
        return None
    return (
        parsed
        if parsed.is_finite() and Decimal("0") <= parsed <= Decimal("1")
        else None
    )


def attach_prior_context(
    contexts: Sequence[IndustryContext],
    prior_by_industry: Mapping[int, IndustryContext],
) -> tuple[IndustryContext, ...]:
    attached: list[IndustryContext] = []
    temperature_order = {value: index for index, value in enumerate(KNOWN_TEMPERATURES)}
    for context in contexts:
        prior = prior_by_industry.get(context.industry_tm_id)
        if (
            not context.valid
            or bool(context.invalid_reasons)
            or prior is None
            or not prior.valid
            or bool(prior.invalid_reasons)
        ):
            attached.append(context)
            continue
        try:
            if _parse_iso_date(prior.as_of_date) >= _parse_iso_date(context.as_of_date):
                attached.append(context)
                continue
        except ValueError:
            attached.append(context)
            continue
        changes: dict[str, object] = {
            "prior_as_of_date": prior.as_of_date,
            "prior_aggregate_right_count_ratio": prior.aggregate_right_count_ratio,
            "prior_aggregate_right_market_cap_ratio": prior.aggregate_right_market_cap_ratio,
        }
        if prior.temperature in temperature_order and context.temperature in temperature_order:
            current_temperature = temperature_order[context.temperature]
            prior_temperature = temperature_order[prior.temperature]
            direction = (
                "rising"
                if current_temperature > prior_temperature
                else "falling"
                if current_temperature < prior_temperature
                else "unchanged"
            )
            changes.update(
                prior_temperature=prior.temperature,
                temperature_direction=direction,
            )
        if prior.right_share is not None and context.right_share is not None:
            changes.update(
                prior_right_share=prior.right_share,
                right_share_change_pp=(context.right_share - prior.right_share)
                * Decimal("100"),
            )
        attached.append(
            replace(context, **changes)
        )
    return tuple(attached)


_HISTORY_SCHEMA_VERSION = "open_trader.trend_industry_context.v1"
_HISTORY_DATE_PATTERN = re.compile(
    r"^(\d{4}-\d{2}-\d{2})(?:-r([1-9]\d*))?\.json$"
)


def load_latest_prior_context(
    history_root: Path,
    *,
    market: str,
    before_date: str,
) -> dict[int, IndustryContext]:
    before = _parse_iso_date(before_date)
    market_name = str(market).upper()
    directory = _history_directory(history_root, market_name, for_write=False)
    latest: dict[int, IndustryContext] = {}
    latest_dates: dict[int, str] = {}
    revisions_by_date: dict[str, list[tuple[int, Path]]] = {}
    try:
        paths = sorted(directory.iterdir())
    except OSError:
        return {}
    for path in paths:
        match = _HISTORY_DATE_PATTERN.fullmatch(path.name)
        if match is None:
            continue
        stored_date = match.group(1)
        try:
            if _parse_iso_date(stored_date) >= before:
                continue
        except ValueError:
            continue
        revision = int(match.group(2) or 0)
        revisions_by_date.setdefault(stored_date, []).append((revision, path))
    for stored_date, revisions in sorted(revisions_by_date.items()):
        contexts: list[IndustryContext] | None = None
        for _revision, path in sorted(revisions, reverse=True):
            contexts = _contexts_from_history_payload(
                _read_history_payload(path),
                market=market_name,
                stored_date=stored_date,
            )
            if contexts is not None:
                break
        if contexts is None:
            continue
        for context in contexts:
            if not context.valid:
                continue
            if context.industry_tm_id not in latest_dates or stored_date > latest_dates[
                context.industry_tm_id
            ]:
                latest[context.industry_tm_id] = context
                latest_dates[context.industry_tm_id] = stored_date
    return latest


def write_industry_context_history(
    history_root: Path,
    *,
    market: str,
    generated_at: str,
    strategy_version: str,
    contexts: Sequence[IndustryContext],
    revision: int = 0,
) -> Path:
    market_name = str(market).upper()
    if not _valid_history_metadata(generated_at, strategy_version):
        raise ValueError("industry context history metadata is invalid")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        raise ValueError("industry context history revision is invalid")
    context_rows = list(contexts)
    if any(not isinstance(context, IndustryContext) for context in context_rows):
        raise ValueError("industry context history rows must be IndustryContext objects")
    dates = {context.as_of_date for context in context_rows}
    if len(dates) > 1:
        raise ValueError("industry context history rows must share one as-of date")
    as_of_date = next(iter(dates), str(generated_at)[:10])
    _parse_iso_date(as_of_date)
    ids = [context.industry_tm_id for context in context_rows]
    if any(_positive_int(value) is None for value in ids) or len(ids) != len(set(ids)):
        raise ValueError("industry context history contains duplicate or invalid IDs")
    directory = _history_directory(history_root, market_name, for_write=True)
    revision_suffix = f"-r{revision}" if revision else ""
    path = directory / f"{as_of_date}{revision_suffix}.json"
    payload = {
        "schema_version": _HISTORY_SCHEMA_VERSION,
        "market": market_name,
        "as_of_date": as_of_date,
        "generated_at": generated_at,
        "strategy_version": strategy_version,
        "industries": [
            _context_to_mapping(context)
            for context in sorted(context_rows, key=lambda item: item.industry_tm_id)
        ],
    }
    content = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    directory.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            raise ValueError("conflicting same-date industry context history") from None
        same_history_identity = (
            isinstance(existing, Mapping)
            and existing.get("schema_version") == _HISTORY_SCHEMA_VERSION
            and existing.get("market") == market_name
            and existing.get("as_of_date") == as_of_date
        )
        existing_industries = (
            existing.get("industries") if isinstance(existing, Mapping) else None
        )
        if same_history_identity and existing_industries == payload["industries"]:
            return path
        aggregate_fields = {
            "aggregate_right_count_ratio",
            "aggregate_right_market_cap_ratio",
            "prior_aggregate_right_count_ratio",
            "prior_aggregate_right_market_cap_ratio",
        }
        flag_legacy_rows = [
            {
                key: value
                for key, value in row.items()
                if key != "member_breadth_collected"
            }
            for row in payload["industries"]
        ]
        if same_history_identity and existing_industries == flag_legacy_rows:
            return path
        missing_member_flag = (
            isinstance(existing_industries, list)
            and all(
                isinstance(row, Mapping)
                and "member_breadth_collected" not in row
                for row in existing_industries
            )
        )
        legacy_optional_fields = aggregate_fields | (
            {"member_breadth_collected"} if missing_member_flag else set()
        )
        legacy_rows = (
            [
                {
                    key: value
                    for key, value in row.items()
                    if key not in legacy_optional_fields
                }
                for row in payload["industries"]
            ]
            if isinstance(existing_industries, list)
            and all(
                isinstance(row, Mapping)
                and aggregate_fields.isdisjoint(row)
                for row in existing_industries
            )
            else None
        )
        if not (
            same_history_identity and existing_industries == legacy_rows
        ):
            raise ValueError("conflicting same-date industry context history")
    temp_path: Path | None = None
    try:
        with NamedTemporaryFile(
            "w", encoding="utf-8", delete=False, dir=directory
        ) as handle:
            handle.write(content)
            temp_path = Path(handle.name)
        temp_path.replace(path)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
    return path


def _history_directory(history_root: Path, market: str, *, for_write: bool) -> Path:
    if history_root.name == "trend_industry_context":
        return history_root / market
    nested = history_root / "trend_industry_context" / market
    direct = history_root / market
    if not for_write and direct.is_dir() and not nested.is_dir():
        return direct
    return nested


def _parse_iso_date(value: object) -> date:
    if not isinstance(value, str):
        raise ValueError("history date must be a string")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ValueError(f"invalid history date: {value!r}") from None


def _valid_history_metadata(generated_at: object, strategy_version: object) -> bool:
    if (
        not isinstance(generated_at, str)
        or not generated_at.strip()
        or not isinstance(strategy_version, str)
        or not strategy_version.strip()
    ):
        return False
    try:
        datetime.fromisoformat(generated_at)
    except ValueError:
        return False
    return True


def _context_to_mapping(context: IndustryContext) -> dict[str, object]:
    return {
        field.name: _json_value(getattr(context, field.name))
        for field in fields(context)
    }


def _json_value(value: object) -> object:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    return value


def _read_history_payload(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None


def _contexts_from_history_payload(
    payload: object, *, market: str, stored_date: str
) -> list[IndustryContext] | None:
    if not isinstance(payload, Mapping):
        return None
    if (
        payload.get("schema_version") != _HISTORY_SCHEMA_VERSION
        or payload.get("market") != market
        or payload.get("as_of_date") != stored_date
        or not _valid_history_metadata(
            payload.get("generated_at"), payload.get("strategy_version")
        )
        or not isinstance(payload.get("industries"), list)
    ):
        return None
    contexts: list[IndustryContext] = []
    seen_ids: set[int] = set()
    for row in payload["industries"]:
        if not isinstance(row, Mapping):
            return None
        context = _context_from_mapping(row)
        if context is None or context.as_of_date != stored_date:
            return None
        if context.industry_tm_id in seen_ids:
            return None
        seen_ids.add(context.industry_tm_id)
        if _context_is_valid_for_history(context):
            contexts.append(context)
    return contexts


def _context_from_mapping(row: Mapping[str, object]) -> IndustryContext | None:
    aggregate_fields = {
        "aggregate_right_count_ratio",
        "aggregate_right_market_cap_ratio",
        "prior_aggregate_right_count_ratio",
        "prior_aggregate_right_market_cap_ratio",
    }
    optional_fields = aggregate_fields | {"member_breadth_collected"}
    if any(field.name not in row for field in fields(IndustryContext) if field.name not in optional_fields):
        return None
    member_breadth_collected = row.get("member_breadth_collected", True)
    if type(member_breadth_collected) is not bool:
        return None
    industry_tm_id = _positive_int(row.get("industry_tm_id"))
    if industry_tm_id is None or not isinstance(row.get("industry"), str):
        return None
    as_of_date = row.get("as_of_date")
    if not isinstance(as_of_date, str):
        return None
    try:
        _parse_iso_date(as_of_date)
    except ValueError:
        return None
    integer_values: dict[str, int] = {}
    for field in (
        "component_count",
        "snapshot_count",
        "tradable_count",
        "valid_count",
        "right_count",
        "warm_to_hot_count",
    ):
        value = row.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None
        integer_values[field] = value
    if not isinstance(row.get("valid"), bool):
        return None
    reasons = row.get("invalid_reasons")
    if not isinstance(reasons, list) or any(not isinstance(item, str) for item in reasons):
        return None
    decimals: dict[str, Decimal] = {}
    for field in ("snapshot_coverage", "right_state_coverage"):
        parsed = _history_decimal(row.get(field))
        if parsed is None:
            return None
        decimals[field] = parsed
    optional_decimals: dict[str, Decimal | None] = {}
    for field in (
        "right_share",
        "strength",
        "prior_right_share",
        "right_share_change_pp",
    ):
        parsed = _history_optional_decimal(row.get(field))
        if row.get(field) is not None and parsed is None:
            return None
        optional_decimals[field] = parsed
    aggregate_decimals = {
        field: _valid_ratio(row.get(field)) for field in aggregate_fields
    }
    temperature = row.get("temperature")
    prior_temperature = row.get("prior_temperature")
    temperature_direction = row.get("temperature_direction")
    prior_as_of_date = row.get("prior_as_of_date")
    for value in (temperature, prior_temperature, temperature_direction, prior_as_of_date):
        if value is not None and not isinstance(value, str):
            return None
    if prior_as_of_date is not None:
        try:
            _parse_iso_date(prior_as_of_date)
        except ValueError:
            return None
    strength = optional_decimals["strength"]
    return IndustryContext(
        industry_tm_id=industry_tm_id,
        industry=str(row["industry"]),
        as_of_date=as_of_date,
        **integer_values,
        snapshot_coverage=decimals["snapshot_coverage"],
        right_state_coverage=decimals["right_state_coverage"],
        right_share=optional_decimals["right_share"],
        temperature=temperature,
        strength=strength,
        valid=row["valid"],
        invalid_reasons=tuple(reasons),
        member_breadth_collected=member_breadth_collected,
        aggregate_right_count_ratio=aggregate_decimals[
            "aggregate_right_count_ratio"
        ],
        aggregate_right_market_cap_ratio=aggregate_decimals[
            "aggregate_right_market_cap_ratio"
        ],
        prior_as_of_date=prior_as_of_date,
        prior_temperature=prior_temperature,
        prior_right_share=optional_decimals["prior_right_share"],
        prior_aggregate_right_count_ratio=aggregate_decimals[
            "prior_aggregate_right_count_ratio"
        ],
        prior_aggregate_right_market_cap_ratio=aggregate_decimals[
            "prior_aggregate_right_market_cap_ratio"
        ],
        temperature_direction=temperature_direction,
        right_share_change_pp=optional_decimals["right_share_change_pp"],
    )


def _context_is_valid_for_history(context: IndustryContext) -> bool:
    if not context.valid or context.invalid_reasons:
        return False
    if context.member_breadth_collected:
        if context.component_count < 10:
            return False
        if not (
            0 <= context.snapshot_count <= context.component_count
            and context.snapshot_coverage >= Decimal("0.9")
            and context.snapshot_coverage
            == Decimal(context.snapshot_count) / Decimal(context.component_count)
        ):
            return False
        if not (
            0 <= context.tradable_count <= context.snapshot_count
            and 0 <= context.valid_count <= context.tradable_count
            and context.valid_count >= 10
            and context.right_state_coverage >= Decimal("0.9")
            and context.right_state_coverage
            == Decimal(context.valid_count) / Decimal(context.tradable_count)
        ):
            return False
        if not (
            0 <= context.right_count <= context.valid_count
            and context.right_share is not None
            and 0 <= context.right_share <= 1
            and context.right_share
            == Decimal(context.right_count) / Decimal(context.valid_count)
        ):
            return False
    elif not (
        context.component_count
        == context.snapshot_count
        == context.tradable_count
        == context.valid_count
        == context.right_count
        == 0
        and context.snapshot_coverage == Decimal("0")
        and context.right_state_coverage == Decimal("0")
        and context.right_share is None
    ):
        return False
    return (
        _nonnegative_int(context.warm_to_hot_count) is not None
        and context.temperature in KNOWN_TEMPERATURES
        and _valid_strength(context.strength) is not None
    )


def _history_decimal(value: object) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(str(value).strip())
    except (InvalidOperation, ValueError, AttributeError):
        return None
    return parsed if parsed.is_finite() else None


def _history_optional_decimal(value: object) -> Decimal | None:
    return None if value is None else _history_decimal(value)
