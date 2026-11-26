"""Fixed private sync helper; install in config, outside daily workspace/add-dir."""

import argparse
import hashlib
import json
import os
import stat
import subprocess
import time
import uuid
from contextlib import contextmanager
from datetime import date
from pathlib import Path

from daily_paper import display_digest, source_digest
from daily_release import PIN
from daily_support import SyncDenied


@contextmanager
def directory(path):
    """Walk from / using descriptors; reject symlinks at every component."""
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("noncanonical directory")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            child = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd
            )
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def read_at(fd, name):
    file = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    with os.fdopen(file, "rb") as stream:
        st = os.fstat(stream.fileno())
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
            raise ValueError("not a single regular file")
        return stream.read()


def read_file(path):
    with directory(path.parent) as fd:
        return read_at(fd, path.name)


def metadata(path):
    bridge = Path(__file__).with_name("cloud-metadata")
    try:
        result = subprocess.run(
            [str(bridge), str(path)], capture_output=True, timeout=10
        )
    except subprocess.TimeoutExpired:
        raise TimeoutError("Foundation metadata timed out") from None
    if result.returncode:
        raise ConnectionError("Foundation metadata unavailable")
    return json.loads(result.stdout)


def folder_identity(folder):
    v = metadata(folder)
    with directory(folder) as fd:
        st = os.fstat(fd)
        v.update(
            path=str(folder),
            device=st.st_dev,
            inode=st.st_ino,
            writable=os.access(folder, os.R_OK | os.W_OK | os.X_OK),
        )
    v["safe"] = (
        v.get("safe") is True and v.get("icloud") is True and v.get("shared") is True
    )
    return v


def verified_report(root, day, revision):
    if date.fromisoformat(day).isoformat() != day or not __import__("re").fullmatch(
        r"v[0-9]{2,}", revision
    ):
        raise ValueError("invalid date/revision")
    report = root / "reports" / f"{day}.html"
    body = read_file(report)
    digest = hashlib.sha256(body).hexdigest()
    evidence = root / "reports/_evidence" / day / revision
    verification = json.loads(read_file(evidence / "report-verification.json"))
    original_digest = source_digest(root, day, revision, verification, body)
    if (
        verification.get("report_date") != day
        or verification.get("revision") != revision
        or verification.get("status") != "reader_verified"
        or verification.get("source", {}).get("commit") != PIN
        or verification.get("report_sha256") != original_digest
        or any(
            verification.get("checks", {}).get(k) is not True
            for k in ("template", "privacy", "allowlist", "evidence_consistency")
        )
    ):
        raise ValueError("current report verification missing or stale")
    for field, filename in [
        ("projection_sha256", "reader-projection.json"),
        ("selection_sha256", "selection-evidence.json"),
        ("fees_sha256", "fees.json"),
    ]:
        if hashlib.sha256(
            read_file(evidence / filename)
        ).hexdigest() != verification.get(field):
            raise ValueError("evidence hash mismatch")
    display_digest(root, day, revision, verification, body)
    return report, digest


def latest_report(root, day):
    evidence = root / "reports/_evidence" / day
    revisions = [
        p.name
        for p in evidence.iterdir()
        if __import__("re").fullmatch(r"v[0-9]{2,}", p.name)
    ]
    if not revisions:
        raise ValueError("no verified revision")
    revision = max(revisions, key=lambda r: int(r[1:]))
    pointer = evidence / "latest-revision.json"
    if pointer.exists():
        current = json.loads(read_file(pointer))
        if current.get("report_date") != day or current.get("revision") != revision:
            raise ValueError("latest revision pointer not current")
    report, digest = verified_report(root, day, revision)
    return report, digest, revision


