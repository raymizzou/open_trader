# Kelly Strategy Capital UI Design

## Goal

Add a strategy-level capital panel to the Kelly Lab strategy tab. The panel
answers one operational question before the user reads order details or strategy
rules:

```text
Can this strategy continue placing simulated orders, and why?
```

This UI follows the conservative capital model selected for the first automated
paper-trading phase:

- capital is attributed by `experiment_id`
- the same symbol may appear in multiple strategies without netting exposure
- submitted buy orders reserve capital immediately
- filled buy orders remain capital usage as positions
- filled sell orders release capital and update realized P/L

## Placement

Use layout option A from the visual design review.

The capital panel appears inside the active Kelly strategy tab, directly below
the strategy header and above these existing sections:

1. order execution
2. order sync
3. strategy details
4. Kelly parameter derivation
5. symbol lifecycle states

The panel is not a separate home-page card and is not hidden behind another
button. Capital state directly affects whether automated orders are allowed, so
it must be visible on the first screen of each strategy tab.

## Summary Metrics

The first row shows six fixed metrics:

- total capital
- occupied capital
- available capital
- utilization percentage
- open buy order count
- realized P/L

Example:

```text
Total capital: USD 30,000
Occupied: USD 8,460
Available: USD 21,540
Utilization: 28.2%
Open buy orders: 2
Realized P/L: +USD 420
```

`available capital` is the primary value. It should be visually emphasized more
than the other metrics because it is the value used by the next order risk
check.

## Occupancy Bar

Below the summary metrics, show a single horizontal utilization bar:

- position occupancy segment
- reserved buy-order segment
- remaining available segment

The bar is informational only. It should not introduce controls or imply manual
editing.

## Breakdown Section

The second row contains three compact panes.

### Capital Breakdown

Shows the conservative capital accounting:

- position occupancy
- pending/submitted buy-order reservation
- accounting rule: buy orders reserve capital when submitted

### Symbol Occupancy

Shows per-symbol capital usage for the active experiment only:

```text
US.RAM   USD 3,720
US.DRAM  USD 2,480
US.SOXX  USD 2,260
```

If the same symbol appears in another experiment, it is not included here unless
the active tab is that other experiment.

### Next Order Impact

When a ready buy order exists, show the next planned order's capital impact:

- symbol
- estimated notional
- available capital after the order
- risk decision

Example:

```text
US.RAM
Estimated: USD 1,200
Available after order: USD 20,340
Risk result: capital is sufficient
```

If no ready buy order exists, this pane should say there is no pending capital
impact. It should not show placeholder numbers.

If the next order is blocked for insufficient capital, show the blocked amount
and reason:

```text
Risk result: blocked
Reason: estimated order USD 4,800 exceeds available capital USD 2,300
```

## Data Contract

The UI expects each experiment to include a `capital` object after the backend
capital snapshot is implemented.

```json
{
  "capital": {
    "currency": "USD",
    "budget": "30000",
    "occupied_notional": "8460",
    "position_notional": "6200",
    "reserved_order_notional": "2260",
    "available_notional": "21540",
    "utilization_pct": "28.2",
    "open_buy_order_count": 2,
    "realized_pnl": "420",
    "updated_at": "2026-07-10 20:40",
    "symbol_occupancy": [
      {
        "market": "US",
        "symbol": "RAM",
        "notional": "3720"
      }
    ],
    "next_order_impact": {
      "market": "US",
      "symbol": "RAM",
      "estimated_notional": "1200",
      "available_after_order": "20340",
      "risk_status": "approved",
      "reason": "capital is sufficient"
    }
  }
}
```

When `capital` is missing, the UI should render a compact unavailable state
instead of hiding the section:

```text
Strategy capital data is not available yet.
```

## Responsive Behavior

Desktop:

- six summary metrics stay in one row when width allows
- the three breakdown panes stay in one row
- the utilization bar remains full width

Mobile or narrow width:

- summary metrics wrap into two columns
- breakdown panes stack vertically
- text values must wrap rather than overflow

The panel should match the current Kelly Lab styling: restrained borders,
compact metric tiles, and no nested decorative cards.

## Testing

Implementation should include:

- unit test for rendering capital metrics from an experiment `capital` object
- unit test for unavailable capital fallback
- unit test for blocked next-order impact wording
- Playwright check that the active Kelly strategy tab shows total capital,
  available capital, occupied capital, and next-order impact
- Playwright check that switching strategy tabs changes the displayed capital
  values

Capital calculation itself belongs in the backend strategy capital phase and
should be tested separately from this UI rendering spec.
