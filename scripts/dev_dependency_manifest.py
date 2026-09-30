"""Print deterministic dependency/build evidence; never production provenance."""
from importlib.metadata import distributions
import hashlib
import json
import os
from pathlib import Path
import platform
import re


def manifest(root: Path) -> dict:
    dockerfile = (root / "Dockerfile.dev").read_text()
    return {
        "role": "test-only; synthetic Git snapshot is not a release SHA",
        "source_sha": os.environ.get("OPEN_TRADER_TEST_SOURCE_SHA", "unknown"),
        "source_state": os.environ.get("OPEN_TRADER_TEST_SOURCE_STATE", "unknown"),
        "python": platform.python_version(),
        "platform": platform.system() + "/" + platform.machine(),
        "lock_sha256": hashlib.sha256((root / "uv.lock").read_bytes()).hexdigest(),
        "base_images": re.findall(r"^FROM (\S+)", dockerfile, re.MULTILINE),
        "dependencies": sorted(
            (re.sub(r"[-_.]+", "-", d.metadata["Name"]).lower(), d.version)
            for d in distributions()
        ),
    }


if __name__ == "__main__":
    print(json.dumps(manifest(Path(__file__).resolve().parents[1]), indent=2, sort_keys=True))
