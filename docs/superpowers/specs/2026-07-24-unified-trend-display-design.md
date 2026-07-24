# Unified Trend Report Display

## Goal

Make the CN, US, and HK trend-report action tables use the same buy-table
fields and layout without changing report data, strategy rules, versions, or
historical artifacts.

## Design

- Extract the current CN buy-table renderer into a market-neutral
  `renderTrendBuyStage` helper.
- Render that helper for all three markets from `renderTrendReportWorkspace`.
- Keep the existing column order:
  筛选价、执行参考价、温度变化、节气、强度、行业、行业温度、市值、日成交额、
  目标仓位、目标金额、预计数量、预计保护线。
- Keep prices in the instrument's market currency. Keep market cap and daily
  amount in the existing normalized CNY billions.
- The Dashboard projection exposes normalized CNY-亿元 fields for legacy local
  currency actions using the existing fixed market rate when a frozen normalized
  field is absent; persisted reports are not rewritten.
- Render absent values as `数据未提供`; do not infer missing business fields.
- Preserve existing execution, risk, audit, and mobile horizontal-scroll
  behavior.

## Scope

The Dashboard renderer/projection, acceptance comparator, and focused tests may
change. Frozen reports, strategy parameters, and exchange-rate definitions
remain untouched; projection-only normalization uses the existing fixed rates.

## Verification

- Existing dashboard web and acceptance tests remain green.
- Add one focused assertion that US/HK buy tables contain the same headings as
  CN and display the missing-value label when a field is null.
- Run the final Dashboard acceptance gate after deployment.
