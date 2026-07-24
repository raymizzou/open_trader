import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from open_trader.trend_industry_context import (
    IndustryContext,
    attach_prior_context,
    calculate_industry_context,
    load_latest_prior_context,
    write_industry_context_history,
)


def _member(
    tm_id: int,
    *,
    as_of_date: str = "2026-07-24",
    tradable: object = True,
    right_side: object = True,
) -> dict[str, object]:
    return {
        "tmId": tm_id,
        "asOfDate": as_of_date,
        "tradableFlag": tradable,
        "isTrendRightSide": right_side,
    }


def _industry(
    *,
    as_of_date: str = "2026-07-24",
    temperature: object = "热",
    strength: object = "88.5",
) -> dict[str, object]:
    return {
        "tmId": 700001,
        "asOfDate": as_of_date,
        "trendTemperatureCurr": temperature,
        "trendStrengthLocalCurr": strength,
    }


def test_calculation_deduplicates_components_and_member_snapshots() -> None:
    components = [*range(1, 11), 1, 2]
    rows = [_member(tm_id) for tm_id in range(1, 10)]
    rows.append(_member(10, right_side=False))
    rows.extend([_member(1), _member(2)])

    context = calculate_industry_context(
        industry_tm_id=700001,
        industry="工业",
        expected_date="2026-07-24",
        component_tm_ids=components,
        member_rows=rows,
        industry_row=_industry(),
        warm_to_hot_count=3,
    )

    assert context.component_count == 10
    assert context.snapshot_count == 10
    assert context.tradable_count == 10
    assert context.valid_count == 10
    assert context.right_count == 9
    assert context.snapshot_coverage == Decimal("1")
    assert context.right_state_coverage == Decimal("1")
    assert context.right_share == Decimal("0.9")
    assert context.warm_to_hot_count == 3
    assert context.temperature == "热"
    assert context.strength == Decimal("88.5")
    assert context.valid
    assert context.invalid_reasons == ()


def test_calculation_uses_only_exact_date_and_boolean_tradable_rows() -> None:
    components = list(range(1, 14))
    rows = [_member(tm_id) for tm_id in range(1, 11)]
    rows.extend(
        [
            _member(11, as_of_date="2026-07-23"),
            _member(12, as_of_date="2026-07-23"),
            _member(13, right_side="true"),
        ]
    )
    rows.append(_member(1, as_of_date="2026-07-23", right_side=False))

    context = calculate_industry_context(
        industry_tm_id=700001,
        industry="工业",
        expected_date="2026-07-24",
        component_tm_ids=components,
        member_rows=rows,
        industry_row=_industry(),
        warm_to_hot_count=0,
    )

    assert context.component_count == 13
    assert context.snapshot_count == 11
    assert context.tradable_count == 11
    assert context.valid_count == 10
    assert context.right_count == 10
    assert context.snapshot_coverage == Decimal("0.8461538461538461538461538462")
    assert context.right_state_coverage == Decimal("0.9090909090909090909090909091")
    assert context.right_share == Decimal("1")
    assert not context.valid
    assert context.invalid_reasons == ("snapshot_coverage_below_90pct",)


def test_calculation_records_stable_reasons_for_invalid_context_inputs() -> None:
    context = calculate_industry_context(
        industry_tm_id=700001,
        industry="工业",
        expected_date="2026-07-24",
        component_tm_ids=list(range(1, 10)),
        member_rows=[
            _member(1, as_of_date="2026-07-23"),
            _member(2, right_side="yes"),
        ],
        industry_row=_industry(as_of_date="2026-07-23", temperature="未知", strength="NaN"),
        warm_to_hot_count=0,
    )

    assert context.snapshot_count == 1
    assert context.tradable_count == 1
    assert context.valid_count == 0
    assert context.right_count == 0
    assert context.snapshot_coverage == Decimal("0.1111111111111111111111111111")
    assert context.right_state_coverage == Decimal("0")
    assert context.right_share is None
    assert context.temperature is None
    assert context.strength is None
    assert not context.valid
    assert context.invalid_reasons == (
        "component_count_below_10",
        "snapshot_coverage_below_90pct",
        "right_state_coverage_below_90pct",
        "valid_count_below_10",
        "industry_temperature_invalid",
        "industry_strength_invalid",
    )


