"""Small offline PR-A comparison; run RSS and tracemalloc in separate processes.

PYTHONPATH=src:tests python tests/lp_metadata_lifetime_probe.py --mode rss
Use --source with an exported polymarket_trading.py to compare an exact baseline.
This measures synthetic metadata, not production capacity or cgroup usage.
"""
import argparse
from datetime import UTC, datetime
import gc
import hashlib
import importlib.metadata
import importlib.util
import json
from pathlib import Path
import resource
import socket
import sys
import threading
import tracemalloc
import weakref

import httpx
from polymarket.models.gamma.market import Market

from test_polymarket_trading import (
    SIGNER, WALLET, _lp_market_payload, _lp_market_page_response, _lp_mock_public_client,
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("rss", "tracemalloc"), required=True)
    parser.add_argument("--source", type=Path, default=Path("src/open_trader/polymarket_trading.py"))
    parser.add_argument("--count", type=int, default=500)
    args = parser.parse_args()
    assert 0 < args.count <= 1500
    source = args.source.read_bytes()
    spec = importlib.util.spec_from_file_location("open_trader._lp_metadata_probe", args.source)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    def deny_network(*args, **kwargs):
        raise AssertionError("offline probe forbids socket connections")

    socket.socket.connect = deny_network
    now = datetime(2026, 10, 6, tzinfo=UTC)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now

    module.datetime = Clock
    conditions = tuple(f"0x{index:064x}" for index in range(args.count))
    requested = set(conditions)
    refs, queries = [], []
    stage = {}
    lock = threading.Lock()
    parse = Market.parse_response

    def observed_parse(cls, payload):
        market = parse(payload)
        if market.condition_id in requested:
            with lock:
                refs.append(weakref.ref(market))
        return market

    Market.parse_response = classmethod(observed_parse)

    def payload(condition):
        return dict(_lp_market_payload(condition), acceptingOrders=True, closed=False,
                    orderMinSize="20", orderPriceMinTickSize="0.01", feesEnabled=False,
                    rewardsMinSize="20", rewardsMaxSpread=3.5, oneDayPriceChange="0.12",
                    description="Settlement follows the published official result. " * 4,
                    volume="123456.78", liquidity="4567.89",
                    tags=[{"id": "1", "slug": "sport", "label": "Sport"}],
                    events=[{"id": str(int(condition, 16) + 1), "slug": "reference"}])

    def handler(request):
        with lock:
            queries.append((request.url.path, tuple(sorted(request.url.params.multi_items()))))
        if request.url.path == "/markets/keyset":
            return _lp_market_page_response(request, (
                payload(value) for value in request.url.params.get_list("condition_ids")
            ))
        assert request.url.path == "/events/keyset"
        with lock:
            if not stage:
                gc.collect()
                stage["live_market_models"] = sum(ref() is not None for ref in refs)
                if args.mode == "tracemalloc":
                    stage["traced_current_mib"] = tracemalloc.get_traced_memory()[0] / 2**20
                else:
                    stage["rss_high_water_mib"] = rss_peak_mib()
        return httpx.Response(200, json={"events": [{
            "id": event_id, "slug": f"event-{event_id}", "ended": False,
            "startTime": "2026-10-06T01:00:00Z",
            "markets": [payload(f"0x{10000 + index:064x}") for index in range(5)],
        } for event_id in request.url.params.get_list("id")]}, request=request)

    def rss_peak_mib():
        value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return value / (2**20 if sys.platform == "darwin" else 1024)

    if args.mode == "tracemalloc":
        tracemalloc.start()
    adapter = module.PolymarketTradingClient(
        module.TradingConfig(SIGNER, WALLET), object(),
        public_client_factory=lambda: _lp_mock_public_client(handler),
    )
    try:
        result = adapter.lp_market_metadata_batch(conditions)
        assert result["state"] == "known", result["state"]
        assert tuple(result["markets"]) == conditions
        assert all(len(row) == 27 for row in result["markets"].values())
        assert result["failed_ids"] == {}
        assert result["deferred_ids"] == result["confirmed_absent_ids"] == ()
        gc.collect()
        metrics = {"stage": stage, "returned_live_market_models": sum(ref() is not None for ref in refs)}
        if args.mode == "tracemalloc":
            current, peak = tracemalloc.get_traced_memory()
            metrics.update(returned_traced_mib=current / 2**20, traced_peak_mib=peak / 2**20)
            tracemalloc.stop()
        else:
            metrics["rss_peak_mib"] = rss_peak_mib()
        encoded = json.dumps(result, default=str, sort_keys=True, separators=(",", ":")).encode()
        print(json.dumps(dict(
            mode=args.mode, count=args.count, source_sha256=hashlib.sha256(source).hexdigest(),
            business_sha256=hashlib.sha256(encoded).hexdigest(),
            request_sha256=hashlib.sha256(json.dumps(sorted(queries)).encode()).hexdigest(),
            request_count=len(queries), python=sys.version.split()[0],
            sdk=importlib.metadata.version("polymarket-client"), **metrics,
        ), sort_keys=True))
    finally:
        adapter.close()


if __name__ == "__main__":
    main()
