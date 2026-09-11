"""Issue #71: N>=3 no-submit validation harness tests."""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import Future
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from open_trader.prediction_arbitrage import BookLevel, ThresholdOrderBook
from open_trader.prediction_n_leg import canonical_payload, fingerprint
from open_trader.prediction_n_leg_validation import (
    _OwnershipLock,
    _snapshot_from_frozen,
    FailClosedExecution,
    build_report,
    frozen_snapshot_from_file,
    readonly_v2_relations,
    run_paper_three_way,
    run_live,
    run_replay,
)
from open_trader.prediction_solver import solve_with_constraint_generation
from open_trader.prediction_solver_backends import CpSatBackend
from open_trader.prediction_solver_worker import WorkerOutcome, WorkerResponse
from open_trader.polymarket_relation_discovery import discover_mechanical_relation_catalog
from open_trader.relation_catalog import RelationCatalog
from open_trader.relation_catalog_v2 import RelationCatalogV2, SqliteCatalogStore


FIXTURE = Path(__file__).parent / "fixtures" / "prediction_n_leg_validation_frozen_n3.json"


def load_fixture() -> dict[str, object]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def live_books() -> dict[str, ThresholdOrderBook]:
    now = datetime(2026, 8, 16, 2, 0, tzinfo=UTC)
    prices = {"a": "0.33", "b": "0.32", "c": "0.31"}
    return {
        contract: ThresholdOrderBook(
            contract,
            (BookLevel(Decimal(prices[contract]), Decimal("10")),),
            (),
            now,
        )
        for contract in prices
    }


def relation_payload(*, qualification: bool = False) -> dict[str, object]:
    fixture = load_fixture()
    problem = dict(fixture["problem"])
    if qualification:
        problem["qualification_constraints"] = [
            {
                "constraint_id": "min-profit",
                "rule_version": "v1",
                "metric": "GUARANTEED_PROFIT_UNITS",
                "comparison": "GREATER_THAN_OR_EQUAL",
                "threshold_numerator": 1,
                "threshold_denominator": 1,
            }
        ]
    return {
        "relation_type": "EXACTLY_ONE",
        # #117: proven fee-free facts so the live resolver dispatches the
        # component instead of skipping it for missing fee facts.
        "endpoints": [
            {
                "venue": "polymarket",
                "contract_id": contract,
                "fees_enabled": False,
            }
            for contract in ("a", "b", "c")
        ],
        "model": {
            "terminal_states": [{}],
            "payouts": [{}],
            "capital_release": "2026-08-16T06:00:00Z",
            "problem": problem,
        },
    }


def paper_three_way_rows(
    *,
    fees_enabled: bool | None = False,
    fee_rate: str | None = None,
    fee_exponent: int | None = None,
    taker_only: bool | None = None,
) -> dict[str, dict[str, object]]:
    """One supported paper row with release time deliberately unknown."""

    fixture = load_fixture()
    problem = deepcopy(fixture["problem"])
    assert isinstance(problem, dict)
    for state in problem["terminal_state_sets"]:
        for atom in state["atoms"]:
            atom["capital_release_at"] = None
    contracts = ("a", "b", "c")
    tokens = {contract: f"paper-token-{contract}" for contract in contracts}
    endpoints = [
        {
            "venue": "polymarket",
            "contract_id": contract,
            "yes_token_id": tokens[contract],
            "fees_enabled": fees_enabled,
            "fee_rate": fee_rate,
            "fee_exponent": fee_exponent,
            "taker_only": taker_only,
            "settlement_rules": "known supported football rule",
        }
        for contract in contracts
    ]
    model = {
        "template": "FOOTBALL_REGULAR_TIME_3WAY_V1",
        "group_id": "paper-group",
        "member_count": 3,
        "directions": {"a": "HOME_WIN", "b": "DRAW", "c": "AWAY_WIN"},
        "rules": {contract: "known supported football rule" for contract in contracts},
        "tokens": {
            contract: {"YES": tokens[contract], "NO": f"paper-no-{contract}"}
            for contract in contracts
        },
        "terminal_states": ["NORMAL_YES", "NORMAL_NO"],
        "payouts": {
            contract: {"NORMAL_YES": 1, "NORMAL_NO": 0}
            for contract in contracts
        },
        "capital_release": None,
        "incomplete_reasons": ["MISSING_CAPITAL_RELEASE_AT"],
        "problem": problem,
    }
    return {
        "paper-three": {
            "version_id": "paper-version",
            "status": "PENDING",
            "activation": "PENDING",
            "endpoints": endpoints,
            "model": model,
        }
    }


