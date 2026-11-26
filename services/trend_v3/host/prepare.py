"""Prepare trusted-host update candidates without changing any installed file."""

import argparse
import ast
import re
import subprocess
from pathlib import Path

SOURCES = ("daily_sync.py", "daily_host.py", "run-daily.py", "daily_paper.py")


def prepare(release, output):
    release = release.resolve(strict=True)
    pin = release.name
    if not re.fullmatch(r"[0-9a-f]{40}", pin):
        raise ValueError("release directory must be named by its full commit")
    expected_parent = Path.home() / ".config/trend-animals/skill-releases"
    if release.parent != expected_parent.resolve(strict=True):
        raise ValueError("release is outside the trusted scheduled release directory")
    # Select authority from Git and immutable checkout location, not request data.
    actual = subprocess.check_output(
        ["git", "-C", str(release), "rev-parse", "HEAD"], text=True
    ).strip()
    dirty = subprocess.check_output(
        ["git", "-C", str(release), "status", "--porcelain"], text=True
    )
    if actual != pin or dirty:
        raise ValueError("selected host release is not a clean exact-SHA checkout")
    service = release / "services/trend_v3"
    existing = Path.home() / ".config/trend-animals/run-daily.py"
    tree = ast.parse(existing.read_text())
    matches = [
        node.value.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        and isinstance(node.value, ast.Constant)
        and any(
            isinstance(target, ast.Name) and target.id == "SPACE_ID"
            for target in node.targets
        )
    ]
    if len(matches) != 1 or not re.fullmatch(r"sp[0-9a-f]+", matches[0]):
        raise ValueError("existing host space authority is unavailable")
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    for name in SOURCES:
        (output / name).write_bytes((service / "host" / name).read_bytes())
    (output / "daily_release.py").write_text(
        "from pathlib import Path\n"
        f"PIN = {pin!r}\nSERVICE_ROOT = Path({str(service)!r})\n"
        f"SPACE_ID = {matches[0]!r}\n"
    )
    # The candidate launcher refuses an old SOP pin. The separately authorized
    # installer must update/check that existing SOP and verify all helper pins.
    return pin


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("release", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    print(prepare(args.release, args.output))
