"""Read the original scheduled launch and the latest verified daily artifacts."""

import json
import os
import re
import stat
import subprocess
from hashlib import sha256
from pathlib import Path

from .paper_evidence import (
    BRIDGE_REPORT_SCHEMA,
    REPORT_SCHEMA,
    bridge_observations,
    day,
    instant,
)

HEX_SHA = re.compile(r"[0-9a-f]{40}")
HEX_DIGEST = re.compile(r"[0-9a-f]{64}")
SESSION_ID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")
REVISION = re.compile(r"v[0-9]{2,}")
PLAN_HEADER = re.compile(
    r"^[ \t]*模拟开仓计划 ([0-9a-f]{64}) · 日报 (v[0-9]{2,})\r?$", re.M
)
NUMBERED_APPROVAL = re.compile(r"只?确认第([1-9][0-9]{0,2}(?:、[1-9][0-9]{0,2})*)项")
CODE_FENCE = re.compile(r"( {0,3})(`{3,}|~{3,})(.*)")


def strict_json(value):
    def object_pairs(pairs):
        result = {}
        for key, item in pairs:
            if key in result:
                raise ValueError("duplicate key in paper input")
            result[key] = item
        return result

    def invalid_number(_):
        raise ValueError("nonfinite number in paper input")

    return json.loads(
        value, object_pairs_hook=object_pairs, parse_constant=invalid_number
    )


def private_bytes(path, *, limit=16_000_000):
    return _owned_bytes(path, limit=limit)


def _owned_bytes(path, *, limit, transcript_root=None):
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("paper input path is not canonical")
    if transcript_root is not None and not path.is_relative_to(
        transcript_root / "sessions"
    ):
        raise ValueError("native transcript is outside the Codex session directory")
    flags = os.O_RDONLY | os.O_NOFOLLOW
    parent_fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for depth, part in enumerate(path.parts[1:-1], start=1):
            new_fd = os.open(part, flags | os.O_DIRECTORY, dir_fd=parent_fd)
            os.close(parent_fd)
            parent_fd = new_fd
            if transcript_root is not None and depth >= len(transcript_root.parts) - 1:
                directory = os.fstat(parent_fd)
                if directory.st_uid != os.geteuid() or directory.st_mode & 0o022:
                    raise PermissionError(
                        "native transcript directory is not owner-controlled"
                    )
        fd = os.open(path.name, flags | os.O_NONBLOCK, dir_fd=parent_fd)
    finally:
        os.close(parent_fd)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        forbidden_mode = 0o077 if transcript_root is None else 0o022
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_nlink != 1
            or info.st_mode & forbidden_mode
            or info.st_size > limit
        ):
            raise PermissionError(
                "paper input ownership, type or permissions do not match"
            )
        raw = stream.read(limit + 1)
        if len(raw) > limit:
            raise ValueError("paper input exceeds the supported size")
        return raw


def read_json(path):
    value = strict_json(private_bytes(path))
    if not isinstance(value, dict):
        raise ValueError("paper input must be an object")
    return value


def require_private_dir(path):
    info = path.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_mode & 0o077
    ):
        raise PermissionError("paper state directory must be private to its owner")


def native_terminal(home, terminal):
    result = subprocess.run(
        [str(home / ".local/bin/hc"), "get", terminal, "--json"],
        capture_output=True,
        check=True,
        timeout=5,
    )
    return strict_json(result.stdout)


def is_ancestor(pid):
    if pid == os.getpid():
        return True
    # The kernel proves our immediate parent without a process-listing command.
    current = os.getppid()
    for _ in range(32):
        if current == pid:
            return True
        if current <= 1:
            break
        try:
            result = subprocess.run(
                ["/bin/ps", "-o", "ppid=", "-p", str(current)],
                capture_output=True,
                check=True,
                timeout=2,
            )
            current = int(result.stdout)
        except (OSError, subprocess.SubprocessError, ValueError):
            # Missing ancestry proof must reject with the original-origin error.
            return False
    return False