def paper_books(
    prices: tuple[str, str, str],
    *,
    now: datetime,
    minima: tuple[str, str, str] = ("2", "5", "3"),
    minimum_notionals: tuple[str | None, str | None, str | None] = (
        None,
        None,
        None,
    ),
    depth: str = "10",
    omit_rules_for: str | None = None,
    descending: bool = False,
) -> dict[str, dict[str, object]]:
    return {
        f"paper-token-{contract}": {
            "token_id": f"paper-token-{contract}",
            "asks": (
                [
                    {"price": str(Decimal(price) + Decimal("0.10")), "size": "3"},
                    {"price": price, "size": "7"},
                ]
                if descending
                else [{"price": price, "size": depth}]
            ),
            "bids": [],
            "confirmed_at": now,
            **(
                {}
                if contract == omit_rules_for
                else {
                    "minimum_order_size": minimum,
                    "tick_size": "0.01",
                    **(
                        {"minimum_order_notional": minimum_notional}
                        if minimum_notional is not None
                        else {}
                    ),
                }
            ),
        }
        for contract, price, minimum, minimum_notional in zip(
            ("a", "b", "c"), prices, minima, minimum_notionals, strict=True
        )
    }


def assert_paper_no_side_effects(report: dict[str, object]) -> None:
    assert report["order_ready"] is False
    assert report["zero_side_effects"]["submitted_orders"] == 0
    assert report["zero_side_effects"]["mutation_attempts"] == 0


def seed_catalog(
    db_path: Path, *, activate: bool, qualification: bool = False
) -> None:
    catalog = RelationCatalogV2(SqliteCatalogStore(db_path))
    entry = catalog.ingest(relation_payload(qualification=qualification))
    catalog.approve(entry["version_id"], actor="test", git_sha="test")
    if activate:
        store = catalog.store
        store.begin_write()
        store["versions"][entry["version_id"]]["activation_status"] = "ACTIVE"
        store.commit_write()


def worker_outcome(request: object, evidence: dict[str, object]) -> WorkerOutcome:
    return WorkerOutcome(
        request.request_id,
        "OK",
        "COMPLETED",
        1,
        1,
        0,
        False,
        True,
        WorkerResponse("p", "cp_sat", request.request_id, "OK", evidence, {}, ()),
        "9.15.6755",
    )


class FakeSolverServer:
    """In-process solver server that returns real #50 evidence per request."""

    def __init__(self) -> None:
        self.submit_calls = 0
        self.requests: list[object] = []

    def submit(self, request: object) -> Future[WorkerOutcome]:
        self.submit_calls += 1
        self.requests.append(request)
        evidence = solve_with_constraint_generation(
            request.request, CpSatBackend(), request.limits
        )
        future: Future[WorkerOutcome] = Future()
        future.set_result(
            worker_outcome(request, canonical_payload(evidence))
        )
        return future


