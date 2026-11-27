"""Prepare trusted-host update candidates without changing any installed file."""

import argparse
import ast
import os
import re
import stat
import subprocess
from pathlib import Path

SOURCES = (
    "daily_sync.py",
    "daily_host.py",
    "run-daily.py",
    "daily_paper.py",
    "daily_handoff.py",
)


def trusted_config_tree(path):
    if path.resolve(strict=True) != path:
        raise ValueError("existing host configuration path is not canonical")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, encoding="utf-8") as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_mode & 0o022
            or info.st_size > 100_000
        ):
            raise ValueError("existing host configuration is not owner controlled")
        return ast.parse(stream.read()).body


def space_assignments(tree):
    return [
        node.value.value if isinstance(node.value, ast.Constant) else None
        for node in tree
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "SPACE_ID"
            for target in node.targets
        )
    ]


def installed_space(config):
    launcher = trusted_config_tree(config / "run-daily.py")
    assignments = space_assignments(launcher)
    imports = [
        node
        for node in launcher
        if isinstance(node, ast.ImportFrom)
        and any((name.asname or name.name) == "SPACE_ID" for name in node.names)
    ]
    if not assignments and len(imports) == 1:
        module = imports[0]
        if (
            module.level == 0
            and module.module == "daily_release"
            and any(
                name.name == "SPACE_ID" and name.asname is None for name in module.names
            )
        ):
            assignments = space_assignments(
                trusted_config_tree(config / "daily_release.py")
            )
    elif imports:
        assignments = []
    if (
        len(assignments) != 1
        or not isinstance(assignments[0], str)
        or not re.fullmatch(r"sp[0-9a-f]+", assignments[0])
    ):
        raise ValueError("existing host space authority is unavailable")
    return assignments[0]


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
    space = installed_space(Path.home() / ".config/trend-animals")
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    for name in SOURCES:
        (output / name).write_bytes((service / "host" / name).read_bytes())
    (output / "daily_release.py").write_text(
        "from pathlib import Path\n"
        f"PIN = {pin!r}\nSERVICE_ROOT = Path({str(service)!r})\n"
        f"SPACE_ID = {space!r}\n"
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
