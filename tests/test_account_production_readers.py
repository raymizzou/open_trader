from __future__ import annotations

import hashlib
import re
from pathlib import Path


FORBIDDEN_PATTERNS = (
    r"account_sync_state\.json",
    r"latest[\"' /]+portfolio\.csv",
    r"latest[\"' /]+quotes\.json",
    r"account_sync[\"' /]+controller_status\.json",
    r"extracted_(?:positions|cash)\.csv",
    r"account_statements[\"' /]+generations",
    r"\bload_account_snapshot\b",
    r"\bload_account_sync_state\b",
    r"\bload_statement_trade_facts\b",
    r"\bdashboard_projection_from_state\b",
    r"\baccepted_portfolio_rows\b",
)

OWNER_MODULES = {
    "account_api.py",
    "account_snapshot.py",
    "account_sync_state.py",
    "account_sync_worker.py",
    "statement_import.py",
    "dashboard_quotes.py",
    "dashboard_acceptance.py",
}

DORMANT_MATCH_COUNTS = {
    ("daily_premarket.py", r"latest[\"' /]+portfolio\.csv"): 2,
    ("daily_premarket.py", r"latest[\"' /]+quotes\.json"): 1,
    ("daily_premarket.py", r"account_sync[\"' /]+controller_status\.json"): 1,
    ("daily_premarket.py", r"account_sync_state\.json"): 1,
    ("daily_premarket.py", r"\bload_account_sync_state\b"): 2,
    ("t_signal_runner.py", r"\bload_account_sync_state\b"): 2,
}

DORMANT_FINGERPRINTS = {
    "daily_premarket.py": "6180dbcb72f8d3e3ac1afce401ff8c019a8bc79d83668968ce8c19876dbc6639",
    "t_signal_runner.py": "7a13bd36418a23af4f61955a87f69a08483b313437041f9de152206d1a0fc222",
}

OWNER_MATCH_COUNTS = {
    ("pipeline.py", r"extracted_(?:positions|cash)\.csv"): 4,
}

OWNER_FINGERPRINTS = {
    "pipeline.py": "5fc90224d598c5e3e4c3b1928c74d439d3883e8365c67db702c896d7211690ce",
}


def _matches() -> list[tuple[str, int, str, str]]:
    source_root = Path(__file__).parents[1] / "src" / "open_trader"
    matches: list[tuple[str, int, str, str]] = []
    for path in sorted(source_root.rglob("*.py")):
        relative_path = path.relative_to(source_root).as_posix()
        if relative_path in OWNER_MODULES:
            continue
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for pattern in FORBIDDEN_PATTERNS:
                if re.search(pattern, line):
                    matches.append(
                        (
                            relative_path,
                            line_number,
                            pattern,
                            line.strip(),
                        )
                    )
    return matches


def test_owner_exclusions_are_exact_relative_paths() -> None:
    assert "account_api.py" in OWNER_MODULES
    assert "nested/account_api.py" not in OWNER_MODULES


def _fingerprint(matches: list[tuple[str, int, str, str]]) -> str:
    payload = "\n".join(
        f"{line_number}:{pattern}:{line}"
        for _path, line_number, pattern, line in matches
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def test_dormant_reader_exceptions_are_frozen() -> None:
    dormant = [
        match
        for match in _matches()
        if (match[0], match[2]) in DORMANT_MATCH_COUNTS
    ]
    counts: dict[tuple[str, str], int] = {}
    for path, _line_number, pattern, _line in dormant:
        key = (path, pattern)
        counts[key] = counts.get(key, 0) + 1
    assert counts == DORMANT_MATCH_COUNTS
    for path, expected in DORMANT_FINGERPRINTS.items():
        assert _fingerprint([match for match in dormant if match[0] == path]) == expected


def test_owner_side_import_reads_are_frozen() -> None:
    owner = [
        match
        for match in _matches()
        if (match[0], match[2]) in OWNER_MATCH_COUNTS
    ]
    counts: dict[tuple[str, str], int] = {}
    for path, _line_number, pattern, _line in owner:
        key = (path, pattern)
        counts[key] = counts.get(key, 0) + 1
    assert counts == OWNER_MATCH_COUNTS
    for path, expected in OWNER_FINGERPRINTS.items():
        assert _fingerprint([match for match in owner if match[0] == path]) == expected


def test_only_approved_modules_read_account_publications() -> None:
    unexpected = [
        match
        for match in _matches()
        if (match[0], match[2]) not in DORMANT_MATCH_COUNTS
        and (match[0], match[2]) not in OWNER_MATCH_COUNTS
    ]
    assert not unexpected, "Unexpected Account publication readers:\n" + "\n".join(
        f"{path}:{line}: {pattern}" for path, line, pattern, _source in unexpected
    )
