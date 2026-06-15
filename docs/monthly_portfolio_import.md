# Monthly Portfolio Import

Run this once per month after placing the latest broker statement PDFs on disk.

```bash
.venv/bin/python -m open_trader import-statements \
  --month 2026-05 \
  --futu /Users/ray/Downloads/futu.pdf \
  --tiger /Users/ray/Downloads/tiger.pdf \
  --phillips /Users/ray/Downloads/phillips.pdf \
  --usd-hkd 7.85
```

Update `--month` and `--usd-hkd` for the target statement month. Replace the PDF paths if the files are stored elsewhere.

Main output:

```text
data/latest/portfolio.csv
```

Trace outputs for the month:

```text
data/runs/<YYYY-MM>/manifest.csv
data/runs/<YYYY-MM>/extracted_positions.csv
data/runs/<YYYY-MM>/extracted_cash.csv
data/runs/<YYYY-MM>/parse_warnings.csv
data/runs/<YYYY-MM>/portfolio.csv
```

## Daily Premarket Advice

After `data/latest/portfolio.csv` exists, run the daily premarket advice workflow:

```bash
.venv/bin/python -m open_trader run-premarket \
  --date 2026-06-16 \
  --portfolio data/latest/portfolio.csv
```

Optional test run for a subset:

```bash
.venv/bin/python -m open_trader run-premarket \
  --date 2026-06-16 \
  --portfolio data/latest/portfolio.csv \
  --symbols VIXY,QQQ \
  --dry-run
```

Main readable output:

```text
reports/premarket/<YYYY-MM-DD>.md
```

Machine-readable action list:

```text
data/latest/premarket_actions.csv
```
