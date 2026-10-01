"""Lightweight spawn targets: importing a lock owner must not import SDKs."""
from pathlib import Path


def hold_preparation_lock(path: str, ready: object, release: object) -> None:
    import fcntl

    lock_path = Path(path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        ready.set()  # type: ignore[attr-defined]
        release.wait()  # type: ignore[attr-defined]  # parent releases or terminates on failure
