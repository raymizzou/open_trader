# Domain docs

## Read before exploring

Read `CONTEXT-MAP.md`, then the context documents relevant to the task:

- `CONTEXT.md`: 趋势纪律
- `docs/contexts/prediction-arbitrage/CONTEXT.md`: Prediction 套利

Read relevant ADRs under `docs/adr/` and any context-local
`docs/adr/` directories.

If a document or directory is absent, proceed silently.
Do not create placeholders or suggest creating them upfront.
Domain-modeling creates documentation when terms or decisions
are actually resolved.

## Layout

Preserve the existing context map and context document locations.
System-wide ADRs belong under `docs/adr/`.
Keep context-specific ADRs with their context.

## Vocabulary and decisions

Use the domain terms defined in the relevant context document,
including its guidance on terms to avoid.

If a needed concept is missing, reconsider the terminology or
note the gap for domain-modeling.

Explicitly flag any proposal that contradicts an existing ADR,
identify the ADR, and explain why it should be reconsidered.