def session_records(home, session, root):
    codex_dir = home / ".codex"
    session_dir = codex_dir / "sessions"
    paths = list(session_dir.glob(f"**/*{session}.jsonl"))
    if len(paths) != 1:
        raise ValueError("native scheduled tab or original launch prompt is unverified")
    raw = _owned_bytes(paths[0], limit=64_000_000, transcript_root=codex_dir)
    records = (strict_json(line) for line in raw.splitlines() if line)
    first = next(records, {})
    if first.get("type") != "session_meta" or (
        first.get("payload", {}).get("id"),
        first.get("payload", {}).get("cwd"),
    ) != (session, str(root)):
        raise ValueError("native scheduled tab or original launch prompt is unverified")
    yield from records


def original_prompt(home, session, root, launch, report_day):
    marker = f"Original immutable host launch ID: {launch}."
    expected = f"Initialize the scheduled daily session for {report_day} (Asia/Taipei)."
    for record in session_records(home, session, root):
        item = record.get("payload", {})
        if record.get("type") != "response_item" or item.get("role") != "user":
            continue
        content = " ".join(part.get("text", "") for part in item.get("content", []))
        if content.startswith(("# AGENTS.md instructions", "<environment_context>")):
            continue
        return marker in content and expected in content
    return False


def delivery_card(plan, *, historical=False):
    rows = [f"模拟开仓计划 {plan['id']} · 日报 {plan['report_version']}"]
    for number, line in enumerate(plan["lines"], 1):
        direction = "做多" if line["direction"] == "long" else "开空"
        rows.append(
            f"第{number}项 | {line['item']} | {line['code']} | "
            f"{line['signal']} {direction} | "
            f"数量 {line['quantity']} | 参考价 {line['reference_price']} | "
            f"估算 {line['notional']} {line['currency']} | "
            f"市价 {line['opens_at']} {line['timezone']}"
        )
    if not historical:
        rows.append(
            "开盘前确认，等开盘提交。开盘后在同一市场交易日内确认，通过即时复核后提交。"
            "收盘后提交可能挂单或被拒。"
        )
    return "\n".join(rows)


def visible_message(item, role):
    return (
        item.get("type") == "message"
        and item.get("role") == role
        and item.get("recipient") in (None, "all")
    )


def message_text(item, role):
    if not visible_message(item, role):
        return None
    parts = item.get("content")
    if not isinstance(parts, list) or len(parts) != 1:
        return None
    (part,) = parts
    expected = "input_text" if role == "user" else "output_text"
    if not isinstance(part, dict) or part.get("type") != expected:
        return None
    content = part.get("text")
    return content if isinstance(content, str) else None


def remove_code_indent(line, columns):
    """Remove up to the structural indent, retaining unconsumed tab columns."""
    width = offset = 0
    while width < columns and offset < len(line):
        char = line[offset]
        if char == " ":
            width += 1
        elif char == "\t":
            width += 4 - width % 4
        else:
            break
        offset += 1
    return width >= columns, " " * max(0, width - columns) + line[offset:]


def standalone_delivery_body(assistant):
    """Read either an unwrapped message or one literal Markdown code block."""
    lines = assistant.split("\n")
    start, end = 0, len(lines)
    while start < end and not lines[start].strip(" \t"):
        start += 1
    while start < end and not lines[end - 1].strip(" \t"):
        end -= 1
    lines = lines[start:end]
    if not lines:
        return None
    opening = CODE_FENCE.fullmatch(lines[0])
    if opening is not None:
        indent, fence, raw_info = opening.groups()
        closing = CODE_FENCE.fullmatch(lines[-1])
        # Backtick fences forbid backticks in their info string. The info
        # otherwise has no role in matching the literal card.
        if (
            len(lines) < 2
            or (fence[0] == "`" and "`" in raw_info)
            or closing is None
            or not closing[2].startswith(fence)
            or closing[3].strip(" \t")
        ):
            return None
        return "\n".join(
            remove_code_indent(line, len(indent))[1] for line in lines[1:-1]
        )

    if remove_code_indent(lines[0], 4)[0]:
        body = []
        for line in lines:
            indented, content = remove_code_indent(line, 4)
            if not indented and line.strip(" \t"):
                return None
            body.append(content)
        return "\n".join(body)
    return "\n".join(lines)


