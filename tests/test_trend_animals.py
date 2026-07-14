from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from open_trader.trend_animals import (
    TrendAnimalsClient,
    TrendAnimalsError,
    TrendAnimalsLookupError,
)


class FakeTransport:
    def __init__(self, responses: dict[str, object]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict[str, list[str]]]] = []

    def __call__(self, url: str, timeout: float) -> object:
        parsed = urlparse(url)
        endpoint = parsed.path.rsplit("/", 1)[-1]
        self.calls.append((endpoint, parse_qs(parsed.query)))
        return self.responses[endpoint]


def success(data: object) -> dict[str, object]:
    return {"code": "00000", "msg": "操作成功", "success": True, "data": data}


def test_paid_response_cache_uses_date_endpoint_and_sorted_params(
    tmp_path: Path,
) -> None:
    transport = FakeTransport(
        {
            "getComponentTicker": success(
                [
                    {
                        "tmId": 1,
                        "tickerSymbol": "600000.SH",
                        "asOfDate": "2026-07-14",
                    }
                ]
            )
        }
    )
    client = TrendAnimalsClient(
        api_key="secret-value", cache_dir=tmp_path, transport=transport
    )

    first = client.get_components(tm_id=622466, expected_date="2026-07-14")
    second = client.get_components(tm_id=622466, expected_date="2026-07-14")

    assert first == second
    assert len(transport.calls) == 1
    assert transport.calls[0] == (
        "getComponentTicker",
        {
            "apiKey": ["secret-value"],
            "tmId": ["622466"],
            "getAllBasicComponentsFlag": ["0"],
        },
    )
    cache_path = next((tmp_path / "responses").glob("*.json"))
    assert "secret-value" not in cache_path.read_text(encoding="utf-8")
    assert "secret-value" not in cache_path.name


def test_snapshot_cache_normalizes_id_and_field_order(tmp_path: Path) -> None:
    rows = [{"tmId": 7, "tickerSymbol": "600025.SH", "asOfDate": "2026-07-14"}]
    first_transport = FakeTransport({"getTickerSnapshot": success(rows)})
    TrendAnimalsClient(
        api_key="secret-value", cache_dir=tmp_path, transport=first_transport
    ).get_snapshots(
        tm_ids=[8, 7, 8],
        fields=("tickerSymbol", "asOfDate", "tmId", "asOfDate"),
        expected_date="2026-07-14",
    )
    second_transport = FakeTransport({})

    cached = TrendAnimalsClient(
        api_key="different-secret", cache_dir=tmp_path, transport=second_transport
    ).get_snapshots(
        tm_ids=[7, 8],
        fields=("tmId", "tickerSymbol", "asOfDate"),
        expected_date="2026-07-14",
    )

    assert cached == rows
    assert first_transport.calls[0][1]["tmIds"] == ["7,8"]
    assert first_transport.calls[0][1]["fields"] == ["asOfDate,tickerSymbol,tmId"]
    assert second_transport.calls == []


def test_search_exact_symbol_caches_tm_id_without_guessing(tmp_path: Path) -> None:
    transport = FakeTransport(
        {
            "searchTicker": success(
                [
                    {"tmId": 7, "tickerSymbol": "600025.SH"},
                    {"tmId": 8, "tickerSymbol": "600026.SH"},
                ]
            )
        }
    )
    client = TrendAnimalsClient(
        api_key="secret-value", cache_dir=tmp_path, transport=transport
    )

    assert client.search_exact_symbol("600025") == 7
    assert client.search_exact_symbol("600025.SH") == 7
    assert len(transport.calls) == 1
    assert transport.calls[0][1] == {
        "apiKey": ["secret-value"],
        "keyword": ["600025"],
    }
    assert (tmp_path / "symbols" / "600025.json").read_text(encoding="utf-8")


def test_snapshot_rejects_wrong_data_date_without_caching(tmp_path: Path) -> None:
    transport = FakeTransport(
        {
            "getTickerSnapshot": success(
                [
                    {
                        "tmId": 7,
                        "tickerSymbol": "600025.SH",
                        "asOfDate": "2026-07-13",
                    }
                ]
            )
        }
    )
    client = TrendAnimalsClient(
        api_key="secret-value", cache_dir=tmp_path, transport=transport
    )

    with pytest.raises(TrendAnimalsError, match="expected 2026-07-14") as exc_info:
        client.get_snapshots(
            tm_ids=[7],
            fields=("tmId", "tickerSymbol", "asOfDate"),
            expected_date="2026-07-14",
        )

    assert "secret-value" not in str(exc_info.value)
    assert not (tmp_path / "responses").exists()


