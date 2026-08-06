# Predict.fun / Polymarket condition-ID mapping

Date: 2026-08-02  
Scope: first-party Predict.fun and Polymarket documentation and anonymous, read-only APIs only. No orders, authentication, wallets, or secrets.

## Findings

Predict exposes three different identifiers on a market and they are not interchangeable:

| Field | Supported meaning | Correct use |
| --- | --- | --- |
| `id` | Predict's numeric API market ID. Predict's `GET /v1/markets/{id}` route accepts this value. | Predict REST paths such as market detail and order-book lookup. |
| `conditionId` | Predict's own on-chain condition hash. Predict's official order guide passes this hash to the Predict SDK's `redeemPositions` and `mergePositions` operations; Predict deploys its own Conditional Tokens contracts on BNB Chain. | Predict position/CTF operations. Do **not** use it as a Polymarket market identifier. |
| `polymarketConditionIds` | A required `string[]` field distinct from Predict's `conditionId`. Its elements are the values to test as Polymarket condition IDs. | Candidate external references for condition-ID lookup against Polymarket. Iterate the array; do not treat the array itself as one ID. |

Predict's public schema gives `polymarketConditionIds` a type but no prose contract for cardinality, ordering, completeness, stability, outcome alignment, or rule equivalence. Therefore an empty array means only "no Polymarket condition ID supplied in this response," and one or more values are explicit external references—not proof that the Predict and Polymarket markets have identical resolution rules. Each returned Polymarket market still needs question, outcomes, dates, and resolution-rule comparison before it can be treated as equivalent.

Sources:

- [Predict Market schema](https://dev.predict.fun/market-14037477d0) — publishes separate `conditionId: string` and `polymarketConditionIds: string[]` fields.
- [Predict get-markets API](https://dev.predict.fun/get-markets-25326905e0) and [get-market-by-ID API](https://dev.predict.fun/get-market-by-id-25552989e0) — the list/detail response shape and numeric `{id}` path.
- [Predict order/position guide](https://dev.predict.fun/how-to-create-or-cancel-orders-679306m0) — calls `conditionId` a hash and uses it for Predict SDK redemption and merging.
- [Predict deployed contracts](https://dev.predict.fun/-deployed-contracts-1860295m0) — Predict's Conditional Tokens deployments on BNB Mainnet and BNB Testnet.

## Supported Polymarket lookup by condition ID

For a value from Predict's `polymarketConditionIds`, the direct full CLOB-market lookup is:

```text
GET https://clob.polymarket.com/markets/{condition_id}
```

Polymarket's public client documents this operation as `getMarket(conditionId)`. The response includes `condition_id`, market question/status, and outcome token IDs, so the caller can assert that the returned `condition_id` exactly equals the requested value.

Polymarket also documents the newer compact CLOB-parameter route:

```text
GET https://clob.polymarket.com/clob-markets/{condition_id}
```

It returns token, tick-size, fee, rewards, and related CLOB parameters. It is useful as an additional existence/tradability check, but its compact response does not echo the condition ID.

For Gamma metadata, condition ID is a repeated **query filter**, not the `GET /markets/{id}` path parameter. Query both open and closed markets because Gamma defaults `closed` to `false`:

```text
GET https://gamma-api.polymarket.com/markets?closed=false&condition_ids={id_1}&condition_ids={id_2}
GET https://gamma-api.polymarket.com/markets?closed=true&condition_ids={id_1}&condition_ids={id_2}
```

Gamma's `GET /markets/{id}` path expects the numeric Gamma market ID. A successful empty result from both Gamma queries is not proof that the condition is invalid; fall back to the CLOB condition-ID lookup.

Sources:

- [Polymarket public client methods](https://docs.polymarket.com/trading/clients/public) — `getMarket(conditionId)` retrieves one market by condition ID.
- [Polymarket CLOB market-info route](https://docs.polymarket.com/api-reference/markets/get-clob-market-info) — documented `GET /clob-markets/{condition_id}` route.
- [Polymarket Gamma list-markets route](https://docs.polymarket.com/api-reference/markets/list-markets) — documents `condition_ids` as a list query parameter.
- [Polymarket Gamma get-market-by-ID route](https://docs.polymarket.com/api-reference/markets/get-market-by-id) — documents `{id}` as an integer.
- [Polymarket market-data model](https://docs.polymarket.com/market-data/overview) — a Polymarket market maps to its own condition ID and outcome token IDs.

## Reproducible read-only verification

Requirements: `curl` and `jq`. The Predict testnet market-list endpoint is used so no API key is needed. The procedure only performs GET requests.

1. Find a Predict market that supplies at least one Polymarket condition ID:

```sh
curl --fail-with-body --silent --show-error --max-time 20 \
  'https://api-testnet.predict.fun/v1/markets?first=100&status=OPEN' \
  | jq -r '
      .data[]
      | select((.polymarketConditionIds // []) | length > 0)
      | [
          (.id | tostring),
          .conditionId,
          .polymarketConditionIds[0],
          (.question // .title)
        ]
      | @tsv
    ' \
  | sed -n '1p'
```

The columns are `predict_id`, `predict_condition_id`, `polymarket_condition_id`, and the Predict question/title. If the first page has no mapped market, follow Predict's returned cursor with the documented `after` parameter and repeat.

2. Assign the first three values from one row, keeping the two condition IDs visibly separate:

```sh
PREDICT_ID='<numeric Predict id>'
PREDICT_CONDITION_ID='<Predict conditionId>'
POLYMARKET_CONDITION_ID='<one polymarketConditionIds element>'
```

3. Re-fetch the Predict record by its numeric API ID and assert both fields independently:

```sh
curl --fail-with-body --silent --show-error --max-time 20 \
  "https://api-testnet.predict.fun/v1/markets/$PREDICT_ID" \
  | jq --arg predict "$PREDICT_CONDITION_ID" \
       --arg poly "$POLYMARKET_CONDITION_ID" '
      .data
      | {
          predict_condition_matches: (.conditionId == $predict),
          polymarket_id_is_listed: (.polymarketConditionIds | index($poly) != null),
          id: .id,
          conditionId: .conditionId,
          polymarketConditionIds: .polymarketConditionIds,
          question: (.question // .title)
        }
    '
```

4. Resolve the external ID through Polymarket's condition-ID route and assert exact identity:

```sh
curl --fail-with-body --silent --show-error --max-time 20 \
  "https://clob.polymarket.com/markets/$POLYMARKET_CONDITION_ID" \
  | jq --arg expected "$POLYMARKET_CONDITION_ID" '
      {
        condition_matches: (.condition_id | ascii_downcase) == ($expected | ascii_downcase),
        condition_id,
        question,
        active,
        closed,
        accepting_orders,
        outcomes: [.tokens[] | {outcome, token_id}]
      }
    '
```

5. Query Gamma separately for open and closed markets. Repeat `condition_ids` once per external reference when checking a batch:

```sh
for CLOSED in false true; do
  curl --fail-with-body --silent --show-error --max-time 20 \
    --get 'https://gamma-api.polymarket.com/markets' \
    --data-urlencode "closed=$CLOSED" \
    --data-urlencode "condition_ids=$POLYMARKET_CONDITION_ID" \
    | jq --arg expected "$POLYMARKET_CONDITION_ID" '
        map({
          condition_matches: (.conditionId | ascii_downcase) == ($expected | ascii_downcase),
          id,
          conditionId,
          question,
          outcomes,
          active,
          closed
        })
      '
done
```

For a batch, add another `--data-urlencode "condition_ids=$ID"` argument for every ID. Do not send one comma-separated value.

6. For IDs absent from both Gamma responses, use the direct CLOB route from step 4. Optionally fetch compact CLOB parameters too:

```sh
curl --fail-with-body --silent --show-error --max-time 20 \
  "https://clob.polymarket.com/clob-markets/$POLYMARKET_CONDITION_ID" \
  | jq '{tokens: .t, minimum_order_size: .mos, minimum_tick_size: .mts}'
```

Pass criteria:

- Predict detail returns the same `conditionId` as the selected Predict list row.
- The selected external ID remains present in Predict's `polymarketConditionIds` array.
- Gamma returns one exact `conditionId` match from either the open or closed query, or Polymarket CLOB returns HTTP 200 and `condition_matches: true` for that external ID.
- Predict `conditionId` and the selected Polymarket condition ID are retained as venue-qualified, separate values even if their formats are both 32-byte hex hashes.
- Any cross-venue equivalence decision is deferred until the questions, outcomes, time boundaries, and resolution rules are compared.

## Empirical live results

Parent-agent read-only run on 2026-08-02:

- Predict source: `GET https://api-testnet.predict.fun/v1/markets?first=100&status=OPEN`, first 100 markets.
- Explicit mapping coverage: 59/100 markets had non-empty `polymarketConditionIds`; the arrays supplied 59 unique external references. The other 41/100 had no explicit mapping.
- Identifier separation: 0/59 external references equaled the corresponding Predict-native `conditionId`.
- Gamma lookup: repeated `condition_ids` parameters were sent in separate `closed=false` and `closed=true` requests. Gamma resolved 53/59 references: 2 open and 51 closed. All 53 returned `conditionId` values and questions exactly matched the requested reference and Predict question.
- CLOB fallback: the six references absent from Gamma all returned HTTP 200 from `GET https://clob.polymarket.com/markets/{condition_id}`. All six responses echoed the exact requested `condition_id`; checked questions also matched exactly.
- Overall result: 59/59 explicit Predict references resolved through official Polymarket APIs. In this sample, `polymarketConditionIds` held Polymarket identifiers and never the Predict-native condition ID.

Examples:

- Predict market `id=1049`: Predict-native `conditionId=0x445a0fc9d54235c10b606a5b25a3860cbfa9d6df0e9f9f20bf6d1960843885ee`; external Polymarket ID `0x0b029a30ea36c6d4393d94243938b2c4f4a221ad471f3553c8bc8f34e9b229e9`. Gamma `closed=true` returned exactly one market, Gamma market `id=1013522`, with the same condition ID and question.
- Predict market `id=423`: external Polymarket ID `0xc2f2e988a909add725da525f4056ffdcfd64e951427199ac176967cc18f98edb` was one of the Gamma misses; the CLOB fallback returned HTTP 200 with the same `condition_id` and question.

## Conclusion and first-version boundary

The official schema plus the live sample support a precise implementation rule: keep Predict `conditionId` as the Predict-native on-chain ID, and treat each `polymarketConditionIds[]` element as an explicit Polymarket condition-ID reference. Resolve each external ID through both Gamma states, then fall back to CLOB when Gamma has no row.

The first version can cover only the explicit-mapped subset. Record at least `predict_markets_seen`, `explicit_mapped_markets`, `unique_external_refs`, `gamma_resolved`, `clob_fallback_resolved`, and `unmapped_skipped`; skip empty/unmapped arrays without title-based inference or preclassification.

Limits:

- This was a Predict **testnet** sample of the first 100 open markets, not a census.
- 41/100 sampled markets had no explicit mapping and must not be inferred from titles.
- Predict mainnet coverage remains unknown until the same read-only check can be run with a mainnet API key.
- The result does not show that every Predict mainnet market maps to Polymarket, nor that matching questions guarantee identical outcomes, time boundaries, or resolution rules.