def visible_delivery(item, card, historical_card):
    if item.get("phase") != "final_answer":
        return None, None, False
    assistant = message_text(item, "assistant")
    if assistant:
        assistant = assistant.replace("\r\n", "\n").replace("\r", "\n")
        literal = standalone_delivery_body(assistant)
        if literal in (card, historical_card):
            return assistant, literal == card, True
        # Preserve an exact line-delimited delivery with surrounding context
        # for named replies. Bare agreement requires the standalone body.
        message = f"\n{assistant}\n"
        if f"\n{card}\n" in message:
            return assistant, True, False
        if f"\n{historical_card}\n" in message:
            return assistant, False, False
    return assistant, None, False


def plan_was_delivered(home, root, scope, plan, created, now):
    card = delivery_card(plan)
    historical_card = delivery_card(plan, historical=True)
    for record in session_records(home, scope["id"], root):
        at = instant(record.get("timestamp"))
        if record.get("type") == "response_item" and at and created <= at <= now:
            _, timing, _ = visible_delivery(
                record.get("payload", {}), card, historical_card
            )
            if timing is not None:
                return True
    return False


def concise_approval(content, plan, *, bare_offer):
    if content == "确认全部" or (
        content == "同意" and bare_offer and len(plan["lines"]) == 1
    ):
        return f"模拟批准 {plan['id']} 全部"
    matched = NUMBERED_APPROVAL.fullmatch(content)
    if not matched:
        return content
    indices = [int(index) for index in matched[1].split("、")]
    if any(index > len(plan["lines"]) for index in indices):
        return content
    selected = ",".join(plan["lines"][index - 1]["item"] for index in indices)
    return f"模拟批准 {plan['id']} {selected}"


def reply_from_text(content, plan, scope, event_id, delivered, at):
    words = content.split(" ")
    if len(words) not in (3, 5) or words[1] != plan["id"]:
        return None
    decision = {"模拟批准": "approve", "模拟撤回": "deny", "模拟重试": "retry"}.get(
        words[0]
    )
    if not decision or (decision == "retry") != (len(words) == 5):
        return None
    by_id = {line["item"]: line for line in plan["lines"]}
    selected = (
        list(by_id)
        if words[2] == "全部" and decision != "retry"
        else words[2].split(",")
    )
    if (
        not selected
        or len(set(selected)) != len(selected)
        or set(selected) - by_id.keys()
    ):
        return None
    items = [
        {
            "item": item,
            "quantity": by_id[item]["quantity"],
            "open": by_id[item]["opens_at"],
        }
        for item in selected
    ]
    if decision == "retry":
        if not re.fullmatch(r"[1-9][0-9]{0,15}", words[3]) or not re.fullmatch(
            r"TV3[0-9a-f]{25}", words[4]
        ):
            return None
        items[0].update(quantity=int(words[3]), after_attempt=words[4])
    return {
        "plan": plan["id"],
        "report_version": plan["report_version"],
        "author": scope["author"],
        "source": "user_reply",
        "owner": scope["owner"],
        "session": scope["id"],
        "event_id": event_id,
        "decision": decision,
        "at": at.isoformat(),
        "delivered_at": delivered.isoformat(),
        "items": items,
    }


