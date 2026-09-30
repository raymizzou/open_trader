"""Check the original 200-line Smoke window without printing log contents."""

from collections import deque
import json
from pathlib import Path
import re
import sys


ERROR_SIGNAL = re.compile(r"traceback|fatal|exception|error", re.IGNORECASE)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def reject_constant(_value):
    raise ValueError("nonstandard_json_constant")


def check(path: Path) -> str | None:
    if not path.is_file():
        return "log_not_regular_file"
    with path.open("rb") as stream:
        lines = deque(stream, maxlen=200)
    for raw_line in lines:
        line = raw_line.decode("utf-8")
        start = line.find("{")
        if start >= 0 and line[start + 1:].lstrip().startswith('"'):
            try:
                payload = json.loads(
                    line[start:], object_pairs_hook=unique_object,
                    parse_constant=reject_constant,
                )
            except (ValueError, RecursionError):
                return "log_record_invalid"
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
        if ERROR_SIGNAL.search(line):
            return "log_error_signal"
    return None


def main() -> int:
    try:
        reason = check(Path(sys.argv[1])) if len(sys.argv) == 2 else "log_argument_invalid"
    except Exception:
        reason = "log_read_failed"
    if reason is not None:
        print(f"production_log_check_failed reason={reason}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