@pytest.mark.parametrize("warm_to_hot_count", [True, -1, "3"])
def test_calculation_rejects_invalid_warm_to_hot_count(
    warm_to_hot_count: object,
) -> None:
    context = calculate_industry_context(
        industry_tm_id=700001,
        industry="工业",
        expected_date="2026-07-24",
        component_tm_ids=list(range(1, 11)),
        member_rows=[_member(tm_id) for tm_id in range(1, 11)],
        industry_row=_industry(),
        warm_to_hot_count=warm_to_hot_count,  # type: ignore[arg-type]
    )

    assert context.warm_to_hot_count == 0
    assert not context.valid
    assert context.invalid_reasons == ("warm_to_hot_count_invalid",)


def test_calculation_requires_matching_industry_state_row_id() -> None:
    industry_row = _industry()
    industry_row.pop("tmId")

    context = calculate_industry_context(
        industry_tm_id=700001,
        industry="工业",
        expected_date="2026-07-24",
        component_tm_ids=list(range(1, 11)),
        member_rows=[_member(tm_id) for tm_id in range(1, 11)],
        industry_row=industry_row,
        warm_to_hot_count=0,
    )

    assert context.temperature is None
    assert context.strength is None
    assert context.invalid_reasons == (
        "industry_temperature_invalid",
        "industry_strength_invalid",
    )


def _valid_context(
    industry_tm_id: int = 700001,
    *,
    as_of_date: str = "2026-07-24",
    temperature: str = "热",
    right_share: str = "0.279",
) -> IndustryContext:
    share = Decimal(right_share)
    return IndustryContext(
        industry_tm_id=industry_tm_id,
        industry="工业",
        as_of_date=as_of_date,
        component_count=1000,
        snapshot_count=1000,
        tradable_count=1000,
        valid_count=1000,
        right_count=int(share * Decimal("1000")),
        snapshot_coverage=Decimal("1"),
        right_state_coverage=Decimal("1"),
        right_share=share,
        warm_to_hot_count=5,
        temperature=temperature,
        strength=Decimal("90"),
        valid=True,
        invalid_reasons=(),
    )


def test_attach_prior_context_sets_direction_and_percentage_point_change() -> None:
    current = _valid_context()
    prior = _valid_context(
        as_of_date="2026-07-23", temperature="温", right_share="0.221"
    )

    [attached] = attach_prior_context((current,), {current.industry_tm_id: prior})

    assert attached.prior_as_of_date == "2026-07-23"
    assert attached.prior_temperature == "温"
    assert attached.prior_right_share == Decimal("0.221")
    assert attached.temperature_direction == "rising"
    assert attached.right_share_change_pp == Decimal("5.8")


@pytest.mark.parametrize(
    ("current_temperature", "prior_temperature", "expected"),
    [("热", "热", "unchanged"), ("温", "热", "falling")],
)
def test_attach_prior_context_sets_unchanged_or_falling_direction(
    current_temperature: str, prior_temperature: str, expected: str
) -> None:
    current = _valid_context(temperature=current_temperature)
    prior = _valid_context(
        as_of_date="2026-07-23", temperature=prior_temperature, right_share="0.221"
    )

    [attached] = attach_prior_context((current,), {current.industry_tm_id: prior})

    assert attached.temperature_direction == expected


def test_attach_prior_context_ignores_invalid_prior() -> None:
    current = _valid_context()
    invalid_prior = replace(
        _valid_context(as_of_date="2026-07-23"),
        valid=False,
        invalid_reasons=("snapshot_coverage_below_90pct",),
    )

    [attached] = attach_prior_context(
        (current,), {current.industry_tm_id: invalid_prior}
    )

    assert attached == current


