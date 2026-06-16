# Trader Advice Template Design

## Goal

Normalize every TradingAgents per-symbol decision into the same user-facing
template so recommendations are easy to scan and compare across holdings.

## Template

Each formatted `advice_summary` uses these lines:

- `评级`
- `操作计划`
- `风控`
- `仓位`
- `催化剂`
- `目标价`
- `时间窗口`
- `理由`

## Data Flow

`TradingAgentsGraph.propagate()` still returns the raw state and decision. Open
Trader keeps the full JSON in `raw_decision`, keeps the compact action in
`advice_action`, and writes the normalized template into `advice_summary`.

## Parsing

The formatter recognizes the current TradingAgents markdown headings:

- `Rating`
- `Executive Summary`
- `Investment Thesis`
- `Price Target`
- `Time Horizon`

When a structured section is missing, the corresponding template value is blank.
When the text is not structured at all, the formatter returns the original text
instead of dropping content.

## Scope

This does not change CSV field names or the downstream watchlist/action
contracts.
