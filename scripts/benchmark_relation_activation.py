#!/usr/bin/env python3
"""Issue #98 slice S5: manual benchmark for incremental relation activation.

Builds three tiers (100 / 1k / 10k) of already-ACTIVE, mutually independent
small-component relations in a throwaway SQLite database (``tempfile``,
removed on exit). For each tier it prints the median single-approve latency
over at least 20 fresh samples plus the p95 tail; the 10k tier additionally
measures one ``approve_many`` batch over 10,000 PENDING relations (total wall
time). All metrics are printed; the exit code is 1 when any measured metric
exceeds its threshold (single-approve median > 50ms at the 10k ACTIVE state,
10k batch total > 60s) and 0 otherwise. ``--seed`` and ``--sizes`` make the
run reproducible/parameterizable; defaults are the three tiers above.

The ACTIVE baselines are seeded through the v2 batch activation seam
(``activate_many`` self-ingests each payload in the same transaction), and
the 10k PENDING set is ingested before the baseline so the per-write-
transaction state reload stays cheap; the judged batch itself is the S3
facade ``approve_many``.

Usage:
    python scripts/benchmark_relation_activation.py
    python scripts/benchmark_relation_activation.py --seed 7 --sizes 100 10000
"""

from __future__ import annotations

import argparse
import math
import os
import random
import sqlite3
import statistics
import sys
import tempfile
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_REPO, "src"))
sys.path.insert(0, os.path.join(_REPO, "tests"))

from open_trader.relation_catalog import RelationCatalog  # noqa: E402
from open_trader.relation_catalog_v2 import (  # noqa: E402
    RelationCatalogV2,
    SqliteCatalogStore,
)
from test_relation_catalog import compiled_problem, compiled_relation_discovery  # noqa: E402
from test_relation_catalog_v2 import _endpoint, _payload  # noqa: E402

AS_OF = "2026-08-15T00:00:00Z"
RELEASE = "2026-12-31T17:00:00Z"
ACTOR = "auditor"
GIT_SHA = "a" * 40

SINGLE_MEDIAN_THRESHOLD_MS = 50.0  # 10k ACTIVE state
BATCH_10K_THRESHOLD_S = 60.0
MIN_SAMPLES = 20
SEED_BATCH = 1_000
TIER_10K = 10_000


def _fast_connection(store: SqliteCatalogStore) -> sqlite3.Connection:
    """Throwaway temp DB only: skip fsync on the store's thread-local
    connections. The pragma is per-connection and may only change outside a
    transaction, so an in-transaction re-run is ignored."""
    connection = _original_connection(store)
    try:
        connection.execute("PRAGMA synchronous=OFF")
    except sqlite3.OperationalError:
        pass
    return connection


_original_connection = SqliteCatalogStore._connection
SqliteCatalogStore._connection = _fast_connection


def relation(prefix: str, index: int) -> dict[str, object]:
    """One mutually independent small v2 payload over two fresh contracts
    with a per-contract rule (unique problem observation keys), so the
    contract index, the observation-key index and the event gate stay
    disjoint from every other relation."""
    contracts = [f"{prefix}-{index}-a", f"{prefix}-{index}-b"]
    return _payload(
        relation_type="EXACTLY_ONE",
        endpoints=[
            dict(_endpoint(contract_id=contract, event_identity_basis="event-a"))
            for contract in contracts
        ],
        problem=compiled_problem(
            contracts,
            {contract: "BUY_YES" for contract in contracts},
            as_of=AS_OF,
            release_at=RELEASE,
            rule={contract: f"rule-{contract}" for contract in contracts},
        ),
    )


def discovery(prefix: str, index: int) -> dict[str, object]:
    """The facade discovery codec for the same independent relation."""
    contracts = [f"{prefix}-{index}-a", f"{prefix}-{index}-b"]
    return compiled_relation_discovery(
        contracts,
        {contract: "BUY_YES" for contract in contracts},
        relation_type="EXACTLY_ONE",
        as_of=AS_OF,
        release=RELEASE,
        rule={contract: f"rule-{contract}" for contract in contracts},
    )


