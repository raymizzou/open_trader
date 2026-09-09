#!/usr/bin/env python3
"""Render and explicitly manage the standalone Trend Curve daily LaunchAgent."""

from __future__ import annotations

import argparse
import os
import plistlib
import subprocess
import sys
from pathlib import Path
from tempfile import NamedTemporaryFile


LABEL = "com.open-trader.trend-curve-daily"
PLIST_NAME = f"{LABEL}.plist"
DEFAULT_LAUNCH_AGENTS_DIR = Path.home() / "Library" / "LaunchAgents"
DEFAULT_LAUNCHCTL = Path("/bin/launchctl")


class InstallerError(ValueError):
    """A user-correctable installer input or launchctl failure."""


def _absolute_existing_path(
    value: str,
    *,
    name: str,
    executable: bool = False,
) -> Path:
    path = Path(value).expanduser()
    if (
        not path.is_absolute()
        or not path.exists()
        or (executable and (not path.is_file() or not os.access(path, os.X_OK)))
    ):
        raise InstallerError(f"{name} must be an absolute existing path")
    return path


def _absolute_path(value: str, *, name: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise InstallerError(f"{name} must be an absolute path")
    return path


def _source_root(value: str) -> Path:
    path = _absolute_existing_path(value, name="code path")
    if not path.is_dir() or not (path / "src" / "open_trader" / "__init__.py").is_file():
        raise InstallerError(
            "code path must contain src/open_trader/__init__.py"
        )
    return path.resolve()


def _reject_launch_agents_output(output: Path, launch_agents_dir: Path) -> Path:
    try:
        resolved_output = output.resolve()
        protected_directories = {
            launch_agents_dir.resolve(),
            DEFAULT_LAUNCH_AGENTS_DIR.expanduser().resolve(),
        }
    except (OSError, RuntimeError) as exc:
        raise InstallerError("plist output path cannot be resolved") from exc
    for protected_directory in protected_directories:
        try:
            resolved_output.relative_to(protected_directory)
        except ValueError:
            continue
        raise InstallerError(
            "plist output must be outside LaunchAgents directories"
        )
    return resolved_output


def _daily_log_directory(daily_config: Path) -> Path:
    return (daily_config.parent / "trend_curve_daily").resolve()


def _build_plist(
    *, code_path: Path, interpreter: Path, daily_config: Path
) -> dict[str, object]:
    working_directory = code_path if code_path.is_dir() else code_path.parent
    log_directory = _daily_log_directory(daily_config)
    return {
        "Label": LABEL,
        "ProgramArguments": [
            str(interpreter),
            "-m",
            "open_trader",
            "trend-curve",
            "daily",
            "--check",
            "--daily-config",
            str(daily_config),
        ],
        "WorkingDirectory": str(working_directory),
        # The application performs the due decision in Asia/Shanghai.  The
        # calendar trigger is only a wake-up hint and is not its source of
        # truth when the host timezone differs.
        "EnvironmentVariables": {
            "TZ": "Asia/Shanghai",
            "PYTHONPATH": str((code_path / "src").resolve()),
        },
        "StandardOutPath": str(log_directory / "launchd.stdout.log"),
        "StandardErrorPath": str(log_directory / "launchd.stderr.log"),
        "StartInterval": 60,
        "RunAtLoad": True,
        "StartCalendarInterval": {"Hour": 12, "Minute": 0},
    }


def _write_plist(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with NamedTemporaryFile(
            mode="wb", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as stream:
            temporary = Path(stream.name)
            plistlib.dump(payload, stream, sort_keys=False)
        os.replace(temporary, path)
    except (OSError, plistlib.InvalidFileException, TypeError, ValueError) as exc:
        try:
            temporary.unlink()
        except (UnboundLocalError, FileNotFoundError):
            pass
        raise InstallerError(f"could not write LaunchAgent plist: {path}") from exc


def _validate_owned_plist(path: Path) -> None:
    try:
        with path.open("rb") as stream:
            payload = plistlib.load(stream)
    except (OSError, plistlib.InvalidFileException, TypeError, ValueError) as exc:
        raise InstallerError("existing LaunchAgent plist is invalid") from exc
    if not isinstance(payload, dict) or payload.get("Label") != LABEL:
        raise InstallerError("existing LaunchAgent plist is not owned by this tool")


def _run_launchctl(launchctl: Path, action: str, plist_path: Path) -> None:
    try:
        completed = subprocess.run(
            [str(launchctl), action, str(plist_path)],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise InstallerError(f"launchctl {action} failed") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        suffix = f": {detail}" if detail else ""
        raise InstallerError(f"launchctl {action} failed{suffix}")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Render or explicitly install the Trend Curve daily LaunchAgent"
    )
    parser.add_argument("--code-path", required=True)
    parser.add_argument("--interpreter", required=True)
    parser.add_argument("--daily-config", required=True)
    parser.add_argument(
        "--launch-agents-dir", default=str(DEFAULT_LAUNCH_AGENTS_DIR)
    )
    parser.add_argument("--launchctl", default=str(DEFAULT_LAUNCHCTL))
    parser.add_argument(
        "--plist-output",
        help="Write a dry-run plist to this explicit path outside LaunchAgents",
    )
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--dry-run", action="store_const", const="dry-run", dest="action")
    actions.add_argument("--install", action="store_const", const="install", dest="action")
    actions.add_argument(
        "--uninstall", action="store_const", const="uninstall", dest="action"
    )
    parser.set_defaults(action="dry-run")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        code_path = _source_root(args.code_path)
        interpreter = _absolute_existing_path(
            args.interpreter, name="interpreter", executable=True
        )
        daily_config = _absolute_existing_path(
            args.daily_config, name="daily config"
        )
        if not daily_config.is_file():
            raise InstallerError("daily config must be an absolute existing file")
        launch_agents_dir = _absolute_path(
            args.launch_agents_dir, name="LaunchAgents directory"
        )
        launchctl = _absolute_path(args.launchctl, name="launchctl")
        plist_path = launch_agents_dir / PLIST_NAME
        payload = _build_plist(
            code_path=code_path,
            interpreter=interpreter,
            daily_config=daily_config,
        )

        if args.action == "dry-run":
            if args.plist_output is not None:
                output_path = _absolute_path(args.plist_output, name="plist output")
                output_path = _reject_launch_agents_output(
                    output_path, launch_agents_dir
                )
                _write_plist(output_path, payload)
                print(f"dry-run plist: {output_path}")
            else:
                print(f"dry-run: would manage {plist_path}")
            return 0

        launchctl = _absolute_existing_path(
            str(launchctl), name="launchctl", executable=True
        )
        if args.plist_output is not None:
            raise InstallerError("--plist-output is only valid with --dry-run")

        if args.action == "install":
            if plist_path.exists():
                _validate_owned_plist(plist_path)
            _write_plist(plist_path, payload)
            try:
                _daily_log_directory(daily_config).mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise InstallerError("could not create daily log directory") from exc
            try:
                _run_launchctl(launchctl, "load", plist_path)
            except InstallerError:
                # Keep the exact generated plist for diagnosis; no unrelated
                # path is touched when launchctl rejects this job.
                raise
            print(f"installed launchd agent: {LABEL}")
            return 0

        if args.action == "uninstall":
            if plist_path.exists():
                _validate_owned_plist(plist_path)
                _run_launchctl(launchctl, "unload", plist_path)
                plist_path.unlink()
                print(f"removed launchd agent: {plist_path}")
            else:
                print(f"launchd agent not installed: {plist_path}")
            return 0

        raise InstallerError(f"unknown action: {args.action}")
    except InstallerError as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
