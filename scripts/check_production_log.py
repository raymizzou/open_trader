"""Check all Smoke log lines without printing log contents."""

import argparse
import json
from pathlib import Path
import re
import sys
import subprocess


ERROR_SIGNAL = re.compile(r"traceback|fatal|exception|error", re.IGNORECASE)
ORDERBOOK_CONNECTION_PREFIX = re.compile(
    r"(?:"
    r"(?:\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?"
    r"(?:Z|[+-]\d{2}:?\d{2})?\s+)?"
    r"INFO\s+open_trader\.polymarket_lp_orderbook\s+"
    r"|INFO:open_trader\.polymarket_lp_orderbook:"
    r")?prediction_lp_orderbook_connection\s+"
)


def normal_orderbook_connection(prefix: str, payload: object) -> bool:
    """Recognize only the producer's normal connection records at INFO."""
    return (
        ORDERBOOK_CONNECTION_PREFIX.fullmatch(prefix) is not None
        and isinstance(payload, dict)
        and payload.keys() == {"at", "event", "reason", "code", "exception_class"}
        and isinstance(payload["at"], str)
        and bool(payload["at"].strip())
        and (
            payload["event"] == "connected" and payload["reason"] is None
            or payload["event"] == "disconnected" and payload["reason"] == "closed"
        )
        and payload["code"] is None
        and (payload["exception_class"] is None or payload["exception_class"] == "")
    )


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def reject_constant(_value):
    raise ValueError("nonstandard_json_constant")


def check(text: str, require_text: str | None = None) -> str | None:
    if not text.strip():
        return "log_empty"
    if require_text is not None and require_text not in text:
        return "log_required_text_missing"
    for line in text.splitlines():
        start = line.find("{")
        if start >= 0 and line[start + 1:].lstrip().startswith('"'):
            try:
                payload = json.loads(
                    line[start:], object_pairs_hook=unique_object,
                    parse_constant=reject_constant,
                )
            except (ValueError, RecursionError):
                return "log_record_invalid"
            if normal_orderbook_connection(line[:start], payload):
                # Keep the prefix and all other fields subject to the error scan.
                del payload["exception_class"]
            if (
                isinstance(payload, dict)
                and payload.get("level") == "INFO"
                and payload.get("status") == "healthy"
                and payload.get("degraded_reasons") == []
                and "last_error" in payload
                and payload["last_error"] is None
            ):
                del payload["last_error"]
            line = line[:start] + json.dumps(payload, ensure_ascii=False)
        if "RemoteProtocolError" in line:
            return "log_remote_protocol_error"
        if ERROR_SIGNAL.search(line):
            return "log_error_signal"
    return None


def main() -> int:
    try:
        parser = argparse.ArgumentParser(exit_on_error=False, allow_abbrev=False)
        parser.add_argument("path", nargs="?")
        parser.add_argument("--file")
        parser.add_argument("--journal-unit")
        parser.add_argument("--since")
        parser.add_argument("--require-text")
        args, unknown = parser.parse_known_args()
        if (unknown or sum(value is not None for value in (args.path, args.file, args.journal_unit)) != 1
            or args.journal_unit is not None and (not args.journal_unit.strip() or not args.since or not args.since.strip())
            or args.journal_unit is None and args.since is not None):
            reason = "log_argument_invalid"
        elif args.journal_unit is not None:
            result = subprocess.run(
                ["journalctl", "-u", args.journal_unit, "--since", args.since, "--no-pager", "-o", "cat"],
                check=True, capture_output=True, timeout=15,
            )
            reason = check(result.stdout.decode("utf-8"), args.require_text)
        else:
            path = Path(args.path or args.file)
            reason = check(path.read_text(encoding="utf-8"), args.require_text) if path.is_file() else "log_not_regular_file"
    except argparse.ArgumentError:
        reason = "log_argument_invalid"
    except Exception:
        reason = "log_read_failed"
    if reason is not None:
        print(f"production_log_check_failed reason={reason}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
