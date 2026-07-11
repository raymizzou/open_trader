# Final Review Fix Report

Status: DONE

## Scope

- Manifest v1 now records a parseable UTC `created_at`, stable normalized paths and SHA-256 hashes for every generated data artifact and both Markdown report locations. The manifest remains the final staged artifact and excludes its own recursive hash.
- API and persisted signal rows now carry `market`, `symbol`, exact `strategy_id`, `strategy_version`, and fixed `parameters`. CSV parameters use compact, sorted JSON; API parameters remain structured objects. HOLD rows and zero-trade summaries are preserved.
- Dashboard JSON requests validate `Content-Length` before reading: invalid/negative values return Chinese HTTP 400 and values over 1 MiB return Chinese HTTP 413 without reading the declared body.
- README and CHANGELOG were left unchanged because their existing output-contract claims remain accurate and do not enumerate the fields changed here.
- No live process, PID, launchd job, or service was restarted.

## TDD evidence

### RED

Command:

```text
/Users/ray/projects/open_trader/.venv/bin/python -m pytest -q tests/test_strategy_backtest.py::test_result_payload_exposes_manifest_backed_assumptions_definition_and_signals tests/test_strategy_backtest.py::test_run_writes_reproducible_manifest_and_normalized_artifacts tests/test_strategy_backtest.py::test_detached_signals_csv_self_describes_strategy_and_parameters tests/test_dashboard_web.py::test_dashboard_http_rejects_invalid_or_oversized_content_length_before_read
```

Observed result: `6 failed in 11.59s`. Failures showed missing signal identity, missing `created_at`, absent CSV identity columns, leaked Python integer error text, and blocked reads for negative/oversized lengths.

Additional RED for external Markdown report hashing:

```text
/Users/ray/projects/open_trader/.venv/bin/python -m pytest -q tests/test_strategy_backtest.py::test_run_writes_reproducible_manifest_and_normalized_artifacts
```

Observed result: `1 failed in 0.36s`; the external report location lacked its SHA-256.

### GREEN

Same six-case command after implementation: `6 passed in 1.98s`.

External report hash case after implementation: `1 passed in 0.27s`.

Focused suites:

```text
/Users/ray/projects/open_trader/.venv/bin/python -m pytest -q tests/test_strategy_backtest.py tests/test_dashboard_web.py
```

Observed result: `98 passed in 14.55s`.

All affected Task 1-7 suites:

```text
/Users/ray/projects/open_trader/.venv/bin/python -m pytest -q tests/test_standard_strategies.py tests/test_backtest_prices.py tests/test_strategy_backtest.py tests/test_backtest.py tests/test_backtest_cli.py tests/test_dashboard.py tests/test_dashboard_web.py
```

Observed result: `187 passed in 14.47s`.

Fresh full suite after the final production change:

```text
/Users/ray/projects/open_trader/.venv/bin/python -m pytest -q
```

Observed result: `1138 passed in 20.95s`.

Static verification (exit 0, no output except the focused pytest line):

```text
/Users/ray/projects/open_trader/.venv/bin/python -m py_compile src/open_trader/strategy_backtest.py src/open_trader/dashboard_web.py tests/test_strategy_backtest.py tests/test_dashboard_web.py
node --check src/open_trader/dashboard_static/dashboard.js
git diff --check
```

## Commits

- `98642c6` — `fix: complete backtest output contracts`

The report itself is committed separately so it can cite the implementation commit without a recursive commit-id dependency.
