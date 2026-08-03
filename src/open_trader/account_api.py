from __future__ import annotations

import argparse
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Mapping
from urllib.parse import urlsplit

from .account_snapshot import load_account_snapshot, load_worker_git_sha


_GIT_SHA_RE = re.compile(r"[0-9a-f]{40}\Z")


def create_account_api(
    data_dir: Path,
    *,
    host: str,
    port: int,
    runtime_metadata: Mapping[str, object] | None = None,
) -> ThreadingHTTPServer:
    try:
        if not ipaddress.ip_address(host).is_loopback:
            raise ValueError("host must be a loopback address")
    except ValueError as error:
        raise ValueError("host must be a loopback address") from error
    runtime = dict(runtime_metadata) if runtime_metadata is not None else _runtime_metadata()

    class AccountApiHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            path = urlsplit(self.path).path
            if path == "/healthz":
                worker_sha = load_worker_git_sha(data_dir)
                api_sha = str(runtime["api_git_sha"])
                self._send_json(
                    {
                        "schema_version": "open_trader.account_api.health.v1",
                        "module": "account_api",
                        "status": "ok",
                        "mode": "shadow",
                        "pid": runtime["pid"],
                        "started_at": runtime["started_at"],
                        "api_git_sha": api_sha,
                        "worker_git_sha": worker_sha,
                        "release_match": bool(worker_sha) and worker_sha == api_sha,
                        "source": "account_sync_worker_publication",
                    }
                )
                return
            if path == "/api/v1/account/snapshot":
                result = load_account_snapshot(
                    data_dir,
                    api_git_sha=str(runtime["api_git_sha"]),
                    now=datetime.now().astimezone(),
                )
                if result.etag and self.headers.get("If-None-Match") == result.etag:
                    self.send_response(HTTPStatus.NOT_MODIFIED)
                    self.send_header("ETag", result.etag)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self._send_json(result.payload, result.status_code, etag=result.etag)
                return
            self._send_json(
                {
                    "schema_version": "open_trader.account_api.error.v1",
                    "code": "not_found",
                    "message": "Not found",
                },
                HTTPStatus.NOT_FOUND,
            )

        def _send_json(
            self,
            payload: dict[str, object],
            status: int | HTTPStatus = HTTPStatus.OK,
            *,
            etag: str | None = None,
        ) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            if etag:
                self.send_header("ETag", etag)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self._write(body)

        def _write(self, body: bytes) -> None:
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                return

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer((host, port), AccountApiHandler)
    server.daemon_threads = True
    server.runtime_metadata = runtime  # type: ignore[attr-defined]
    return server


def serve_account_api(data_dir: Path) -> None:
    host = "127.0.0.1"
    port = 8768
    server = create_account_api(data_dir, host=host, port=port)
    runtime = {
        "schema_version": "open_trader.account_api.runtime.v1",
        "module": "account_api",
        "mode": "shadow",
        **server.runtime_metadata,  # type: ignore[attr-defined]
        "host": host,
        "port": server.server_address[1],
    }
    print(
        f"account_api_runtime: {json.dumps(runtime, separators=(',', ':'))}",
        flush=True,
    )
    try:
        server.serve_forever()
    finally:
        server.server_close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="open-trader account-api")
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    args = parser.parse_args(argv)
    serve_account_api(args.data_dir)
    return 0


def _runtime_metadata() -> dict[str, object]:
    cwd = Path.cwd().resolve()
    try:
        api_git_sha = subprocess.check_output(
            ["git", "-C", str(cwd), "rev-parse", "HEAD"], text=True
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        api_git_sha = ""
    if _GIT_SHA_RE.fullmatch(api_git_sha) is None:
        api_git_sha = ""
    return {
        "pid": os.getpid(),
        "started_at": datetime.now().astimezone().isoformat(),
        "cwd": str(cwd),
        "api_git_sha": api_git_sha,
    }
