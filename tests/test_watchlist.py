import csv
from pathlib import Path

import pytest

from open_trader.watchlist import ParsedTrigger, build_watchlist, parse_watch_trigger


ACTION_FIELDNAMES = [
    "run_date",
    "symbol",
    "market",
    "portfolio_weight_hkd",
    "severity",
    "change_type",
    "suggested_action",
    "summary",
    "rationale",
    "watch_trigger",
]


def write_actions(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=ACTION_FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.mark.parametrize(
    ("text", "trigger_type", "operator", "price"),
    [
        ("below 95", "price", "<=", "95"),
        ("under 95.50", "price", "<=", "95.50"),
        ("breaks below 95", "price", "<=", "95"),
        ("<= 95", "price", "<=", "95"),
        ("above 110", "price", ">=", "110"),
        ("over 110.25", "price", ">=", "110.25"),
        ("breaks above 110", "price", ">=", "110"),
        (">= 110", "price", ">=", "110"),
        ("open below 95", "open_price", "<=", "95"),
        ("open above 110", "open_price", ">=", "110"),
    ],
)
def test_parse_watch_trigger_returns_monitorable_price_trigger(
    text: str,
    trigger_type: str,
    operator: str,
    price: str,
) -> None:
    assert parse_watch_trigger(text) == ParsedTrigger(
        trigger_type=trigger_type,
        operator=operator,
        trigger_price=price,
        trigger_text=text,
        status="active",
        error="",
    )


def test_parse_watch_trigger_marks_empty_trigger_as_no_trigger() -> None:
    assert parse_watch_trigger("") == ParsedTrigger(
        trigger_type="none",
        operator="",
        trigger_price="",
        trigger_text="",
        status="no_trigger",
        error="",
    )


def test_parse_watch_trigger_marks_unclear_text_as_manual_review() -> None:
    text = "watch if support fails"

    assert parse_watch_trigger(text) == ParsedTrigger(
        trigger_type="manual_review",
        operator="",
        trigger_price="",
        trigger_text=text,
        status="manual_review",
        error="",
    )


@pytest.mark.parametrize(
    "text",
    [
        "not below 95",
        "do not buy below 95",
        "below 95 or above 110",
        "above 110 below 95",
        "below 95abc",
    ],
)
def test_parse_watch_trigger_marks_ambiguous_comparisons_as_manual_review(
    text: str,
) -> None:
    assert parse_watch_trigger(text) == ParsedTrigger(
        trigger_type="manual_review",
        operator="",
        trigger_price="",
        trigger_text=text,
        status="manual_review",
        error="",
    )


def test_build_watchlist_writes_run_and_latest_outputs(tmp_path: Path) -> None:
    actions_path = tmp_path / "data/latest/premarket_actions.csv"
    write_actions(
        actions_path,
        [
            {
                "run_date": "2026-06-16",
                "symbol": "VIXY",
                "market": "US",
                "portfolio_weight_hkd": "3.05%",
                "severity": "high",
                "change_type": "action_changed",
                "suggested_action": "reduce",
                "summary": "VIXY changed",
                "rationale": "Fake rationale",
                "watch_trigger": "below 95",
            },
            {
                "run_date": "2026-06-16",
                "symbol": "QQQ",
                "market": "US",
                "portfolio_weight_hkd": "1.40%",
                "severity": "medium",
                "change_type": "new_signal",
                "suggested_action": "watch",
                "summary": "QQQ changed",
                "rationale": "Fake rationale",
                "watch_trigger": "support fails",
            },
        ],
    )

    result = build_watchlist(actions_path, tmp_path / "data")

    assert result.run_date == "2026-06-16"
    assert result.watchlist_count == 2
    assert result.watchlist_path == tmp_path / "data/runs/2026-06-16/watchlist.csv"
    assert result.latest_path == tmp_path / "data/latest/watchlist.csv"
    assert result.watchlist_path.exists()
    assert result.latest_path.exists()

    rows = list(csv.DictReader(result.watchlist_path.open(encoding="utf-8")))
    assert rows[0]["symbol"] == "VIXY"
    assert rows[0]["trigger_type"] == "price"
    assert rows[0]["operator"] == "<="
    assert rows[0]["trigger_price"] == "95"
    assert rows[0]["status"] == "active"
    assert rows[1]["symbol"] == "QQQ"
    assert rows[1]["trigger_type"] == "manual_review"
    assert rows[1]["status"] == "manual_review"


def test_build_watchlist_accepts_positional_run_date_and_update_latest(
    tmp_path: Path,
) -> None:
    actions_path = tmp_path / "data/latest/premarket_actions.csv"
    write_actions(
        actions_path,
        [
            {
                "run_date": "2026-06-16",
                "symbol": "VIXY",
                "market": "US",
                "portfolio_weight_hkd": "3.05%",
                "severity": "high",
                "change_type": "action_changed",
                "suggested_action": "reduce",
                "summary": "VIXY changed",
                "rationale": "Fake rationale",
                "watch_trigger": "below 95",
            },
        ],
    )

    result = build_watchlist(actions_path, tmp_path / "data", None, True)

    assert result.run_date == "2026-06-16"
    assert result.latest_path.exists()


def test_build_watchlist_omitted_run_date_filters_to_latest_actions(
    tmp_path: Path,
) -> None:
    actions_path = tmp_path / "data/latest/premarket_actions.csv"
    write_actions(
        actions_path,
        [
            {
                "run_date": "2026-06-15",
                "symbol": "OLD",
                "market": "US",
                "portfolio_weight_hkd": "1.00%",
                "severity": "medium",
                "change_type": "new_signal",
                "suggested_action": "watch",
                "summary": "Old signal",
                "rationale": "Fake rationale",
                "watch_trigger": "below 10",
            },
            {
                "run_date": "2026-06-16",
                "symbol": "NEW",
                "market": "US",
                "portfolio_weight_hkd": "2.00%",
                "severity": "high",
                "change_type": "action_changed",
                "suggested_action": "reduce",
                "summary": "New signal",
                "rationale": "Fake rationale",
                "watch_trigger": "below 20",
            },
        ],
    )

    result = build_watchlist(actions_path, tmp_path / "data")

    rows = list(csv.DictReader(result.watchlist_path.open(encoding="utf-8")))
    assert result.run_date == "2026-06-16"
    assert result.watchlist_count == 1
    assert [row["symbol"] for row in rows] == ["NEW"]
    assert (
        result.latest_path.read_text(encoding="utf-8")
        == result.watchlist_path.read_text(encoding="utf-8")
    )


def test_build_watchlist_explicit_run_date_filters_matching_and_blank_rows(
    tmp_path: Path,
) -> None:
    actions_path = tmp_path / "data/latest/premarket_actions.csv"
    write_text(
        actions_path,
        ",".join(ACTION_FIELDNAMES)
        + "\n"
        + "2026-06-15,OLD,US,1.00%,medium,new_signal,watch,Old signal,"
        + "Fake rationale,below 10\n"
        + "2026-06-16,NEW,US,2.00%,high,action_changed,reduce,New signal,"
        + "Fake rationale,below 20\n"
        + ",FALLBACK,US,3.00%,low,new_signal,watch,Fallback signal,"
        + "Fake rationale,below 30\n",
    )

    result = build_watchlist(
        actions_path,
        tmp_path / "data",
        run_date="2026-06-15",
        update_latest=False,
    )

    rows = list(csv.DictReader(result.watchlist_path.open(encoding="utf-8")))
    assert result.watchlist_count == 2
    assert [row["symbol"] for row in rows] == ["OLD", "FALLBACK"]
    assert {row["run_date"] for row in rows} == {"2026-06-15"}


def test_build_watchlist_reads_bom_prefixed_actions_header(tmp_path: Path) -> None:
    actions_path = tmp_path / "data/latest/premarket_actions.csv"
    write_text(
        actions_path,
        "\ufeff"
        + ",".join(ACTION_FIELDNAMES)
        + "\n"
        + "2026-06-16,VIXY,US,3.05%,high,action_changed,reduce,VIXY changed,"
        + "Fake rationale,below 95\n",
    )

    result = build_watchlist(actions_path, tmp_path / "data")

    assert result.watchlist_count == 1


def test_build_watchlist_dry_run_does_not_update_latest(tmp_path: Path) -> None:
    actions_path = tmp_path / "data/latest/premarket_actions.csv"
    latest_path = tmp_path / "data/latest/watchlist.csv"
    latest_path.parent.mkdir(parents=True, exist_ok=True)
    latest_path.write_text("existing\n", encoding="utf-8")
    write_actions(
        actions_path,
        [
            {
                "run_date": "2026-06-16",
                "symbol": "VIXY",
                "market": "US",
                "portfolio_weight_hkd": "3.05%",
                "severity": "high",
                "change_type": "action_changed",
                "suggested_action": "reduce",
                "summary": "VIXY changed",
                "rationale": "Fake rationale",
                "watch_trigger": "below 95",
            },
        ],
    )

    result = build_watchlist(
        actions_path=actions_path,
        data_dir=tmp_path / "data",
        run_date=None,
        update_latest=False,
    )

    assert result.watchlist_path.exists()
    assert latest_path.read_text(encoding="utf-8") == "existing\n"


def test_build_watchlist_writes_empty_headers_when_no_actions(tmp_path: Path) -> None:
    actions_path = tmp_path / "data/latest/premarket_actions.csv"
    write_actions(actions_path, [])

    result = build_watchlist(
        actions_path=actions_path,
        data_dir=tmp_path / "data",
        run_date="2026-06-16",
        update_latest=True,
    )

    rows = list(csv.DictReader(result.watchlist_path.open(encoding="utf-8")))
    assert rows == []
    assert result.watchlist_count == 0


def test_build_watchlist_unmatched_explicit_date_preserves_latest(
    tmp_path: Path,
) -> None:
    actions_path = tmp_path / "data/latest/premarket_actions.csv"
    latest_path = tmp_path / "data/latest/watchlist.csv"
    latest_path.parent.mkdir(parents=True, exist_ok=True)
    latest_path.write_text("existing\n", encoding="utf-8")
    write_actions(
        actions_path,
        [
            {
                "run_date": "2026-06-16",
                "symbol": "VIXY",
                "market": "US",
                "portfolio_weight_hkd": "3.05%",
                "severity": "high",
                "change_type": "action_changed",
                "suggested_action": "reduce",
                "summary": "VIXY changed",
                "rationale": "Fake rationale",
                "watch_trigger": "below 95",
            },
        ],
    )

    with pytest.raises(ValueError, match="no action rows match run_date 2026-06-15"):
        build_watchlist(
            actions_path,
            tmp_path / "data",
            run_date="2026-06-15",
            update_latest=True,
        )

    assert latest_path.read_text(encoding="utf-8") == "existing\n"
    assert not (tmp_path / "data/runs/2026-06-15/watchlist.csv").exists()


def test_build_watchlist_rejects_path_like_explicit_run_date(
    tmp_path: Path,
) -> None:
    actions_path = tmp_path / "data/latest/premarket_actions.csv"
    latest_path = tmp_path / "data/latest/watchlist.csv"
    latest_path.parent.mkdir(parents=True, exist_ok=True)
    latest_path.write_text("existing\n", encoding="utf-8")
    write_actions(actions_path, [])

    with pytest.raises(ValueError, match="invalid run_date.*../latest"):
        build_watchlist(
            actions_path,
            tmp_path / "data",
            run_date="../latest",
            update_latest=True,
        )

    assert latest_path.read_text(encoding="utf-8") == "existing\n"
    assert not (tmp_path / "data/runs").exists()


def test_build_watchlist_rejects_path_like_csv_run_date(tmp_path: Path) -> None:
    actions_path = tmp_path / "data/latest/premarket_actions.csv"
    write_text(
        actions_path,
        ",".join(ACTION_FIELDNAMES)
        + "\n"
        + "../latest,VIXY,US,3.05%,high,action_changed,reduce,VIXY changed,"
        + "Fake rationale,below 95\n",
    )

    with pytest.raises(ValueError, match="row 2.*run_date.*../latest"):
        build_watchlist(actions_path, tmp_path / "data")

    assert not (tmp_path / "data/runs").exists()


def test_build_watchlist_rejects_malformed_csv_run_date(tmp_path: Path) -> None:
    actions_path = tmp_path / "data/latest/premarket_actions.csv"
    write_text(
        actions_path,
        ",".join(ACTION_FIELDNAMES)
        + "\n"
        + "2026-02-30,VIXY,US,3.05%,high,action_changed,reduce,VIXY changed,"
        + "Fake rationale,below 95\n",
    )

    with pytest.raises(ValueError, match="row 2.*run_date.*2026-02-30"):
        build_watchlist(actions_path, tmp_path / "data")


def test_build_watchlist_missing_required_columns_raises_value_error(
    tmp_path: Path,
) -> None:
    actions_path = tmp_path / "data/latest/premarket_actions.csv"
    write_text(
        actions_path,
        "run_date,symbol,market,portfolio_weight_hkd,severity,suggested_action\n"
        "2026-06-16,VIXY,US,3.05%,high,reduce\n",
    )

    with pytest.raises(ValueError, match="missing action column\\(s\\).*watch_trigger"):
        build_watchlist(
            actions_path=actions_path,
            data_dir=tmp_path / "data",
            run_date=None,
            update_latest=True,
        )


def test_build_watchlist_duplicate_columns_raises_value_error(tmp_path: Path) -> None:
    actions_path = tmp_path / "data/latest/premarket_actions.csv"
    write_text(
        actions_path,
        ",".join(ACTION_FIELDNAMES + ["symbol"])
        + "\n"
        + "2026-06-16,VIXY,US,3.05%,high,action_changed,reduce,VIXY changed,"
        + "Fake rationale,below 95,VIXY\n",
    )

    with pytest.raises(ValueError, match="duplicate action column\\(s\\).*symbol"):
        build_watchlist(actions_path, tmp_path / "data")


def test_build_watchlist_without_rows_requires_run_date(tmp_path: Path) -> None:
    actions_path = tmp_path / "data/latest/premarket_actions.csv"
    write_actions(actions_path, [])

    with pytest.raises(ValueError, match="--date is required"):
        build_watchlist(
            actions_path=actions_path,
            data_dir=tmp_path / "data",
            run_date=None,
            update_latest=True,
        )


def test_build_watchlist_extra_column_raises_value_error(tmp_path: Path) -> None:
    actions_path = tmp_path / "data/latest/premarket_actions.csv"
    write_text(
        actions_path,
        ",".join(ACTION_FIELDNAMES)
        + "\n"
        + "2026-06-16,VIXY,US,3.05%,high,action_changed,reduce,VIXY changed,"
        + "Fake rationale,below 95,unexpected\n",
    )

    with pytest.raises(
        ValueError,
        match="row 2.*symbol VIXY.*extra column",
    ):
        build_watchlist(actions_path, tmp_path / "data")


def test_build_watchlist_ragged_row_missing_required_cell_raises_value_error(
    tmp_path: Path,
) -> None:
    actions_path = tmp_path / "data/latest/premarket_actions.csv"
    write_text(
        actions_path,
        ",".join(ACTION_FIELDNAMES)
        + "\n"
        + "2026-06-16,VIXY,US,3.05%,high,action_changed,reduce,VIXY changed,"
        + "Fake rationale\n",
    )

    with pytest.raises(
        ValueError,
        match="row 2.*symbol VIXY.*watch_trigger",
    ):
        build_watchlist(
            actions_path=actions_path,
            data_dir=tmp_path / "data",
            run_date=None,
            update_latest=True,
        )


def test_build_watchlist_blank_required_cell_raises_value_error(
    tmp_path: Path,
) -> None:
    actions_path = tmp_path / "data/latest/premarket_actions.csv"
    write_actions(
        actions_path,
        [
            {
                "run_date": "2026-06-16",
                "symbol": "VIXY",
                "market": "US",
                "portfolio_weight_hkd": "3.05%",
                "severity": "high",
                "change_type": "action_changed",
                "suggested_action": "   ",
                "summary": "VIXY changed",
                "rationale": "Fake rationale",
                "watch_trigger": "",
            },
        ],
    )

    with pytest.raises(
        ValueError,
        match="row 2.*symbol VIXY.*suggested_action",
    ):
        build_watchlist(actions_path, tmp_path / "data")
