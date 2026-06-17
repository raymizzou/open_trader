# Chinese Notification Message Design

## Goal

Make Feishu notification messages readable on mobile by replacing the current
English, report-like text with compact Chinese summaries.

## Decisions

- Daily notifications use Chinese labels and a compact table-like list.
- Trigger notifications use Chinese labels and highlight one action.
- Each actionable row shows the same three primary fields first:
  - `标的`
  - `方向`
  - `仓位`
- `仓位` means suggested quantity plus suggested notional, for example
  `10股 / USD 1,952`.
- When no actionable size exists, show a clear Chinese fallback such as
  `暂无，需人工确认` or `不操作`.
- Keep sending plain text through the existing notifier interface. Do not add
  Feishu cards in this change.

## Daily Message Shape

```text
【Open Trader 日报】2026-06-17

汇总：可执行 1｜需复核 1｜观察 1

标的｜方向｜仓位
US.AAPL｜买入｜10股 / USD 1,952
US.TSLA｜复核｜暂无，需人工确认
US.MSFT｜观察｜不操作

报告：
reports/trade_actions/2026-06-17.md
```

## Trigger Message Shape

```text
【价格触发】US.AAPL

标的：US.AAPL
方向：买入
仓位：10股 / USD 1,952
价格：195.20
原因：到达买入条件
```

## Direction Mapping

- `BUY`: `买入`
- `ADD`: `加仓`
- `TRIM`: `减仓`
- `SELL_STOP`: `止损卖出`
- `TAKE_PROFIT`: `止盈`
- `HOLD`: `观察`
- `REVIEW`: `复核`

Unknown actions fall back to the original action text.

## Scope

This change only updates message rendering and tests. Notification routing,
Feishu credentials, WeCom support, same-day trigger silence, and trading action
generation stay unchanged.