class VerifiedCopy:
    def __init__(
        self,
        source,
        dest,
        authorize,
        uploaded,
        expected=None,
        allowed_destinations=None,
    ):
        self.source, self.dest = source, dest
        self.authorize, self.uploaded, self.expected = authorize, uploaded, expected
        self.allowed_destinations = allowed_destinations

    def copy_verified(self):
        if not self.authorize():
            raise ValueError("folder identity/permission denied")
        body = read_file(self.source)
        digest = hashlib.sha256(body).hexdigest()
        if self.expected is not None and digest != self.expected:
            raise ValueError("report changed after verification")
        with directory(self.dest.parent) as fd:
            before = os.fstat(fd)
            try:
                existing = read_at(fd, self.dest.name)
            except FileNotFoundError:
                existing = None
            if (
                existing is not None
                and existing != body
                and self.allowed_destinations is not None
                and hashlib.sha256(existing).hexdigest()
                not in self.allowed_destinations
            ):
                raise SyncDenied("conflicting destination not a known verified version")
            if existing != body:
                tmp = ".trend-sync-" + uuid.uuid4().hex
                handle = os.open(
                    tmp,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=fd,
                )
                try:
                    with os.fdopen(handle, "wb") as stream:
                        stream.write(body)
                        stream.flush()
                        os.fsync(stream.fileno())
                    # Recheck the named directory before atomic replace.
                    # Never follow paths for writes.
                    if not self.authorize():
                        raise ValueError("folder changed before replace")
                    with directory(self.dest.parent) as current:
                        st = os.fstat(current)
                        if (st.st_dev, st.st_ino) != (before.st_dev, before.st_ino):
                            raise ValueError("folder replaced")
                    try:
                        current_body = read_at(fd, self.dest.name)
                    except FileNotFoundError:
                        current_body = None
                    if current_body != existing or read_file(self.source) != body:
                        raise ValueError("source/destination changed before replace")
                    os.replace(tmp, self.dest.name, src_dir_fd=fd, dst_dir_fd=fd)
                    os.fsync(fd)
                finally:
                    try:
                        os.unlink(tmp, dir_fd=fd)
                    except FileNotFoundError:
                        pass
            if read_at(fd, self.dest.name) != body or read_file(self.source) != body:
                raise ValueError("copy/source verification failed")
        return digest

    def upload(self, digest):
        # A positive Foundation result is bound to the still-matching destination bytes.
        return bool(
            self.uploaded(self.dest)
            and hashlib.sha256(read_file(self.dest)).hexdigest() == digest
            and self.authorize()
        )


def sync_report(home, day, revision, proof, launch):
    """Sandbox reader: never copy on unavailable metadata or execute host work."""
    root = home / ".local/share/trend-animals"
    source, digest, latest = latest_report(root, day)
    if revision != latest:
        raise ValueError("requested revision is stale")
    folder = home / "Library/Mobile Documents/com~apple~CloudDocs/trend_v3"
    receipt_path = home / ".config/trend-animals/daily-state" / f"{day}.upload.json"
    receipt = json.loads(read_file(receipt_path))
    if (
        receipt.get("day") != day
        or receipt.get("launch") != launch
        or receipt.get("revision") != latest
        or receipt.get("sha256") != digest
        or receipt.get("uploaded") is not True
        or not 0 <= time.time() - receipt.get("timestamp", 0) <= 300
    ):
        raise ValueError("fresh host upload receipt pending")
    current = metadata(folder)
    if (
        current.get("safe") is not True
        or current.get("icloud") is not True
        or current.get("shared") is False
        or current.get("permission") not in (None, "read-write")
        or current.get("owner") not in (None, "", receipt["identity"]["owner"])
        or current.get("role") not in (None, "unknown", "participant")
    ):
        raise SyncDenied("current folder explicitly denied/changed")
    with directory(folder) as fd:
        st = os.fstat(fd)
        if (
            (st.st_dev, st.st_ino)
            != (receipt["identity"]["device"], receipt["identity"]["inode"])
            or not os.access(folder, os.R_OK | os.W_OK | os.X_OK)
            or hashlib.sha256(read_at(fd, source.name)).hexdigest() != digest
        ):
            raise ValueError("current destination changed")
    return dict(
        bytes_verified=True,
        uploaded=True,
        sha256=digest,
        revision=latest,
        other_device_receipt="UNVERIFIED",
        source="trusted host receipt",
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--day", required=True)
    parser.add_argument("--revision", default="v01")
    parser.add_argument("--launch", required=True)
    parser.add_argument(
        "--proof", required=True, help="exact immutable original launch JSON, or null"
    )
    args = parser.parse_args()
    try:
        result = sync_report(
            Path.home(), args.day, args.revision, json.loads(args.proof), args.launch
        )
    except (OSError, ValueError, ConnectionError, subprocess.SubprocessError):
        print(json.dumps({"status": "sync pending", "market_queries": 0}))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0 if result["uploaded"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