def test_history_load_uses_latest_valid_date_strictly_before_requested_date(
    tmp_path: Path,
) -> None:
    old = _valid_context(as_of_date="2026-07-20")
    latest = _valid_context(as_of_date="2026-07-23", right_share="0.221")
    write_industry_context_history(
        tmp_path, market="CN", generated_at="2026-07-20T18:00:00+08:00", strategy_version="v8", contexts=(old,)
    )
    assert (tmp_path / "trend_industry_context" / "CN" / "2026-07-20.json").exists()
    write_industry_context_history(
        tmp_path, market="CN", generated_at="2026-07-23T18:00:00+08:00", strategy_version="v8", contexts=(latest,)
    )
    write_industry_context_history(
        tmp_path, market="CN", generated_at="2026-07-24T18:00:00+08:00", strategy_version="v8", contexts=(_valid_context(),)
    )

    loaded = load_latest_prior_context(
        tmp_path, market="CN", before_date="2026-07-24"
    )

    assert loaded[700001].as_of_date == "2026-07-23"
    assert loaded[700001].right_share == Decimal("0.221")


def test_history_loader_skips_invalid_file_and_duplicate_industry_rows(
    tmp_path: Path,
) -> None:
    valid = _valid_context(as_of_date="2026-07-22")
    path = write_industry_context_history(
        tmp_path, market="CN", generated_at="2026-07-22T18:00:00+08:00", strategy_version="v8", contexts=(valid,)
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["industries"].append(payload["industries"][0])
    path.write_text(json.dumps(payload), encoding="utf-8")

    assert load_latest_prior_context(
        tmp_path, market="CN", before_date="2026-07-24"
    ) == {}


@pytest.mark.parametrize("field", ["generated_at", "strategy_version"])
def test_history_loader_requires_top_level_metadata(
    tmp_path: Path, field: str
) -> None:
    path = write_industry_context_history(
        tmp_path,
        market="CN",
        generated_at="2026-07-22T18:00:00+08:00",
        strategy_version="v8",
        contexts=(_valid_context(as_of_date="2026-07-22"),),
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.pop(field)
    path.write_text(json.dumps(payload), encoding="utf-8")

    assert load_latest_prior_context(
        tmp_path, market="CN", before_date="2026-07-24"
    ) == {}


def test_history_loader_skips_semantically_invalid_stored_valid_context(
    tmp_path: Path,
) -> None:
    # This mutation is intentionally performed in-memory to keep the test at
    # the history loader seam while proving stored `valid=True` is not trusted.
    valid = _valid_context(industry_tm_id=700002, as_of_date="2026-07-22")
    invalid = _valid_context(industry_tm_id=700001, as_of_date="2026-07-22")
    path = write_industry_context_history(
        tmp_path,
        market="CN",
        generated_at="2026-07-22T18:00:00+08:00",
        strategy_version="v8",
        contexts=(invalid, valid),
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    next(
        row for row in payload["industries"] if row["industry_tm_id"] == 700001
    )["component_count"] = 9
    path.write_text(json.dumps(payload), encoding="utf-8")

    loaded = load_latest_prior_context(
        tmp_path, market="CN", before_date="2026-07-24"
    )

    assert set(loaded) == {700002}


def test_history_write_is_idempotent_for_same_context_and_rejects_conflicts(
    tmp_path: Path,
) -> None:
    context = _valid_context(as_of_date="2026-07-24")
    path = write_industry_context_history(
        tmp_path,
        market="CN",
        generated_at="2026-07-24T18:00:00+08:00",
        strategy_version="v8",
        contexts=(context,),
    )
    original = path.read_bytes()
    assert write_industry_context_history(
        tmp_path,
        market="CN",
        generated_at="2026-07-24T19:00:00+08:00",
        strategy_version="v8",
        contexts=(context,),
    ) == path
    assert path.read_bytes() == original
    with pytest.raises(ValueError, match="conflicting same-date"):
        write_industry_context_history(
            tmp_path,
            market="CN",
            generated_at="2026-07-24T20:00:00+08:00",
            strategy_version="v8",
            contexts=(replace(context, right_count=999),),
        )
