# Eastmoney Local Statement Config Design

## Goal

Import the encrypted Eastmoney statement without placing its password in chat,
command arguments, logs, Git, or generated artifacts.

## Local Files

Move the statement to the ignored runtime path:

```text
data/statements/eastmoney/2026-07/statement.pdf
```

Store local settings in the existing ignored, mode-`0600` file:

```text
config/daily_premarket.env
```

Use these keys:

```text
OPEN_TRADER_EASTMONEY_STATEMENT=/Users/ray/projects/open_trader/data/statements/eastmoney/2026-07/statement.pdf
OPEN_TRADER_EASTMONEY_PDF_PASSWORD=
```

The tracked example file contains empty placeholders only.

## CLI Behavior

`import-statements` accepts `--config`, defaulting to
`config/daily_premarket.env`.

For an Eastmoney import:

1. Explicit `--eastmoney PATH` overrides the configured statement path.
2. If `--eastmoney` is omitted, the CLI uses
   `OPEN_TRADER_EASTMONEY_STATEMENT` when present.
3. The password comes from `OPEN_TRADER_EASTMONEY_PDF_PASSWORD` when nonblank.
4. If the password is missing, the CLI falls back to the existing hidden
   `getpass` prompt.
5. Missing files and invalid configuration fail before import.

Existing Phillips-only and combined import behavior remains unchanged.

## Security

- Never print, log, serialize, or include the password in an exception.
- Never place the password in process arguments.
- Do not copy local values into the tracked example file.
- Preserve mode `0600` on `config/daily_premarket.env`.
- The statement lives below ignored `data/` and must not be force-added.

## Verification

Tests cover config path fallback, explicit path precedence, environment-file
password use, hidden prompt fallback, missing file handling, and output secrecy.
After implementation, move the PDF, let the user fill the password locally,
run the real import, restart Dashboard, and rerun `make acceptance`. Only
acceptance `PASS` completes deployment.
