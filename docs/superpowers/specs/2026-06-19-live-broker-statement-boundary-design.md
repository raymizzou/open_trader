# Live Broker Statement Boundary Design

## Goal

Make monthly statement import stop depending on brokers that now have live
account sync. Futu and Tiger holdings should come from their read-only live
account sync commands. Monthly `import-statements` should only require broker
statements for brokers that still lack a live holdings API, currently Phillips.

## Current Problem

`import-statements` still requires `--futu`, `--tiger`, and `--phillips`. That
keeps old statement-derived Futu/Tiger rows in the portfolio workflow and can
produce mixed broker rows such as `brokers=futu;tiger`, which the live sync
commands correctly block instead of splitting automatically.

The current `main` branch still has the same required Futu/Tiger statement
arguments, so this is not solved by merging latest `main`.

## Selected Approach

Change the monthly import CLI to make Phillips the only required statement
input. Remove Futu and Tiger from the default `import-statements` pipeline.

Keep the existing Futu and Tiger statement parsers and parser tests for
historical/backfill capability, but do not expose them through the default
monthly import command. This keeps the change narrow and avoids deleting
working parsing code that may still be useful for old data checks.

## User Workflow

1. Run monthly statement import for Phillips:

```bash
.venv/bin/python -m open_trader import-statements \
  --month 2026-06 \
  --phillips /path/to/phillips.pdf \
  --usd-hkd 7.85
```

2. Refresh live Futu rows:

```bash
.venv/bin/python -m open_trader sync-futu-portfolio \
  --date 2026-06-19 \
  --update-latest
```

3. Refresh live Tiger rows:

```bash
.venv/bin/python -m open_trader sync-tiger-portfolio \
  --config-dir /Users/ray/Downloads \
  --date 2026-06-19 \
  --update-latest
```

The recommended safety order remains dry-run first without `--update-latest`,
review dated artifacts, then promote.

## Components

- `src/open_trader/cli.py`
  - Remove `--futu` and `--tiger` from `import-statements`.
  - Pass only `{"phillips": args.phillips}` and `[PhillipsStatementParser()]`
    to `run_import()`.
  - Remove unused parser imports if they become unused by CLI.

- `src/open_trader/pipeline.py`
  - No structural change expected. It already accepts an arbitrary parser list
    and statement path mapping.

- Tests
  - Update import CLI tests so `--futu` and `--tiger` are no longer needed.
  - Assert help does not expose `--futu` or `--tiger`.
  - Preserve parser tests for Futu/Tiger statement parsing.

- Docs
  - Update README and monthly docs to show Phillips-only monthly import.
  - Explain that Futu and Tiger current holdings come from live sync.
  - Remove statements saying `import-statements` still requires Tiger.

## Error Handling

The monthly import command should keep the existing atomic dated/latest write
behavior. Live sync commands continue to own mixed-broker protection and
blocking data checks.

If users still have old mixed Futu/Tiger rows in `data/latest/portfolio.csv`,
live sync will keep blocking until those rows are cleaned or regenerated from
the new workflow.

## Testing

Run focused CLI/pipeline tests after the change:

```bash
.venv/bin/python -m pytest tests/test_pipeline.py -q
```

Run broker live-sync tests to catch regressions in the replacement workflow:

```bash
.venv/bin/python -m pytest tests/test_futu_account.py tests/test_tiger_account.py tests/test_tiger_account_cli.py -q
```

Run the full suite before completion:

```bash
.venv/bin/python -m pytest -q
```