def transcript_replies(home, root, scope, plan, created, now):
    """Read only visible deliveries and actual user messages from this session."""
    card = delivery_card(plan)
    historical_card = delivery_card(plan, historical=True)
    identity = (plan["id"], plan["report_version"])
    displayed = set()
    delivered = None
    late_timing_disclosed = False
    bare_offer = False
    replies = []
    for record in session_records(home, scope["id"], root):
        if record.get("type") != "response_item":
            continue
        at = instant(record.get("timestamp"))
        if not at or at > now:
            continue
        item = record.get("payload", {})
        if visible_message(item, "assistant"):
            assistant, timing, standalone = visible_delivery(
                item, card, historical_card
            )
            bare_offer = False
            if assistant:
                displayed.update(PLAN_HEADER.findall(assistant))
                if created <= at and timing is not None:
                    delivered, late_timing_disclosed = at, timing
                    bare_offer = timing and standalone
            continue
        if not delivered or at <= delivered or not visible_message(item, "user"):
            continue
        content = message_text(item, "user")
        if content and content.startswith(
            ("# AGENTS.md instructions", "<environment_context>")
        ):
            continue
        offered = bare_offer
        bare_offer = False
        if not content:
            continue
        meta = item.get("internal_chat_message_metadata_passthrough") or {}
        if meta and (
            not isinstance(meta, dict)
            or meta.get("content_item_kinds") != ["user.text"]
            or meta.get("content_item_metadata") != [{"provenance": {"type": "user"}}]
        ):
            continue
        # Native rollout messages can omit the optional message ID. The exact
        # recorded session, timestamp and text still identify a reply durably.
        event_id = sha256(
            json.dumps([scope["id"], item.get("id"), at.isoformat(), content]).encode()
        ).hexdigest()
        content = content.strip()
        if displayed == {identity}:
            content = concise_approval(content, plan, bare_offer=offered)
        reply = reply_from_text(content, plan, scope, event_id, delivered, at)
        if reply is not None and late_timing_disclosed:
            reply["late_timing_disclosed"] = True
        replies.append(reply)
    return replies


def scheduled_scope(home, root, report_day, session):
    if not SESSION_ID.fullmatch(session or ""):
        raise ValueError("original scheduled Codex session is unavailable")
    stored = home / ".config/trend-animals/daily-state"
    launch = read_json(stored / f"{report_day}.launch.json")
    id_ = launch.get("launch") or ""
    terminal = launch.get("terminal") or ""
    if (
        launch.get("day") != report_day
        or launch.get("name") != f"trend507-{report_day}"
        or launch.get("home") != str(home)
        or launch.get("root") != str(root)
        or launch.get("session") not in (None, session)
        or not re.fullmatch(r"[0-9a-f]{32}", id_)
        or not re.fullmatch(r"t[0-9a-z]+", terminal)
        or type(launch.get("created_ms")) is not int
        or not isinstance(launch.get("argv"), list)
        or not launch["argv"]
        or not launch.get("tab")
        or not launch.get("space")
    ):
        raise ValueError("scheduled launch receipt does not match this day and session")
    info = native_terminal(home, terminal)
    labels, foreground = info.get("labels") or {}, info.get("foreground") or {}
    if (
        info.get("state") != "live"
        or info.get("created_ms") != launch["created_ms"]
        or info.get("cwd") != str(root)
        or info.get("argv") != launch["argv"]
        or labels.get("herdr.tab") != launch["tab"]
        or labels.get("herdr.space") != launch["space"]
        or (info.get("status") or {}).get("app") != "codex"
        or foreground.get("name") != "codex"
        or foreground.get("cwd") != str(root)
        or type(foreground.get("pid")) is not int
        or not is_ancestor(foreground["pid"])
        or not original_prompt(home, session, root, id_, report_day)
    ):
        raise ValueError("native scheduled tab or original launch prompt is unverified")
    return {
        "owner": launch["tab"],
        "id": session,
        "date": report_day,
        "kind": "scheduled",
        "launch": id_,
        "author": "scheduled_tab_user",
    }


def release_pin(home):
    releases = home / ".config/trend-animals/skill-releases"
    relative = Path(__file__).resolve().relative_to(releases)
    if not HEX_SHA.fullmatch(relative.parts[0]):
        raise ValueError("paper code is outside the pinned daily skill release")
    return relative.parts[0]


def upload_receipt(home, scope, verification):
    root = home / ".config/trend-animals/daily-state"
    upload = read_json(root / f"{scope['date']}.upload.json")
    if (
        upload.get("day") != scope["date"]
        or upload.get("launch") != scope["launch"]
        or upload.get("revision") != verification["revision"]
        or upload.get("source_sha256", upload.get("sha256"))
        != verification["report_sha256"]
        or upload.get("uploaded") is not True
    ):
        raise ValueError("matching host report upload receipt is unavailable")


