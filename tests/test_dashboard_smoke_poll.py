"""Offline contract tests for the Production Smoke polling barrier (no browser)."""
from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(("split", "last_response"), [
    ("split", "venues"), ("split", "identity"), ("standalone", "venues"),
])
@pytest.mark.parametrize("fault", ["none", "nleg-read", "invalid-json"])
def test_smoke_poll_waits_for_request_and_dashboard_completion(split, last_response, fault):
    result = subprocess.run(
        ["node", "tests/support/dashboard-smoke-poll-fixture.cjs", split, last_response, fault],
        cwd=ROOT, capture_output=True, text=True, timeout=10,
    )
    if fault != "none":
        assert result.returncode != 0
        diagnostic = "paused N-leg emitted a read" if fault == "nleg-read" else "paused N-leg state was not applied"
        assert diagnostic in result.stderr, result.stderr
    else:
        assert result.returncode == 0, result.stderr
        assert "poll barrier applied" in result.stdout


def test_production_smoke_uses_barrier_before_unchanged_negative_assertions():
    smoke = (ROOT / "tests/e2e/production-smoke.spec.ts").read_text()
    assert "import { waitForPredictionPoll } from './prediction-poll-barrier.cjs';" in smoke
    barrier = smoke.index("await waitForPredictionPoll(page, split);")
    applied = smoke.index("expect(await page.evaluate(() => state.predictionMarket.nLegStatus)).toBe('paused');", barrier)
    paused = smoke.index("await expect(page.getByRole('status').filter({ hasText: '多腿套利已暂停' })).toBeVisible();", barrier)
    assert barrier < applied < paused < smoke.index("expect(nLegReadRequests).toEqual([]);", barrier)
    assert barrier < smoke.index("expect(pendingNLegReads.size).toBe(0);", barrier)


@pytest.mark.parametrize("mutation", ["ignore-identity", "ignore-application"])
def test_smoke_poll_regression_rejects_incomplete_barriers(tmp_path, mutation):
    source = (ROOT / "tests/e2e/prediction-poll-barrier.cjs").read_text()
    if mutation == "ignore-identity":
        original = "&& (!splitMode || !state.predictionMarket.executionRequestInFlight)"
        assert original in source
        source = source.replace(original, "")
        diagnostic = "poll observers armed before initial Air identity applied"
    else:
        original = "await page.waitForFunction(readsApplied, split);"
        position = source.rindex(original)
        source = source[:position] + source[position:].replace(original, "", 1)
        diagnostic = "transport completion is not Dashboard application"
    helper = tmp_path / "incomplete-poll-barrier.cjs"
    helper.write_text(source)
    result = subprocess.run(
        ["node", "tests/support/dashboard-smoke-poll-fixture.cjs", "split", "identity", "none", str(helper)],
        cwd=ROOT, capture_output=True, text=True, timeout=10,
    )
    assert result.returncode != 0
    assert diagnostic in result.stderr, result.stderr