def _seed_baseline(catalog: RelationCatalog, size: int) -> float:
    """Publish ``size`` ACTIVE relations via the v2 batch activation seam;
    returns the seed wall time in seconds."""
    seeder = RelationCatalogV2(SqliteCatalogStore(str(catalog.path)))
    started = time.perf_counter()
    for batch in range(size // SEED_BATCH):
        payloads = [
            relation("seed", batch * SEED_BATCH + index)
            for index in range(SEED_BATCH)
        ]
        result = seeder.activate_many(payloads, actor=ACTOR, git_sha=GIT_SHA)
        if result["status"] != "ACTIVE" or any(
            entry["status"] != "APPROVED" for entry in result["results"].values()
        ):
            raise RuntimeError(f"seed batch {batch} did not publish cleanly")
    return time.perf_counter() - started


def _single_approve_samples(
    catalog: RelationCatalog, size: int, rng: random.Random
) -> list[float]:
    """Median/p95 source: at least ``MIN_SAMPLES`` fresh single approves
    against ``size`` ACTIVE members; returns per-call wall times in ms."""
    times: list[float] = []
    offset = rng.randrange(1_000_000)
    for index in range(MIN_SAMPLES):
        version_id = catalog.ingest(discovery("single", offset + index))["version_id"]
        started = time.perf_counter()
        catalog.approve(version_id, {"version_id": version_id}, actor=ACTOR, git_sha=GIT_SHA)
        times.append((time.perf_counter() - started) * 1000)
    return times


def _batch_10k(catalog: RelationCatalog, pending_ids: list[str]) -> float:
    """One ``approve_many`` batch over 10,000 PENDING relations against the
    10k ACTIVE baseline; returns the batch total wall time in seconds (the
    PENDING ingests are counted separately as setup, not in the total)."""
    started = time.perf_counter()
    outcome = catalog.approve_many(
        [{"version_id": version_id} for version_id in pending_ids],
        actor=ACTOR,
        git_sha=GIT_SHA,
    )
    elapsed = time.perf_counter() - started
    if outcome["counts"] != {
        "total": TIER_10K,
        "active": TIER_10K,
        "blocked": 0,
        "error": 0,
    }:
        raise RuntimeError(f"10k batch did not activate cleanly: {outcome['counts']}")
    return elapsed


def _run_tier(data_dir: str, size: int, rng: random.Random) -> dict[str, float]:
    catalog = RelationCatalog(data_dir)
    # The 10k PENDING set is ingested while the versions/latest tables are
    # still small: SqliteCatalogStore reloads the version id set and the
    # latest/approved tables per write transaction, so ingesting after the
    # baseline seed would cost ~10x more setup time.
    pending_ids: list[str] = []
    if size >= TIER_10K:
        for index in range(TIER_10K):
            pending_ids.append(catalog.ingest(discovery("pending", index))["version_id"])
    seed_s = _seed_baseline(catalog, size)
    samples = _single_approve_samples(catalog, size, rng)
    row = {
        "seed_s": seed_s,
        "single_median_ms": statistics.median(samples),
        "single_p95_ms": sorted(samples)[math.ceil(len(samples) * 0.95) - 1],
    }
    if size >= TIER_10K:
        row["batch_10k_s"] = _batch_10k(catalog, pending_ids)
    return row


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Benchmark relation catalog incremental activation"
    )
    parser.add_argument("--seed", type=int, default=98, help="random seed (default 98)")
    parser.add_argument(
        "--sizes",
        type=int,
        nargs="+",
        default=[100, 1_000, TIER_10K],
        help="baseline sizes to benchmark (default 100 1000 10000)",
    )
    args = parser.parse_args(argv)
    rng = random.Random(args.seed)

    failures: list[str] = []
    print(f"benchmark_relation_activation seed={args.seed} sizes={args.sizes}")
    with tempfile.TemporaryDirectory(prefix="benchmark_relation_activation_") as tmp:
        for size in args.sizes:
            tier_dir = os.path.join(tmp, f"tier-{size}")
            row = _run_tier(tier_dir, size, rng)
            print(
                f"tier={size:>6}  seed_s={row['seed_s']:.2f}  "
                f"single_approve_median_ms={row['single_median_ms']:.2f}  "
                f"single_approve_p95_ms={row['single_p95_ms']:.2f}"
                + (
                    f"  approve_many_10k_s={row['batch_10k_s']:.2f}"
                    if "batch_10k_s" in row
                    else ""
                )
            )
            if row["single_median_ms"] > SINGLE_MEDIAN_THRESHOLD_MS:
                failures.append(
                    f"tier {size}: single-approve median {row['single_median_ms']:.1f}ms "
                    f"> {SINGLE_MEDIAN_THRESHOLD_MS:.0f}ms"
                )
            if size >= TIER_10K and row["batch_10k_s"] > BATCH_10K_THRESHOLD_S:
                failures.append(
                    f"tier {size}: approve_many 10k total {row['batch_10k_s']:.1f}s "
                    f"> {BATCH_10K_THRESHOLD_S:.0f}s"
                )
    if failures:
        print("THRESHOLD EXCEEDED:")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("all metrics within thresholds")
    return 0


if __name__ == "__main__":
    sys.exit(main())
