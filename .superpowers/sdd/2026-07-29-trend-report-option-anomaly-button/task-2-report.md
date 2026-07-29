# Task 2 report: trend-row Futu option anomaly dialog

## Status

Implemented and committed on `feat/trend-report-option-anomaly-button`.

The existing US/HK buy and hold trend rows now render `期权异动` inside the
existing symbol cell. Available facts open a native `<dialog>`; missing,
unsupported, stale, or errored facts render a disabled button with the server
reason in `title` and `aria-label`. Sell, review, risk-skip, and CN rows do not
render the button. No new table column, modal manager, Futu request, Python
projection, Feishu change, or legacy aggregate change was added.

The dialog renders the fixed source/identity/date/window, summary,
signal/confidence/constraint, all structured categories, and native close
targets. Existing delegated report-container handlers open and close it, so
native Escape and focus containment remain browser-provided.

## Commits

- `fc988d8 feat: show Futu option anomaly dialog in trend rows`

## Tests

The required RED test was run before implementation and failed because the
renderer and CSS did not exist. After implementation:

```text
$ /Users/ray/projects/open_trader/.venv/bin/python -m pytest -q \
    tests/test_dashboard_web.py::test_dashboard_renders_option_anomaly_button_and_native_dialog \
    tests/test_dashboard_web.py::test_dashboard_trend_option_button_mobile_layout_css
..                                                                       [100%]
2 passed in 0.64s
```

Focused trend-report checks:

```text
$ /Users/ray/projects/open_trader/.venv/bin/python -m pytest -q tests/test_dashboard_web.py -k trend_report
7 passed, 269 deselected in 0.76s

$ /Users/ray/projects/open_trader/.venv/bin/python -m pytest -q tests/test_dashboard_web.py -k cn_trend
2 passed, 274 deselected in 0.59s

$ /Users/ray/projects/open_trader/.venv/bin/python -m pytest -q tests/test_dashboard_web.py -k option_attention
2 passed, 274 deselected in 0.55s
```

Full dashboard web test file:

```text
$ /Users/ray/projects/open_trader/.venv/bin/python -m pytest -q tests/test_dashboard_web.py
276 passed in 38.53s
```

`git diff --check` passed with no output before commit.

## Concerns

- The worktree has no local `.venv` symlink; the commands above use the shared
  workspace interpreter at `/Users/ray/projects/open_trader/.venv/bin/python`.
- Browser acceptance and live dashboard verification remain parent-task gates;
  this task only covers render/CSS behavior and focused Python-driven JS tests.
- Dialog data is intentionally limited to `item.option_anomaly`; the server
  projection and date/freshness policy are supplied by Task 1.
