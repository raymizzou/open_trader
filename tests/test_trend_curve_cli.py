from __future__ import annotations

from base64 import b64encode
import csv
from datetime import datetime, timezone
import hashlib
import json
import os
import subprocess
import sqlite3
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import pytest

import open_trader.trend_animals as trend_animals
import open_trader.cli as cli
import open_trader.notifications as notifications
import open_trader.trend_curve_research as trend_curve_research
from open_trader.cli import build_parser


SLB_CURVE_ENCRYPTED = (
    "ehtRChN4vTYXmnU0XeI1jUK90sU2F7krL05NDT98YL8UTnyA+ZPzfFhGtc16tZucbQiHicdZb6zASL09TNBfXk9jMxyga1yBUjoxwZmQV9f6VhEKsU0ASQEFlmLlF9Drr5Dhe3ZG0O3JuK/ZhV319o/zMC9iziANRRZEbU3zeZltSCoRpfm/2nkUUxlYReMvaHhaE5njUaXGc4yXvmKKB/DB+3KE/phKeRKYP/zZ2mB7dEvRW7nppyiVRq35neeyP0EMKv28Jvjf5VVzhukl+JrtrR6YsHnyPcDOTcf3qT+vPyEvieKpK9oVsMc0dcvRHmRw5vxii3b0k0L5VaXOPd4II3UiPVt8hIeQwSE5BvTLaqbOYAZgIdb8VFR0sVfGUR0b8XqS9i1f1LUB7XEaSmB/OknT2hbVGtJcsN96W0GYUMgCwgxHQxmJspmlFlZ9zJ2DCMGc0XKeQL/ztER2WCvYidySWZe7Il/lPCsc6UPgwHQw2n+SVAG7E0zG7UoqK8FPcneVJd77KoFVcj8vx8woK3cTAbqZEzmyNPg9t6SdN01c0qONGzC6qFBivXyw6R0OEGWI9UYbPc4r3WcbmTiBKYdVxn+31ioLr1lG8w3NElYsMBArmln2uBWUonOzeme6spz0p3d1qgAfGsyve6Ixu/sMagxMIXR6ZUizW+UmgKmbEQVx8b3hiGK4JFp6"
)


def _daily_encrypted_curve_payload(payload: dict[str, object]) -> str:
    completed = subprocess.run(
        [
            "openssl",
            "enc",
            "-aes-128-ecb",
            "-K",
            "41464433303434323736393838413830",
            "-nosalt",
        ],
        input=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        capture_output=True,
        check=True,
    )
    return b64encode(completed.stdout).decode("ascii")


def _daily_supplier_timestamp(value: str) -> int:
    return int(
        datetime.fromisoformat(f"{value}T00:00:00+08:00")
        .astimezone(timezone.utc)
        .timestamp()
        * 1000
    )


def _daily_supplier_payload() -> dict[str, object]:
    return {
        "code": "00000",
        "data": [
            [{"labelName": "开香槟"}, {"labelName": "危险信号"}],
            [
                {
                    "rq": _daily_supplier_timestamp("2026-09-04"),
                    "下行趋势": "立秋\n右侧第7天",
                }
            ],
            [
                {
                    "rq": _daily_supplier_timestamp("2026-09-03"),
                    "px": "1.11",
                    "rps": "11.1",
                    "temperature": "温",
                    "mom": "1",
                    "yoy": "2",
                    "bar": "3",
                    "momDelta": "u",
                    "yoyDelta": "d",
                    "yield": "0.031",
                },
                {
                    "rq": _daily_supplier_timestamp("2026-09-04"),
                    "px": "1.22",
                    "rps": "12.2",
                    "temperature": "热",
                    "mom": "4",
                    "yoy": "5",
                    "bar": "6",
                    "momDelta": "u",
                    "yoyDelta": "d",
                    "yield": "0.031",
                },
            ],
            {},
        ],
    }


def _write_daily_hermes_success(path: Path) -> None:
    path.write_text(
        f"#!{sys.executable}\n"
        "import json\n"
        "print(json.dumps({'success': True, 'platform': 'feishu', 'message_id': 'test-message'}))\n",
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | 0o111)


