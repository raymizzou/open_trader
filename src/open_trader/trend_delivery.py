from __future__ import annotations

import hashlib
import json
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Mapping

from .daily_premarket import send_notification_with_results
from .notifications import Notifier


_VALID_STATUSES = {"prepared", "pending", "sent", "delivery_failed"}


def deliver_daily_trend_text(
    notifier: Notifier, *, ledger_path: Path, title: str, message: str
) -> str:
    ledger = _read_ledger(ledger_path)
    if ledger is None:
        ledger = _write_ledger(ledger_path, "prepared", title, message)
    return _deliver_frozen(notifier, ledger_path, ledger)


def retry_daily_trend_text(
    notifier: Notifier, *, ledger_path: Path
) -> str | None:
    ledger = _read_ledger(ledger_path)
    if ledger is None:
        return None
    return _deliver_frozen(notifier, ledger_path, ledger)


def _deliver_frozen(
    notifier: Notifier, ledger_path: Path, ledger: Mapping[str, object]
) -> str:
    status = str(ledger["status"])
    if status == "sent":
        return "sent_prior_message"
    title, message = str(ledger["title"]), str(ledger["message"])
    _write_ledger(ledger_path, "pending", title, message)
    attempts = send_notification_with_results(
        notifier,
        title,
        message,
        channels={"feishu", "feishu_app"},
    )
    delivered = any(
        item.channel.startswith("feishu") and item.success for item in attempts
    )
    result = "sent" if delivered else "delivery_failed"
    _write_ledger(ledger_path, result, title, message)
    return result


def _content_hash(title: str, message: str) -> str:
    return hashlib.sha256(f"{title}\0{message}".encode("utf-8")).hexdigest()


def _write_ledger(
    path: Path, status: str, title: str, message: str
) -> dict[str, str]:
    if status not in _VALID_STATUSES:
        raise ValueError("invalid daily trend delivery status")
    payload = {
        "status": status,
        "title": title,
        "message": message,
        "content_sha256": _content_hash(title, message),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with NamedTemporaryFile(
            "w", encoding="utf-8", delete=False, dir=path.parent
        ) as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            temp_path = Path(handle.name)
        temp_path.replace(path)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
    return payload


def _read_ledger(path: Path) -> dict[str, str] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ValueError("daily trend delivery ledger is unreadable") from None
    if not isinstance(payload, dict) or payload.get("status") not in _VALID_STATUSES:
        raise ValueError("daily trend delivery ledger has invalid status")
    title, message = payload.get("title"), payload.get("message")
    if not isinstance(title, str) or not isinstance(message, str):
        raise ValueError("daily trend delivery ledger has invalid text")
    if payload.get("content_sha256") != _content_hash(title, message):
        raise ValueError("daily trend delivery ledger hash mismatch")
    return {
        "status": str(payload["status"]),
        "title": title,
        "message": message,
        "content_sha256": str(payload["content_sha256"]),
    }