def check_selection_identity(
    selection, projection, report_day, revision, release, digest
):
    if not isinstance(projection, dict) or not isinstance(selection, dict):
        raise ValueError("daily report artifact date does not match")
    dates = [selection[key] for key in ("report_date", "date") if key in selection]
    if (
        projection.get("date") != report_day
        or not dates
        or any(value != report_day for value in dates)
    ):
        raise ValueError("daily report artifact date does not match")
    canonical = "schema" in selection
    if canonical and selection["schema"] not in (REPORT_SCHEMA, BRIDGE_REPORT_SCHEMA):
        raise ValueError("unsupported daily selection evidence schema")
    if selection.get("projection_sha256", None if canonical else digest) != digest:
        raise ValueError("daily report artifact digest does not match")
    source = selection.get("source", {} if canonical else {"commit": release})
    if (
        not isinstance(source, dict)
        or source.get("commit") != release
        or selection.get("revision", None if canonical else revision) != revision
    ):
        raise ValueError("daily report revision does not match its verification")


def latest_report(
    root,
    report_day,
    release,
    *,
    frozen_revision=None,
    source_only=False,
    expected_reader=None,
):
    """Verify the exact current bytes when a caller has already captured them."""
    if not day(report_day) or not HEX_SHA.fullmatch(release):
        raise ValueError("invalid daily report date or release")
    index = root / "reports/_evidence" / report_day
    revisions = [p.name for p in index.iterdir() if REVISION.fullmatch(p.name)]
    if not revisions:
        raise ValueError("verified daily report revision is unavailable")
    revision = max(revisions, key=lambda value: int(value[1:]))
    pointer = index / "latest-revision.json"
    if frozen_revision is not None:
        if not REVISION.fullmatch(frozen_revision) or frozen_revision not in revisions:
            raise ValueError("frozen daily report revision is unavailable")
        revision = frozen_revision
    elif pointer.exists():
        selected = read_json(pointer)
        if (selected.get("report_date"), selected.get("revision")) != (
            report_day,
            revision,
        ):
            raise ValueError("latest daily report pointer does not match")
    evidence = index / revision
    info = read_json(evidence / "report-verification.json")
    if (
        info.get("report_date") != report_day
        or info.get("revision") != revision
        or info.get("status") != "reader_verified"
        or info.get("source", {}).get("commit") != release
        or any(
            info.get("checks", {}).get(flag) is not True
            for flag in ("template", "privacy", "allowlist", "evidence_consistency")
        )
    ):
        raise ValueError("daily report revision does not match its verification")
    reader = root / "reports" / f"{report_day}.html"
    current = private_bytes(reader) if reader.exists() else b""
    if expected_reader is not None and current != expected_reader:
        raise ValueError("daily report artifact digest does not match")
    if (
        sha256(current).hexdigest() != info.get("report_sha256")
        and frozen_revision is not None
        and expected_reader is None
    ):
        for archived in (
            evidence / "history" / reader.name,
            evidence / "reader-original.html",
        ):
            if archived.exists():
                candidate = private_bytes(archived)
                if sha256(candidate).hexdigest() == info.get("report_sha256"):
                    current = candidate
                    break
    display = sha256(current).hexdigest() != info.get("report_sha256")
    if display:
        original = evidence / "reader-original.html"
        if not original.exists() or sha256(
            private_bytes(original)
        ).hexdigest() != info.get("report_sha256"):
            raise ValueError("daily report artifact digest does not match")
    checked = {}
    for field, path in (
        ("projection_sha256", evidence / "reader-projection.json"),
        ("selection_sha256", evidence / "selection-evidence.json"),
        ("fees_sha256", evidence / "fees.json"),
    ):
        raw = private_bytes(path)
        if sha256(raw).hexdigest() != info.get(field):
            raise ValueError("daily report artifact digest does not match")
        checked[field] = raw
    projection = strict_json(checked["projection_sha256"])
    selection = strict_json(checked["selection_sha256"])
    check_selection_identity(
        selection, projection, report_day, revision, release, info["projection_sha256"]
    )
    if not source_only:
        bridge_observations(projection, selection, info)
    if display and not source_only:
        from .paper_report import verified_display

        if current != verified_display(root, info):
            raise ValueError("daily report artifact digest does not match")
    projection.pop("paper_entry", None)
    return projection, selection, info