def test_three_way_paper_report_prices_legal_equal_lots(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    rows = paper_three_way_rows()
    requested: list[tuple[str, ...]] = []

    def source(token_ids: tuple[str, ...]) -> dict[str, dict[str, object]]:
        requested.append(tuple(token_ids))
        return paper_books(
            ("0.30", "0.32", "0.33"), now=now, descending=True
        )

    positive = run_paper_three_way(
        rows,
        book_source=source,
        data_dir=tmp_path / "positive",
        as_of=now,
    )

    assert positive["status"] == "PASS"
    assert positive["component_id"] == "component:a:b:c"
    assert positive["qualification_status"] == "UNKNOWN"
    assert positive["economics"] == {
        "quantity_lots": 5,
        "payout_lower_bound_units": 5_000_000,
        "cost_upper_bound_units": 4_750_000,
        "guaranteed_profit_units": 250_000,
        "payout_lower_bound": "5",
        "cost_upper_bound": "4.75",
        "guaranteed_profit": "0.25",
        "economic_decision": "PROFITABLE",
    }
    assert [leg["quantity_lots"] for leg in positive["legs"]] == [5, 5, 5]
    assert positive["quantity_domain"] == [0, 5]
    assert positive["capital_release_at"] is None
    assert positive["evaluated_at"] == now.isoformat()
    assert len(requested) == 1
    assert_paper_no_side_effects(positive)

    minimum_notional = run_paper_three_way(
        rows,
        book_source=lambda token_ids: paper_books(
            ("0.30", "0.32", "0.33"),
            now=now,
            minimum_notionals=("2", None, None),
        ),
        data_dir=tmp_path / "minimum-notional",
    )
    assert minimum_notional["status"] == "PASS"
    assert minimum_notional["economics"] == {
        "quantity_lots": 7,
        "payout_lower_bound_units": 7_000_000,
        "cost_upper_bound_units": 6_650_000,
        "guaranteed_profit_units": 350_000,
        "payout_lower_bound": "7",
        "cost_upper_bound": "6.65",
        "guaranteed_profit": "0.35",
        "economic_decision": "PROFITABLE",
    }
    assert minimum_notional["order_rules"]["status"] == "UNKNOWN_MINIMUM_NOTIONAL"
    assert minimum_notional["legs"][0]["minimum_order_notional"] == "2"
    assert_paper_no_side_effects(minimum_notional)

    negative = run_paper_three_way(
        rows,
        book_source=lambda token_ids: paper_books(
            ("0.35", "0.35", "0.35"), now=now
        ),
        data_dir=tmp_path / "negative",
    )
    assert negative["status"] == "PASS"
    assert negative["economics"]["cost_upper_bound_units"] == 5_250_000
    assert negative["economics"]["guaranteed_profit_units"] == -250_000
    assert negative["economics"]["economic_decision"] == "REJECTED"
    assert_paper_no_side_effects(negative)

    charging = run_paper_three_way(
        paper_three_way_rows(
            fees_enabled=True,
            fee_rate="0.04",
            fee_exponent=1,
            taker_only=True,
        ),
        book_source=lambda token_ids: paper_books(
            ("0.33", "0.33", "0.33"), now=now
        ),
        data_dir=tmp_path / "charging",
    )
    assert charging["status"] == "PASS"
    assert charging["economics"]["cost_upper_bound_units"] == 5_082_660
    assert charging["economics"]["guaranteed_profit_units"] == -82_660
    assert charging["fees"]["status"] == "CHARGING"
    assert charging["fees"]["rate"] == "0.04"
    assert_paper_no_side_effects(charging)

    blocked_cases = (
        ("UNKNOWN_FEE_FACTS", paper_three_way_rows(fees_enabled=None)),
        (
            "UNKNOWN_FEE_FACTS",
            paper_three_way_rows(
                fees_enabled=True,
                fee_rate="0.04",
                fee_exponent=None,
                taker_only=True,
            ),
        ),
        ("STALE_BOOK", rows),
        ("INSUFFICIENT_DEPTH", rows),
        ("UNKNOWN_ORDER_RULES", rows),
    )
    for reason, case_rows in blocked_cases:
        if reason == "STALE_BOOK":
            case_books = lambda token_ids: paper_books(
                ("0.30", "0.32", "0.33"),
                now=now - timedelta(seconds=11),
            )
        elif reason == "INSUFFICIENT_DEPTH":
            case_books = lambda token_ids: paper_books(
                ("0.30", "0.32", "0.33"), now=now, depth="4"
            )
        elif reason == "UNKNOWN_ORDER_RULES":
            case_books = lambda token_ids: paper_books(
                ("0.30", "0.32", "0.33"), now=now, omit_rules_for="b"
            )
        else:
            case_books = source
        blocked = run_paper_three_way(
            case_rows,
            book_source=case_books,
            data_dir=tmp_path / reason.lower(),
        )
        assert blocked["status"] == "BLOCKED"
        assert blocked["reason"] == reason
        assert_paper_no_side_effects(blocked)


def test_three_way_paper_normalizes_catalog_dollar_payouts(tmp_path: Path) -> None:
    fixture = Path(__file__).parent / "fixtures" / "polymarket_three_way_football.json"
    event = json.loads(fixture.read_text(encoding="utf-8"))
    draw = event["markets"][1]
    labels = json.loads(draw["outcomes"])
    token_ids = json.loads(draw["clobTokenIds"])
    by_label = dict(zip(labels, token_ids, strict=True))
    draw["outcomes"] = json.dumps(["No", "Yes"])
    draw["clobTokenIds"] = json.dumps([by_label["No"], by_label["Yes"]])

    discovered = discover_mechanical_relation_catalog([event])
    assert len(discovered.groups) == 1
    catalog = RelationCatalog(tmp_path / "catalog")
    catalog.ingest_mechanical_relation(discovered.groups[0])
    rows = catalog.review_rows()
    row = rows[0]
    assert row["model"]["problem"]["valuation_unit_id"] == "USD"
    assert row["model"]["problem"]["terminal_state_sets"][0]["atoms"][1][
        "payouts"
    ][0]["payout_lower_bound_per_lot_units"] == 1
    assert all(
        fact == {"exponent": 1, "taker_only": True}
        for fact in row["model"]["fee_facts"].values()
    )

    now = datetime.now(UTC)
    prices = ("0.30", "0.32", "0.33")

    def source(token_ids: tuple[str, ...]) -> dict[str, dict[str, object]]:
        return {
            token_id: {
                "token_id": token_id,
                "asks": [{"price": price, "size": "10"}],
                "bids": [],
                "confirmed_at": now,
                "minimum_order_size": minimum,
                "tick_size": "0.01",
            }
            for token_id, price, minimum in zip(
                token_ids, prices, ("2", "5", "3"), strict=True
            )
        }

    report = run_paper_three_way(
        rows,
        book_source=source,
        data_dir=tmp_path / "paper",
    )

    assert report["status"] == "PASS"
    assert report["economics"] == {
        "quantity_lots": 5,
        "payout_lower_bound_units": 5_000_000,
        "cost_upper_bound_units": 4_912_175,
        "guaranteed_profit_units": 87_825,
        "payout_lower_bound": "5",
        "cost_upper_bound": "4.912175",
        "guaranteed_profit": "0.087825",
        "economic_decision": "PROFITABLE",
    }
    assert report["order_ready"] is False
    assert_paper_no_side_effects(report)


def test_three_way_paper_rejects_future_books(tmp_path: Path) -> None:
    rows = paper_three_way_rows()
    as_of = datetime(2026, 8, 16, 2, 0, tzinfo=UTC)

    future = run_paper_three_way(
        rows,
        book_source=lambda token_ids: paper_books(
            ("0.30", "0.32", "0.33"),
            now=as_of + timedelta(hours=1),
        ),
        data_dir=tmp_path / "future",
        as_of=as_of,
    )
    assert future["status"] == "BLOCKED"
    assert future["reason"] == "FUTURE_BOOK"
    assert "economics" not in future
    assert "legs" not in future
    assert_paper_no_side_effects(future)

    same_time = run_paper_three_way(
        rows,
        book_source=lambda token_ids: paper_books(
            ("0.30", "0.32", "0.33"),
            now=as_of,
        ),
        data_dir=tmp_path / "same-time",
        as_of=as_of,
    )
    assert same_time["status"] == "PASS"
    assert_paper_no_side_effects(same_time)

    old = run_paper_three_way(
        rows,
        book_source=lambda token_ids: paper_books(
            ("0.30", "0.32", "0.33"),
            now=as_of - timedelta(seconds=11),
        ),
        data_dir=tmp_path / "old",
        as_of=as_of,
    )
    assert old["status"] == "BLOCKED"
    assert old["reason"] == "STALE_BOOK"
    assert_paper_no_side_effects(old)

    started = datetime.now(UTC)
    received: list[datetime] = []

    def source_after_start(token_ids: tuple[str, ...]) -> dict[str, dict[str, object]]:
        received_at = datetime.now(UTC)
        received.append(received_at)
        return paper_books(
            ("0.30", "0.32", "0.33"),
            now=received_at,
        )

    default_clock = run_paper_three_way(
        rows,
        book_source=source_after_start,
        data_dir=tmp_path / "default-clock",
    )
    assert received and received[0] >= started
    assert default_clock["status"] == "PASS"
    assert_paper_no_side_effects(default_clock)


def test_three_way_paper_rejects_off_tick_prices(tmp_path: Path) -> None:
    rows = paper_three_way_rows()
    now = datetime(2026, 8, 16, 2, 0, tzinfo=UTC)

    off_tick = run_paper_three_way(
        rows,
        book_source=lambda token_ids: paper_books(
            ("0.465", "0.465", "0.465"),
            now=now,
        ),
        data_dir=tmp_path / "off-tick",
        as_of=now,
    )
    assert off_tick["status"] == "BLOCKED"
    assert off_tick["reason"] == "OFF_TICK_PRICE"
    assert "economics" not in off_tick
    assert "legs" not in off_tick
    assert_paper_no_side_effects(off_tick)

    aligned = run_paper_three_way(
        rows,
        book_source=lambda token_ids: paper_books(
            ("0.46", "0.46", "0.46"),
            now=now,
        ),
        data_dir=tmp_path / "aligned",
        as_of=now,
    )
    assert aligned["status"] == "PASS"
    assert_paper_no_side_effects(aligned)


def test_replay_n3_happy_path() -> None:
    report = run_replay(frozen_snapshot_from_file(FIXTURE))

    assert report["status"] == "PASS"
    assert report["legs"] == 3
    assert [q["action_id"] for q in report["quantities"] if q["quantity_lots"] > 0] == [
        "buy-yes-a",
        "buy-yes-b",
        "buy-yes-c",
    ]
    decision = report["execution_decision"]
    assert decision["order_ready"] is False
    # #74: the fixed n3 solution is proven UNSAFE against the default zero
    # cap: the adversary closes at 650,000 (fills a+b, scenario a:no/b:no/c:yes).
    assert decision["partial_fill_proof"] == "PARTIAL_FILL_UNSAFE"
    assert decision["reason"] == "PARTIAL_FILL_UNSAFE"
    proof = decision["proof"]
    assert proof["solver_termination"] == "CLOSED"
    assert proof["verifier_status"] == "QUALIFIED_VERIFIED"
    assert proof["lower_bound_units"] == proof["upper_bound_units"] == 650_000
    assert proof["cap_units"] == 0
    counterexample = proof["counterexample"]
    assert counterexample["loss_units"] == 650_000
    assert counterexample["cap_units"] == 0
    assert {
        (row["action_id"], row["quantity_lots"])
        for row in counterexample["fill_quantities"]
    } == {("buy-yes-a", 1), ("buy-yes-b", 1)}
    assert report["market"]["guaranteed_profit_units"] == 40_000
    assert report["oracle_differential"]["pass"] is True
    assert all(item["pass"] for item in report["oracle_differential"]["checks"])
    assert all(item["pass"] for item in report["expected_vs_actual"])


def test_snapshot_from_frozen_routes_buy_no_to_bids() -> None:
    fixture = load_fixture()
    problem = dict(fixture["problem"])
    actions = [dict(action) for action in problem["actions"]]
    for action in actions:
        if action["action_id"] == "buy-yes-c":
            action["side"] = "BUY_NO"
    problem["actions"] = actions
    books = dict(fixture["books"])
    books["buy-yes-c"] = {
        "asks": [],
        "bids": [{"price": "0.69", "size": "10"}],
    }
    snapshot = _snapshot_from_frozen(
        {
            "component_id": "validation:exactly-one-n3",
            "problem": problem,
            "books": books,
        }
    )
    books_by_id = {leg.leg_id: leg.book for leg in snapshot.legs}
    assert books_by_id["buy-yes-a"].asks == (
        BookLevel(Decimal("0.33"), Decimal("10")),
    )
    assert books_by_id["buy-yes-a"].bids == ()
    assert books_by_id["buy-yes-c"].asks == ()
    assert books_by_id["buy-yes-c"].bids == (
        BookLevel(Decimal("0.69"), Decimal("10")),
    )


def test_replay_two_leg_snapshot_is_rejected(tmp_path: Path) -> None:
    data = load_fixture()
    problem = data["problem"]
    problem["actions"] = [
        action for action in problem["actions"] if action["action_id"] != "buy-yes-c"
    ]
    problem["terminal_state_sets"] = [
        state for state in problem["terminal_state_sets"] if state["market_contract_id"] != "c"
    ]
    problem["constraint_model"]["relations"][0]["contract_ids"] = ["a", "b"]
    data["expected"]["portfolio_actions"] = ["buy-yes-a", "buy-yes-b"]
    data["books"].pop("buy-yes-c")
    data["content_fingerprint"] = fingerprint({"problem": problem, "books": data["books"]})
    path = tmp_path / "two-leg.json"
    path.write_text(json.dumps(data), encoding="utf-8")

    report = run_replay(frozen_snapshot_from_file(path))

    assert report["status"] == "FAIL"
    assert report["reason"] == "N_LESS_THAN_3"
    assert report["legs"] == 2


def test_frozen_snapshot_rejects_tampered_content(tmp_path: Path) -> None:
    data = load_fixture()
    data["books"]["buy-yes-a"]["asks"][0]["price"] = "0.99"
    path = tmp_path / "tampered.json"
    path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(ValueError, match="content fingerprint mismatch"):
        frozen_snapshot_from_file(path)


def test_live_no_active_relation_is_blocked(tmp_path: Path) -> None:
    db = tmp_path / "catalog.sqlite3"
    seed_catalog(db, activate=False)
    exported = readonly_v2_relations(db)
    server = FakeSolverServer()

    live = run_live(
        exported["rows"],
        book_source=lambda _: live_books(),
        data_dir=tmp_path / "run",
        catalog=exported,
        solver_server=server,
    )

    assert live["status"] == "BLOCKED"
    assert live["reason"] == "NO_ACTIVE_N3_RELATION"
    assert live["zero_side_effects"]["submitted_orders"] == 0
    assert server.submit_calls == 0


def test_live_active_n3_relation_with_injected_books_qualifies(tmp_path: Path) -> None:
    db = tmp_path / "catalog.sqlite3"
    seed_catalog(db, activate=True)
    exported = readonly_v2_relations(db)
    seam = FailClosedExecution()
    server = FakeSolverServer()

    live = run_live(
        exported["rows"],
        book_source=lambda _: live_books(),
        data_dir=tmp_path / "run",
        catalog=exported,
        execution=seam,
        solver_server=server,
    )

    assert live["status"] == "PASS"
    assert live["legs"] == 3
    assert live["qualified_verified"] is True
    assert live["guaranteed_profit_units"] == 40_000
    assert live["execution_decision"]["order_ready"] is False
    # The harness store has no safety config, so the execution fails the
    # unsettled-capital gate before the #74 proof is ever applicable.
    assert (
        live["execution_decision"]["reason"]
        == "UNSETTLED_CAP_EXCEEDED"
    )
    assert live["execution_decision"]["capital_use_units"] == 960_000
    assert live["execution_decision"]["market_solution_fingerprint"] == (
        "sha256:9d51b1158c352878df159fe2fcb12d0e3160cdd30c5d70056491f294cb2b4cc2"
    )
    assert server.submit_calls >= 1
    assert seam.submit_attempts == 0
    assert seam.mutation_attempts == 0
    assert live["zero_side_effects"]["submitted_orders"] == 0


def test_live_proven_no_qualified_opportunity_passes(tmp_path: Path) -> None:
    db = tmp_path / "catalog.sqlite3"
    seed_catalog(db, activate=True, qualification=True)
    exported = readonly_v2_relations(db)
    server = FakeSolverServer()

    def expensive_books(_: tuple[str, ...]) -> dict[str, ThresholdOrderBook]:
        now = datetime(2026, 8, 16, 2, 0, tzinfo=UTC)
        return {
            contract: ThresholdOrderBook(
                contract,
                (BookLevel(Decimal("0.55"), Decimal("10")),),
                (),
                now,
            )
            for contract in ("a", "b", "c")
        }

    live = run_live(
        exported["rows"],
        book_source=expensive_books,
        data_dir=tmp_path / "run",
        catalog=exported,
        solver_server=server,
        poll_timeout_seconds=5.0,
    )

    assert live["status"] == "PASS"
    assert live["qualified_verified"] is False
    assert live["legs"] == 0
    assert live["execution_decision"] is None
    assert server.submit_calls >= 1
    assert live["zero_side_effects"]["submitted_orders"] == 0


def test_live_missing_books_is_blocked(tmp_path: Path) -> None:
    db = tmp_path / "catalog.sqlite3"
    seed_catalog(db, activate=True)
    exported = readonly_v2_relations(db)
    server = FakeSolverServer()

    live = run_live(
        exported["rows"],
        book_source=lambda _: {},
        data_dir=tmp_path / "run",
        catalog=exported,
        solver_server=server,
    )

    assert live["status"] == "BLOCKED"
    assert live["reason"] == "MISSING_BOOKS"
    assert server.submit_calls == 0


def leg_token_problem_rows(
    *, include_endpoint_tokens: bool
) -> dict[str, dict[str, object]]:
    """Issue #114 rows over the frozen N3 problem with its c leg flipped to
    BUY_NO, exercising direction-aware book reads on one contract."""

    fixture = load_fixture()
    problem = dict(fixture["problem"])
    actions = [dict(action) for action in problem["actions"]]
    for action in actions:
        if action["action_id"] == "buy-yes-c":
            action["side"] = "BUY_NO"
    problem["actions"] = actions
    tokens = {
        "a": {"yes_token_id": "tok-yes-a"},
        "b": {"yes_token_id": "tok-yes-b"},
        "c": {"yes_token_id": "tok-yes-c", "no_token_id": "tok-no-c"},
    }
    endpoints = []
    for contract in ("a", "b", "c"):
        endpoint: dict[str, object] = {
            "venue": "polymarket",
            "contract_id": contract,
        }
        if include_endpoint_tokens:
            endpoint.update(tokens[contract])
        endpoints.append(endpoint)
    return {
        "validation:exactly-one-n3": {
            "version_id": "v-1",
            "status": "APPROVED",
            "activation": "ACTIVE",
            "endpoints": endpoints,
            "model": {
                "terminal_states": [{}],
                "payouts": [{}],
                "capital_release": "2026-08-16T06:00:00Z",
                "problem": problem,
            },
        }
    }


def token_books(requested: list[tuple[str, ...]]) -> dict[str, ThresholdOrderBook]:
    now = datetime(2026, 8, 16, 2, 0, tzinfo=UTC)
    prices = {
        "tok-yes-a": "0.33",
        "tok-yes-b": "0.32",
        "tok-no-c": "0.31",
    }
    assert requested  # the seam must be consulted at least once
    return {
        token: ThresholdOrderBook(
            token,
            (BookLevel(Decimal(prices[token]), Decimal("10")),),
            (),
            now,
        )
        for token in prices
    }


def test_live_resolves_book_requests_by_action_direction(tmp_path: Path) -> None:
    """C1: the book seam sees exactly the direction-resolved CLOB tokens —
    the BUY_NO leg reads the NO token's book and the YES legs read their YES
    tokens; raw contract ids never reach the seam."""

    rows = leg_token_problem_rows(include_endpoint_tokens=True)
    requested: list[tuple[str, ...]] = []
    server = FakeSolverServer()

    live = run_live(
        rows,
        book_source=lambda token_ids: (
            requested.append(tuple(token_ids)) or token_books(requested)
        ),
        data_dir=tmp_path / "run",
        catalog={"generation": 1},
        solver_server=server,
    )

    assert requested, "book seam was never consulted"
    assert {token for call in requested for token in call} == {
        "tok-yes-a", "tok-yes-b", "tok-no-c",
    }
    assert live.get("reason") != "MISSING_BOOKS"


def test_live_leg_token_map_injection_covers_legacy_rows(tmp_path: Path) -> None:
    """C1: legacy rows without token fields still reach the direction-resolved
    book query when the caller injects the contract -> token map."""

    rows = leg_token_problem_rows(include_endpoint_tokens=False)
    requested: list[tuple[str, ...]] = []
    server = FakeSolverServer()

    live = run_live(
        rows,
        book_source=lambda token_ids: (
            requested.append(tuple(token_ids)) or token_books(requested)
        ),
        data_dir=tmp_path / "run",
        catalog={"generation": 1},
        solver_server=server,
        leg_token_map={
            "a": {"yes_token_id": "tok-yes-a"},
            "b": {"yes_token_id": "tok-yes-b"},
            "c": {"yes_token_id": "tok-yes-c", "no_token_id": "tok-no-c"},
        },
    )

    assert requested, "book seam was never consulted"
    assert {token for call in requested for token in call} == {
        "tok-yes-a", "tok-yes-b", "tok-no-c",
    }
    assert live.get("reason") != "MISSING_BOOKS"


def test_fail_closed_seam_blocks_mutation() -> None:
    seam = FailClosedExecution()

    with pytest.raises(AssertionError, match="must never be reached"):
        seam.submit()
    with pytest.raises(AssertionError, match="must never be reached"):
        seam.mutate()

    assert seam.submit_attempts == 1
    assert seam.mutation_attempts == 1


def test_report_schema_and_fingerprints(tmp_path: Path) -> None:
    replay = run_replay(frozen_snapshot_from_file(FIXTURE))
    db = tmp_path / "catalog.sqlite3"
    seed_catalog(db, activate=True)
    exported = readonly_v2_relations(db)
    server = FakeSolverServer()
    live = run_live(
        exported["rows"],
        book_source=lambda _: live_books(),
        data_dir=tmp_path / "run",
        catalog=exported,
        solver_server=server,
    )

    report = build_report(replay=replay, live=live, data_dir=tmp_path / "run")

    assert report["status"] == "PASS"
    assert report["schema_version"] == "open_trader.prediction_n_leg_validation.report.v1"
    assert isinstance(report["pid"], int) and report["pid"] > 0
    assert report["cwd"]
    assert report["git_sha"]
    assert report["captured_at"]
    assert report["zero_side_effects"]["submitted_orders"] == 0
    replay_fingerprints = report["replay"]["fingerprints"]
    assert replay_fingerprints["content"] == load_fixture()["content_fingerprint"]
    assert replay_fingerprints["structure"]
    assert report["replay"]["constraint_generation_rounds"]["master_rounds"] >= 0
    assert report["replay"]["timings"]["solve_seconds"] >= 0
    assert report["live"]["fingerprints"]["catalog_generation"] == exported["generation"]


def test_live_resolver_is_stopped_and_lock_released(tmp_path: Path) -> None:
    db = tmp_path / "catalog.sqlite3"
    seed_catalog(db, activate=True)
    exported = readonly_v2_relations(db)
    server = FakeSolverServer()
    data_dir = tmp_path / "run"

    live = run_live(
        exported["rows"],
        book_source=lambda _: live_books(),
        data_dir=data_dir,
        catalog=exported,
        solver_server=server,
    )

    assert live["status"] == "PASS"
    assert not [
        thread
        for thread in threading.enumerate()
        if thread.name == "prediction-live-resolver"
    ]
    lock_path = data_dir / "prediction_arbitrage" / ".nleg-validation.lock"
    with _OwnershipLock(lock_path):
        pass


def test_live_lock_unavailable_creates_no_solver_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "catalog.sqlite3"
    seed_catalog(db, activate=True)
    exported = readonly_v2_relations(db)
    data_dir = tmp_path / "run"
    lock_path = data_dir / "prediction_arbitrage" / ".nleg-validation.lock"
    constructed: list[object] = []

    class ExplodingOwner:
        def __init__(self, command: object) -> None:
            constructed.append(command)
            raise AssertionError("owned solver server must not be constructed before lock")

    monkeypatch.setattr(
        "open_trader.prediction_n_leg_validation.SolverServerOwner",
        ExplodingOwner,
    )

    started = time.perf_counter()
    with _OwnershipLock(lock_path):
        live = run_live(
            exported["rows"],
            book_source=lambda _: live_books(),
            data_dir=data_dir,
            catalog=exported,
        )
    elapsed = time.perf_counter() - started

    assert live["status"] == "BLOCKED"
    assert live["reason"] == "VALIDATION_LOCK_UNAVAILABLE"
    assert constructed == []
    assert elapsed < 5.0
