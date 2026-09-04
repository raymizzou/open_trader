"""Issue #106: minimal opportunity episodes — tracker, store, and display.

Seam 1 (T1-T9) exercises the pure ``EpisodeTracker`` state machine with an
explicit clock; expected values come from the approved design semantics
("Episode 只有在…连续 5 分钟新鲜负证明才关闭", DEFAULT_SAFETY_CONFIG
``episode_rearm_gap_seconds=300``) and the approved card mock.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from open_trader.prediction_n_leg_episodes import (
    CLOSE_COMPONENT_RETIRED,
    CLOSE_NO_QUALIFIED_OPPORTUNITY,
    EpisodeRecord,
    EpisodeStore,
    EpisodeTracker,
)


BASE = datetime(2026, 9, 1, 10, 0, 0, tzinfo=UTC)


def fingerprints(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "component_generation": 1,
        "model_fingerprint": "sha256:model",
        "quote_fingerprint": "sha256:quote",
        "qualification_fingerprint": "sha256:qual",
        "qualification_policy_version": "v1",
    }
    payload.update(overrides)
    return payload


def qualified(tracker: EpisodeTracker, component_id: str, at: datetime, profit: str, *, would_submit: bool = False, lineage: str = "L1") -> None:
    tracker.observe_qualified(
        component_id,
        lineage,
        Decimal(profit),
        would_submit,
        None,
        fingerprints(),
        at,
    )


def negative(
    tracker: EpisodeTracker,
    component_id: str,
    at: datetime,
    *,
    fingerprint_value: str = "sha256:neg",
    binding_matches: bool = True,
    quote_fresh: bool = True,
    gap_seconds: float = 300.0,
    generation: int | None = 1,
    qualification_policy_version: str | None = None,
) -> None:
    tracker.observe_negative(
        component_id,
        proof_fingerprint=fingerprint_value,
        generation=generation,
        model_fingerprint="sha256:model",
        quote_fingerprint="sha256:quote",
        qualification_fingerprint="sha256:qual",
        binding_matches=binding_matches,
        quote_fresh=quote_fresh,
        gap_seconds=gap_seconds,
        now=at,
        qualification_policy_version=qualification_policy_version,
    )


class CountingStore(EpisodeStore):
    """EpisodeStore that counts episode saves (persist-only-on-change)."""

    def __init__(self, data_dir: Path) -> None:
        super().__init__(data_dir)
        self.save_calls = 0

    def save_episode(self, record: EpisodeRecord) -> None:
        self.save_calls += 1
        super().save_episode(record)


def test_t10_store_roundtrip_and_restart_semantics(tmp_path: Path) -> None:
    store = EpisodeStore(tmp_path)
    instance = EpisodeTracker(store=store)
    qualified(instance, "component:x:y", BASE, "12.40", would_submit=False)
    qualified(
        instance,
        "component:x:y",
        BASE + timedelta(seconds=30),
        "12.40",
        would_submit=True,
    )
    qualified(
        instance,
        "component:x:y",
        BASE + timedelta(seconds=90),
        "12.40",
        would_submit=False,
    )
    qualified(
        instance,
        "component:x:y",
        BASE + timedelta(seconds=120),
        "12.40",
        would_submit=True,
    )

    open_rows = store.load_open()
    assert set(open_rows) == {"component:x:y"}
    row = open_rows["component:x:y"]
    assert row["opportunity_episode_id"] == (
        instance.episode("component:x:y").opportunity_episode_id
    )
    assert float(row["would_submit_ready_seconds"]) == 60.0
    assert row["would_submit_ready_since"] == (
        BASE + timedelta(seconds=120)
    ).isoformat()
    assert row["best_guaranteed_profit"] == "12.40"
    assert row["opened_at"] == BASE.isoformat()

    load_at = BASE + timedelta(seconds=600)
    reloaded = EpisodeTracker(store=store)
    reloaded.load_open(now=load_at)
    episode = reloaded.episode("component:x:y")
    assert episode.status == "ONGOING"
    assert episode.opened_at == BASE
    assert episode.would_submit_ready_seconds == 60.0
    assert episode.would_submit_ready_since is None
    assert episode.negative_close_started_at is None
    assert episode.best_guaranteed_profit == Decimal("12.40")


def test_t11_ddl_is_idempotent_expand_only_and_hides_closed_rows(
    tmp_path: Path,
) -> None:
    first_store = EpisodeStore(tmp_path)
    second_store = EpisodeStore(tmp_path)
    instance = EpisodeTracker(store=first_store)
    qualified(instance, "component:x:y", BASE, "12.40")
    episode_id = instance.episode("component:x:y").opportunity_episode_id
    instance.component_retired("component:x:y", now=BASE + timedelta(seconds=60))

    assert second_store.load_open() == {}
    with sqlite3.connect(first_store.path) as connection:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    assert {"opportunity_episodes", "opportunity_episode_proofs"} <= tables
    with sqlite3.connect(first_store.path) as connection:
        closed = connection.execute(
            "SELECT closed_at, close_reason FROM opportunity_episodes"
            " WHERE opportunity_episode_id=?",
            (episode_id,),
        ).fetchone()
    assert closed is not None and closed[0] is not None


def test_t9_each_reset_source_resets_without_closing() -> None:
    def opens_with_negative(instance: EpisodeTracker) -> None:
        qualified(instance, "component:x:y", BASE, "12.40")
        negative(instance, "component:x:y", BASE + timedelta(seconds=60))

    # Quote staleness.
    stale = EpisodeTracker()
    opens_with_negative(stale)
    stale.mark_quote_stale("component:x:y", now=BASE + timedelta(seconds=90))
    negative(stale, "component:x:y", BASE + timedelta(seconds=150))
    assert stale.episode("component:x:y").status == "ONGOING"

    # Binding mismatch (negative arrives bound to different fingerprints).
    mismatched = EpisodeTracker()
    opens_with_negative(mismatched)
    negative(
        mismatched,
        "component:x:y",
        BASE + timedelta(seconds=90),
        binding_matches=False,
        fingerprint_value="sha256:other",
    )
    negative(mismatched, "component:x:y", BASE + timedelta(seconds=150))
    assert mismatched.episode("component:x:y").status == "ONGOING"

    # Component generation change arrives with the next qualified snapshot.
    generation_changed = EpisodeTracker()
    opens_with_negative(generation_changed)
    generation_changed.observe_qualified(
        "component:x:y",
        "L1",
        Decimal("12.40"),
        False,
        None,
        fingerprints(component_generation=2),
        BASE + timedelta(seconds=90),
    )
    negative(generation_changed, "component:x:y", BASE + timedelta(seconds=150))
    assert generation_changed.episode("component:x:y").status == "ONGOING"

    # Qualification policy version change arrives the same way.
    policy_changed = EpisodeTracker()
    opens_with_negative(policy_changed)
    policy_changed.observe_qualified(
        "component:x:y",
        "L1",
        Decimal("12.40"),
        False,
        None,
        fingerprints(qualification_policy_version="v2"),
        BASE + timedelta(seconds=90),
    )
    negative(policy_changed, "component:x:y", BASE + timedelta(seconds=150))
    episode = policy_changed.episode("component:x:y")
    assert episode.status == "ONGOING"
    assert episode.qualification_policy_version == "v2"


def test_t8_gap_seconds_is_consumed_from_the_caller() -> None:
    instance = EpisodeTracker()
    qualified(instance, "component:x:y", BASE, "12.40")
    negative(
        instance,
        "component:x:y",
        BASE + timedelta(seconds=60),
        gap_seconds=120.0,
    )
    negative(
        instance,
        "component:x:y",
        BASE + timedelta(seconds=150),
        gap_seconds=120.0,
    )
    assert instance.episode("component:x:y").status == "ONGOING"

    negative(
        instance,
        "component:x:y",
        BASE + timedelta(seconds=180),
        gap_seconds=120.0,
    )
    closed = instance.episode("component:x:y")
    assert closed.status == "CLOSED"
    assert closed.close_reason == CLOSE_NO_QUALIFIED_OPPORTUNITY
    assert closed.closed_at == BASE + timedelta(seconds=180)


def test_t7_component_retired_closes_immediately() -> None:
    instance = EpisodeTracker()
    qualified(instance, "component:x:y", BASE, "12.40")
    negative(instance, "component:x:y", BASE + timedelta(seconds=60))
    retire_at = BASE + timedelta(seconds=480)
    instance.component_retired("component:x:y", now=retire_at)
    closed = instance.episode("component:x:y")
    assert closed.status == "CLOSED"
    assert closed.close_reason == CLOSE_COMPONENT_RETIRED
    assert closed.closed_at == retire_at
    # Retirement is final: later negatives never resurrect or re-close it.
    negative(instance, "component:x:y", retire_at + timedelta(seconds=600))
    assert instance.episode("component:x:y").close_reason == CLOSE_COMPONENT_RETIRED


def test_t6_qualified_after_close_reopens_with_new_id_and_reset_counters() -> None:
    instance = EpisodeTracker()
    qualified(instance, "component:x:y", BASE, "12.40", would_submit=True)
    first = instance.episode("component:x:y")
    instance.component_retired("component:x:y", now=BASE + timedelta(seconds=60))

    reopen_at = BASE + timedelta(seconds=1200)
    qualified(instance, "component:x:y", reopen_at, "7.70", would_submit=False)
    reopened = instance.episode("component:x:y")
    assert reopened.opportunity_episode_id != first.opportunity_episode_id
    assert reopened.episode_lineage_id == first.episode_lineage_id
    assert reopened.opened_at == reopen_at
    assert reopened.best_guaranteed_profit == Decimal("7.70")
    assert reopened.worst_guaranteed_profit == Decimal("7.70")
    assert reopened.would_submit_ready_seconds == 0.0
    assert reopened.would_submit_ready_since is None
    assert reopened.negative_close_started_at is None
    assert reopened.close_reason is None


def test_t5_episode_tracks_best_and_worst_guaranteed_profit() -> None:
    instance = EpisodeTracker()
    for at_seconds, profit in ((0, "12.40"), (30, "25.00"), (60, "3.10")):
        qualified(
            instance,
            "component:x:y",
            BASE + timedelta(seconds=at_seconds),
            profit,
        )
    episode = instance.episode("component:x:y")
    assert episode.best_guaranteed_profit == Decimal("25.00")
    assert episode.worst_guaranteed_profit == Decimal("3.10")


def test_t4_would_submit_accumulates_on_transitions_and_close() -> None:
    instance = EpisodeTracker()
    qualified(instance, "component:x:y", BASE, "12.40", would_submit=False)
    qualified(
        instance,
        "component:x:y",
        BASE + timedelta(seconds=30),
        "12.40",
        would_submit=True,
    )
    qualified(
        instance,
        "component:x:y",
        BASE + timedelta(seconds=90),
        "12.40",
        would_submit=False,
    )
    running = instance.episode("component:x:y")
    assert running.would_submit_ready_seconds == 60.0
    assert running.would_submit_ready_since is None

    qualified(
        instance,
        "component:x:y",
        BASE + timedelta(seconds=120),
        "12.40",
        would_submit=True,
    )
    instance.component_retired("component:x:y", now=BASE + timedelta(seconds=180))
    closed = instance.episode("component:x:y")
    assert closed.status == "CLOSED"
    assert closed.close_reason == CLOSE_COMPONENT_RETIRED
    assert closed.would_submit_ready_seconds == 120.0
    assert closed.would_submit_ready_since is None


def test_t2b_negative_close_exact_boundary_299_ongoing_300_closed() -> None:
    # The exact 4:59/5:00 boundary pair (issue #65 D4): t2 exercises 240s and
    # 300s after the window starts, never the one-second-short edge itself.
    instance = EpisodeTracker()
    qualified(instance, "component:x:y", BASE, "12.40")
    window_start = BASE + timedelta(seconds=60)
    negative(instance, "component:x:y", window_start)

    negative(
        instance, "component:x:y", window_start + timedelta(seconds=299)
    )
    assert instance.episode("component:x:y").status == "ONGOING"

    close_at = window_start + timedelta(seconds=300)
    negative(instance, "component:x:y", close_at)
    closed = instance.episode("component:x:y")
    assert closed.status == "CLOSED"
    assert closed.close_reason == CLOSE_NO_QUALIFIED_OPPORTUNITY
    assert closed.closed_at == close_at


def test_t3_unknown_resets_the_negative_close_timer() -> None:
    instance = EpisodeTracker()
    qualified(instance, "component:x:y", BASE, "12.40")
    negative(instance, "component:x:y", BASE + timedelta(seconds=60))
    instance.observe_unknown("component:x:y", now=BASE + timedelta(seconds=120))
    # UNKNOWN clears the window: unknown time never counts toward closing.
    assert instance.episode("component:x:y").negative_close_started_at is None

    # The next accepted negative restarts the window at its own timestamp.
    negative(instance, "component:x:y", BASE + timedelta(seconds=180))
    negative(instance, "component:x:y", BASE + timedelta(seconds=360))
    assert instance.episode("component:x:y").status == "ONGOING"

    negative(instance, "component:x:y", BASE + timedelta(seconds=421))
    # 421-180=241s is still under the 300s gap: ONGOING.
    assert instance.episode("component:x:y").status == "ONGOING"

    negative(instance, "component:x:y", BASE + timedelta(seconds=480))
    closed = instance.episode("component:x:y")
    assert closed.status == "CLOSED"
    assert closed.close_reason == CLOSE_NO_QUALIFIED_OPPORTUNITY


def test_t2_consecutive_fresh_negatives_close_after_gap_with_side_rows(
    tmp_path: Path,
) -> None:
    store = EpisodeStore(tmp_path)
    instance = EpisodeTracker(store=store)
    qualified(instance, "component:x:y", BASE, "12.40")
    episode_id = instance.episode("component:x:y").opportunity_episode_id
    negative(instance, "component:x:y", BASE + timedelta(seconds=60))
    negative(instance, "component:x:y", BASE + timedelta(seconds=240))
    running = instance.episode("component:x:y")
    assert running.status == "ONGOING"
    assert running.negative_close_started_at == BASE + timedelta(seconds=60)

    negative(instance, "component:x:y", BASE + timedelta(seconds=300))
    assert instance.episode("component:x:y").status == "ONGOING"

    close_at = BASE + timedelta(seconds=360)
    negative(instance, "component:x:y", close_at, fingerprint_value="sha256:neg-4")
    closed = instance.episode("component:x:y")
    assert closed.status == "CLOSED"
    assert closed.close_reason == CLOSE_NO_QUALIFIED_OPPORTUNITY
    assert closed.closed_at == close_at

    proofs = store.proofs_for_episode(episode_id)
    assert [str(row["proof_fingerprint"]) for row in proofs] == [
        "sha256:neg",
        "sha256:neg",
        "sha256:neg",
        "sha256:neg-4",
    ]
    assert all(row["generation"] == 1 for row in proofs)
    assert all(
        row["model_fingerprint"] == "sha256:model"
        and row["quote_fingerprint"] == "sha256:quote"
        and row["qualification_fingerprint"] == "sha256:qual"
        for row in proofs
    )


def test_t1_qualified_observation_opens_then_updates_one_episode() -> None:
    instance = EpisodeTracker()
    qualified(instance, "component:x:y", BASE, "12.40")
    episode = instance.episode("component:x:y")
    assert episode is not None
    assert episode.status == "ONGOING"
    assert episode.opportunity_episode_id
    episode_id = episode.opportunity_episode_id
    assert episode.opened_at == BASE
    assert episode.best_guaranteed_profit == Decimal("12.40")
    assert episode.worst_guaranteed_profit == Decimal("12.40")
    assert episode.would_submit_ready_seconds == 0.0
    assert episode.close_reason is None

    later = BASE + timedelta(seconds=30)
    qualified(instance, "component:x:y", later, "9.90")
    same = instance.episode("component:x:y")
    assert same is not None
    assert same.opportunity_episode_id == episode_id
    assert same.best_guaranteed_profit == Decimal("12.40")
    assert same.worst_guaranteed_profit == Decimal("9.90")
    assert same.last_seen_at == later


def test_t22_stale_and_unknown_ticks_persist_only_on_state_change(
    tmp_path: Path,
) -> None:
    store = CountingStore(tmp_path)
    instance = EpisodeTracker(store=store)
    qualified(instance, "component:x:y", BASE, "12.40")
    negative(instance, "component:x:y", BASE + timedelta(seconds=60))
    assert instance.episode("component:x:y").negative_close_started_at is not None
    saves_at_window_start = store.save_calls

    # A stale tick while the window runs IS a state change: exactly one save.
    stale_at = BASE + timedelta(seconds=90)
    instance.mark_quote_stale("component:x:y", now=stale_at)
    assert instance.episode("component:x:y").negative_close_started_at is None
    assert store.save_calls == saves_at_window_start + 1

    # Quiet stale/unknown ticks with nothing to clear never persist again
    # (a permanently stale open episode must not upsert every tick).
    instance.mark_quote_stale("component:x:y", now=BASE + timedelta(seconds=120))
    instance.observe_unknown("component:x:y", now=BASE + timedelta(seconds=150))
    episode = instance.episode("component:x:y")
    assert episode.status == "ONGOING"
    assert store.save_calls == saves_at_window_start + 1
    assert episode.updated_at == stale_at

    # The binding-mismatch branch obeys the same guard: with the window
    # already cleared, a mismatched negative persists nothing.
    negative(
        instance,
        "component:x:y",
        BASE + timedelta(seconds=180),
        binding_matches=False,
        fingerprint_value="sha256:other",
    )
    assert store.save_calls == saves_at_window_start + 1


def test_t20_generation_change_restarts_the_negative_window() -> None:
    instance = EpisodeTracker()
    qualified(instance, "component:x:y", BASE, "12.40")
    negative(instance, "component:x:y", BASE + timedelta(seconds=60))
    assert (
        instance.episode("component:x:y").negative_close_started_at
        == BASE + timedelta(seconds=60)
    )

    # A gen-2 negative (otherwise identical binding) must rebind the record
    # to generation 2, clear the gen-1 window, and restart it at its own
    # timestamp — the gen-1 window may never close the episode.
    rebind_at = BASE + timedelta(seconds=240)
    negative(instance, "component:x:y", rebind_at, generation=2)
    episode = instance.episode("component:x:y")
    assert episode.component_generation == 2
    assert episode.negative_close_started_at == rebind_at

    # The close gap now counts under gen 2 only: 240s < 300s stays open.
    negative(
        instance,
        "component:x:y",
        rebind_at + timedelta(seconds=240),
        generation=2,
    )
    assert instance.episode("component:x:y").status == "ONGOING"

    # A full 300s under gen 2 closes with the no-opportunity cause.
    close_at = rebind_at + timedelta(seconds=300)
    negative(instance, "component:x:y", close_at, generation=2)
    closed = instance.episode("component:x:y")
    assert closed.status == "CLOSED"
    assert closed.close_reason == CLOSE_NO_QUALIFIED_OPPORTUNITY
    assert closed.closed_at == close_at


def test_t21_policy_version_change_restarts_the_negative_window() -> None:
    instance = EpisodeTracker()
    qualified(instance, "component:x:y", BASE, "12.40")
    negative(instance, "component:x:y", BASE + timedelta(seconds=60))
    assert (
        instance.episode("component:x:y").negative_close_started_at
        == BASE + timedelta(seconds=60)
    )

    # A negative proven under qualification policy v2 must rebind the record
    # and restart the window at its own timestamp.
    rebind_at = BASE + timedelta(seconds=180)
    negative(
        instance,
        "component:x:y",
        rebind_at,
        qualification_policy_version="v2",
    )
    episode = instance.episode("component:x:y")
    assert episode.qualification_policy_version == "v2"
    assert episode.negative_close_started_at == rebind_at

    # 240s < 300s under v2 stays open; a full 300s under v2 closes.
    negative(
        instance,
        "component:x:y",
        rebind_at + timedelta(seconds=240),
        qualification_policy_version="v2",
    )
    assert instance.episode("component:x:y").status == "ONGOING"

    close_at = rebind_at + timedelta(seconds=300)
    negative(
        instance,
        "component:x:y",
        close_at,
        qualification_policy_version="v2",
    )
    closed = instance.episode("component:x:y")
    assert closed.status == "CLOSED"
    assert closed.close_reason == CLOSE_NO_QUALIFIED_OPPORTUNITY
    assert closed.closed_at == close_at