@pytest.mark.parametrize(
    "payload",
    [
        {"code": "99999", "msg": "failed", "success": False, "data": []},
        {"code": "00000", "msg": "wrong flag", "success": 1, "data": []},
        ["not", "a", "response", "mapping"],
    ],
)
def test_response_envelope_must_be_a_success(payload: object, tmp_path: Path) -> None:
    client = TrendAnimalsClient(
        api_key="secret-value",
        cache_dir=tmp_path,
        transport=FakeTransport({"getUpdateStatus": payload}),
    )

    with pytest.raises(TrendAnimalsError, match="getUpdateStatus"):
        client.get_update_status()


def test_response_data_must_be_a_list_of_string_keyed_mappings(tmp_path: Path) -> None:
    for data in ({"row": 1}, ["not a mapping"], [{1: "not a string key"}]):
        client = TrendAnimalsClient(
            api_key="secret-value",
            cache_dir=tmp_path,
            transport=FakeTransport({"getUpdateStatus": success(data)}),
        )

        with pytest.raises(TrendAnimalsError, match="getUpdateStatus"):
            client.get_update_status()


def test_snapshot_billing_rejects_missing_data_field(tmp_path: Path) -> None:
    transport = FakeTransport(
        {
            "getSnapshotColumnBilling": {
                "code": "00000",
                "msg": "操作成功",
                "success": True,
            }
        }
    )
    client = TrendAnimalsClient(
        api_key="secret-value", cache_dir=tmp_path, transport=transport
    )

    with pytest.raises(TrendAnimalsError, match="getSnapshotColumnBilling"):
        client.get_snapshot_billing()


def test_account_balance_requires_one_summary_mapping(tmp_path: Path) -> None:
    client = TrendAnimalsClient(
        api_key="secret-value",
        cache_dir=tmp_path,
        transport=FakeTransport({"getAccountBalance": success([])}),
    )

    with pytest.raises(TrendAnimalsError, match="no unique summary"):
        client.get_account_balance()


def test_exact_symbol_miss_has_distinct_error(tmp_path: Path) -> None:
    client = TrendAnimalsClient(
        api_key="secret-value",
        cache_dir=tmp_path,
        transport=FakeTransport(
            {
                "searchTicker": success(
                    [{"tmId": 8, "tickerSymbol": "600026.SH"}]
                )
            }
        ),
    )

    with pytest.raises(TrendAnimalsLookupError, match="no unique exact match"):
        client.search_exact_symbol("600025")


@pytest.mark.parametrize(
    "row",
    [
        {"tmId": "7", "tickerSymbol": "600025.SH"},
        {"tmId": True, "tickerSymbol": "600025.SH"},
        {"tmId": 7, "tickerSymbol": 600025},
    ],
)
def test_exact_symbol_rejects_invalid_match_values(
    row: dict[str, object], tmp_path: Path
) -> None:
    client = TrendAnimalsClient(
        api_key="secret-value",
        cache_dir=tmp_path,
        transport=FakeTransport({"searchTicker": success([row])}),
    )

    with pytest.raises(TrendAnimalsError, match="searchTicker"):
        client.search_exact_symbol("600025")


def test_corrupt_response_cache_fails_without_repurchase(tmp_path: Path) -> None:
    transport = FakeTransport(
        {
            "getComponentTicker": success(
                [{"tmId": 1, "asOfDate": "2026-07-14"}]
            )
        }
    )
    client = TrendAnimalsClient(
        api_key="secret-value", cache_dir=tmp_path, transport=transport
    )
    client.get_components(tm_id=622466, expected_date="2026-07-14")
    next((tmp_path / "responses").glob("*.json")).write_text(
        "not-json", encoding="utf-8"
    )

    with pytest.raises(TrendAnimalsError, match="cache"):
        client.get_components(tm_id=622466, expected_date="2026-07-14")

    assert len(transport.calls) == 1


