"""Retain the daily report's selection evidence before verifying its publication."""

import os
from hashlib import sha256
from pathlib import Path
from tempfile import NamedTemporaryFile

from .paper_authority import (
    HEX_SHA,
    REVISION,
    check_selection_identity,
    private_bytes,
    read_json,
    require_private_dir,
    strict_json,
)
from .paper_entry import encode
from .paper_evidence import REPORT_SCHEMA, TAIPEI, day, instant


def normalize_field_sources(selection, report_day):
    """Translate references to retained snapshots, without deriving new facts."""
    if "field_sources" not in selection:
        return selection
    records = selection.get("field_records")
    records = dict(records) if isinstance(records, dict) else {}
    sources = selection["field_sources"]
    if not isinstance(sources, dict):
        sources, records = {}, {}
    identity_fields = ("tmId", "asset", "tickerSymbol", "asOfDate")
    for key, proof in sources.items():
        if not isinstance(proof, dict) or not isinstance(proof.get("path"), str):
            records.pop(key, None)
            continue
        raw = proof.get("record")
        if not isinstance(raw, dict) or not all(
            name in raw for name in identity_fields
        ):
            records.pop(key, None)
            continue
        source = Path(proof["path"])
        if source.parts and not source.is_absolute() and not day(source.parts[0]):
            source = Path(report_day) / source
        normalized = {
            "source": source.as_posix(),
            "sha256": proof.get("sha256"),
            "location": proof.get("location"),
            "identity": {name: raw[name] for name in identity_fields},
            "fields": sorted(raw),
            "record": raw,
        }
        if "version" in proof:
            normalized["version"] = proof["version"]
        if key in records and records[key] != normalized:
            records.pop(key)
        else:
            records[key] = normalized
    return {
        key: value for key, value in selection.items() if key != "field_sources"
    } | {"field_records": records}


def prepare_report_evidence(root, report_day, revision, release):
    """Write a private canonical selection for an unpublished report revision."""
    if (
        not day(report_day)
        or not REVISION.fullmatch(revision)
        or not HEX_SHA.fullmatch(release)
    ):
        raise ValueError("invalid daily report date or release")
    directory = root.resolve(strict=True) / "reports/_evidence" / report_day / revision
    require_private_dir(directory)
    destination = directory / "selection-evidence.json"
    if (directory / "report-verification.json").exists() or destination.exists():
        raise ValueError("daily selection evidence already exists")
    projection = private_bytes(directory / "reader-projection.json")
    digest = sha256(projection).hexdigest()
    draft = read_json(directory / "selection-draft.json")
    check_selection_identity(
        draft, strict_json(projection), report_day, revision, release, digest
    )
    cutoff = instant(draft.get("cutoff"))
    if cutoff is None or cutoff.astimezone(TAIPEI).date().isoformat() != report_day:
        raise ValueError("frozen report cutoff is unavailable")
    selection = normalize_field_sources(draft, report_day)
    selection = {key: value for key, value in selection.items() if key != "date"}
    selection.update(
        schema=REPORT_SCHEMA,
        report_date=report_day,
        revision=revision,
        source={"commit": release},
        projection_sha256=digest,
    )
    selection.setdefault("field_records", {})
    selection.setdefault("instruments", [])
    payload = encode(selection).encode()
    with NamedTemporaryFile(dir=directory, prefix=".selection-") as temporary:
        temporary.write(payload)
        temporary.flush()
        os.fsync(temporary.fileno())
        os.link(temporary.name, destination)
    return {
        "projection_sha256": digest,
        "selection_sha256": sha256(payload).hexdigest(),
    }


