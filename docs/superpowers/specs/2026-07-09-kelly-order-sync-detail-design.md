# Kelly Order Sync Detail Design

## Goal

Improve the Kelly experiment `订单同步` block so it shows which simulated
orders were synced, instead of only showing aggregate order and fill counts.
The first version stays read-only and uses mock/dashboard payload data.

## Scope

The experiment card keeps its current summary:

- sync status
- Futu environment
- latest sync time
- total order count
- total fill count
- message and next action

Below that summary, the card adds a compact order table. It is scoped to the
active strategy tab and only shows orders belonging to that experiment.

## Data Shape

`order_sync` may contain an optional `orders` list:

```json
{
  "order_sync": {
    "status": "success",
    "environment": "SIMULATE",
    "last_synced_at": "2026-07-08 10:08",
    "order_count": 7,
    "fill_count": 5,
    "message": "富途模拟盘订单已同步。",
    "next_action": "可以继续扫描入场与退出信号。",
    "orders": [
      {
        "market": "US",
        "symbol": "RAM",
        "side": "buy",
        "submitted_at": "2026-07-08 10:01",
        "order_price": "12.34",
        "order_qty": "800",
        "filled_qty": "800",
        "avg_fill_price": "12.34",
        "status": "filled",
        "order_id": "SIM-10001"
      }
    ]
  }
}
```

The UI must tolerate missing fields by rendering `-` for that cell.

## UI Design

Use a compact table with these columns:

```text
标的 | 方向 | 下单时间 | 订单价 | 订单数量 | 成交数量 | 成交均价 | 状态
```

Each order row shows the Futu order id as secondary text under the symbol. The
first version does not render individual fill rows or expandable details.

Side labels:

- `buy` -> `买入`
- `sell` -> `卖出`

Status labels:

- `filled` -> `已成交`
- `partial_filled` -> `部分成交`
- `submitted` / `pending` -> `待成交`
- `cancelled` -> `已撤单`
- `rejected` -> `拒单`
- `failed` -> `失败`

Unknown statuses are displayed as their raw value.

If `orders` is missing or empty, the block shows `暂无同步订单明细。` below the
summary.

## Error Handling

The table is informational only. A failed sync status does not trigger any
action in the UI. Failed/rejected orders should still be visible in the table
when present so the user can see which symbol failed and why the strategy did
not progress.

## Testing

Tests must cover:

- dashboard JS renders order rows with symbol, side, time, price, quantity,
  fill quantity, average fill price, status, and order id
- empty order detail fallback renders `暂无同步订单明细。`
- Playwright verifies the first strategy tab shows only its order rows and the
  second strategy tab shows its own failed/rejected order rows
- existing Kelly strategy details, parameter derivation, and symbol states still
  render

No test should require a live Futu OpenD connection.