def test_corrupt_symbol_cache_fails_without_search(tmp_path: Path) -> None:
    cache_path = tmp_path / "symbols" / "600025.json"
    cache_path.parent.mkdir(parents=True)
    cache_path.write_text('{"symbol": "600025", "tmId": "7"}', encoding="utf-8")
    transport = FakeTransport({})
    client = TrendAnimalsClient(
        api_key="secret-value", cache_dir=tmp_path, transport=transport
    )

    with pytest.raises(TrendAnimalsError, match="cache"):
        client.search_exact_symbol("600025")

    assert transport.calls == []


def test_transport_error_redacts_api_key(tmp_path: Path) -> None:
    def failing_transport(url: str, timeout: float) -> object:
        raise RuntimeError(f"request failed: {url}")

    client = TrendAnimalsClient(
        api_key="secret-value", cache_dir=tmp_path, transport=failing_transport
    )

    with pytest.raises(TrendAnimalsError, match="getUpdateStatus") as exc_info:
        client.get_update_status()

    assert "secret-value" not in str(exc_info.value)


def test_paid_response_that_echoes_secret_is_not_cached(tmp_path: Path) -> None:
    client = TrendAnimalsClient(
        api_key="secret-value",
        cache_dir=tmp_path,
        transport=FakeTransport(
            {
                "getComponentTicker": success(
                    [{"asOfDate": "2026-07-14", "echo": "secret-value"}]
                )
            }
        ),
    )

    with pytest.raises(TrendAnimalsError, match="unsafe") as exc_info:
        client.get_components(tm_id=622466, expected_date="2026-07-14")

    assert "secret-value" not in str(exc_info.value)
    assert not (tmp_path / "responses").exists()


def test_cached_response_that_contains_current_secret_is_rejected(tmp_path: Path) -> None:
    rows = [
        {
            "asOfDate": "2026-07-14",
            "tickerSymbol": "600025.SH",
            "echo": "secret-value",
        }
    ]
    TrendAnimalsClient(
        api_key="first-secret",
        cache_dir=tmp_path,
        transport=FakeTransport({"getComponentTicker": success(rows)}),
    ).get_components(tm_id=622466, expected_date="2026-07-14")
    transport = FakeTransport({})
    client = TrendAnimalsClient(
        api_key="secret-value", cache_dir=tmp_path, transport=transport
    )

    with pytest.raises(TrendAnimalsError, match="unsafe") as exc_info:
        client.get_components(tm_id=622466, expected_date="2026-07-14")

    assert "secret-value" not in str(exc_info.value)
    assert transport.calls == []


def test_secret_shaped_inputs_never_reach_paths_or_errors(tmp_path: Path) -> None:
    client = TrendAnimalsClient(
        api_key="600025", cache_dir=tmp_path, transport=FakeTransport({})
    )

    with pytest.raises(ValueError) as exc_info:
        client.search_exact_symbol("600025")

    assert "600025" not in str(exc_info.value)
    assert list(tmp_path.rglob("*")) == []


def test_expected_date_is_redacted_if_it_matches_secret(tmp_path: Path) -> None:
    client = TrendAnimalsClient(
        api_key="2026-07-14",
        cache_dir=tmp_path,
        transport=FakeTransport(
            {
                "getComponentTicker": success(
                    [{"tmId": 1, "asOfDate": "2026-07-13"}]
                )
            }
        ),
    )

    with pytest.raises(TrendAnimalsError) as exc_info:
        client.get_components(tm_id=622466, expected_date="2026-07-14")

    assert "2026-07-14" not in str(exc_info.value)


@pytest.mark.parametrize(
    ("tm_ids", "fields"),
    [
        ([7, "8"], ["tmId"]),
        ([[7]], ["tmId"]),
        ([7], ["tmId", 8]),
        ([7], [["tmId"]]),
        ([], ["tmId"]),
        ([7], []),
    ],
)
def test_snapshot_inputs_are_validated_before_building_a_paid_request(
    tm_ids: object, fields: object, tmp_path: Path
) -> None:
    transport = FakeTransport({})
    client = TrendAnimalsClient(
        api_key="secret-value", cache_dir=tmp_path, transport=transport
    )

    with pytest.raises(ValueError, match="tm_ids|fields"):
        client.get_snapshots(
            tm_ids=tm_ids,  # type: ignore[arg-type]
            fields=fields,  # type: ignore[arg-type]
            expected_date="2026-07-14",
        )

    assert transport.calls == []


def test_blank_api_key_is_rejected_without_echoing_it(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="TREND_ANIMALS_API_KEY is required"):
        TrendAnimalsClient(api_key="   ", cache_dir=tmp_path)