def _write_cli_inputs(database: Path, prices: Path) -> None:
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TABLE trend_curve_points (
                market TEXT NOT NULL,
                symbol TEXT NOT NULL,
                curve_date TEXT NOT NULL,
                price TEXT NOT NULL,
                temperature TEXT NOT NULL,
                strength TEXT NOT NULL,
                mom TEXT,
                yoy TEXT,
                bar TEXT,
                asset_id INTEGER NOT NULL,
                group_id INTEGER NOT NULL,
                tm_id INTEGER NOT NULL,
                ccy_id INTEGER NOT NULL,
                PRIMARY KEY (market, symbol, curve_date)
            )
            """
        )
        connection.executemany(
            """
            INSERT INTO trend_curve_points
            (market, symbol, curve_date, price, temperature, strength,
             mom, yoy, bar, asset_id, group_id, tm_id, ccy_id)
            VALUES ('US', 'TEST', ?, '10', ?, '80', NULL, NULL, NULL,
                    1, 2, 3, 4)
            """,
            [("2026-01-01", "温"), ("2026-01-02", "热")],
        )
    with prices.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=("date", "open", "high", "low", "close"))
        writer.writeheader()
        writer.writerows(
            [
                {"date": "2026-01-01", "open": "10", "high": "10", "low": "10", "close": "10"},
                {"date": "2026-01-02", "open": "10", "high": "10", "low": "10", "close": "10"},
                {"date": "2026-01-03", "open": "11", "high": "11", "low": "11", "close": "11"},
            ]
        )


def _write_portfolio_cli_inputs(
    database: Path,
    prices_dir: Path,
    portfolio: Path,
    exclusions: Path,
) -> None:
    prices_dir.mkdir()
    _write_cli_inputs(database, prices_dir / "TEST.csv")
    portfolio.write_text(
        "market,asset_class,symbol,analysis_symbol,name,market_value_hkd,ai_eligible\n"
        "US,stock,TEST,TEST,测试标的,100,true\n"
        "US,stock,SKIP,SKIP,排除标的,100,true\n",
        encoding="utf-8",
    )
    exclusions.write_text(
        json.dumps({"US.SKIP": "configured exclusion"}), encoding="utf-8"
    )


def test_trend_curve_cli_exposes_collect_only() -> None:
    parser = build_parser()
    collect_args = parser.parse_args(
        ["trend-curve", "collect", "--watchlist", "watchlist.json"]
    )

    assert collect_args.command == "trend-curve"
    assert collect_args.trend_curve_command == "collect"
    assert collect_args.watchlist == Path("watchlist.json")
    assert collect_args.database == Path("data/trend_curve/history.sqlite3")
    assert collect_args.mmkv_helper is None

    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(
            [
                "trend-curve",
                "backtest",
                "--market",
                "US",
                "--symbol",
                "SLB",
                "--entry-transition",
                "平转温",
                "--exit-transition",
                "热→温",
            ]
        )
    assert exc_info.value.code == 2


def test_trend_curve_cli_accepts_explicit_mmkv_snapshot() -> None:
    parser = build_parser()
    explicit_args = parser.parse_args(
        [
            "trend-curve",
            "collect",
            "--watchlist",
            "watchlist.json",
            "--mmkv-path",
            "copied/wx64e4edbab5e14356",
        ]
    )
    default_args = parser.parse_args(
        ["trend-curve", "collect", "--watchlist", "watchlist.json"]
    )

    assert (explicit_args.mmkv_path, default_args.mmkv_path) == (
        Path("copied/wx64e4edbab5e14356"),
        None,
    )


def test_trend_curve_cli_reports_and_resumes_incomplete_batch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    watchlist = tmp_path / "watchlist.json"
    targets = [
        {
            "market": "CN",
            "symbol": symbol,
            "asset_id": 10002,
            "group_id": 303121,
            "tm_id": tm_id,
            "ccy_id": 100,
        }
        for symbol, tm_id in (("A", 101), ("B", 202), ("C", 303))
    ]
    watchlist.write_text(json.dumps(targets), encoding="utf-8")
    database = tmp_path / "history.sqlite3"
    healthy_response = _daily_encrypted_curve_payload(_daily_supplier_payload())
    curve_requests: list[int] = []
    credential_reads: list[tuple[str, int]] = []
    response_mode = "auth-block-b"

    def read_credentials(*_args: object, **_kwargs: object) -> tuple[str, int]:
        credentials = ("cli-token", 123)
        credential_reads.append(credentials)
        return credentials

    def curve_transport(
        _url: str, body: bytes, _headers: dict[str, str]
    ) -> dict[str, object]:
        nonlocal response_mode
        target_id = json.loads(body)["id"]
        curve_requests.append(target_id)
        if response_mode == "auth-block-b" and target_id == 202:
            return {"success": False, "code": "A00004"}
        return {
            "success": True,
            "code": "00000",
            "data": {"encryptedData": healthy_response},
        }

    monkeypatch.setattr(
        trend_curve_research, "read_wechat_mini_credentials", read_credentials
    )
    monkeypatch.setattr(
        trend_curve_research, "_default_curve_transport", curve_transport
    )

    command = [
        "trend-curve",
        "collect",
        "--watchlist",
        str(watchlist),
        "--database",
        str(database),
        "--batch-id",
        "cli-batch-abc",
        "--require-snapshot",
        "--expected-date",
        "CN.A=2026-09-04",
        "--expected-date",
        "CN.B=2026-09-04",
        "--expected-date",
        "CN.C=2026-09-04",
    ]

    first_exit = cli.main(command)
    first_output = capsys.readouterr()
    assert (
        first_exit,
        first_output.err,
        "batch_id: cli-batch-abc" in first_output.out,
        "status: auth_blocked" in first_output.out,
        "completed: 1" in first_output.out,
        "pending: 2" in first_output.out,
        "auth_blocked" in first_output.out,
        curve_requests,
    ) == (1, "", True, True, True, True, True, [101, 202])

    response_mode = "healthy"
    second_exit = cli.main(command)
    second_output = capsys.readouterr()
    with sqlite3.connect(database) as connection:
        point_rows = connection.execute(
            "SELECT market, symbol, curve_date FROM trend_curve_points "
            "ORDER BY market, symbol, curve_date"
        ).fetchall()
        snapshot_rows = connection.execute(
            "SELECT market, symbol, snapshot_date FROM trend_curve_daily_snapshots "
            "ORDER BY market, symbol, snapshot_date"
        ).fetchall()
        batch_rows = connection.execute(
            "SELECT market, symbol, completed_at FROM trend_curve_batch_items "
            "WHERE batch_id = ? ORDER BY market, symbol",
            ("cli-batch-abc",),
        ).fetchall()
    assert (
        second_exit,
        second_output.err,
        "batch_id: cli-batch-abc" in second_output.out,
        "status: complete" in second_output.out,
        "completed: 3" in second_output.out,
        "pending: 0" in second_output.out,
        curve_requests,
        point_rows,
        snapshot_rows,
        len(batch_rows),
        all(row[2] is not None for row in batch_rows),
        len(credential_reads),
    ) == (
        0,
        "",
        True,
        True,
        True,
        True,
        [101, 202, 202, 303],
        [
            ("CN", "A", "2026-09-03"),
            ("CN", "A", "2026-09-04"),
            ("CN", "B", "2026-09-03"),
            ("CN", "B", "2026-09-04"),
            ("CN", "C", "2026-09-03"),
            ("CN", "C", "2026-09-04"),
        ],
        [
            ("CN", "A", "2026-09-04"),
            ("CN", "B", "2026-09-04"),
            ("CN", "C", "2026-09-04"),
        ],
        3,
        True,
        2,
    )

    before_invalid_requests = list(curve_requests)
    before_invalid_credentials = list(credential_reads)
    collect_prefix = command[:9]
    invalid_cases = (
        (
            collect_prefix
            + [
                "--expected-date",
                "CN.A=2026-9-4",
                "--expected-date",
                "CN.B=2026-09-04",
                "--expected-date",
                "CN.C=2026-09-04",
            ],
            "expected dates are malformed",
        ),
        (
            collect_prefix + ["--expected-date", "CN.A=2026-09-04"],
            "expected dates are malformed",
        ),
        (
            collect_prefix
            + [
                "--expected-date",
                "CN.A=2026-09-04",
                "--expected-date",
                "CN.A=2026-09-04",
                "--expected-date",
                "CN.B=2026-09-04",
                "--expected-date",
                "CN.C=2026-09-04",
            ],
            "contains duplicate key",
        ),
    )
    for invalid_command, diagnostic in invalid_cases:
        with pytest.raises(SystemExit) as error:
            cli.main(invalid_command)
        invalid_output = capsys.readouterr()
        assert error.value.code == 2
        assert diagnostic in invalid_output.err
    assert (curve_requests, credential_reads) == (
        before_invalid_requests,
        before_invalid_credentials,
    )


def test_trend_curve_launchd_installer_is_explicit_and_scoped(
    tmp_path: Path,
) -> None:
    installer = Path(__file__).parents[1] / "scripts" / "install_trend_curve_launchd.py"
    code_root = Path(__file__).resolve().parents[1]
    config = tmp_path / "daily.json"
    config.write_text('{"coverage": "cached"}', encoding="utf-8")
    launch_agents = tmp_path / "LaunchAgents"
    launchctl_log = tmp_path / "launchctl.log"
    fake_launchctl = tmp_path / "launchctl"
    fake_launchctl.write_text(
        f"#!{sys.executable}\n"
        "import pathlib, sys\n"
        f"pathlib.Path({str(launchctl_log)!r}).open('a', encoding='utf-8').write("
        "' '.join(sys.argv[1:]) + '\\n')\n",
        encoding="utf-8",
    )
    fake_launchctl.chmod(fake_launchctl.stat().st_mode | 0o111)
    database = tmp_path / "history.sqlite3"
    database.write_bytes(b"database-survives-install-and-uninstall")

    def invoke(
        *actions: str,
        code_path: Path = code_root,
        interpreter: str = sys.executable,
        daily_config: Path = config,
        plist_output: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        command = [
            sys.executable,
            str(installer),
            "--code-path",
            str(code_path),
            "--interpreter",
            interpreter,
            "--daily-config",
            str(daily_config),
            "--launch-agents-dir",
            str(launch_agents),
            "--launchctl",
            str(fake_launchctl),
        ]
        if plist_output is not None:
            command.extend(("--plist-output", str(plist_output)))
        command.extend(actions)
        return subprocess.run(command, capture_output=True, text=True, check=False)

    # Invalid absolute paths fail before an install can create its plist or call launchctl.
    for invalid_kwargs in (
        {"code_path": tmp_path / "missing-code"},
        {"interpreter": str(tmp_path / "missing-python")},
        {"daily_config": tmp_path / "missing-config.json"},
    ):
        invalid = invoke("--install", **invalid_kwargs)
        assert invalid.returncode == 2
        assert "absolute existing" in invalid.stderr
        assert not launch_agents.exists()
        assert not launchctl_log.exists()

    default_dry_run = invoke()
    assert (
        default_dry_run.returncode,
        default_dry_run.stderr,
        launch_agents.exists(),
        launchctl_log.exists(),
    ) == (0, "", False, False)

    generated = tmp_path / "generated.plist"
    explicit_dry_run = invoke("--dry-run", plist_output=generated)
    assert (
        explicit_dry_run.returncode,
        explicit_dry_run.stderr,
        generated.is_file(),
        launch_agents.exists(),
        launchctl_log.exists(),
    ) == (0, "", True, False, False)
    with generated.open("rb") as stream:
        plist = __import__("plistlib").load(stream)
    assert (
        plist["Label"],
        plist["WorkingDirectory"],
        plist["ProgramArguments"],
        plist["StartInterval"],
        plist["RunAtLoad"],
        plist["StartCalendarInterval"],
        "KeepAlive" in plist,
    ) == (
        "com.open-trader.trend-curve-daily",
        str(code_root),
        [
            sys.executable,
            "-m",
            "open_trader",
            "trend-curve",
            "daily",
            "--check",
            "--daily-config",
            str(config),
        ],
        60,
        True,
        {"Hour": 12, "Minute": 0},
        False,
    )

    installed = invoke("--install")
    plist_path = launch_agents / "com.open-trader.trend-curve-daily.plist"
    assert (
        installed.returncode,
        installed.stderr,
        plist_path.is_file(),
        launchctl_log.read_text(encoding="utf-8").splitlines(),
        database.read_bytes(),
    ) == (
        0,
        "",
        True,
        [f"load {plist_path}"],
        b"database-survives-install-and-uninstall",
    )
    unrelated = launch_agents / "unrelated.plist"
    unrelated.write_bytes(b"keep me")

    uninstalled = invoke("--uninstall")
    assert (
        uninstalled.returncode,
        uninstalled.stderr,
        plist_path.exists(),
        unrelated.read_bytes(),
        launchctl_log.read_text(encoding="utf-8").splitlines(),
        database.read_bytes(),
    ) == (
        0,
        "",
        False,
        b"keep me",
        [f"load {plist_path}", f"unload {plist_path}"],
        b"database-survives-install-and-uninstall",
    )


def test_trend_curve_installer_binds_selected_source_root(
    tmp_path: Path,
) -> None:
    installer = Path(__file__).parents[1] / "scripts" / "install_trend_curve_launchd.py"
    code_root = Path(__file__).resolve().parents[1]
    config = tmp_path / "daily.json"
    config.write_text('{"coverage": "cached"}', encoding="utf-8")
    launch_agents = tmp_path / "LaunchAgents"
    fake_launchctl = tmp_path / "launchctl"
    fake_launchctl.write_text(f"#!{sys.executable}\n", encoding="utf-8")
    fake_launchctl.chmod(fake_launchctl.stat().st_mode | 0o111)

    def invoke(
        *actions: str,
        code_path: Path = code_root,
        daily_config: Path = config,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                str(installer),
                "--code-path",
                str(code_path),
                "--interpreter",
                sys.executable,
                "--daily-config",
                str(daily_config),
                "--launch-agents-dir",
                str(launch_agents),
                "--launchctl",
                str(fake_launchctl),
                *actions,
            ],
            capture_output=True,
            text=True,
            check=False,
        )

    rendered = tmp_path / "rendered.plist"
    dry_run = invoke("--dry-run", "--plist-output", str(rendered))
    assert (dry_run.returncode, dry_run.stderr, rendered.is_file()) == (0, "", True)
    with rendered.open("rb") as stream:
        plist = __import__("plistlib").load(stream)

    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    environment.update(plist["EnvironmentVariables"])
    import_check = subprocess.run(
        [
            sys.executable,
            "-c",
            "import open_trader; print(open_trader.__file__)",
        ],
        cwd=plist["WorkingDirectory"],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert (
        import_check.returncode,
        Path(import_check.stdout.strip()).resolve(),
    ) == (0, (code_root / "src" / "open_trader" / "__init__.py").resolve())

    help_check = subprocess.run(
        [
            sys.executable,
            "-m",
            "open_trader",
            "trend-curve",
            "daily",
            "--help",
        ],
        cwd=plist["WorkingDirectory"],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert help_check.returncode == 0

    empty_code_root = tmp_path / "empty-code"
    empty_code_root.mkdir()
    code_file = tmp_path / "code-file"
    code_file.write_text("not a source root", encoding="utf-8")
    config_directory = tmp_path / "config-directory"
    config_directory.mkdir()
    for invalid_code_path, invalid_config in (
        (empty_code_root, config),
        (code_file, config),
        (code_root, config_directory),
    ):
        invalid = invoke(
            "--install",
            code_path=invalid_code_path,
            daily_config=invalid_config,
        )
        assert invalid.returncode == 2
        assert not launch_agents.exists()


def test_trend_curve_installer_dry_run_rejects_launchagents_output(
    tmp_path: Path,
) -> None:
    installer = Path(__file__).parents[1] / "scripts" / "install_trend_curve_launchd.py"
    code_root = Path(__file__).resolve().parents[1]
    config = tmp_path / "daily.json"
    config.write_text('{"coverage": "cached"}', encoding="utf-8")
    home = tmp_path / "home"
    standard_launch_agents = home / "Library" / "LaunchAgents"
    standard_launch_agents.mkdir(parents=True)
    configured_launch_agents = tmp_path / "configured-agents"
    configured_launch_agents.mkdir()
    launchctl_log = tmp_path / "launchctl.log"
    fake_launchctl = tmp_path / "launchctl"
    fake_launchctl.write_text(
        f"#!{sys.executable}\n"
        f"import pathlib\npathlib.Path({str(launchctl_log)!r}).write_text('called')\n",
        encoding="utf-8",
    )
    fake_launchctl.chmod(fake_launchctl.stat().st_mode | 0o111)

    def invoke(
        output: Path,
        *,
        launch_agents_dir: Path | None = configured_launch_agents,
        explicit_dry_run: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        command = [
            sys.executable,
            str(installer),
            "--code-path",
            str(code_root),
            "--interpreter",
            sys.executable,
            "--daily-config",
            str(config),
        ]
        if launch_agents_dir is not None:
            command.extend(("--launch-agents-dir", str(launch_agents_dir)))
        command.extend(("--launchctl", str(fake_launchctl)))
        if explicit_dry_run:
            command.append("--dry-run")
        command.extend(("--plist-output", str(output)))
        environment = dict(os.environ)
        environment["HOME"] = str(home)
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            env=environment,
        )

    # The default action is already dry-run; the explicit action is a
    # compatibility spelling used by operators.
    default_output = standard_launch_agents / "default.plist"
    explicit_output = configured_launch_agents / "explicit.plist"
    dotdot_output = configured_launch_agents / "nested" / ".." / "dotdot.plist"
    symlink_alias = tmp_path / "agents-alias"
    symlink_alias.symlink_to(configured_launch_agents, target_is_directory=True)
    symlink_output = symlink_alias / "symlink.plist"
    custom_to_standard = standard_launch_agents / "custom-standard.plist"
    protected_outputs = (
        default_output,
        explicit_output,
        dotdot_output,
        symlink_output,
        custom_to_standard,
    )
    for output in protected_outputs:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"keep-this-plist")

    invocations = [
        invoke(default_output, launch_agents_dir=None),
        invoke(explicit_output, explicit_dry_run=True),
        invoke(dotdot_output),
        invoke(symlink_output),
        invoke(custom_to_standard),
    ]
    assert all(result.returncode == 2 for result in invocations)
    assert all("plist output" in result.stderr for result in invocations)
    assert all(output.read_bytes() == b"keep-this-plist" for output in protected_outputs)
    assert not launchctl_log.exists()

    ordinary_output = tmp_path / "ordinary" / "generated.plist"
    ordinary = invoke(ordinary_output)
    assert (
        ordinary.returncode,
        ordinary.stderr,
        ordinary_output.is_file(),
        launchctl_log.exists(),
    ) == (0, "", True, False)


def test_trend_curve_installer_preserves_foreign_or_invalid_plist(
    tmp_path: Path,
) -> None:
    installer = Path(__file__).parents[1] / "scripts" / "install_trend_curve_launchd.py"
    code_root = Path(__file__).resolve().parents[1]
    config = tmp_path / "daily.json"
    config.write_text('{"coverage": "cached"}', encoding="utf-8")
    launch_agents = tmp_path / "LaunchAgents"
    launch_agents.mkdir()
    plist_path = launch_agents / "com.open-trader.trend-curve-daily.plist"
    launchctl_log = tmp_path / "launchctl.log"
    fake_launchctl = tmp_path / "launchctl"
    fake_launchctl.write_text(
        f"#!{sys.executable}\n"
        "import pathlib, sys\n"
        f"pathlib.Path({str(launchctl_log)!r}).open('a', encoding='utf-8').write(' '.join(sys.argv[1:]) + '\\n')\n",
        encoding="utf-8",
    )
    fake_launchctl.chmod(fake_launchctl.stat().st_mode | 0o111)

    def invoke(action: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                str(installer),
                "--code-path",
                str(code_root),
                "--interpreter",
                sys.executable,
                "--daily-config",
                str(config),
                "--launch-agents-dir",
                str(launch_agents),
                "--launchctl",
                str(fake_launchctl),
                action,
            ],
            capture_output=True,
            text=True,
            check=False,
        )

    invalid_plists = (
        __import__("plistlib").dumps({"Label": "com.other.agent"}),
        b"not-a-plist",
        __import__("plistlib").dumps({"ProgramArguments": []}),
    )
    for content in invalid_plists:
        plist_path.write_bytes(content)
        install = invoke("--install")
        uninstall = invoke("--uninstall")
        assert (
            install.returncode,
            uninstall.returncode,
            install.stderr,
            uninstall.stderr,
            plist_path.read_bytes(),
            launchctl_log.exists(),
        ) == (2, 2, install.stderr, uninstall.stderr, content, False)
        assert "com.other.agent" not in install.stderr + uninstall.stderr

    valid_existing = __import__("plistlib").dumps(
        {"Label": "com.open-trader.trend-curve-daily"}
    )
    plist_path.write_bytes(valid_existing)
    installed = invoke("--install")
    uninstalled = invoke("--uninstall")
    assert (
        installed.returncode,
        uninstalled.returncode,
        installed.stderr,
        uninstalled.stderr,
        plist_path.exists(),
        launchctl_log.read_text(encoding="utf-8").splitlines(),
    ) == (
        0,
        0,
        "",
        "",
        False,
        [f"load {plist_path}", f"unload {plist_path}"],
    )


def test_trend_curve_installer_configures_independent_logs(
    tmp_path: Path,
) -> None:
    installer = Path(__file__).parents[1] / "scripts" / "install_trend_curve_launchd.py"
    code_root = Path(__file__).resolve().parents[1]
    config_parent = tmp_path / "runtime" / "config"
    config_parent.mkdir(parents=True)
    config = config_parent / "daily.json"
    config.write_text('{"coverage": "cached"}', encoding="utf-8")
    launch_agents = tmp_path / "LaunchAgents"
    launchctl_log = tmp_path / "launchctl.log"
    expected_log_parent = config_parent / "trend_curve_daily"
    fake_launchctl = tmp_path / "launchctl"
    fake_launchctl.write_text(
        f"#!{sys.executable}\n"
        "import pathlib, sys\n"
        f"assert pathlib.Path({str(expected_log_parent)!r}).is_dir()\n"
        f"pathlib.Path({str(launchctl_log)!r}).open('a', encoding='utf-8').write(' '.join(sys.argv[1:]) + '\\n')\n",
        encoding="utf-8",
    )
    fake_launchctl.chmod(fake_launchctl.stat().st_mode | 0o111)
    database = tmp_path / "runtime" / "history.sqlite3"
    database.write_bytes(b"keep-database")

    command_prefix = [
        sys.executable,
        str(installer),
        "--code-path",
        str(code_root),
        "--interpreter",
        sys.executable,
        "--daily-config",
        str(config),
        "--launch-agents-dir",
        str(launch_agents),
        "--launchctl",
        str(fake_launchctl),
    ]
    rendered = tmp_path / "rendered.plist"
    dry_run = subprocess.run(
        [*command_prefix, "--dry-run", "--plist-output", str(rendered)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert (
        dry_run.returncode,
        dry_run.stderr,
        rendered.is_file(),
        expected_log_parent.exists(),
        launch_agents.exists(),
    ) == (0, "", True, False, False)
    with rendered.open("rb") as stream:
        plist = __import__("plistlib").load(stream)
    stdout_path = Path(plist["StandardOutPath"])
    stderr_path = Path(plist["StandardErrorPath"])
    assert (
        stdout_path,
        stderr_path,
        stdout_path.is_absolute(),
        stderr_path.is_absolute(),
        stdout_path != stderr_path,
    ) == (
        (expected_log_parent / "launchd.stdout.log").resolve(),
        (expected_log_parent / "launchd.stderr.log").resolve(),
        True,
        True,
        True,
    )

    installed = subprocess.run(
        [*command_prefix, "--install"],
        capture_output=True,
        text=True,
        check=False,
    )
    stdout_path.write_bytes(b"stdout-sentinel")
    stderr_path.write_bytes(b"stderr-sentinel")
    plist_path = launch_agents / "com.open-trader.trend-curve-daily.plist"
    assert (
        installed.returncode,
        installed.stderr,
        expected_log_parent.is_dir(),
        plist_path.is_file(),
        database.read_bytes(),
    ) == (0, "", True, True, b"keep-database")

    uninstalled = subprocess.run(
        [*command_prefix, "--uninstall"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert (
        uninstalled.returncode,
        uninstalled.stderr,
        plist_path.exists(),
        stdout_path.read_bytes(),
        stderr_path.read_bytes(),
        database.read_bytes(),
        launchctl_log.read_text(encoding="utf-8").splitlines(),
    ) == (
        0,
        "",
        False,
        b"stdout-sentinel",
        b"stderr-sentinel",
        b"keep-database",
        [f"load {plist_path}", f"unload {plist_path}"],
    )


def test_daily_recovers_after_login_without_waiting_for_tomorrow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    mappings_root = tmp_path / "mappings"
    mapping_rows = {
        "CN": {
            "asset": "A股",
            "futu_symbol": "SH.600000",
            "market": "CN",
            "schema_version": "open_trader.trend_symbol_mapping.v1",
            "trend_animals_symbol": "600000.SH",
            "trend_animals_tm_id": 101,
        },
        "HK": {
            "asset": "港股",
            "futu_symbol": "HK.00700",
            "market": "HK",
            "schema_version": "open_trader.trend_symbol_mapping.v1",
            "trend_animals_symbol": "0700.HK",
            "trend_animals_tm_id": 202,
        },
        "US": {
            "asset": "美股",
            "futu_symbol": "US.AAPL",
            "market": "US",
            "schema_version": "open_trader.trend_symbol_mapping.v1",
            "trend_animals_symbol": "AAPL",
            "trend_animals_tm_id": 303,
        },
    }
    for market, row in mapping_rows.items():
        directory = mappings_root / market
        directory.mkdir(parents=True)
        (directory / f"{row['futu_symbol']}.json").write_text(
            json.dumps(row), encoding="utf-8"
        )

    hermes_capture = tmp_path / "hermes.log"
    hermes = tmp_path / "hermes-fake"
    hermes.write_text(
        f"#!{sys.executable}\n"
        "import pathlib, sys\n"
        f"pathlib.Path({str(hermes_capture)!r}).open('a', encoding='utf-8').write('run\\n')\n"
        "print('{\"success\": true, \"platform\": \"feishu\", \"message_id\": \"m\"}')\n",
        encoding="utf-8",
    )
    hermes.chmod(hermes.stat().st_mode | 0o111)

    healthy_response = _daily_encrypted_curve_payload(_daily_supplier_payload())
    curve_requests: list[int] = []
    credential_reads: list[tuple[str, int]] = []
    credentials: tuple[str, int] = ("before-login-token", 111)
    response_mode = "auth-block-b"

    def read_credentials(*_args: object, **_kwargs: object) -> tuple[str, int]:
        credential_reads.append(credentials)
        return credentials

    def curve_transport(
        _url: str, body: bytes, _headers: dict[str, str]
    ) -> dict[str, object]:
        target_id = json.loads(body)["id"]
        curve_requests.append(target_id)
        if response_mode == "auth-block-b" and target_id == 202:
            return {"success": False, "code": "A00004"}
        return {
            "success": True,
            "code": "00000",
            "data": {"encryptedData": healthy_response},
        }

    monkeypatch.setattr(
        trend_curve_research, "read_wechat_mini_credentials", read_credentials
    )
    monkeypatch.setattr(
        trend_curve_research, "_default_curve_transport", curve_transport
    )
    monkeypatch.setattr(
        trend_animals,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("daily must not call the paid endpoint")
        ),
    )
    monkeypatch.setattr(
        notifications,
        "_post_json",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("daily must not send a direct notification")
        ),
    )
    database = tmp_path / "history.sqlite3"
    config = tmp_path / "daily.json"
    config.write_text(
        json.dumps(
            {
                "coverage": "cached",
                "database": str(database),
                "mappings_root": str(mappings_root),
                "request_interval_seconds": 0.001,
                "request_limit": 100,
                "max_duration_seconds": 60.0,
                "hermes_timeout_seconds": 1.0,
                "hermes_executable": str(hermes),
            }
        ),
        encoding="utf-8",
    )
    command = ["trend-curve", "daily", "--check", "--daily-config", str(config)]
    shanghai = ZoneInfo("Asia/Shanghai")
    current_now = datetime(2026, 9, 9, 12, 10, tzinfo=shanghai)
    monkeypatch.setattr(cli, "_trend_curve_daily_now", lambda: current_now)

    first_exit = cli.main(command)
    first_output = capsys.readouterr()
    first_payload = json.loads(first_output.out)
    assert (
        first_exit,
        first_output.err,
        first_payload["status"],
        first_payload["completed_count"],
        first_payload["pending_count"],
        first_payload["request_count"],
        first_payload["stop_reason"],
        curve_requests,
        hermes_capture.read_text(encoding="utf-8").splitlines(),
    ) == (1, "", "auth_blocked", 1, 2, 2, "auth_blocked", [101, 202], ["run"])

    current_now = datetime(2026, 9, 9, 13, 0, tzinfo=shanghai)
    second_exit = cli.main(command)
    second_output = capsys.readouterr()
    second_payload = json.loads(second_output.out)
    assert (
        second_exit,
        second_output.err,
        second_payload["status"],
        second_payload["request_count"],
        second_payload["delivery_status"],
        curve_requests,
        hermes_capture.read_text(encoding="utf-8").splitlines(),
    ) == (1, "", "auth_blocked", 0, "not_run", [101, 202], ["run"])

    credentials = ("after-login-token", 222)
    response_mode = "healthy"
    third_exit = cli.main(command)
    third_output = capsys.readouterr()
    third_payload = json.loads(third_output.out)
    with sqlite3.connect(database) as connection:
        completed_items = connection.execute(
            "SELECT market, symbol, completed_at FROM trend_curve_batch_items "
            "WHERE batch_id = ? ORDER BY market, symbol",
            (first_payload["batch_id"],),
        ).fetchall()
    assert (
        third_exit,
        third_output.err,
        third_payload["status"],
        third_payload["completed_count"],
        third_payload["pending_count"],
        third_payload["request_count"],
        curve_requests,
        hermes_capture.read_text(encoding="utf-8").splitlines(),
        completed_items,
        credential_reads,
        all(secret not in third_output.out for secret in ("before-login-token", "after-login-token", "111", "222")),
    ) == (
        0,
        "",
        "complete",
        3,
        0,
        2,
        [101, 202, 202, 303],
        ["run", "run"],
        [("CN", "600000", completed_items[0][2]), ("HK", "00700", completed_items[1][2]), ("US", "AAPL", completed_items[2][2])],
        [
            ("before-login-token", 111),
            ("before-login-token", 111),
            ("after-login-token", 222),
        ],
        True,
    )


def test_daily_catches_up_once_after_missed_noon(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    mappings_root = tmp_path / "mappings"
    mapping = {
        "asset": "A股",
        "futu_symbol": "SH.600000",
        "market": "CN",
        "schema_version": "open_trader.trend_symbol_mapping.v1",
        "trend_animals_symbol": "600000.SH",
        "trend_animals_tm_id": 101,
    }
    mapping_directory = mappings_root / "CN"
    mapping_directory.mkdir(parents=True)
    (mapping_directory / "SH.600000.json").write_text(
        json.dumps(mapping), encoding="utf-8"
    )
    hermes_capture = tmp_path / "hermes.log"
    hermes = tmp_path / "hermes-fake"
    hermes.write_text(
        f"#!{sys.executable}\n"
        "import pathlib\n"
        f"pathlib.Path({str(hermes_capture)!r}).open('a', encoding='utf-8').write('run\\n')\n"
        "print('{\"success\": true, \"platform\": \"feishu\", \"message_id\": \"m\"}')\n",
        encoding="utf-8",
    )
    hermes.chmod(hermes.stat().st_mode | 0o111)
    database = tmp_path / "history.sqlite3"
    config = tmp_path / "daily.json"
    config.write_text(
        json.dumps(
            {
                "coverage": "cached",
                "database": str(database),
                "mappings_root": str(mappings_root),
                "request_interval_seconds": 0.001,
                "request_limit": 100,
                "max_duration_seconds": 60.0,
                "hermes_timeout_seconds": 1.0,
                "hermes_executable": str(hermes),
            }
        ),
        encoding="utf-8",
    )
    healthy_response = _daily_encrypted_curve_payload(_daily_supplier_payload())
    curve_requests: list[int] = []
    credential_reads: list[tuple[str, int]] = []

    def read_credentials(*_args: object, **_kwargs: object) -> tuple[str, int]:
        credential_reads.append(("daily-token", 123))
        return ("daily-token", 123)

    def curve_transport(
        _url: str, body: bytes, _headers: dict[str, str]
    ) -> dict[str, object]:
        curve_requests.append(json.loads(body)["id"])
        return {
            "success": True,
            "code": "00000",
            "data": {"encryptedData": healthy_response},
        }

    monkeypatch.setattr(
        trend_curve_research, "read_wechat_mini_credentials", read_credentials
    )
    monkeypatch.setattr(
        trend_curve_research, "_default_curve_transport", curve_transport
    )
    monkeypatch.setattr(
        trend_animals,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("daily must not call the paid endpoint")
        ),
    )
    monkeypatch.setattr(
        notifications,
        "_post_json",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("daily must not send a direct notification")
        ),
    )
    command = ["trend-curve", "daily", "--check", "--daily-config", str(config)]
    current_now = datetime(2026, 9, 10, 1, 0, tzinfo=timezone.utc)  # 09:00 Shanghai
    monkeypatch.setattr(cli, "_trend_curve_daily_now", lambda: current_now)

    before_noon_exit = cli.main(command)
    before_noon_output = capsys.readouterr()
    before_noon_payload = json.loads(before_noon_output.out)
    assert (
        before_noon_exit,
        before_noon_output.err,
        before_noon_payload["status"],
        before_noon_payload["request_count"],
        before_noon_payload["delivery_status"],
        curve_requests,
        credential_reads,
        database.exists(),
        hermes_capture.exists(),
    ) == (0, "", "not_due", 0, "not_run", [], [], False, False)

    current_now = datetime(2026, 9, 10, 6, 0, tzinfo=timezone.utc)  # 14:00 Shanghai
    catch_up_exit = cli.main(command)
    catch_up_output = capsys.readouterr()
    catch_up_payload = json.loads(catch_up_output.out)
    with sqlite3.connect(database) as connection:
        batch_count = connection.execute(
            "SELECT COUNT(*) FROM trend_curve_batches WHERE batch_id = ?",
            ("trend-curve-daily-cached-2026-09-10",),
        ).fetchone()[0]
        snapshot_dates = connection.execute(
            "SELECT snapshot_date FROM trend_curve_daily_snapshots "
            "ORDER BY snapshot_date"
        ).fetchall()
    assert (
        catch_up_exit,
        catch_up_output.err,
        catch_up_payload["status"],
        catch_up_payload["completed_count"],
        catch_up_payload["pending_count"],
        catch_up_payload["request_count"],
        curve_requests,
        batch_count,
        snapshot_dates,
        hermes_capture.read_text(encoding="utf-8").splitlines(),
    ) == (0, "", "complete", 1, 0, 1, [101], 1, [("2026-09-04",)], ["run"])

    repeated_exit = cli.main(command)
    repeated_output = capsys.readouterr()
    repeated_payload = json.loads(repeated_output.out)
    assert (
        repeated_exit,
        repeated_output.err,
        repeated_payload["status"],
        repeated_payload["request_count"],
        repeated_payload["delivery_status"],
        curve_requests,
        hermes_capture.read_text(encoding="utf-8").splitlines(),
    ) == (0, "", "complete", 0, "not_run", [101], ["run"])


def test_daily_checks_wait_after_stable_failure_but_continue_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    mappings_root = tmp_path / "mappings"
    mapping_rows = {
        "CN": {
            "asset": "A股",
            "futu_symbol": "SH.600000",
            "market": "CN",
            "schema_version": "open_trader.trend_symbol_mapping.v1",
            "trend_animals_symbol": "600000.SH",
            "trend_animals_tm_id": 101,
        },
        "HK": {
            "asset": "港股",
            "futu_symbol": "HK.00700",
            "market": "HK",
            "schema_version": "open_trader.trend_symbol_mapping.v1",
            "trend_animals_symbol": "0700.HK",
            "trend_animals_tm_id": 202,
        },
        "US": {
            "asset": "美股",
            "futu_symbol": "US.AAPL",
            "market": "US",
            "schema_version": "open_trader.trend_symbol_mapping.v1",
            "trend_animals_symbol": "AAPL",
            "trend_animals_tm_id": 303,
        },
    }
    for market, row in mapping_rows.items():
        directory = mappings_root / market
        directory.mkdir(parents=True)
        (directory / f"{row['futu_symbol']}.json").write_text(
            json.dumps(row), encoding="utf-8"
        )

    healthy_response = _daily_encrypted_curve_payload(_daily_supplier_payload())
    source_root = Path(__file__).resolve().parents[1] / "src"
    shanghai = ZoneInfo("Asia/Shanghai")
    current_now = datetime(2026, 9, 9, 12, 0, tzinfo=shanghai)
    monkeypatch.setattr(cli, "_trend_curve_daily_now", lambda: current_now)
    monkeypatch.setattr(
        trend_curve_research,
        "read_wechat_mini_credentials",
        lambda *_args, **_kwargs: ("stable-failure-token", 123456),
    )

    for mode in ("data_gap", "decryption", "transport"):
        database = tmp_path / f"failure-{mode}.sqlite3"
        config = tmp_path / f"failure-{mode}.json"
        hermes_capture = tmp_path / f"failure-{mode}-hermes.log"
        hermes = tmp_path / f"failure-{mode}-hermes"
        hermes.write_text(
            f"#!{sys.executable}\n"
            "import pathlib\n"
            f"pathlib.Path({str(hermes_capture)!r}).open('a', encoding='utf-8').write('send\\n')\n"
            "print('{\"success\": true, \"platform\": \"feishu\", \"message_id\": \"m\"}')\n",
            encoding="utf-8",
        )
        hermes.chmod(hermes.stat().st_mode | 0o111)
        config.write_text(
            json.dumps(
                {
                    "coverage": "cached",
                    "database": str(database),
                    "mappings_root": str(mappings_root),
                    "request_interval_seconds": 0.001,
                    "request_limit": 10,
                    "max_duration_seconds": 60.0,
                    "hermes_timeout_seconds": 1.0,
                    "hermes_executable": str(hermes),
                }
            ),
            encoding="utf-8",
        )
        requests: list[int] = []

        def curve_transport(
            _url: str, body: bytes, _headers: dict[str, str]
        ) -> dict[str, object]:
            requests.append(json.loads(body)["id"])
            if mode == "transport":
                raise RuntimeError("synthetic transport failure")
            if mode == "decryption":
                return {
                    "success": True,
                    "code": "00000",
                    "data": {"encryptedData": "not-base64"},
                }
            return {"success": False, "code": "00000"}

        monkeypatch.setattr(
            trend_curve_research, "_default_curve_transport", curve_transport
        )
        command = ["trend-curve", "daily", "--check", "--daily-config", str(config)]
        first_exit = cli.main(command)
        first_output = capsys.readouterr()
        first_payload = json.loads(first_output.out)
        summary = json.loads(
            Path(first_payload["summary_path"]).read_text(encoding="utf-8")
        )
        assert (
            first_exit,
            first_output.err,
            first_payload["status"],
            first_payload["completed_count"],
            first_payload["pending_count"],
            first_payload["request_count"],
            first_payload["stop_reason"],
            summary["delivery_status"],
            requests,
            hermes_capture.read_text(encoding="utf-8").splitlines(),
        ) == (1, "", "partial", 0, 3, 3, "data_gap", "accepted", [101, 202, 303], ["send"])
        summary_count = len(list((tmp_path / "trend_curve_daily").glob("*.json")))

        child = subprocess.run(
            [
                sys.executable,
                "-c",
                "from datetime import datetime\n"
                "import sys\n"
                "from zoneinfo import ZoneInfo\n"
                "import open_trader.cli as cli\n"
                "import open_trader.trend_curve_research as research\n"
                "def fail(*_args, **_kwargs):\n"
                "    raise AssertionError('stable failure must not retry external work')\n"
                "cli._trend_curve_daily_now = lambda: datetime(2026, 9, 9, 12, 0, tzinfo=ZoneInfo('Asia/Shanghai'))\n"
                "research.read_wechat_mini_credentials = lambda *_args, **_kwargs: ('stable-failure-token', 123456)\n"
                "research._default_curve_transport = fail\n"
                "raise SystemExit(cli.main(['trend-curve', 'daily', '--check', '--daily-config', sys.argv[1]]))\n",
                str(config),
            ],
            cwd=Path(__file__).resolve().parents[1],
            env={**os.environ, "PYTHONPATH": str(source_root)},
            capture_output=True,
            text=True,
            check=False,
        )
        child_payload = json.loads(child.stdout)
        assert (
            child.returncode,
            child.stderr,
            child_payload["status"],
            child_payload["completed_count"],
            child_payload["pending_count"],
            child_payload["request_count"],
            child_payload["stop_reason"],
            requests,
            hermes_capture.read_text(encoding="utf-8").splitlines(),
            len(list((tmp_path / "trend_curve_daily").glob("*.json"))),
        ) == (1, "", "partial", 0, 3, 0, "data_gap", [101, 202, 303], ["send"], summary_count)

        def healthy_transport(
            _url: str, body: bytes, _headers: dict[str, str]
        ) -> dict[str, object]:
            requests.append(json.loads(body)["id"])
            return {
                "success": True,
                "code": "00000",
                "data": {"encryptedData": healthy_response},
            }

        monkeypatch.setattr(
            trend_curve_research, "_default_curve_transport", healthy_transport
        )
        manual_exit = cli.main(
            ["trend-curve", "daily", "--daily-config", str(config)]
        )
        manual_output = capsys.readouterr()
        manual_payload = json.loads(manual_output.out)
        assert (
            manual_exit,
            manual_output.err,
            manual_payload["status"],
            manual_payload["completed_count"],
            manual_payload["pending_count"],
            requests,
            hermes_capture.read_text(encoding="utf-8").splitlines(),
        ) == (0, "", "complete", 3, 0, [101, 202, 303, 101, 202, 303], ["send", "send"])

    budget_database = tmp_path / "budget.sqlite3"
    budget_config = tmp_path / "budget.json"
    budget_hermes_capture = tmp_path / "budget-hermes.log"
    budget_hermes = tmp_path / "budget-hermes"
    _write_daily_hermes_success(budget_hermes)
    budget_config.write_text(
        json.dumps(
            {
                "coverage": "cached",
                "database": str(budget_database),
                "mappings_root": str(mappings_root),
                "request_interval_seconds": 0.001,
                "request_limit": 2,
                "max_duration_seconds": 60.0,
                "hermes_timeout_seconds": 1.0,
                "hermes_executable": str(budget_hermes),
            }
        ),
        encoding="utf-8",
    )
    budget_requests: list[int] = []

    def budget_transport(
        _url: str, body: bytes, _headers: dict[str, str]
    ) -> dict[str, object]:
        budget_requests.append(json.loads(body)["id"])
        return {
            "success": True,
            "code": "00000",
            "data": {"encryptedData": healthy_response},
        }

    monkeypatch.setattr(
        trend_curve_research, "_default_curve_transport", budget_transport
    )
    budget_command = [
        "trend-curve",
        "daily",
        "--check",
        "--daily-config",
        str(budget_config),
    ]
    first_budget_exit = cli.main(budget_command)
    first_budget_output = capsys.readouterr()
    first_budget_payload = json.loads(first_budget_output.out)
    second_budget_exit = cli.main(budget_command)
    second_budget_output = capsys.readouterr()
    second_budget_payload = json.loads(second_budget_output.out)
    assert (
        first_budget_exit,
        first_budget_payload["status"],
        first_budget_payload["stop_reason"],
        first_budget_payload["completed_count"],
        first_budget_payload["pending_count"],
        first_budget_payload["request_count"],
        second_budget_exit,
        second_budget_output.err,
        second_budget_payload["status"],
        second_budget_payload["completed_count"],
        second_budget_payload["pending_count"],
        second_budget_payload["request_count"],
        budget_requests,
    ) == (1, "partial", "request_limit", 2, 1, 2, 0, "", "complete", 3, 0, 1, [101, 202, 303])


def test_daily_prioritizes_previous_uncovered_targets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    mappings_root = tmp_path / "mappings"
    mapping_rows = {
        "CN": {
            "asset": "A股",
            "futu_symbol": "SH.600000",
            "market": "CN",
            "schema_version": "open_trader.trend_symbol_mapping.v1",
            "trend_animals_symbol": "600000.SH",
            "trend_animals_tm_id": 101,
        },
        "HK": {
            "asset": "港股",
            "futu_symbol": "HK.00700",
            "market": "HK",
            "schema_version": "open_trader.trend_symbol_mapping.v1",
            "trend_animals_symbol": "0700.HK",
            "trend_animals_tm_id": 202,
        },
        "US": {
            "asset": "美股",
            "futu_symbol": "US.AAPL",
            "market": "US",
            "schema_version": "open_trader.trend_symbol_mapping.v1",
            "trend_animals_symbol": "AAPL",
            "trend_animals_tm_id": 303,
        },
    }
    for market, row in mapping_rows.items():
        directory = mappings_root / market
        directory.mkdir(parents=True)
        (directory / f"{row['futu_symbol']}.json").write_text(
            json.dumps(row), encoding="utf-8"
        )
    hermes = tmp_path / "hermes-fake"
    hermes.write_text(
        f"#!{sys.executable}\n"
        "print('{\"success\": true, \"platform\": \"feishu\", \"message_id\": \"m\"}')\n",
        encoding="utf-8",
    )
    hermes.chmod(hermes.stat().st_mode | 0o111)
    database = tmp_path / "history.sqlite3"
    config = tmp_path / "daily.json"
    config_payload = {
        "coverage": "cached",
        "database": str(database),
        "mappings_root": str(mappings_root),
        "request_interval_seconds": 0.001,
        "request_limit": 1,
        "max_duration_seconds": 60.0,
        "hermes_timeout_seconds": 1.0,
        "hermes_executable": str(hermes),
    }
    config.write_text(json.dumps(config_payload), encoding="utf-8")
    target_dates = {
        101: ("2026-09-03", "2026-09-04"),
        202: ("2026-09-04", "2026-09-05"),
        303: ("2026-09-05", "2026-09-06"),
    }
    curve_requests: list[int] = []

    def payload_for_dates(first_date: str, latest_date: str) -> str:
        payload = _daily_supplier_payload()
        summary = payload["data"][1][0]
        history = payload["data"][2]
        assert isinstance(summary, dict)
        assert isinstance(history, list)
        summary["rq"] = _daily_supplier_timestamp(latest_date)
        history[0]["rq"] = _daily_supplier_timestamp(first_date)
        history[1]["rq"] = _daily_supplier_timestamp(latest_date)
        return _daily_encrypted_curve_payload(payload)

    def curve_transport(
        _url: str, body: bytes, _headers: dict[str, str]
    ) -> dict[str, object]:
        target_id = json.loads(body)["id"]
        curve_requests.append(target_id)
        first_date, latest_date = target_dates[target_id]
        if target_id == 101 and len(curve_requests) > 1:
            first_date, latest_date = "2026-09-06", "2026-09-07"
        return {
            "success": True,
            "code": "00000",
            "data": {
                "encryptedData": payload_for_dates(first_date, latest_date)
            },
        }

    monkeypatch.setattr(
        trend_curve_research,
        "read_wechat_mini_credentials",
        lambda *_args, **_kwargs: ("token", 123),
    )
    monkeypatch.setattr(
        trend_curve_research, "_default_curve_transport", curve_transport
    )
    monkeypatch.setattr(
        trend_animals,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("daily must not call the paid endpoint")
        ),
    )
    monkeypatch.setattr(
        notifications,
        "_post_json",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("daily must not send a direct notification")
        ),
    )
    command = ["trend-curve", "daily", "--check", "--daily-config", str(config)]
    current_now = datetime(2026, 9, 9, 6, 0, tzinfo=timezone.utc)  # 14:00 Shanghai
    monkeypatch.setattr(cli, "_trend_curve_daily_now", lambda: current_now)

    prior_exit = cli.main(command)
    prior_output = capsys.readouterr()
    prior_payload = json.loads(prior_output.out)
    assert (
        prior_exit,
        prior_output.err,
        prior_payload["batch_id"],
        prior_payload["status"],
        prior_payload["completed_count"],
        prior_payload["pending_count"],
        curve_requests,
    ) == (
        1,
        "",
        "trend-curve-daily-cached-2026-09-09",
        "partial",
        1,
        2,
        [101],
    )

    config_payload["request_limit"] = 100
    config.write_text(json.dumps(config_payload), encoding="utf-8")
    current_now = datetime(2026, 9, 10, 6, 0, tzinfo=timezone.utc)
    next_exit = cli.main(command)
    next_output = capsys.readouterr()
    next_payload = json.loads(next_output.out)
    with sqlite3.connect(database) as connection:
        prior_items = connection.execute(
            "SELECT market, symbol, completed_at, evidence_json "
            "FROM trend_curve_batch_items WHERE batch_id = ? ORDER BY market, symbol",
            (prior_payload["batch_id"],),
        ).fetchall()
        next_items = connection.execute(
            "SELECT market, symbol, completed_at, evidence_json "
            "FROM trend_curve_batch_items WHERE batch_id = ? ORDER BY market, symbol",
            (next_payload["batch_id"],),
        ).fetchall()
        snapshots = connection.execute(
            "SELECT market, symbol, snapshot_date FROM trend_curve_daily_snapshots "
            "ORDER BY market, symbol, snapshot_date"
        ).fetchall()
    prior_state = [
        (
            row[1],
            row[2] is not None,
            json.loads(row[3])["snapshot_date"] if row[3] is not None else None,
            json.loads(row[3])["point_dates"] if row[3] is not None else None,
        )
        for row in prior_items
    ]
    next_state = [
        (row[1], json.loads(row[3])["snapshot_date"])
        for row in next_items
    ]
    assert (
        next_exit,
        next_output.err,
        next_payload["status"],
        next_payload["completed_count"],
        next_payload["pending_count"],
        curve_requests,
        prior_state,
        next_state,
        snapshots,
    ) == (
        0,
        "",
        "complete",
        3,
        0,
        [101, 202, 303, 101],
        [
            ("600000", True, "2026-09-04", ["2026-09-03", "2026-09-04"]),
            ("00700", False, None, None),
            ("AAPL", False, None, None),
        ],
        [("600000", "2026-09-07"), ("00700", "2026-09-05"), ("AAPL", "2026-09-06")],
        [
            ("CN", "600000", "2026-09-04"),
            ("CN", "600000", "2026-09-07"),
            ("HK", "00700", "2026-09-05"),
            ("US", "AAPL", "2026-09-06"),
        ],
    )


def test_daily_prioritizes_uncovered_targets_after_missed_days(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    mappings_root = tmp_path / "mappings"
    mapping_rows = {
        "CN": {
            "asset": "A股",
            "futu_symbol": "SH.600000",
            "market": "CN",
            "schema_version": "open_trader.trend_symbol_mapping.v1",
            "trend_animals_symbol": "600000.SH",
            "trend_animals_tm_id": 101,
        },
        "HK": {
            "asset": "港股",
            "futu_symbol": "HK.00700",
            "market": "HK",
            "schema_version": "open_trader.trend_symbol_mapping.v1",
            "trend_animals_symbol": "0700.HK",
            "trend_animals_tm_id": 202,
        },
        "US": {
            "asset": "美股",
            "futu_symbol": "US.AAPL",
            "market": "US",
            "schema_version": "open_trader.trend_symbol_mapping.v1",
            "trend_animals_symbol": "AAPL",
            "trend_animals_tm_id": 303,
        },
    }
    for market, row in mapping_rows.items():
        directory = mappings_root / market
        directory.mkdir(parents=True)
        (directory / f"{row['futu_symbol']}.json").write_text(
            json.dumps(row), encoding="utf-8"
        )

    database = tmp_path / "history.sqlite3"
    config = tmp_path / "daily.json"
    hermes_capture = tmp_path / "hermes.log"
    hermes = tmp_path / "hermes-fake"
    hermes.write_text(
        f"#!{sys.executable}\n"
        "import pathlib\n"
        f"pathlib.Path({str(hermes_capture)!r}).open('a', encoding='utf-8').write('run\\n')\n"
        "print('{\"success\": true, \"platform\": \"feishu\", \"message_id\": \"m\"}')\n",
        encoding="utf-8",
    )
    hermes.chmod(hermes.stat().st_mode | 0o111)
    config_payload = {
        "coverage": "cached",
        "database": str(database),
        "mappings_root": str(mappings_root),
        "request_interval_seconds": 0.001,
        "request_limit": 1,
        "max_duration_seconds": 60.0,
        "hermes_timeout_seconds": 1.0,
        "hermes_executable": str(hermes),
    }
    config.write_text(json.dumps(config_payload), encoding="utf-8")
    healthy_response = _daily_encrypted_curve_payload(_daily_supplier_payload())
    requests: list[int] = []

    def curve_transport(
        _url: str, body: bytes, _headers: dict[str, str]
    ) -> dict[str, object]:
        requests.append(json.loads(body)["id"])
        return {
            "success": True,
            "code": "00000",
            "data": {"encryptedData": healthy_response},
        }

    monkeypatch.setattr(
        trend_curve_research,
        "read_wechat_mini_credentials",
        lambda *_args, **_kwargs: ("daily-token", 123456),
    )
    monkeypatch.setattr(
        trend_curve_research, "_default_curve_transport", curve_transport
    )
    monkeypatch.setattr(
        cli,
        "_trend_curve_daily_now",
        lambda: datetime(2026, 9, 9, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
    )
    command = ["trend-curve", "daily", "--check", "--daily-config", str(config)]
    seed_exit = cli.main(command)
    seed_output = capsys.readouterr()
    seed_payload = json.loads(seed_output.out)
    assert (
        seed_exit,
        seed_output.err,
        seed_payload["status"],
        seed_payload["completed_count"],
        seed_payload["pending_count"],
        seed_payload["request_count"],
        requests,
    ) == (1, "", "partial", 1, 2, 1, [101])

    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO trend_curve_batches "
            "(batch_id, manifest, require_snapshot, expected_dates_json, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                "trend-curve-daily-cached-2026-09-12",
                "future",
                1,
                "{}",
                "2026-09-12T04:00:00+00:00",
            ),
        )
        connection.execute(
            "INSERT INTO trend_curve_batches "
            "(batch_id, manifest, require_snapshot, expected_dates_json, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                "unrelated-cached-2026-09-10",
                "unrelated",
                1,
                "{}",
                "2026-09-10T04:00:00+00:00",
            ),
        )
        connection.commit()

    config_payload["request_limit"] = 100
    config.write_text(json.dumps(config_payload), encoding="utf-8")
    monkeypatch.setattr(
        cli,
        "_trend_curve_daily_now",
        lambda: datetime(2026, 9, 11, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
    )
    missed_days_exit = cli.main(command)
    missed_days_output = capsys.readouterr()
    missed_days_payload = json.loads(missed_days_output.out)
    with sqlite3.connect(database) as connection:
        old_items = connection.execute(
            "SELECT market, symbol, completed_at FROM trend_curve_batch_items "
            "WHERE batch_id = ? ORDER BY market, symbol",
            ("trend-curve-daily-cached-2026-09-09",),
        ).fetchall()
        new_items = connection.execute(
            "SELECT market, symbol, completed_at FROM trend_curve_batch_items "
            "WHERE batch_id = ? ORDER BY market, symbol",
            ("trend-curve-daily-cached-2026-09-11",),
        ).fetchall()
        point_dates = connection.execute(
            "SELECT market, symbol, curve_date FROM trend_curve_points "
            "ORDER BY market, symbol, curve_date"
        ).fetchall()
    assert (
        missed_days_exit,
        missed_days_output.err,
        missed_days_payload["status"],
        missed_days_payload["completed_count"],
        missed_days_payload["pending_count"],
        missed_days_payload["request_count"],
        requests,
        [(row[0], row[1], row[2] is not None) for row in old_items],
        [(row[0], row[1], row[2] is not None) for row in new_items],
        point_dates,
    ) == (
        0,
        "",
        "complete",
        3,
        0,
        3,
        [101, 202, 303, 101],
        [("CN", "600000", True), ("HK", "00700", False), ("US", "AAPL", False)],
        [("CN", "600000", True), ("HK", "00700", True), ("US", "AAPL", True)],
        [
            ("CN", "600000", "2026-09-03"),
            ("CN", "600000", "2026-09-04"),
            ("HK", "00700", "2026-09-03"),
            ("HK", "00700", "2026-09-04"),
            ("US", "AAPL", "2026-09-03"),
            ("US", "AAPL", "2026-09-04"),
        ],
    )


def test_daily_prioritizes_gaps_using_each_prior_frozen_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    mapping_rows = {
        "CN": {
            "asset": "A股",
            "futu_symbol": "SH.600000",
            "market": "CN",
            "schema_version": "open_trader.trend_symbol_mapping.v1",
            "trend_animals_symbol": "600000.SH",
            "trend_animals_tm_id": 101,
        },
        "HK": {
            "asset": "港股",
            "futu_symbol": "HK.00700",
            "market": "HK",
            "schema_version": "open_trader.trend_symbol_mapping.v1",
            "trend_animals_symbol": "0700.HK",
            "trend_animals_tm_id": 202,
        },
        "US": {
            "asset": "美股",
            "futu_symbol": "US.AAPL",
            "market": "US",
            "schema_version": "open_trader.trend_symbol_mapping.v1",
            "trend_animals_symbol": "AAPL",
            "trend_animals_tm_id": 303,
        },
        "US.D": {
            "asset": "美股",
            "futu_symbol": "US.MSFT",
            "market": "US",
            "schema_version": "open_trader.trend_symbol_mapping.v1",
            "trend_animals_symbol": "MSFT",
            "trend_animals_tm_id": 404,
        },
    }

    def write_mappings(root: Path, include_d: bool) -> None:
        for key, row in mapping_rows.items():
            if key == "US.D" and not include_d:
                continue
            market = row["market"]
            directory = root / market
            directory.mkdir(parents=True, exist_ok=True)
            (directory / f"{row['futu_symbol']}.json").write_text(
                json.dumps(row), encoding="utf-8"
            )

    def write_config(
        path: Path,
        database: Path,
        mappings_root: Path,
        hermes: Path,
        request_limit: int,
    ) -> None:
        path.write_text(
            json.dumps(
                {
                    "coverage": "cached",
                    "database": str(database),
                    "mappings_root": str(mappings_root),
                    "request_interval_seconds": 0.001,
                    "request_limit": request_limit,
                    "max_duration_seconds": 60.0,
                    "hermes_timeout_seconds": 1.0,
                    "hermes_executable": str(hermes),
                }
            ),
            encoding="utf-8",
        )

    def batch_snapshot(database: Path, batch_id: str) -> tuple[object, ...]:
        with sqlite3.connect(database) as connection:
            batch = connection.execute(
                "SELECT manifest, require_snapshot, expected_dates_json, created_at "
                "FROM trend_curve_batches WHERE batch_id = ?",
                (batch_id,),
            ).fetchone()
            items = connection.execute(
                "SELECT market, symbol, completed_at, evidence_json, issue_reason "
                "FROM trend_curve_batch_items WHERE batch_id = ? "
                "ORDER BY market, symbol",
                (batch_id,),
            ).fetchall()
        return (batch, items)

    def run_daily(
        config: Path, now: datetime
    ) -> tuple[int, dict[str, object]]:
        monkeypatch.setattr(cli, "_trend_curve_daily_now", lambda: now)
        exit_code = cli.main(
            ["trend-curve", "daily", "--check", "--daily-config", str(config)]
        )
        output = capsys.readouterr()
        assert output.err == ""
        return exit_code, json.loads(output.out)

    hermes = tmp_path / "hermes-fake"
    _write_daily_hermes_success(hermes)
    healthy_response = _daily_encrypted_curve_payload(_daily_supplier_payload())
    requests: list[int] = []

    def curve_transport(
        _url: str, body: bytes, _headers: dict[str, str]
    ) -> dict[str, object]:
        requests.append(json.loads(body)["id"])
        return {
            "success": True,
            "code": "00000",
            "data": {"encryptedData": healthy_response},
        }

    monkeypatch.setattr(
        trend_curve_research,
        "read_wechat_mini_credentials",
        lambda *_args, **_kwargs: ("daily-token", 123456),
    )
    monkeypatch.setattr(
        trend_curve_research, "_default_curve_transport", curve_transport
    )
    monkeypatch.setattr(
        trend_animals,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("daily must not call the paid endpoint")
        ),
    )
    monkeypatch.setattr(
        notifications,
        "_post_json",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("daily must not send a direct notification")
        ),
    )

    # Scope grows: a completed ABC batch must not become incomplete merely
    # because today's frozen scope adds D.
    first_root = tmp_path / "growing-mappings"
    write_mappings(first_root, include_d=False)
    first_database = tmp_path / "growing.sqlite3"
    first_config = tmp_path / "growing.json"
    write_config(first_config, first_database, first_root, hermes, request_limit=1)
    first_seed_exit, first_seed = run_daily(
        first_config,
        datetime(2026, 9, 9, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
    )
    assert (
        first_seed_exit,
        first_seed["status"],
        first_seed["completed_count"],
        first_seed["pending_count"],
        requests,
    ) == (1, "partial", 1, 2, [101])

    write_config(first_config, first_database, first_root, hermes, request_limit=100)
    second_seed_exit, second_seed = run_daily(
        first_config,
        datetime(2026, 9, 10, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
    )
    assert (
        second_seed_exit,
        second_seed["status"],
        second_seed["completed_count"],
        second_seed["pending_count"],
        requests,
    ) == (0, "complete", 3, 0, [101, 202, 303, 101])
    growing_old_snapshot = batch_snapshot(
        first_database, "trend-curve-daily-cached-2026-09-09"
    )
    growing_complete_snapshot = batch_snapshot(
        first_database, "trend-curve-daily-cached-2026-09-10"
    )

    write_mappings(first_root, include_d=True)
    requests.clear()
    growing_exit, growing = run_daily(
        first_config,
        datetime(2026, 9, 11, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
    )
    assert (
        growing_exit,
        growing["status"],
        growing["completed_count"],
        growing["pending_count"],
        requests,
    ) == (0, "complete", 4, 0, [202, 303, 101, 404])
    assert batch_snapshot(
        first_database, "trend-curve-daily-cached-2026-09-09"
    ) == growing_old_snapshot
    assert batch_snapshot(
        first_database, "trend-curve-daily-cached-2026-09-10"
    ) == growing_complete_snapshot

    # Every run uses the same supplier dates, which must remain provider dates
    # rather than being replaced by the observation day.
    with sqlite3.connect(first_database) as connection:
        growing_dates = connection.execute(
            "SELECT market, symbol, curve_date FROM trend_curve_points "
            "ORDER BY market, symbol, curve_date"
        ).fetchall()
        growing_snapshots = connection.execute(
            "SELECT market, symbol, snapshot_date "
            "FROM trend_curve_daily_snapshots ORDER BY market, symbol, snapshot_date"
        ).fetchall()
    assert set(growing_dates) == {
        ("CN", "600000", "2026-09-03"),
        ("CN", "600000", "2026-09-04"),
        ("HK", "00700", "2026-09-03"),
        ("HK", "00700", "2026-09-04"),
        ("US", "AAPL", "2026-09-03"),
        ("US", "AAPL", "2026-09-04"),
        ("US", "MSFT", "2026-09-03"),
        ("US", "MSFT", "2026-09-04"),
    }
    assert set(growing_snapshots) == {
        ("CN", "600000", "2026-09-04"),
        ("HK", "00700", "2026-09-04"),
        ("US", "AAPL", "2026-09-04"),
        ("US", "MSFT", "2026-09-04"),
    }

    # Scope shrinks: a latest incomplete ABCD batch with only D pending must
    # not cause the older ABC B/C gaps to be retried when today's scope omits D.
    second_root = tmp_path / "shrinking-mappings"
    write_mappings(second_root, include_d=False)
    second_database = tmp_path / "shrinking.sqlite3"
    second_config = tmp_path / "shrinking.json"
    write_config(second_config, second_database, second_root, hermes, request_limit=1)
    second_first_exit, second_first = run_daily(
        second_config,
        datetime(2026, 9, 9, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
    )
    assert (
        second_first_exit,
        second_first["status"],
        second_first["completed_count"],
        second_first["pending_count"],
    ) == (1, "partial", 1, 2)

    write_mappings(second_root, include_d=True)
    write_config(second_config, second_database, second_root, hermes, request_limit=3)
    requests.clear()
    second_day_exit, second_day = run_daily(
        second_config,
        datetime(2026, 9, 10, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
    )
    assert (
        second_day_exit,
        second_day["status"],
        second_day["completed_count"],
        second_day["pending_count"],
        requests,
    ) == (1, "partial", 3, 1, [202, 303, 101])
    shrinking_old_snapshot = batch_snapshot(
        second_database, "trend-curve-daily-cached-2026-09-09"
    )
    shrinking_incomplete_snapshot = batch_snapshot(
        second_database, "trend-curve-daily-cached-2026-09-10"
    )
    assert all(row[2] is not None for row in shrinking_incomplete_snapshot[1][:3])
    assert shrinking_incomplete_snapshot[1][3][2] is None

    (second_root / "US" / "US.MSFT.json").unlink()
    write_config(second_config, second_database, second_root, hermes, request_limit=100)
    requests.clear()
    shrinking_exit, shrinking = run_daily(
        second_config,
        datetime(2026, 9, 11, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
    )
    assert (
        shrinking_exit,
        shrinking["status"],
        shrinking["completed_count"],
        shrinking["pending_count"],
        requests,
    ) == (0, "complete", 3, 0, [101, 202, 303])
    assert batch_snapshot(
        second_database, "trend-curve-daily-cached-2026-09-09"
    ) == shrinking_old_snapshot
    assert batch_snapshot(
        second_database, "trend-curve-daily-cached-2026-09-10"
    ) == shrinking_incomplete_snapshot


def test_manual_pause_overrides_all_automatic_triggers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    mappings_root = tmp_path / "mappings"
    mapping_rows = {
        "CN": {
            "asset": "A股",
            "futu_symbol": "SH.600000",
            "market": "CN",
            "schema_version": "open_trader.trend_symbol_mapping.v1",
            "trend_animals_symbol": "600000.SH",
            "trend_animals_tm_id": 101,
        },
        "HK": {
            "asset": "港股",
            "futu_symbol": "HK.00700",
            "market": "HK",
            "schema_version": "open_trader.trend_symbol_mapping.v1",
            "trend_animals_symbol": "0700.HK",
            "trend_animals_tm_id": 202,
        },
    }
    for market, row in mapping_rows.items():
        directory = mappings_root / market
        directory.mkdir(parents=True)
        (directory / f"{row['futu_symbol']}.json").write_text(
            json.dumps(row), encoding="utf-8"
        )
    hermes_capture = tmp_path / "hermes.log"
    hermes = tmp_path / "hermes-fake"
    hermes.write_text(
        f"#!{sys.executable}\n"
        f"import pathlib\npathlib.Path({str(hermes_capture)!r}).open('a', encoding='utf-8').write('run\\n')\n"
        "print('{\"success\": true, \"platform\": \"feishu\", \"message_id\": \"m\"}')\n",
        encoding="utf-8",
    )
    hermes.chmod(hermes.stat().st_mode | 0o111)
    database = tmp_path / "history.sqlite3"
    config = tmp_path / "daily.json"
    config.write_text(
        json.dumps(
            {
                "coverage": "cached",
                "database": str(database),
                "mappings_root": str(mappings_root),
                "request_interval_seconds": 0.001,
                "request_limit": 100,
                "max_duration_seconds": 60.0,
                "hermes_timeout_seconds": 1.0,
                "hermes_executable": str(hermes),
            }
        ),
        encoding="utf-8",
    )
    command = ["trend-curve", "daily", "--check", "--daily-config", str(config)]
    credentials: tuple[str, int] = ("before-pause-token", 111)
    credential_reads: list[tuple[str, int]] = []
    curve_requests: list[int] = []
    response_mode = "auth-block-b"
    healthy_response = _daily_encrypted_curve_payload(_daily_supplier_payload())

    def read_credentials(*_args: object, **_kwargs: object) -> tuple[str, int]:
        credential_reads.append(credentials)
        return credentials

    def curve_transport(
        _url: str, body: bytes, _headers: dict[str, str]
    ) -> dict[str, object]:
        nonlocal response_mode
        target_id = json.loads(body)["id"]
        curve_requests.append(target_id)
        if response_mode == "auth-block-b" and target_id == 202:
            return {"success": False, "code": "A00004"}
        return {
            "success": True,
            "code": "00000",
            "data": {"encryptedData": healthy_response},
        }

    monkeypatch.setattr(
        trend_curve_research, "read_wechat_mini_credentials", read_credentials
    )
    monkeypatch.setattr(
        trend_curve_research, "_default_curve_transport", curve_transport
    )
    monkeypatch.setattr(
        trend_animals,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("daily must not call the paid endpoint")
        ),
    )
    monkeypatch.setattr(
        notifications,
        "_post_json",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("daily must not send a direct notification")
        ),
    )

    pause_exit = cli.main(["trend-curve", "pause", "--daily-config", str(config)])
    pause_output = capsys.readouterr()
    pause_payload = json.loads(pause_output.out)
    control_path = Path(f"{database}.daily-control.json")
    assert (
        pause_exit,
        pause_output.err,
        pause_payload,
        control_path.is_file(),
        json.loads(control_path.read_text(encoding="utf-8"))["status"],
    ) == (0, "", {"status": "paused", "reason": "manual"}, True, "paused")

    current_now = datetime(2026, 9, 10, 4, 0, tzinfo=timezone.utc)  # 12:00 Shanghai
    monkeypatch.setattr(cli, "_trend_curve_daily_now", lambda: current_now)
    first_paused_exit = cli.main(command)
    first_paused_output = capsys.readouterr()
    first_paused_payload = json.loads(first_paused_output.out)
    credentials = ("after-wake-token", 222)
    current_now = datetime(2026, 9, 10, 7, 0, tzinfo=timezone.utc)  # 15:00 Shanghai
    second_paused_exit = cli.main(command)
    second_paused_output = capsys.readouterr()
    second_paused_payload = json.loads(second_paused_output.out)
    assert (
        first_paused_exit,
        first_paused_output.err,
        first_paused_payload["status"],
        first_paused_payload["stop_reason"],
        first_paused_payload["request_count"],
        second_paused_exit,
        second_paused_output.err,
        second_paused_payload["status"],
        second_paused_payload["request_count"],
        curve_requests,
        credential_reads,
        hermes_capture.exists(),
    ) == (0, "", "paused", "manual_pause", 0, 0, "", "paused", 0, [], [], False)

    source_root = str(Path(__file__).resolve().parents[1] / "src")
    child_environment = dict(os.environ)
    child_environment["PYTHONPATH"] = os.pathsep.join(
        path for path in (source_root, child_environment.get("PYTHONPATH", "")) if path
    )
    fresh_process = subprocess.run(
        [
            sys.executable,
            "-c",
            "from datetime import datetime\n"
            "import sys\n"
            "from zoneinfo import ZoneInfo\n"
            "import open_trader.cli as cli\n"
            "import open_trader.trend_curve_research as research\n"
            "cli._trend_curve_daily_now = lambda: datetime(2026, 9, 10, 4, 0, tzinfo=ZoneInfo('UTC'))\n"
            "research.read_wechat_mini_credentials = lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError('paused process must not read credentials'))\n"
            "research._default_curve_transport = lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError('paused process must not call HTTP'))\n"
            "raise SystemExit(cli.main(['trend-curve', 'daily', '--check', '--daily-config', sys.argv[1]]))\n",
            str(config),
        ],
        capture_output=True,
        cwd=Path(__file__).resolve().parents[1],
        env=child_environment,
        text=True,
    )
    fresh_payload = json.loads(fresh_process.stdout)
    assert (
        fresh_process.returncode,
        fresh_process.stderr,
        fresh_payload["status"],
        fresh_payload["stop_reason"],
        fresh_payload["request_count"],
        curve_requests,
        credential_reads,
        hermes_capture.exists(),
    ) == (0, "", "paused", "manual_pause", 0, [], [], False)

    resume_exit = cli.main(["trend-curve", "resume", "--daily-config", str(config)])
    resume_output = capsys.readouterr()
    assert (
        resume_exit,
        resume_output.err,
        json.loads(resume_output.out),
        control_path.exists(),
    ) == (0, "", {"status": "resumed", "reason": "manual"}, False)

    auth_exit = cli.main(command)
    auth_output = capsys.readouterr()
    auth_payload = json.loads(auth_output.out)
    assert (
        auth_exit,
        auth_output.err,
        auth_payload["status"],
        auth_payload["completed_count"],
        auth_payload["pending_count"],
        auth_payload["request_count"],
        curve_requests,
        credential_reads,
        hermes_capture.read_text(encoding="utf-8").splitlines(),
    ) == (1, "", "auth_blocked", 1, 1, 2, [101, 202], [("after-wake-token", 222)], ["run"])

    pause_again_exit = cli.main(["trend-curve", "pause", "--daily-config", str(config)])
    capsys.readouterr()
    response_mode = "healthy"
    paused_auth_exit = cli.main(command)
    paused_auth_output = capsys.readouterr()
    paused_auth_payload = json.loads(paused_auth_output.out)
    assert (
        pause_again_exit,
        paused_auth_exit,
        paused_auth_output.err,
        paused_auth_payload["status"],
        paused_auth_payload["stop_reason"],
        paused_auth_payload["request_count"],
        curve_requests,
    ) == (0, 0, "", "paused", "manual_pause", 0, [101, 202])

    final_resume_exit = cli.main(["trend-curve", "resume", "--daily-config", str(config)])
    capsys.readouterr()
    credentials = ("after-resume-token", 333)
    final_exit = cli.main(command)
    final_output = capsys.readouterr()
    final_payload = json.loads(final_output.out)
    assert (
        final_resume_exit,
        final_exit,
        final_output.err,
        final_payload["status"],
        final_payload["completed_count"],
        final_payload["pending_count"],
        final_payload["request_count"],
        curve_requests,
        credential_reads,
        hermes_capture.read_text(encoding="utf-8").splitlines(),
    ) == (
        0,
        0,
        "",
        "complete",
        2,
        0,
        1,
        [101, 202, 202],
        [
            ("after-wake-token", 222),
            ("after-resume-token", 333),
        ],
        ["run", "run"],
    )


def test_next_noon_does_not_duplicate_an_overnight_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    mappings_root = tmp_path / "mappings"
    mapping_rows = {
        "CN": {
            "asset": "A股",
            "futu_symbol": "SH.600000",
            "market": "CN",
            "schema_version": "open_trader.trend_symbol_mapping.v1",
            "trend_animals_symbol": "600000.SH",
            "trend_animals_tm_id": 101,
        },
        "HK": {
            "asset": "港股",
            "futu_symbol": "HK.00700",
            "market": "HK",
            "schema_version": "open_trader.trend_symbol_mapping.v1",
            "trend_animals_symbol": "0700.HK",
            "trend_animals_tm_id": 202,
        },
    }
    for market, row in mapping_rows.items():
        directory = mappings_root / market
        directory.mkdir(parents=True)
        (directory / f"{row['futu_symbol']}.json").write_text(
            json.dumps(row), encoding="utf-8"
        )
    database = tmp_path / "history.sqlite3"
    config = tmp_path / "daily.json"
    hermes_capture = tmp_path / "hermes.log"
    hermes = tmp_path / "hermes-fake"
    hermes.write_text(
        f"#!{sys.executable}\n"
        f"import pathlib\npathlib.Path({str(hermes_capture)!r}).open('a', encoding='utf-8').write('run\\n')\n"
        "print('{\"success\": true, \"platform\": \"feishu\", \"message_id\": \"m\"}')\n",
        encoding="utf-8",
    )
    hermes.chmod(hermes.stat().st_mode | 0o111)
    config.write_text(
        json.dumps(
            {
                "coverage": "cached",
                "database": str(database),
                "mappings_root": str(mappings_root),
                "request_interval_seconds": 0.001,
                "request_limit": 100,
                "max_duration_seconds": 60.0,
                "hermes_timeout_seconds": 1.0,
                "hermes_executable": str(hermes),
            }
        ),
        encoding="utf-8",
    )
    healthy_response = _daily_encrypted_curve_payload(_daily_supplier_payload())
    response_file = tmp_path / "healthy-response.txt"
    response_file.write_text(healthy_response, encoding="ascii")
    waiting = tmp_path / "waiting-for-b"
    release = tmp_path / "release-b"
    child_script = tmp_path / "overnight-child.py"
    child_script.write_text(
        "from __future__ import annotations\n"
        "import json, pathlib, sys, time\n"
        "from datetime import datetime\n"
        "from zoneinfo import ZoneInfo\n"
        "import open_trader.trend_curve_research as research\n"
        "database = pathlib.Path(sys.argv[1])\n"
        "mappings_root = pathlib.Path(sys.argv[2])\n"
        "hermes = pathlib.Path(sys.argv[3])\n"
        "response = pathlib.Path(sys.argv[4]).read_text(encoding='ascii')\n"
        "waiting = pathlib.Path(sys.argv[5])\n"
        "release = pathlib.Path(sys.argv[6])\n"
        "research.read_wechat_mini_credentials = lambda *a, **k: ('child-token', 123)\n"
        "def transport(_url, body, _headers):\n"
        "    target_id = json.loads(body)['id']\n"
        "    if target_id == 202:\n"
        "        waiting.touch()\n"
        "        while not release.exists():\n"
        "            time.sleep(0.01)\n"
        "    return {'success': True, 'code': '00000', 'data': {'encryptedData': response}}\n"
        "research._default_curve_transport = transport\n"
        "result = research.run_daily_trend_curve(\n"
        "    mappings_root=mappings_root, database=database,\n"
        "    request_interval_seconds=0.001, request_limit=100,\n"
        "    max_duration_seconds=60.0, hermes_timeout_seconds=1.0,\n"
        "    hermes_executable=hermes,\n"
        "    now=datetime(2026, 9, 10, 12, 0, tzinfo=ZoneInfo('Asia/Shanghai')),\n"
        ")\n"
        "print(json.dumps(result), flush=True)\n",
        encoding="utf-8",
    )
    child = subprocess.Popen(
        [
            sys.executable,
            str(child_script),
            str(database),
            str(mappings_root),
            str(hermes),
            str(response_file),
            str(waiting),
            str(release),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 5.0
        while not waiting.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert waiting.exists(), "child did not reach B"
        with sqlite3.connect(database) as connection:
            committed_before = connection.execute(
                "SELECT market, symbol, curve_date FROM trend_curve_points "
                "ORDER BY market, symbol, curve_date"
            ).fetchall()
            batch_state_before = connection.execute(
                "SELECT batch_id, market, symbol, completed_at "
                "FROM trend_curve_batch_items ORDER BY batch_id, market, symbol"
            ).fetchall()
        monkeypatch.setattr(
            trend_curve_research,
            "read_wechat_mini_credentials",
            lambda *_args, **_kwargs: ("overnight-check-token", 999),
        )
        monkeypatch.setattr(
            trend_curve_research,
            "_default_curve_transport",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("overnight check must not call HTTP")
            ),
        )
        current_now = datetime(2026, 9, 11, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        monkeypatch.setattr(cli, "_trend_curve_daily_now", lambda: current_now)
        with pytest.raises(SystemExit) as overnight_error:
            cli.main(
                ["trend-curve", "daily", "--check", "--daily-config", str(config)]
            )
        overnight_output = capsys.readouterr()
        with sqlite3.connect(database) as connection:
            committed_after = connection.execute(
                "SELECT market, symbol, curve_date FROM trend_curve_points "
                "ORDER BY market, symbol, curve_date"
            ).fetchall()
            batch_state_after = connection.execute(
                "SELECT batch_id, market, symbol, completed_at "
                "FROM trend_curve_batch_items ORDER BY batch_id, market, symbol"
            ).fetchall()
        assert (
            overnight_error.value.code,
            "already running" in overnight_output.err,
            overnight_output.out,
            committed_after,
            batch_state_after,
            hermes_capture.exists(),
        ) == (
            2,
            True,
            "",
            committed_before,
            batch_state_before,
            False,
        )
    finally:
        release.touch()
        try:
            child.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            child.terminate()
            child.wait(timeout=5.0)


def _prepare_reconcile_cli(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    paid_rows: list[dict[str, object]],
) -> tuple[list[str], list[tuple[str, float]], list[tuple[str, dict[str, object], float]], Path]:
    watchlist = tmp_path / "watchlist.json"
    watchlist.write_text(
        json.dumps(
            [
                {
                    "market": "US",
                    "symbol": "SLB",
                    "asset_id": 10002,
                    "group_id": 332171,
                    "tm_id": 337127,
                    "ccy_id": 101,
                }
            ]
        ),
        encoding="utf-8",
    )
    mmkv_path = tmp_path / "wx64e4edbab5e14356"
    mmkv_path.write_bytes(b"snapshot")
    Path(f"{mmkv_path}.crc").write_bytes(b"crc")
    mmkv_helper = tmp_path / "open-trader-mmkv-dump"
    mmkv_helper.write_text(
        "#!/bin/sh\n"
        "printf '%s\\t%s\\n' other ignored\n"
        "printf '%s\\t%s\\n' vuex '{\"user\":{\"token\":\"mini-token\",\"info\":{\"id\":456789}}}'\n",
        encoding="utf-8",
    )
    mmkv_helper.chmod(mmkv_helper.stat().st_mode | 0o111)
    config_path = tmp_path / "daily.env"
    config_path.write_text(
        "\n".join(
            (
                f"OPEN_TRADER_REPO={tmp_path}",
                f"OPEN_TRADER_PYTHON={sys.executable}",
                "OPEN_TRADER_TIMEZONE=Asia/Shanghai",
                "OPEN_TRADER_DEADLINE=23:59",
                "OPEN_TRADER_FUTU_HOST=127.0.0.1",
                "OPEN_TRADER_FUTU_PORT=11111",
                "DEEPSEEK_API_KEY=test-key",
                "TREND_ANIMALS_API_KEY=paid-key",
                "OPEN_TRADER_NOTIFIERS=feishu",
                "OPEN_TRADER_FEISHU_WEBHOOK_URL=https://example.invalid/hook",
            )
        ),
        encoding="utf-8",
    )

    def curve_transport(
        _url: str, _body: bytes, _headers: dict[str, str]
    ) -> dict[str, object]:
        return {
            "success": True,
            "code": "00000",
            "data": {"encryptedData": SLB_CURVE_ENCRYPTED},
        }

    monkeypatch.setattr(trend_curve_research, "_default_curve_transport", curve_transport)
    paid_requests: list[tuple[str, float]] = []

    class PaidResponse:
        def __enter__(self) -> "PaidResponse":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self) -> bytes:
            return json.dumps(
                {"success": True, "code": "00000", "data": paid_rows}
            ).encode("utf-8")

    def paid_transport(url: str, timeout: float) -> PaidResponse:
        paid_requests.append((url, timeout))
        return PaidResponse()

    monkeypatch.setattr(trend_animals, "urlopen", paid_transport)
    deliveries: list[tuple[str, dict[str, object], float]] = []

    def feishu_transport(
        url: str, payload: dict[str, object], timeout: float
    ) -> dict[str, object]:
        deliveries.append((url, payload, timeout))
        return {"code": 0}

    monkeypatch.setattr(notifications, "_post_json", feishu_transport)
    database = tmp_path / "history.sqlite3"
    command = [
        "trend-curve",
        "collect",
        "--watchlist",
        str(watchlist),
        "--database",
        str(database),
        "--mmkv-path",
        str(mmkv_path),
        "--mmkv-helper",
        str(mmkv_helper),
        "--reconcile-and-notify",
        "--config",
        str(config_path),
    ]
    return command, paid_requests, deliveries, database


def test_trend_curve_collect_reconciles_same_day_snapshot_and_notifies_match(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (
        command,
        paid_requests,
        deliveries,
        _database,
    ) = _prepare_reconcile_cli(
        monkeypatch,
        tmp_path,
        [
            {
                "tmId": 337127,
                "asOfDate": "2026-09-02",
                "trendTemperaturePrev": "温",
                "trendTemperatureCurr": "温",
                "trendStrengthLocalCurr": "90.8",
            }
        ],
    )
    exit_code = cli.main(command)

    captured = capsys.readouterr()
    message = deliveries[0][1]["content"]["text"]
    requested_fields = parse_qs(
        urlparse(paid_requests[0][0]).query
    )["fields"][0].split(",")
    assert (
        exit_code,
        captured.err,
        "database:" in captured.out,
        "targets: 1" in captured.out,
        "points: 7" in captured.out,
        len(paid_requests),
        tuple(sorted(requested_fields)),
        len(deliveries),
        "趋势曲线采集对账一致" in message,
        "数据日期：US 2026-09-02" in message,
        "标的：1/1" in message,
        "对账字段：前一温度、当前温度、当前本地强度" in message,
        "结果：全部一致" in message,
    ) == (
        0,
        "",
        True,
        True,
        True,
        1,
        (
            "asOfDate",
            "tmId",
            "trendStrengthLocalCurr",
            "trendTemperatureCurr",
            "trendTemperaturePrev",
        ),
        1,
        True,
        True,
        True,
        True,
        True,
    )


def test_trend_curve_collect_reconciles_fresh_paid_snapshot_on_repeat(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    paid_rows = [
        {
            "tmId": 337127,
            "asOfDate": "2026-09-02",
            "trendTemperaturePrev": "温",
            "trendTemperatureCurr": "温",
            "trendStrengthLocalCurr": "90.8",
        }
    ]
    command, paid_requests, deliveries, database = _prepare_reconcile_cli(
        monkeypatch, tmp_path, paid_rows
    )

    first_exit = cli.main(command)
    paid_rows[0]["trendTemperatureCurr"] = "热"
    try:
        second_exit = cli.main(command)
    except SystemExit as exc:
        second_exit = exc.code

    with sqlite3.connect(database) as connection:
        curve_rows = connection.execute(
            """
            SELECT market, symbol, curve_date, COUNT(*)
            FROM trend_curve_points
            WHERE market = 'US' AND symbol = 'SLB' AND curve_date = '2026-09-02'
            GROUP BY market, symbol, curve_date
            """
        ).fetchall()
    first_message = deliveries[0][1]["content"]["text"]
    second_message = deliveries[1][1]["content"]["text"]
    assert (
        first_exit,
        "趋势曲线采集对账一致" in first_message,
        second_exit != 0,
        "趋势曲线采集对账异常" in second_message,
        "US.SLB 2026-09-02 当前温度：曲线=温，API=热" in second_message,
        len(paid_requests),
        curve_rows,
    ) == (
        0,
        True,
        True,
        True,
        True,
        2,
        [("US", "SLB", "2026-09-02", 1)],
    )


def test_trend_curve_collect_notifies_field_mismatch_and_returns_nonzero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    command, _paid_requests, deliveries, database = _prepare_reconcile_cli(
        monkeypatch,
        tmp_path,
        [
            {
                "tmId": 337127,
                "asOfDate": "2026-09-02",
                "trendTemperaturePrev": "温",
                "trendTemperatureCurr": "热",
                "trendStrengthLocalCurr": "90.8",
            }
        ],
    )

    try:
        exit_code = cli.main(command)
    except SystemExit as exc:
        exit_code = exc.code

    with sqlite3.connect(database) as connection:
        curve_rows = connection.execute(
            """
            SELECT market, symbol, curve_date
            FROM trend_curve_points
            WHERE market = 'US' AND symbol = 'SLB' AND curve_date = '2026-09-02'
            """
        ).fetchall()
    message = deliveries[0][1]["content"]["text"]
    assert (
        exit_code != 0,
        curve_rows,
        len(deliveries),
        "趋势曲线采集对账异常" in message,
        "US.SLB 2026-09-02 当前温度：曲线=温，API=热" in message,
    ) == (
        True,
        [("US", "SLB", "2026-09-02")],
        1,
        True,
        True,
    )


def test_trend_curve_collect_notifies_missing_paid_snapshot_and_returns_nonzero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    command, _paid_requests, deliveries, database = _prepare_reconcile_cli(
        monkeypatch, tmp_path, []
    )

    try:
        exit_code = cli.main(command)
    except SystemExit as exc:
        exit_code = exc.code

    with sqlite3.connect(database) as connection:
        curve_rows = connection.execute(
            """
            SELECT market, symbol, curve_date
            FROM trend_curve_points
            WHERE market = 'US' AND symbol = 'SLB' AND curve_date = '2026-09-02'
            """
        ).fetchall()
    message = deliveries[0][1]["content"]["text"]
    assert (
        exit_code != 0,
        curve_rows,
        len(deliveries),
        "趋势曲线采集对账异常" in message,
        "US.SLB 2026-09-02：API 快照缺失" in message,
    ) == (
        True,
        [("US", "SLB", "2026-09-02")],
        1,
        True,
        True,
    )


def test_trend_curve_failure_notifies_feishu_and_keeps_nonzero_exit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    portfolio = tmp_path / "portfolio.csv"
    mappings_root = tmp_path / "mappings"
    config_path = tmp_path / "daily.env"
    mmkv_path = tmp_path / "mmkv-snapshot"
    portfolio.write_text(
        "market,asset_class,symbol,analysis_symbol,ai_eligible\n"
        "US,stock,ESTC,ESTC,true\n",
        encoding="utf-8",
    )
    mapping_directory = mappings_root / "US"
    mapping_directory.mkdir(parents=True)
    (mapping_directory / "US.ESTC.json").write_text(
        (
            '{"asset":"美股","futu_symbol":"US.ESTC","market":"US",'
            '"schema_version":"open_trader.trend_symbol_mapping.v1",'
            '"trend_animals_symbol":"ESTC","trend_animals_tm_id":334101}'
        ),
        encoding="utf-8",
    )
    mmkv_path.write_bytes(b"snapshot")
    Path(f"{mmkv_path}.crc").write_bytes(b"crc")
    config_path.write_text(
        "\n".join(
            (
                f"OPEN_TRADER_REPO={tmp_path}",
                f"OPEN_TRADER_PYTHON={sys.executable}",
                "OPEN_TRADER_TIMEZONE=Asia/Shanghai",
                "OPEN_TRADER_DEADLINE=23:59",
                "OPEN_TRADER_FUTU_HOST=127.0.0.1",
                "OPEN_TRADER_FUTU_PORT=11111",
                "DEEPSEEK_API_KEY=test-key",
                "OPEN_TRADER_NOTIFIERS=feishu,macos",
                "OPEN_TRADER_FEISHU_WEBHOOK_URL=https://example.invalid/hook",
            )
        ),
        encoding="utf-8",
    )
    captured_feishu: list[tuple[str, str]] = []
    captured_non_feishu: list[tuple[str, str]] = []

    def capture_feishu(self: object, title: str, message: str) -> None:
        captured_feishu.append((title, message))

    def capture_non_feishu(self: object, title: str, message: str) -> None:
        captured_non_feishu.append((title, message))

    monkeypatch.setattr(notifications.FeishuWebhookNotifier, "notify", capture_feishu)
    monkeypatch.setattr(notifications.MacOSNotifier, "notify", capture_non_feishu)

    with pytest.raises(SystemExit) as exc_info:
        cli.main(
            [
                "trend-curve",
                "collect",
                "--portfolio",
                str(portfolio),
                "--mappings-root",
                str(mappings_root),
                "--database",
                str(tmp_path / "history.sqlite3"),
                "--mmkv-path",
                str(mmkv_path),
                "--mmkv-helper",
                str(tmp_path / "missing-mmkv-helper"),
                "--notify-failure",
                "--config",
                str(config_path),
            ]
        )

    assert (exc_info.value.code, captured_feishu, captured_non_feishu) == (
        2,
        [("趋势曲线采集失败", "trend-curve collect 失败：MMKV helper is unavailable")],
        [],
    )


def test_trend_curve_backtest_cli_emits_versioned_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "history.sqlite3"
    prices = tmp_path / "prices.csv"
    _write_cli_inputs(database, prices)

    exit_code = cli.main(
        [
            "trend-curve",
            "backtest",
            "--database",
            str(database),
            "--prices",
            str(prices),
            "--market",
            "US",
            "--symbol",
            "TEST",
            "--start-date",
            "2026-01-01",
            "--end-date",
            "2026-01-03",
            "--initial-cash",
            "1000",
        ]
    )
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert {
        "exit_code": exit_code,
        "stdout_line_count": len(captured.out.strip().splitlines()),
        "stderr": captured.err,
        "schema": payload["schema"],
        "strategy_id": payload["strategy_id"],
        "database_hash": payload["source_hashes"]["trend_curve_database"],
        "prices_hash": payload["source_hashes"]["ohlc_csv"],
        "commission_bps": payload["assumptions"]["commission_bps"],
        "slippage_bps": payload["assumptions"]["slippage_bps"],
        "sections_present": all(
            key in payload
            for key in (
                "decisions",
                "trades",
                "equity_curve",
                "completed_rounds",
                "metrics",
                "buy_and_hold",
            )
        ),
    } == {
        "exit_code": 0,
        "stdout_line_count": 1,
        "stderr": "",
        "schema": "open_trader.trend_curve_backtest.v1",
        "strategy_id": "trend_curve_warm_to_hot_flat_exit/US/v1",
        "database_hash": hashlib.sha256(database.read_bytes()).hexdigest(),
        "prices_hash": hashlib.sha256(prices.read_bytes()).hexdigest(),
        "commission_bps": "10",
        "slippage_bps": "5",
        "sections_present": True,
    }


def test_trend_curve_portfolio_backtest_cli_emits_one_versioned_json_document(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "history.sqlite3"
    prices_dir = tmp_path / "prices"
    portfolio = tmp_path / "portfolio.csv"
    exclusions = tmp_path / "exclusions.json"
    _write_portfolio_cli_inputs(database, prices_dir, portfolio, exclusions)
    before_files = {
        path.relative_to(tmp_path)
        for path in tmp_path.rglob("*")
        if path.is_file()
    }

    exit_code = cli.main(
        [
            "trend-curve",
            "portfolio-backtest",
            "--database",
            str(database),
            "--prices-dir",
            str(prices_dir),
            "--portfolio",
            str(portfolio),
            "--exclusions",
            str(exclusions),
            "--start-date",
            "2026-01-01",
            "--end-date",
            "2026-01-02",
        ]
    )
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    after_files = {
        path.relative_to(tmp_path)
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    per_symbol = payload["per_symbol"]
    fixed_sections = (
        "schema",
        "strategy_id",
        "requested_range",
        "assumptions",
        "preflight",
        "weights",
        "strategy",
        "buy_and_hold",
        "per_symbol",
        "source_hashes",
    )

    assert {
        "exit_code": exit_code,
        "stderr": captured.err,
        "stdout_line_count": len(captured.out.strip().splitlines()),
        "schema": payload["schema"],
        "strategy_id": payload["strategy_id"],
        "requested_range": payload["requested_range"],
        "initial_cash": payload["assumptions"]["initial_cash"],
        "caveats": payload["caveats"],
        "symbol": per_symbol[0]["symbol"],
        "name_zh": per_symbol[0]["name_zh"],
        "source_hashes": payload["source_hashes"],
        "sections_present": all(key in payload for key in fixed_sections),
        "files_unchanged": after_files == before_files,
    } == {
        "exit_code": 0,
        "stderr": "",
        "stdout_line_count": 1,
        "schema": "open_trader.trend_curve_portfolio_backtest.v1",
        "strategy_id": "trend_curve_warm_to_hot_flat_exit/US/v1",
        "requested_range": {"start": "2026-01-01", "end": "2026-01-02"},
        "initial_cash": "1000000",
        "caveats": [
            "Current holdings and weights are applied retrospectively, so results include survivorship and lookahead bias and do not reconstruct the historical account."
        ],
        "symbol": "TEST",
        "name_zh": "测试标的",
        "source_hashes": {
            "portfolio_csv": hashlib.sha256(portfolio.read_bytes()).hexdigest(),
            "exclusions_json": hashlib.sha256(exclusions.read_bytes()).hexdigest(),
            "trend_curve_database": hashlib.sha256(database.read_bytes()).hexdigest(),
            "ohlc_csvs": {
                "TEST": hashlib.sha256(
                    (prices_dir / "TEST.csv").read_bytes()
                ).hexdigest()
            },
        },
        "sections_present": True,
        "files_unchanged": True,
    }


def test_daily_freezes_mapping_scope_and_resumes_by_observation_day(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    mappings_root = tmp_path / "mappings"
    mapping_rows = {
        "CN": {
            "asset": "A股",
            "futu_symbol": "SH.600000",
            "market": "CN",
            "schema_version": "open_trader.trend_symbol_mapping.v1",
            "trend_animals_symbol": "600000.SH",
            "trend_animals_tm_id": 101,
        },
        "HK": {
            "asset": "港股",
            "futu_symbol": "HK.00700",
            "market": "HK",
            "schema_version": "open_trader.trend_symbol_mapping.v1",
            "trend_animals_symbol": "0700.HK",
            "trend_animals_tm_id": 202,
        },
        "US": {
            "asset": "美股",
            "futu_symbol": "US.AAPL",
            "market": "US",
            "schema_version": "open_trader.trend_symbol_mapping.v1",
            "trend_animals_symbol": "AAPL",
            "trend_animals_tm_id": 303,
        },
    }
    for market, row in mapping_rows.items():
        directory = mappings_root / market
        directory.mkdir(parents=True)
        (directory / f"{row['futu_symbol']}.json").write_text(
            json.dumps(row), encoding="utf-8"
        )

    database = tmp_path / "history.sqlite3"
    config_path = tmp_path / "trend-curve-daily.json"
    hermes = tmp_path / "hermes-fake"
    _write_daily_hermes_success(hermes)
    config_path.write_text(
        json.dumps(
            {
                "coverage": "cached",
                "database": str(database),
                "mappings_root": str(mappings_root),
                "request_interval_seconds": 0.001,
                "request_limit": 100,
                "max_duration_seconds": 60.0,
                "hermes_timeout_seconds": 1.0,
                "hermes_executable": str(hermes),
            }
        ),
        encoding="utf-8",
    )
    command = ["trend-curve", "daily", "--daily-config", str(config_path)]
    healthy_response = _daily_encrypted_curve_payload(_daily_supplier_payload())
    malformed_response = "not-a-valid-encrypted-payload"
    response_by_id = {
        101: healthy_response,
        202: malformed_response,
        303: healthy_response,
    }
    curve_requests: list[int] = []
    credentials = ("daily-token", 987654321)
    credential_reads: list[tuple[object, object]] = []

    def read_credentials(*_args: object, **_kwargs: object) -> tuple[object, object]:
        credential_reads.append(credentials)
        return credentials

    def curve_transport(
        _url: str, body: bytes, _headers: dict[str, str]
    ) -> dict[str, object]:
        target_id = json.loads(body)["id"]
        curve_requests.append(target_id)
        return {
            "success": True,
            "code": "00000",
            "data": {"encryptedData": response_by_id[target_id]},
        }

    monkeypatch.setattr(
        trend_curve_research, "read_wechat_mini_credentials", read_credentials
    )
    monkeypatch.setattr(
        trend_curve_research, "_default_curve_transport", curve_transport
    )
    monkeypatch.setattr(
        cli,
        "_trend_curve_daily_now",
        lambda: datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc),
        raising=False,
    )
    monkeypatch.setattr(
        trend_animals,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("daily must not call the paid endpoint")
        ),
    )
    monkeypatch.setattr(
        notifications,
        "_post_json",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("daily must not send a direct notification")
        ),
    )

    first_exit = cli.main(command)
    first_output = capsys.readouterr()
    first_payload = json.loads(first_output.out)

    with sqlite3.connect(database) as connection:
        first_rows = connection.execute(
            "SELECT market, symbol, curve_date FROM trend_curve_points "
            "ORDER BY market, symbol, curve_date"
        ).fetchall()
        first_snapshots = connection.execute(
            "SELECT market, symbol, snapshot_date FROM trend_curve_daily_snapshots "
            "ORDER BY market, symbol, snapshot_date"
        ).fetchall()
        batch_rows = connection.execute(
            "SELECT batch_id, COUNT(*) FROM trend_curve_batch_items "
            "GROUP BY batch_id ORDER BY batch_id"
        ).fetchall()
    assert (
        first_exit,
        first_output.err,
        first_payload["observation_date"],
        first_payload["coverage"],
        first_payload["freshness"],
        first_payload["status"],
        first_payload["target_count"],
        first_payload["completed_count"],
        first_payload["pending_count"],
        first_payload["request_count"],
        first_payload["provider_date_range"],
        curve_requests,
        first_rows,
        first_snapshots,
        batch_rows,
        len(credential_reads),
    ) == (
        1,
        "",
        "2026-09-09",
        "cached",
        "unknown",
        "partial",
        3,
        2,
        1,
        3,
        {"start": "2026-09-03", "end": "2026-09-04"},
        [101, 202, 303],
        [
            ("CN", "600000", "2026-09-03"),
            ("CN", "600000", "2026-09-04"),
            ("US", "AAPL", "2026-09-03"),
            ("US", "AAPL", "2026-09-04"),
        ],
        [
            ("CN", "600000", "2026-09-04"),
            ("US", "AAPL", "2026-09-04"),
        ],
        [("trend-curve-daily-cached-2026-09-09", 3)],
        1,
    )
    assert set((issue["market"], issue["symbol"], issue["reason"]) for issue in first_payload["issues"]) == {
        ("HK", "00700", "data_gap")
    }

    response_by_id[202] = healthy_response
    second_exit = cli.main(command)
    second_output = capsys.readouterr()
    second_payload = json.loads(second_output.out)
    assert (
        second_exit,
        second_output.err,
        second_payload["status"],
        second_payload["completed_count"],
        second_payload["pending_count"],
        second_payload["request_count"],
        curve_requests,
    ) == (0, "", "complete", 3, 0, 1, [101, 202, 303, 202])

    with sqlite3.connect(database) as connection:
        all_rows = connection.execute(
            "SELECT market, symbol, curve_date FROM trend_curve_points "
            "ORDER BY market, symbol, curve_date"
        ).fetchall()
        all_snapshots = connection.execute(
            "SELECT market, symbol, snapshot_date FROM trend_curve_daily_snapshots "
            "ORDER BY market, symbol, snapshot_date"
        ).fetchall()
        batch_rows = connection.execute(
            "SELECT batch_id, COUNT(*) FROM trend_curve_batch_items "
            "GROUP BY batch_id ORDER BY batch_id"
        ).fetchall()
    assert (
        all_rows,
        all_snapshots,
        batch_rows,
    ) == (
        [
            ("CN", "600000", "2026-09-03"),
            ("CN", "600000", "2026-09-04"),
            ("HK", "00700", "2026-09-03"),
            ("HK", "00700", "2026-09-04"),
            ("US", "AAPL", "2026-09-03"),
            ("US", "AAPL", "2026-09-04"),
        ],
        [
            ("CN", "600000", "2026-09-04"),
            ("HK", "00700", "2026-09-04"),
            ("US", "AAPL", "2026-09-04"),
        ],
        [("trend-curve-daily-cached-2026-09-09", 3)],
    )

    def no_credentials(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("verified daily batch must not read credentials")

    def no_transport(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("verified daily batch must not request HTTP")

    monkeypatch.setattr(
        trend_curve_research, "read_wechat_mini_credentials", no_credentials
    )
    monkeypatch.setattr(trend_curve_research, "_default_curve_transport", no_transport)
    third_exit = cli.main(command)
    third_output = capsys.readouterr()
    third_payload = json.loads(third_output.out)
    assert (
        third_exit,
        third_output.err,
        third_payload["status"],
        third_payload["request_count"],
        curve_requests,
    ) == (0, "", "complete", 0, [101, 202, 303, 202])

    mapping_rows["HK"]["trend_animals_tm_id"] = 999
    (mappings_root / "HK" / "HK.00700.json").write_text(
        json.dumps(mapping_rows["HK"]), encoding="utf-8"
    )
    database_before_drift = database.read_bytes()
    credentials_before_drift = len(credential_reads)
    with pytest.raises(SystemExit) as drift_error:
        cli.main(command)
    drift_output = capsys.readouterr()
    assert (
        drift_error.value.code,
        curve_requests,
        len(credential_reads),
        database.read_bytes() == database_before_drift,
        "batch request does not match frozen batch" in drift_output.err,
    ) == (2, [101, 202, 303, 202], credentials_before_drift, True, True)

    mapping_rows["HK"]["trend_animals_tm_id"] = 202
    (mappings_root / "HK" / "HK.00700.json").write_text(
        json.dumps(mapping_rows["HK"]), encoding="utf-8"
    )
    monkeypatch.setattr(
        cli,
        "_trend_curve_daily_now",
        lambda: datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc),
    )
    monkeypatch.setattr(trend_curve_research, "_default_curve_transport", curve_transport)
    monkeypatch.setattr(
        trend_curve_research, "read_wechat_mini_credentials", read_credentials
    )
    fourth_exit = cli.main(command)
    fourth_output = capsys.readouterr()
    fourth_payload = json.loads(fourth_output.out)
    with sqlite3.connect(database) as connection:
        batch_count = connection.execute(
            "SELECT COUNT(*) FROM trend_curve_batches"
        ).fetchone()[0]
        total_point_rows = connection.execute(
            "SELECT COUNT(*) FROM trend_curve_points"
        ).fetchone()[0]
    assert (
        fourth_exit,
        fourth_output.err,
        fourth_payload["observation_date"],
        fourth_payload["batch_id"],
        fourth_payload["batch_id"] == first_payload["batch_id"],
        fourth_payload["coverage"],
        fourth_payload["freshness"],
        fourth_payload["status"],
        fourth_payload["target_count"],
        fourth_payload["completed_count"],
        fourth_payload["pending_count"],
        fourth_payload["request_count"],
        curve_requests,
        batch_count,
        total_point_rows,
    ) == (
        0,
        "",
        "2026-09-10",
        "trend-curve-daily-cached-2026-09-10",
        False,
        "cached",
        "unknown",
        "complete",
        3,
        3,
        0,
        3,
        [101, 202, 303, 202, 101, 202, 303],
        2,
        6,
    )


def test_daily_auth_block_waits_for_changed_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    mappings_root = tmp_path / "mappings"
    mapping_rows = {
        "CN": {
            "asset": "A股",
            "futu_symbol": "SH.600000",
            "market": "CN",
            "schema_version": "open_trader.trend_symbol_mapping.v1",
            "trend_animals_symbol": "600000.SH",
            "trend_animals_tm_id": 101,
        },
        "HK": {
            "asset": "港股",
            "futu_symbol": "HK.00700",
            "market": "HK",
            "schema_version": "open_trader.trend_symbol_mapping.v1",
            "trend_animals_symbol": "0700.HK",
            "trend_animals_tm_id": 202,
        },
        "US": {
            "asset": "美股",
            "futu_symbol": "US.AAPL",
            "market": "US",
            "schema_version": "open_trader.trend_symbol_mapping.v1",
            "trend_animals_symbol": "AAPL",
            "trend_animals_tm_id": 303,
        },
    }
    for market, row in mapping_rows.items():
        directory = mappings_root / market
        directory.mkdir(parents=True)
        (directory / f"{row['futu_symbol']}.json").write_text(
            json.dumps(row), encoding="utf-8"
        )

    database = tmp_path / "history.sqlite3"
    config_path = tmp_path / "trend-curve-daily.json"
    hermes = tmp_path / "hermes-fake"
    _write_daily_hermes_success(hermes)
    config_path.write_text(
        json.dumps(
            {
                "coverage": "cached",
                "database": str(database),
                "mappings_root": str(mappings_root),
                "request_interval_seconds": 0.001,
                "request_limit": 100,
                "max_duration_seconds": 60.0,
                "hermes_timeout_seconds": 1.0,
                "hermes_executable": str(hermes),
            }
        ),
        encoding="utf-8",
    )
    command = ["trend-curve", "daily", "--daily-config", str(config_path)]
    healthy_response = _daily_encrypted_curve_payload(_daily_supplier_payload())
    auth_response = _daily_encrypted_curve_payload({"code": "A00004"})
    response_by_id = {101: healthy_response, 202: auth_response, 303: healthy_response}
    curve_requests: list[int] = []
    current_credentials: tuple[object, object] = ("token-one", 111111111)
    credential_reads: list[tuple[object, object]] = []

    def read_credentials(*_args: object, **_kwargs: object) -> tuple[object, object]:
        credential_reads.append(current_credentials)
        return current_credentials

    def curve_transport(
        _url: str, body: bytes, _headers: dict[str, str]
    ) -> dict[str, object]:
        target_id = json.loads(body)["id"]
        curve_requests.append(target_id)
        return {
            "success": True,
            "code": "00000",
            "data": {"encryptedData": response_by_id[target_id]},
        }

    monkeypatch.setattr(
        trend_curve_research, "read_wechat_mini_credentials", read_credentials
    )
    monkeypatch.setattr(
        trend_curve_research, "_default_curve_transport", curve_transport
    )
    monkeypatch.setattr(
        cli,
        "_trend_curve_daily_now",
        lambda: datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc),
        raising=False,
    )
    monkeypatch.setattr(
        trend_animals,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("daily must not call the paid endpoint")
        ),
    )
    monkeypatch.setattr(
        notifications,
        "_post_json",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("daily must not send a direct notification")
        ),
    )

    first_exit = cli.main(command)
    first_output = capsys.readouterr()
    first_payload = json.loads(first_output.out)
    with sqlite3.connect(database) as connection:
        first_points = connection.execute(
            "SELECT market, symbol, curve_date FROM trend_curve_points "
            "ORDER BY market, symbol, curve_date"
        ).fetchall()
        first_snapshots = connection.execute(
            "SELECT market, symbol, snapshot_date FROM trend_curve_daily_snapshots "
            "ORDER BY market, symbol, snapshot_date"
        ).fetchall()
    assert (
        first_exit,
        first_output.err,
        first_payload["status"],
        first_payload["completed_count"],
        first_payload["pending_count"],
        first_payload["request_count"],
        curve_requests,
        first_points,
        first_snapshots,
    ) == (
        1,
        "",
        "auth_blocked",
        1,
        2,
        2,
        [101, 202],
        [("CN", "600000", "2026-09-03"), ("CN", "600000", "2026-09-04")],
        [("CN", "600000", "2026-09-04")],
    )

    second_exit = cli.main(command)
    second_output = capsys.readouterr()
    second_payload = json.loads(second_output.out)
    assert (
        second_exit,
        second_output.err,
        second_payload["status"],
        second_payload["completed_count"],
        second_payload["pending_count"],
        second_payload["request_count"],
        curve_requests,
    ) == (1, "", "auth_blocked", 1, 2, 0, [101, 202])

    current_credentials = ("token-two", 222222222)
    third_exit = cli.main(command)
    third_output = capsys.readouterr()
    third_payload = json.loads(third_output.out)
    assert (
        third_exit,
        third_output.err,
        third_payload["status"],
        third_payload["completed_count"],
        third_payload["pending_count"],
        third_payload["request_count"],
        curve_requests,
    ) == (1, "", "auth_blocked", 1, 2, 1, [101, 202, 202])

    current_credentials = ("token-three", 333333333)
    response_by_id[202] = healthy_response
    fourth_exit = cli.main(command)
    fourth_output = capsys.readouterr()
    fourth_payload = json.loads(fourth_output.out)
    with sqlite3.connect(database) as connection:
        points = connection.execute(
            "SELECT market, symbol, curve_date FROM trend_curve_points "
            "ORDER BY market, symbol, curve_date"
        ).fetchall()
        snapshots = connection.execute(
            "SELECT market, symbol, snapshot_date FROM trend_curve_daily_snapshots "
            "ORDER BY market, symbol, snapshot_date"
        ).fetchall()
    persisted_bytes = b"".join(
        path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()
    )
    assert (
        fourth_exit,
        fourth_output.err,
        fourth_payload["status"],
        fourth_payload["completed_count"],
        fourth_payload["pending_count"],
        fourth_payload["request_count"],
        curve_requests,
        points,
        snapshots,
        len(credential_reads),
        all(
            secret not in persisted_bytes and secret not in fourth_output.out.encode()
            for secret in (
                b"token-one",
                b"111111111",
                b"token-two",
                b"222222222",
                b"token-three",
                b"333333333",
            )
        ),
    ) == (
        0,
        "",
        "complete",
        3,
        0,
        2,
        [101, 202, 202, 202, 303],
        [
            ("CN", "600000", "2026-09-03"),
            ("CN", "600000", "2026-09-04"),
            ("HK", "00700", "2026-09-03"),
            ("HK", "00700", "2026-09-04"),
            ("US", "AAPL", "2026-09-03"),
            ("US", "AAPL", "2026-09-04"),
        ],
        [
            ("CN", "600000", "2026-09-04"),
            ("HK", "00700", "2026-09-04"),
            ("US", "AAPL", "2026-09-04"),
        ],
        4,
        True,
    )


def test_daily_auth_block_survives_observation_day_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    mappings_root = tmp_path / "mappings"
    mapping_rows = {
        "CN": {
            "asset": "A股",
            "futu_symbol": "SH.600000",
            "market": "CN",
            "schema_version": "open_trader.trend_symbol_mapping.v1",
            "trend_animals_symbol": "600000.SH",
            "trend_animals_tm_id": 101,
        },
        "HK": {
            "asset": "港股",
            "futu_symbol": "HK.00700",
            "market": "HK",
            "schema_version": "open_trader.trend_symbol_mapping.v1",
            "trend_animals_symbol": "0700.HK",
            "trend_animals_tm_id": 202,
        },
        "US": {
            "asset": "美股",
            "futu_symbol": "US.AAPL",
            "market": "US",
            "schema_version": "open_trader.trend_symbol_mapping.v1",
            "trend_animals_symbol": "AAPL",
            "trend_animals_tm_id": 303,
        },
    }
    for market, row in mapping_rows.items():
        directory = mappings_root / market
        directory.mkdir(parents=True)
        (directory / f"{row['futu_symbol']}.json").write_text(
            json.dumps(row), encoding="utf-8"
        )

    database = tmp_path / "history.sqlite3"
    config_path = tmp_path / "trend-curve-daily.json"
    hermes = tmp_path / "hermes-fake"
    _write_daily_hermes_success(hermes)
    config_path.write_text(
        json.dumps(
            {
                "coverage": "cached",
                "database": str(database),
                "mappings_root": str(mappings_root),
                "request_interval_seconds": 0.001,
                "request_limit": 100,
                "max_duration_seconds": 60.0,
                "hermes_timeout_seconds": 1.0,
                "hermes_executable": str(hermes),
            }
        ),
        encoding="utf-8",
    )
    command = ["trend-curve", "daily", "--daily-config", str(config_path)]
    healthy_response = _daily_encrypted_curve_payload(_daily_supplier_payload())
    auth_response = _daily_encrypted_curve_payload({"code": "A00004"})
    response_by_id = {101: healthy_response, 202: auth_response, 303: healthy_response}
    curve_requests: list[int] = []
    current_credentials: tuple[object, object] = ("rollover-token-one", 444444444)

    def read_credentials(*_args: object, **_kwargs: object) -> tuple[object, object]:
        return current_credentials

    def curve_transport(
        _url: str, body: bytes, _headers: dict[str, str]
    ) -> dict[str, object]:
        target_id = json.loads(body)["id"]
        curve_requests.append(target_id)
        return {
            "success": True,
            "code": "00000",
            "data": {"encryptedData": response_by_id[target_id]},
        }

    monkeypatch.setattr(
        trend_curve_research, "read_wechat_mini_credentials", read_credentials
    )
    monkeypatch.setattr(
        trend_curve_research, "_default_curve_transport", curve_transport
    )
    monkeypatch.setattr(
        cli,
        "_trend_curve_daily_now",
        lambda: datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc),
        raising=False,
    )
    monkeypatch.setattr(
        trend_animals,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("daily must not call the paid endpoint")
        ),
    )
    monkeypatch.setattr(
        notifications,
        "_post_json",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("daily must not send a direct notification")
        ),
    )

    first_exit = cli.main(command)
    first_output = capsys.readouterr()
    first_payload = json.loads(first_output.out)
    with sqlite3.connect(database) as connection:
        first_points = connection.execute(
            "SELECT market, symbol, curve_date FROM trend_curve_points "
            "ORDER BY market, symbol, curve_date"
        ).fetchall()
        first_snapshots = connection.execute(
            "SELECT market, symbol, snapshot_date FROM trend_curve_daily_snapshots "
            "ORDER BY market, symbol, snapshot_date"
        ).fetchall()
        first_batch_items = connection.execute(
            "SELECT market, symbol, point_count, snapshot_date, evidence_json, "
            "completed_at FROM trend_curve_batch_items "
            "WHERE batch_id = ? ORDER BY market, symbol",
            (first_payload["batch_id"],),
        ).fetchall()
    assert (
        first_exit,
        first_output.err,
        first_payload["batch_id"],
        first_payload["observation_date"],
        first_payload["status"],
        first_payload["target_count"],
        first_payload["completed_count"],
        first_payload["pending_count"],
        first_payload["request_count"],
        curve_requests,
        first_points,
        first_snapshots,
        len(first_batch_items),
        first_batch_items[0][5] is not None,
        first_batch_items[1][5] is None,
        first_batch_items[2][5] is None,
    ) == (
        1,
        "",
        "trend-curve-daily-cached-2026-09-09",
        "2026-09-09",
        "auth_blocked",
        3,
        1,
        2,
        2,
        [101, 202],
        [("CN", "600000", "2026-09-03"), ("CN", "600000", "2026-09-04")],
        [("CN", "600000", "2026-09-04")],
        3,
        True,
        True,
        True,
    )

    monkeypatch.setattr(
        cli,
        "_trend_curve_daily_now",
        lambda: datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc),
    )
    second_exit = cli.main(command)
    second_output = capsys.readouterr()
    second_payload = json.loads(second_output.out)
    with sqlite3.connect(database) as connection:
        second_points = connection.execute(
            "SELECT market, symbol, curve_date FROM trend_curve_points "
            "ORDER BY market, symbol, curve_date"
        ).fetchall()
        second_snapshots = connection.execute(
            "SELECT market, symbol, snapshot_date FROM trend_curve_daily_snapshots "
            "ORDER BY market, symbol, snapshot_date"
        ).fetchall()
        second_batch_items = connection.execute(
            "SELECT market, symbol, point_count, snapshot_date, evidence_json, "
            "completed_at FROM trend_curve_batch_items "
            "WHERE batch_id = ? ORDER BY market, symbol",
            (first_payload["batch_id"],),
        ).fetchall()
    assert (
        second_exit,
        second_output.err,
        second_payload["batch_id"],
        second_payload["observation_date"],
        second_payload["status"],
        second_payload["target_count"],
        second_payload["completed_count"],
        second_payload["pending_count"],
        second_payload["request_count"],
        curve_requests,
        second_points,
        second_snapshots,
        second_batch_items,
    ) == (
        1,
        "",
        "trend-curve-daily-cached-2026-09-10",
        "2026-09-10",
        "auth_blocked",
        3,
        0,
        3,
        0,
        [101, 202],
        first_points,
        first_snapshots,
        first_batch_items,
    )

    current_credentials = ("rollover-token-two", 555555555)
    response_by_id = {101: healthy_response, 202: healthy_response, 303: healthy_response}
    third_exit = cli.main(command)
    third_output = capsys.readouterr()
    third_payload = json.loads(third_output.out)
    with sqlite3.connect(database) as connection:
        final_points = connection.execute(
            "SELECT market, symbol, curve_date FROM trend_curve_points "
            "ORDER BY market, symbol, curve_date"
        ).fetchall()
        final_snapshots = connection.execute(
            "SELECT market, symbol, snapshot_date FROM trend_curve_daily_snapshots "
            "ORDER BY market, symbol, snapshot_date"
        ).fetchall()
        final_batch_items = connection.execute(
            "SELECT market, symbol, completed_at FROM trend_curve_batch_items "
            "WHERE batch_id = ? ORDER BY market, symbol",
            (third_payload["batch_id"],),
        ).fetchall()
        prior_batch_items_after_recovery = connection.execute(
            "SELECT market, symbol, point_count, snapshot_date, evidence_json, "
            "completed_at FROM trend_curve_batch_items "
            "WHERE batch_id = ? ORDER BY market, symbol",
            (first_payload["batch_id"],),
        ).fetchall()
    persisted_bytes = b"".join(
        path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()
    )
    assert (
        third_exit,
        third_output.err,
        third_payload["batch_id"],
        third_payload["observation_date"],
        third_payload["coverage"],
        third_payload["freshness"],
        third_payload["status"],
        third_payload["target_count"],
        third_payload["completed_count"],
        third_payload["pending_count"],
        third_payload["request_count"],
        third_payload["provider_date_range"],
        curve_requests,
        final_points,
        final_snapshots,
        len(final_batch_items),
        all(row[2] is not None for row in final_batch_items),
        prior_batch_items_after_recovery,
        all(
            secret not in persisted_bytes and secret not in third_output.out.encode()
            for secret in (
                b"rollover-token-one",
                b"444444444",
                b"rollover-token-two",
                b"555555555",
            )
        ),
    ) == (
        0,
        "",
        "trend-curve-daily-cached-2026-09-10",
        "2026-09-10",
        "cached",
        "unknown",
        "complete",
        3,
        3,
        0,
        3,
        {"start": "2026-09-03", "end": "2026-09-04"},
        [101, 202, 202, 303, 101],
        [
            ("CN", "600000", "2026-09-03"),
            ("CN", "600000", "2026-09-04"),
            ("HK", "00700", "2026-09-03"),
            ("HK", "00700", "2026-09-04"),
            ("US", "AAPL", "2026-09-03"),
            ("US", "AAPL", "2026-09-04"),
        ],
        [
            ("CN", "600000", "2026-09-04"),
            ("HK", "00700", "2026-09-04"),
            ("US", "AAPL", "2026-09-04"),
        ],
        3,
        True,
        first_batch_items,
        True,
    )


def test_daily_budget_preserves_progress_and_sends_one_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    mappings_root = tmp_path / "mappings"
    mapping_rows = {
        "CN": {
            "asset": "A股",
            "futu_symbol": "SH.600000",
            "market": "CN",
            "schema_version": "open_trader.trend_symbol_mapping.v1",
            "trend_animals_symbol": "600000.SH",
            "trend_animals_tm_id": 101,
        },
        "HK": {
            "asset": "港股",
            "futu_symbol": "HK.00700",
            "market": "HK",
            "schema_version": "open_trader.trend_symbol_mapping.v1",
            "trend_animals_symbol": "0700.HK",
            "trend_animals_tm_id": 202,
        },
        "US": {
            "asset": "美股",
            "futu_symbol": "US.AAPL",
            "market": "US",
            "schema_version": "open_trader.trend_symbol_mapping.v1",
            "trend_animals_symbol": "AAPL",
            "trend_animals_tm_id": 303,
        },
    }
    for market, row in mapping_rows.items():
        directory = mappings_root / market
        directory.mkdir(parents=True)
        (directory / f"{row['futu_symbol']}.json").write_text(
            json.dumps(row), encoding="utf-8"
        )

    hermes_capture = tmp_path / "hermes-argv.log"
    hermes = tmp_path / "hermes-fake"
    hermes.write_text(
        f"#!{sys.executable}\n"
        "import json, os, pathlib, sys, time\n"
        "capture = pathlib.Path(os.environ['OPEN_TRADER_TEST_HERMES_CAPTURE'])\n"
        "with capture.open('a', encoding='utf-8') as handle:\n"
        "    handle.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "mode = os.environ.get('OPEN_TRADER_TEST_HERMES_MODE', 'success')\n"
        "if mode == 'timeout':\n"
        "    time.sleep(1.0)\n"
        "elif mode == 'failed':\n"
        "    print(json.dumps({'success': True, 'platform': 'feishu',\n"
        "        'message_id': '', 'error': os.environ.get('OPEN_TRADER_TEST_HERMES_SECRET')}))\n"
        "else:\n"
        "    print(json.dumps({'success': True, 'platform': 'feishu', 'message_id': 'fake-message'}))\n",
        encoding="utf-8",
    )
    hermes.chmod(hermes.stat().st_mode | 0o111)
    monkeypatch.setenv("OPEN_TRADER_TEST_HERMES_CAPTURE", str(hermes_capture))
    hermes_secret = "hermes-secret-token-user-987654321"
    monkeypatch.setenv("OPEN_TRADER_TEST_HERMES_SECRET", hermes_secret)

    healthy_response = _daily_encrypted_curve_payload(_daily_supplier_payload())
    curve_requests: list[int] = []
    credentials = ("daily-token", 987654321)
    credential_reads: list[tuple[object, object]] = []

    def read_credentials(*_args: object, **_kwargs: object) -> tuple[object, object]:
        credential_reads.append(credentials)
        return credentials

    def curve_transport(
        _url: str, body: bytes, _headers: dict[str, str]
    ) -> dict[str, object]:
        target_id = json.loads(body)["id"]
        curve_requests.append(target_id)
        return {
            "success": True,
            "code": "00000",
            "data": {"encryptedData": healthy_response},
        }

    monkeypatch.setattr(
        trend_curve_research, "read_wechat_mini_credentials", read_credentials
    )
    monkeypatch.setattr(
        trend_curve_research, "_default_curve_transport", curve_transport
    )
    monkeypatch.setattr(
        cli,
        "_trend_curve_daily_now",
        lambda: datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc),
        raising=False,
    )
    monkeypatch.setattr(
        trend_animals,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("daily must not call the paid endpoint")
        ),
    )
    monkeypatch.setattr(
        notifications,
        "_post_json",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("daily must not send a direct notification")
        ),
    )

    database = tmp_path / "history.sqlite3"
    config_path = tmp_path / "trend-curve-daily.json"
    config_payload = {
        "coverage": "cached",
        "database": str(database),
        "mappings_root": str(mappings_root),
        "request_interval_seconds": 0.001,
        "request_limit": 2,
        "max_duration_seconds": 60.0,
        "hermes_timeout_seconds": 0.5,
        "hermes_executable": str(hermes),
    }
    config_path.write_text(json.dumps(config_payload), encoding="utf-8")
    command = ["trend-curve", "daily", "--daily-config", str(config_path)]

    monkeypatch.setenv("OPEN_TRADER_TEST_HERMES_MODE", "success")
    first_exit = cli.main(command)
    first_output = capsys.readouterr()
    first_payload = json.loads(first_output.out)
    first_summary_path = Path(first_payload["summary_path"])
    first_summary = json.loads(first_summary_path.read_text(encoding="utf-8"))
    gap_path = Path(first_summary["gap_file"])
    gap_payload = json.loads(gap_path.read_text(encoding="utf-8"))
    with sqlite3.connect(database) as connection:
        first_points = connection.execute(
            "SELECT market, symbol, curve_date FROM trend_curve_points "
            "ORDER BY market, symbol, curve_date"
        ).fetchall()
        first_snapshots = connection.execute(
            "SELECT market, symbol, snapshot_date FROM trend_curve_daily_snapshots "
            "ORDER BY market, symbol, snapshot_date"
        ).fetchall()
    assert (
        first_exit,
        first_output.err,
        first_payload["status"],
        first_payload["completed_count"],
        first_payload["pending_count"],
        first_payload["request_count"],
        first_payload["stop_reason"],
        first_payload["delivery_status"],
        curve_requests,
        first_points,
        first_snapshots,
        first_summary["coverage"],
        first_summary["freshness"],
        first_summary["provider_date_range"],
        first_summary["request_count"],
        first_summary["stop_reason"],
        gap_payload["pending_targets"],
        len(credential_reads),
    ) == (
        1,
        "",
        "partial",
        2,
        1,
        2,
        "request_limit",
        "accepted",
        [101, 202],
        [
            ("CN", "600000", "2026-09-03"),
            ("CN", "600000", "2026-09-04"),
            ("HK", "00700", "2026-09-03"),
            ("HK", "00700", "2026-09-04"),
        ],
        [
            ("CN", "600000", "2026-09-04"),
            ("HK", "00700", "2026-09-04"),
        ],
        "cached",
        "unknown",
        {"start": "2026-09-03", "end": "2026-09-04"},
        2,
        "request_limit",
        [{"market": "US", "symbol": "AAPL", "reason": "request_limit"}],
        1,
    )

    monkeypatch.setenv("OPEN_TRADER_TEST_HERMES_MODE", "failed")
    second_exit = cli.main(command)
    second_output = capsys.readouterr()
    second_payload = json.loads(second_output.out)
    second_summary = json.loads(
        Path(second_payload["summary_path"]).read_text(encoding="utf-8")
    )
    assert (
        second_exit,
        second_output.err,
        second_payload["status"],
        second_payload["completed_count"],
        second_payload["pending_count"],
        second_payload["request_count"],
        second_payload["delivery_status"],
        curve_requests,
        second_summary["delivery_status"],
        len(credential_reads),
    ) == (1, "", "complete", 3, 0, 1, "failed", [101, 202, 303], "failed", 2)

    timeout_database = tmp_path / "timeout.sqlite3"
    timeout_config = tmp_path / "timeout.json"
    timeout_payload = dict(config_payload)
    timeout_payload.update({"database": str(timeout_database), "request_limit": 1})
    timeout_config.write_text(json.dumps(timeout_payload), encoding="utf-8")
    monkeypatch.setenv("OPEN_TRADER_TEST_HERMES_MODE", "timeout")
    timeout_exit = cli.main(
        ["trend-curve", "daily", "--daily-config", str(timeout_config)]
    )
    timeout_output = capsys.readouterr()
    timeout_result = json.loads(timeout_output.out)
    assert (
        timeout_exit,
        timeout_output.err,
        timeout_result["status"],
        timeout_result["completed_count"],
        timeout_result["pending_count"],
        timeout_result["request_count"],
        timeout_result["delivery_status"],
        curve_requests,
    ) == (1, "", "partial", 1, 2, 1, "unknown", [101, 202, 303, 101])

    # An already-complete same-day check is a local no-op, not a new Hermes run.
    third_exit = cli.main(command)
    third_output = capsys.readouterr()
    third_payload = json.loads(third_output.out)
    assert (
        third_exit,
        third_output.err,
        third_payload["status"],
        third_payload["request_count"],
        third_payload["delivery_status"],
        "summary_path" in third_payload,
        curve_requests,
    ) == (0, "", "complete", 0, "not_run", False, [101, 202, 303, 101])

    hermes_invocations = [
        json.loads(line) for line in hermes_capture.read_text(encoding="utf-8").splitlines()
    ]
    assert len(hermes_invocations) == 3
    assert hermes_invocations[0] == [
        "send",
        "--to",
        "feishu",
        "--subject",
        "Trend curve daily",
        "--file",
        str(first_summary_path.resolve()),
        "--json",
    ]
    assert all(
        secret not in path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file() and path != hermes
        for secret in (hermes_secret.encode("utf-8"), b"daily-token", b"987654321")
    )

    deadline_database = tmp_path / "deadline.sqlite3"
    deadline_config = tmp_path / "deadline.json"
    deadline_payload = dict(config_payload)
    deadline_payload.update(
        {
            "database": str(deadline_database),
            "request_interval_seconds": 0.5,
            "request_limit": 2,
            "max_duration_seconds": 1.0,
        }
    )
    deadline_config.write_text(json.dumps(deadline_payload), encoding="utf-8")
    clock = [0.0]
    sleeps: list[float] = []
    deadline_requests: list[int] = []

    def deadline_clock() -> float:
        return clock[0]

    def deadline_transport(
        _url: str, body: bytes, _headers: dict[str, str]
    ) -> dict[str, object]:
        target_id = json.loads(body)["id"]
        deadline_requests.append(target_id)
        clock[0] = 2.0
        return {
            "success": True,
            "code": "00000",
            "data": {"encryptedData": healthy_response},
        }

    monkeypatch.setattr(trend_curve_research.time, "monotonic", deadline_clock)
    monkeypatch.setattr(
        trend_curve_research.time,
        "sleep",
        lambda seconds: sleeps.append(seconds),
    )
    monkeypatch.setattr(trend_curve_research, "_default_curve_transport", deadline_transport)
    monkeypatch.setenv("OPEN_TRADER_TEST_HERMES_MODE", "success")
    deadline_exit = cli.main(
        ["trend-curve", "daily", "--daily-config", str(deadline_config)]
    )
    deadline_output = capsys.readouterr()
    deadline_result = json.loads(deadline_output.out)
    with sqlite3.connect(deadline_database) as connection:
        deadline_progress = connection.execute(
            "SELECT market, symbol, completed_at FROM trend_curve_batch_items "
            "ORDER BY market, symbol"
        ).fetchall()
    assert (
        deadline_exit,
        deadline_output.err,
        deadline_result["status"],
        deadline_result["completed_count"],
        deadline_result["pending_count"],
        deadline_result["request_count"],
        deadline_result["stop_reason"],
        deadline_requests,
        sleeps,
        sum(row[2] is not None for row in deadline_progress),
    ) == (
        1,
        "",
        "partial",
        1,
        2,
        1,
        "deadline_exceeded",
        [101],
        [],
        1,
    )

    invalid_config = tmp_path / "invalid.json"
    invalid_payload = dict(config_payload)
    invalid_payload["request_interval_seconds"] = 0
    invalid_config.write_text(json.dumps(invalid_payload), encoding="utf-8")
    before_invalid_requests = list(curve_requests)
    before_invalid_invocations = hermes_capture.read_text(encoding="utf-8").count("\n")
    with pytest.raises(SystemExit) as invalid_error:
        cli.main(["trend-curve", "daily", "--daily-config", str(invalid_config)])
    invalid_output = capsys.readouterr()
    assert (
        invalid_error.value.code,
        "request_interval_seconds" in invalid_output.err,
        curve_requests,
        hermes_capture.read_text(encoding="utf-8").count("\n"),
    ) == (2, True, before_invalid_requests, before_invalid_invocations)


def test_daily_enforces_request_interval_and_post_sleep_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    mappings_root = tmp_path / "mappings"
    for market, symbol, trend_symbol, futu_symbol, tm_id in (
        ("CN", "600000", "600000.SH", "SH.600000", 101),
        ("HK", "00700", "0700.HK", "HK.00700", 202),
        ("US", "AAPL", "AAPL", "US.AAPL", 303),
    ):
        row = {
            "asset": {"CN": "A股", "HK": "港股", "US": "美股"}[market],
            "futu_symbol": futu_symbol,
            "market": market,
            "schema_version": "open_trader.trend_symbol_mapping.v1",
            "trend_animals_symbol": trend_symbol,
            "trend_animals_tm_id": tm_id,
        }
        directory = mappings_root / market
        directory.mkdir(parents=True)
        (directory / f"{futu_symbol}.json").write_text(
            json.dumps(row), encoding="utf-8"
        )
    healthy_response = _daily_encrypted_curve_payload(_daily_supplier_payload())
    hermes = tmp_path / "hermes-fake"
    _write_daily_hermes_success(hermes)
    current_now = datetime(2026, 9, 9, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    monkeypatch.setattr(cli, "_trend_curve_daily_now", lambda: current_now)
    monkeypatch.setattr(
        trend_curve_research,
        "read_wechat_mini_credentials",
        lambda *_args, **_kwargs: ("daily-token", 123456),
    )

    def run_case(
        name: str,
        *,
        first_response_time: float,
        max_duration_seconds: float,
    ) -> tuple[dict[str, object], list[tuple[int, float]], list[float], list[tuple[str, str, str]]]:
        database = tmp_path / f"{name}.sqlite3"
        config = tmp_path / f"{name}.json"
        config.write_text(
            json.dumps(
                {
                    "coverage": "cached",
                    "database": str(database),
                    "mappings_root": str(mappings_root),
                    "request_interval_seconds": 0.5,
                    "request_limit": 10,
                    "max_duration_seconds": max_duration_seconds,
                    "hermes_timeout_seconds": 1.0,
                    "hermes_executable": str(hermes),
                }
            ),
            encoding="utf-8",
        )
        clock = [0.0]
        sleeps: list[float] = []
        request_times: list[tuple[int, float]] = []

        def clock_now() -> float:
            return clock[0]

        def sleep(seconds: float) -> None:
            sleeps.append(seconds)
            clock[0] += seconds

        def curve_transport(
            _url: str, body: bytes, _headers: dict[str, str]
        ) -> dict[str, object]:
            target_id = json.loads(body)["id"]
            request_times.append((target_id, clock[0]))
            if target_id == 101:
                clock[0] = first_response_time
            return {
                "success": True,
                "code": "00000",
                "data": {"encryptedData": healthy_response},
            }

        monkeypatch.setattr(trend_curve_research.time, "monotonic", clock_now)
        monkeypatch.setattr(trend_curve_research.time, "sleep", sleep)
        monkeypatch.setattr(
            trend_curve_research, "_default_curve_transport", curve_transport
        )
        exit_code = cli.main(
            ["trend-curve", "daily", "--daily-config", str(config)]
        )
        output = capsys.readouterr()
        payload = json.loads(output.out)
        with sqlite3.connect(database) as connection:
            progress = connection.execute(
                "SELECT market, symbol, curve_date FROM trend_curve_points "
                "ORDER BY market, symbol, curve_date"
            ).fetchall()
        payload["exit_code"] = exit_code
        payload["stderr"] = output.err
        return payload, request_times, sleeps, progress

    interval_payload, interval_requests, interval_sleeps, interval_progress = run_case(
        "interval", first_response_time=0.2, max_duration_seconds=10.0
    )
    assert (
        interval_payload["exit_code"],
        interval_payload["stderr"],
        interval_payload["status"],
        interval_requests,
        interval_sleeps,
        interval_requests[1][1] >= 0.7,
        len(interval_progress),
    ) == (0, "", "complete", [(101, 0.0), (202, 0.7), (303, 1.2)], [0.5, 0.5], True, 6)

    deadline_payload, deadline_requests, deadline_sleeps, deadline_progress = run_case(
        "post-sleep-deadline", first_response_time=0.6, max_duration_seconds=1.0
    )
    assert (
        deadline_payload["exit_code"],
        deadline_payload["stderr"],
        deadline_payload["status"],
        deadline_payload["completed_count"],
        deadline_payload["pending_count"],
        deadline_payload["request_count"],
        deadline_payload["stop_reason"],
        deadline_requests,
        deadline_sleeps,
        deadline_progress,
    ) == (
        1,
        "",
        "partial",
        1,
        2,
        1,
        "deadline_exceeded",
        [(101, 0.0)],
        [0.5],
        [
            ("CN", "600000", "2026-09-03"),
            ("CN", "600000", "2026-09-04"),
        ],
    )


def test_daily_summary_keeps_large_gap_details_out_of_message(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    mappings_root = tmp_path / "mappings"
    for index in range(13):
        symbol = f"600{index:03d}"
        row = {
            "asset": "A股",
            "futu_symbol": f"SH.{symbol}",
            "market": "CN",
            "schema_version": "open_trader.trend_symbol_mapping.v1",
            "trend_animals_symbol": f"{symbol}.SH",
            "trend_animals_tm_id": 101 + index,
        }
        directory = mappings_root / "CN"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"SH.{symbol}.json").write_text(
            json.dumps(row), encoding="utf-8"
        )

    database = tmp_path / "history.sqlite3"
    config = tmp_path / "daily.json"
    notification_capture = tmp_path / "notification.json"
    hermes = tmp_path / "hermes-fake"
    hermes.write_text(
        f"#!{sys.executable}\n"
        "import json, pathlib, sys\n"
        "summary = pathlib.Path(sys.argv[sys.argv.index('--file') + 1])\n"
        f"pathlib.Path({str(notification_capture)!r}).write_text(summary.read_text(encoding='utf-8'), encoding='utf-8')\n"
        "print(json.dumps({'success': True, 'platform': 'feishu', 'message_id': 'm'}))\n",
        encoding="utf-8",
    )
    hermes.chmod(hermes.stat().st_mode | 0o111)
    config.write_text(
        json.dumps(
            {
                "coverage": "cached",
                "database": str(database),
                "mappings_root": str(mappings_root),
                "request_interval_seconds": 0.001,
                "request_limit": 20,
                "max_duration_seconds": 60.0,
                "hermes_timeout_seconds": 1.0,
                "hermes_executable": str(hermes),
            }
        ),
        encoding="utf-8",
    )
    healthy_response = _daily_encrypted_curve_payload(_daily_supplier_payload())
    requests: list[int] = []

    def curve_transport(
        _url: str, body: bytes, _headers: dict[str, str]
    ) -> dict[str, object]:
        target_id = json.loads(body)["id"]
        requests.append(target_id)
        if target_id == 101:
            return {
                "success": True,
                "code": "00000",
                "data": {"encryptedData": healthy_response},
            }
        return {"success": False, "code": "00000"}

    monkeypatch.setattr(
        trend_curve_research,
        "read_wechat_mini_credentials",
        lambda *_args, **_kwargs: ("daily-token", 123456),
    )
    monkeypatch.setattr(
        trend_curve_research, "_default_curve_transport", curve_transport
    )
    monkeypatch.setattr(
        cli,
        "_trend_curve_daily_now",
        lambda: datetime(2026, 9, 9, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
    )
    result = cli.main(
        ["trend-curve", "daily", "--daily-config", str(config)]
    )
    output = capsys.readouterr()
    payload = json.loads(output.out)
    summary = json.loads(Path(payload["summary_path"]).read_text(encoding="utf-8"))
    notification = json.loads(notification_capture.read_text(encoding="utf-8"))
    gap = Path(summary["gap_file"])
    gap_payload = json.loads(gap.read_text(encoding="utf-8"))
    with sqlite3.connect(database) as connection:
        successful_rows = connection.execute(
            "SELECT market, symbol, curve_date FROM trend_curve_points"
        ).fetchall()
    notification_text = json.dumps(notification, ensure_ascii=False)
    assert (
        result,
        output.err,
        payload["status"],
        payload["completed_count"],
        payload["pending_count"],
        payload["request_count"],
        requests,
        notification["coverage"],
        notification["target_count"],
        notification["completed_count"],
        notification["pending_count"],
        notification["gap_file"],
        notification["issue_counts"],
        "issues" in notification,
        len(gap_payload["pending_targets"]),
        set(item["reason"] for item in gap_payload["pending_targets"]),
        successful_rows,
        "600001" in notification_text,
    ) == (
        1,
        "",
        "partial",
        1,
        12,
        13,
        list(range(101, 114)),
        "cached",
        13,
        1,
        12,
        str(gap.resolve()),
        {"data_gap": 12},
        False,
        12,
        {"data_gap"},
        [("CN", "600000", "2026-09-03"), ("CN", "600000", "2026-09-04")],
        False,
    )
    assert len(
        [issue for issue in payload["issues"] if issue["reason"] == "data_gap"]
    ) == 12


def test_daily_rejects_contradictory_hermes_receipts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    mappings_root = tmp_path / "mappings"
    mapping_directory = mappings_root / "CN"
    mapping_directory.mkdir(parents=True)
    mapping_directory.joinpath("SH.600000.json").write_text(
        json.dumps(
            {
                "asset": "A股",
                "futu_symbol": "SH.600000",
                "market": "CN",
                "schema_version": "open_trader.trend_symbol_mapping.v1",
                "trend_animals_symbol": "600000.SH",
                "trend_animals_tm_id": 101,
            }
        ),
        encoding="utf-8",
    )
    response = _daily_encrypted_curve_payload(_daily_supplier_payload())
    hermes_capture = tmp_path / "hermes-invocations.log"
    hermes = tmp_path / "hermes-fake"
    hermes.write_text(
        f"#!{sys.executable}\n"
        "import json, os, pathlib\n"
        "pathlib.Path(os.environ['OPEN_TRADER_TEST_HERMES_CAPTURE']).open('a', encoding='utf-8').write('run\\n')\n"
        "print(os.environ['OPEN_TRADER_TEST_HERMES_RECEIPT'])\n",
        encoding="utf-8",
    )
    hermes.chmod(hermes.stat().st_mode | 0o111)
    monkeypatch.setattr(
        trend_curve_research,
        "read_wechat_mini_credentials",
        lambda *_args, **_kwargs: ("daily-test-token", 123456),
    )
    monkeypatch.setattr(
        trend_curve_research,
        "_default_curve_transport",
        lambda *_args, **_kwargs: {
            "success": True,
            "code": "00000",
            "data": {"encryptedData": response},
        },
    )
    monkeypatch.setattr(
        cli,
        "_trend_curve_daily_now",
        lambda: datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc),
        raising=False,
    )
    monkeypatch.setenv("OPEN_TRADER_TEST_HERMES_CAPTURE", str(hermes_capture))

    receipts = (
        {"success": True, "platform": "feishu", "message_id": "ok", "error": "contradictory-secret"},
        {"success": True, "platform": "feishu", "message_id": "ok", "skipped": True},
    )
    for index, receipt in enumerate(receipts):
        database = tmp_path / f"history-{index}.sqlite3"
        config = tmp_path / f"daily-{index}.json"
        config.write_text(
            json.dumps(
                {
                    "coverage": "cached",
                    "database": str(database),
                    "mappings_root": str(mappings_root),
                    "request_interval_seconds": 0.001,
                    "request_limit": 10,
                    "max_duration_seconds": 60.0,
                    "hermes_timeout_seconds": 1.0,
                    "hermes_executable": str(hermes),
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setenv(
            "OPEN_TRADER_TEST_HERMES_RECEIPT", json.dumps(receipt, separators=(",", ":"))
        )
        exit_code = cli.main(
            ["trend-curve", "daily", "--daily-config", str(config)]
        )
        output = capsys.readouterr()
        payload = json.loads(output.out)
        summary_path = Path(payload["summary_path"])
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        with sqlite3.connect(database) as connection:
            committed = connection.execute(
                "SELECT market, symbol, curve_date FROM trend_curve_points"
            ).fetchall()
        assert (
            exit_code,
            output.err,
            payload["status"],
            payload["delivery_status"],
            summary["delivery_status"],
            committed,
            hermes_capture.read_text(encoding="utf-8").splitlines(),
        ) == (
            1,
            "",
            "complete",
            "failed",
            "failed",
            [("CN", "600000", "2026-09-03"), ("CN", "600000", "2026-09-04")],
            ["run"] * (index + 1),
        )
        persisted = b"".join(
            path.read_bytes()
            for path in tmp_path.rglob("*")
            if path.is_file() and path != hermes
        )
        assert b"contradictory-secret" not in persisted


def test_daily_preserves_unknown_delivery_on_interruption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    mappings_root = tmp_path / "mappings"
    mapping_directory = mappings_root / "CN"
    mapping_directory.mkdir(parents=True)
    mapping_directory.joinpath("SH.600000.json").write_text(
        json.dumps(
            {
                "asset": "A股",
                "futu_symbol": "SH.600000",
                "market": "CN",
                "schema_version": "open_trader.trend_symbol_mapping.v1",
                "trend_animals_symbol": "600000.SH",
                "trend_animals_tm_id": 101,
            }
        ),
        encoding="utf-8",
    )
    response = _daily_encrypted_curve_payload(_daily_supplier_payload())
    hermes = tmp_path / "hermes-fake"
    _write_daily_hermes_success(hermes)
    monkeypatch.setattr(
        trend_curve_research,
        "read_wechat_mini_credentials",
        lambda *_args, **_kwargs: ("interrupt-test-token", 123456),
    )
    monkeypatch.setattr(
        trend_curve_research,
        "_default_curve_transport",
        lambda *_args, **_kwargs: {
            "success": True,
            "code": "00000",
            "data": {"encryptedData": response},
        },
    )
    monkeypatch.setattr(
        cli,
        "_trend_curve_daily_now",
        lambda: datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc),
        raising=False,
    )
    real_subprocess_run = subprocess.run
    active_interruption: list[type[BaseException] | None] = [None]
    invocations: list[Path] = []

    def run_external(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        argv = args[0] if args else kwargs.get("args")
        if isinstance(argv, list) and argv and argv[0] == str(hermes):
            summary_path = Path(argv[argv.index("--file") + 1])
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            assert summary["delivery_status"] == "unknown"
            invocations.append(summary_path)
            interruption = active_interruption[0]
            assert interruption is not None
            raise interruption()
        return real_subprocess_run(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(trend_curve_research.subprocess, "run", run_external)

    for index, interruption in enumerate((KeyboardInterrupt, InterruptedError)):
        active_interruption[0] = interruption
        database = tmp_path / f"history-{index}.sqlite3"
        config = tmp_path / f"daily-{index}.json"
        config.write_text(
            json.dumps(
                {
                    "coverage": "cached",
                    "database": str(database),
                    "mappings_root": str(mappings_root),
                    "request_interval_seconds": 0.001,
                    "request_limit": 10,
                    "max_duration_seconds": 60.0,
                    "hermes_timeout_seconds": 1.0,
                    "hermes_executable": str(hermes),
                }
            ),
            encoding="utf-8",
        )
        raised: type[BaseException] | None = None
        try:
            exit_code = cli.main(["trend-curve", "daily", "--daily-config", str(config)])
        except interruption:
            raised = interruption
        output = capsys.readouterr()
        if interruption is KeyboardInterrupt:
            assert raised is interruption
            assert output.out == ""
        else:
            assert raised is None
            assert exit_code == 1
            assert output.out
        summary_files = sorted(
            (database.parent / "trend_curve_daily").glob("*.json")
        )
        assert len(summary_files) == index + 1
        summary = json.loads(summary_files[-1].read_text(encoding="utf-8"))
        with sqlite3.connect(database) as connection:
            progress = connection.execute(
                "SELECT market, symbol, completed_at FROM trend_curve_batch_items"
            ).fetchall()
            points = connection.execute(
                "SELECT market, symbol, curve_date FROM trend_curve_points"
            ).fetchall()
        assert (
            len(invocations),
            summary["delivery_status"],
            progress,
            points,
        ) == (
            index + 1,
            "unknown",
            [("CN", "600000", "2026-09-09T12:00:00+00:00")],
            [("CN", "600000", "2026-09-03"), ("CN", "600000", "2026-09-04")],
        )

        complete_exit = cli.main(
            ["trend-curve", "daily", "--daily-config", str(config)]
        )
        complete_output = capsys.readouterr()
        complete_payload = json.loads(complete_output.out)
        assert (
            complete_exit,
            complete_output.err,
            complete_payload["status"],
            complete_payload["delivery_status"],
            len(invocations),
        ) == (0, "", "complete", "not_run", index + 1)