def simulation_section(paper, report_day, revision):
    """Use the exact same allowlist as the scheduled reader."""
    import importlib.util
    from datetime import date

    path = (
        Path(__file__).resolve().parents[2]
        / ".agents/skills/trend-v3-daily-report/paper_public.py"
    )
    spec = importlib.util.spec_from_file_location("trend_paper_public", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.render_simulation(paper, date.fromisoformat(report_day), revision)


def atomic_private(path, raw):
    with NamedTemporaryFile(dir=path.parent, prefix=".paper-", delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def display_bytes(original, paper, report_day, revision):
    import re

    pattern = r'(<section id="simulation"[^>]*>\s*<h2[^>]*>.*?</h2>).*?(</section>)'
    result, count = re.subn(
        pattern,
        lambda match: match[1]
        + "\n"
        + simulation_section(paper, report_day, revision)
        + "\n      "
        + match[2],
        original.decode("utf-8"),
        count=1,
        flags=re.S,
    )
    if count != 1:
        raise ValueError("daily reader simulation section unavailable")
    return result.encode("utf-8")


def stored_status(root, report_day, now, info=None):
    from .paper_entry import PaperEntry
    from .paper_store import transaction

    database = root / "reports/_evidence/.paper-entry/state.sqlite3"
    if not database.exists():
        return None
    with transaction(database) as store:
        row = store.execute(
            "SELECT body FROM plans JOIN current_plan ON plans.id=current_plan.id "
            "WHERE current_plan.report_day=?",
            (report_day,),
        ).fetchone()
    if row is None:
        return None
    plan = strict_json(row["body"])
    if info is not None and plan["report_version"] != info["revision"]:
        return None
    if info is not None and (
        plan["date"] != report_day
        or plan["report_hash"]
        != sha256(
            (info["projection_sha256"] + info["selection_sha256"]).encode()
        ).hexdigest()
    ):
        raise ValueError("daily display evidence binding does not match")
    scope = {
        "owner": plan["owner"],
        "id": plan["session"],
        "date": report_day,
        "kind": "scheduled",
    }
    broker = type("ReadOnly", (), {"environment": "SIMULATE"})()
    return PaperEntry(database, broker, scope).status(now=now)


def verified_display(root, info):
    """Recompute the display from immutable source and current durable paper state."""
    report_day, revision = info["report_date"], info["revision"]
    evidence = root / "reports/_evidence" / report_day / revision
    manifest = read_json(evidence / "paper-display.json")
    original = private_bytes(evidence / "reader-original.html")
    if (
        sha256(original).hexdigest() != info["report_sha256"]
        or manifest.get("source_sha256") != info["report_sha256"]
        or manifest.get("source") != info["source"]
        or manifest.get("revision") != revision
        or manifest.get("report_date") != report_day
        or instant(manifest.get("as_of")) is None
    ):
        raise ValueError("daily display source binding does not match")
    paper = stored_status(root, report_day, instant(manifest["as_of"]), info)
    if paper is None or paper["report_version"] != revision:
        raise ValueError("daily display state does not match")
    expected = display_bytes(original, paper, report_day, revision)
    if sha256(expected).hexdigest() != manifest.get("display_sha256"):
        raise ValueError("daily display state does not match")
    return expected


def refresh_report(root, report_day, release, now):
    """Update only the display; preserve every original report evidence byte."""
    import fcntl

    from .paper_authority import latest_report

    lock_path = root / "reports/_evidence/.paper-entry/display.lock"
    with lock_path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        _, _, info = latest_report(root, report_day, release, source_only=True)
        paper = stored_status(root, report_day, now, info)
        if paper is None or paper["report_version"] != info["revision"]:
            return None
        reader = root / "reports" / f"{report_day}.html"
        evidence = root / "reports/_evidence" / report_day / info["revision"]
        original_path = evidence / "reader-original.html"
        if not original_path.exists():
            original = private_bytes(reader)
            if sha256(original).hexdigest() != info["report_sha256"]:
                raise ValueError("daily report artifact digest does not match")
            with NamedTemporaryFile(dir=evidence, prefix=".original-") as stream:
                stream.write(original)
                stream.flush()
                os.fsync(stream.fileno())
                os.link(stream.name, original_path)
        original = private_bytes(original_path)
        if sha256(original).hexdigest() != info["report_sha256"]:
            raise ValueError("daily report artifact digest does not match")
        output = display_bytes(original, paper, report_day, info["revision"])
        manifest = {
            "report_date": report_day,
            "revision": info["revision"],
            "source": info["source"],
            "source_sha256": info["report_sha256"],
            "display_sha256": sha256(output).hexdigest(),
            "as_of": now.isoformat(),
        }
        atomic_private(evidence / "paper-display.json", encode(manifest).encode())
        atomic_private(reader, output)
        return manifest


def pending_until(root, report_day, release):
    """Bound host sync to saved outstanding orders across US midnight."""
    from .paper_authority import latest_report
    from .paper_store import transaction

    database = root / "reports/_evidence/.paper-entry/state.sqlite3"
    if not database.exists():
        return None
    latest_report(root, report_day, release, source_only=True)
    with transaction(database) as store:
        rows = store.execute(
            "SELECT attempts.item, plans.body FROM attempts JOIN plans "
            "ON plans.id=attempts.plan WHERE plans.report_day=? "
            "AND attempts.state IN ('active', 'unknown')",
            (report_day,),
        )
        deadlines = []
        for row in rows:
            plan = strict_json(row["body"])
            for line in plan["lines"]:
                if line["item"] == row["item"]:
                    closes = instant(line["closes_at"])
                    if closes is not None:
                        deadlines.append(closes)
    return max(deadlines, default=None)
