# Private Kelly receipt import

From `services/trend_v3`, use the service's Python environment and set
`PYTHONPATH=src`. Supply the official report data root, the Taipei report
date, the frozen revision, and that revision's source commit. Keep the
execution DB at its existing path. Both the original and holdings-enabled
execution stores are read-only inputs. Create a separate owner-only directory
(mode `0700`) for the Kelly DB and private receipts. Formal commands reject the
fabricated Kelly demo DB. Ledger filenames end in `.sqlite`. Use absolute paths
for the evidence.

```sh
umask 077
PYTHONPATH=src python -m trend_v3.kelly_paper init \
  --root "$report_root" --date "$report_date" --revision "$report_revision" \
  --release "$report_source_commit" --execution-db "$execution_db" --ledger "$kelly_db"
```

Use `inspect` with the same arguments for current counts. Use `sync` with
those arguments and `--receipts "$receipt_manifest"` to import reviewed
receipts. `sync` without a manifest reports counts and leaves events alone.
The execution DB is read-only. The report scope filters the counts by exact
opening date, revision and evidence digest. An accepted order can have zero
fills. Orders with a nonzero reported fill need detailed deals. Closed rounds
with incomplete fee evidence have unknown net P&L and contribute no sample.

## Prepare receipt evidence

Use the original SIMULATE account export or another original broker document.
The operator must compare the normalized rows with the original. Record only
values the document establishes, including the environment, account, exact
executed quantities and actual execution timestamps. Retain all three kinds
of files privately with mode `0600` in the same directory. Each reference
described below is a JSON object with `file` (basename) and `sha256` (hex
digest of the file bytes). Compute the digests after writing each file.

1. Keep the **original broker document**. Keep the order/deal history, position
   evidence for a manually documented action, or complete fee statement as
   applicable.
2. Write a **normalized document** with `schema` set to
   `trend-v3-kelly-document-v1`, `source` set to `broker_export` or
   `manual_document`, and `original` referencing the original file. Put its
   records in an `orders` or `settlements` array. The importer verifies
   both digests and checks the normalized records against the official store
   and ledger. The operator verifies what the original document says.
3. Write a **manifest** with `schema` set to `trend-v3-kelly-receipts-v1`
   and two arrays, `orders` and `settlements`. Each order binding has
   `order_id` and `proof` referencing the normalized document. Include
   `attempt` (the official execution-store token) for official orders.
   Include `opening_order_id` for supplementary orders or a retry that
   continues an existing opening. Each settlement binding has
   `opening_order_id` and `proof`. The manifest binds records. Order and
   fee values come from the normalized documents.

A normalized **order** row has `broker: "Futu"`, `environment: "SIMULATE"`,
`account_id` (decimal string), `account_market` (the original trade account
market), `order_id`, `code` (routed contract or security code), `product`,
`currency`, `side` (`buy` or `sell`), `order_quantity`, `state`
(`active`, `filled` or `cancelled`), `filled_quantity`, and
`filled_notional` (exact decimal text in the order currency). Quantities
are JSON integers. Its `deals` array contains `deal_id`, `executed_at`
(ISO 8601 with UTC offset), `quantity`, and `price` (decimal text).
Notional includes the authorized contract multiplier. If the broker provides
cumulative history, include ordered `checkpoints` with each snapshot's
`filled_quantity` and `filled_notional`. Each checkpoint must match the
exact detailed deal prefix. Use an empty deal list for an aggregate record
with no verified execution details. No fills are created from that aggregate.

For an opening already recorded in a round, include its official order and
all previously imported deals when submitting an update. Use the same native
deal IDs in a broker export and a manually normalized document. Supplementary
orders must prove the start and end net position through `position_before`
and `position_after` (signed integers, short is negative) in the broker
record. A supplementary order may add to, reduce, or close a verified
position. Keep the entire sequence and anchor it to its original opening
order. The importer fails closed if it cannot establish an unambiguous
sequence or if the position crosses zero. A supplementary document does not
authorize or place an order.

A normalized **settlement** row has `opening_order_id`, `broker: "Futu"`,
`environment: "SIMULATE"`, `account_id`, `account_market`, `currency`,
`confirmed_at` (ISO 8601 with UTC offset), and `fees`, an object naming
every executed order in the closed round and its confirmed fee in decimal
text. Record a known zero as `"0"`. Wait for complete fee evidence and
terminal order states before submitting settlement. An import either
commits the validated receipt package together or rolls it back.

For a fee billed in another currency, the entry in `fees` is an object
with `amount` (signed exact decimal text, positive for a charge and
negative for a rebate), `currency` (the billed currency), and `fx` (the
file and SHA-256 reference for a normalized document retaining the original
conversion proof). Its `fee_conversions` array must have one matching row
per order. Each row contains these fields.

- `opening_order_id`, `order_id`, `broker`, `environment`, `account_id`, and
  `account_market` bind the conversion to the same settlement and order.
- `confirmed_at` matches the settlement confirmation time. `fee_amount`
  matches the original signed fee.
- `from_currency` is the billed currency. `to_currency` is the ledger and
  settlement currency. `rate` is positive exact decimal text in units of
  the ledger currency per unit billed. The importer multiplies the signed
  fee by this documented rate. It rejects evidence that requires guessing,
  inverting a rate, or rounding beyond the ledger's precision.

Use the original same-currency decimal strings for fees already billed in the
ledger currency.

## Generate private report metadata

Run `catalog` with `--root`, `--date`, `--revision`, and `--release`.
Redirect stdout to a mode-`0600` catalog in your private folder. The
catalog includes the complete class list from the verified frozen parent
board, the opening report reference and the original HTML digest. The class
list uses the reader board edition. Trade attribution still requires the
opening evidence and approval. Compare the original HTML at `reports/<date>.html`,
`reports/_evidence/<date>/<revision>/history/<date>.html`, or
`reports/_evidence/<date>/<revision>/reader-original.html` with that digest.
Use only the matching shell in the private preview.
