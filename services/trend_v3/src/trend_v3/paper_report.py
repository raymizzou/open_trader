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
