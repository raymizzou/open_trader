from __future__ import annotations

import argparse
import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parents[2]
STATIC_DIR = ROOT / "src" / "open_trader" / "dashboard_static"
FIXTURE_PATH = Path(__file__).with_name("fixtures") / "kelly-dashboard.json"


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/":
            self._send_file(STATIC_DIR / "index.html", "text/html; charset=utf-8")
            return
        if path == "/static/dashboard.css":
            self._send_file(STATIC_DIR / "dashboard.css", "text/css; charset=utf-8")
            return
        if path == "/static/dashboard.js":
            self._send_file(STATIC_DIR / "dashboard.js", "application/javascript; charset=utf-8")
            return
        if path == "/api/dashboard":
            self._send_json(json.loads(FIXTURE_PATH.read_text(encoding="utf-8")))
            return
        if path == "/api/quotes":
            self._send_json(
                {
                    "status": "ok",
                    "requested_count": 0,
                    "quote_count": 0,
                    "missing_count": 0,
                    "fetched_at": "2026-07-07T15:30:00+08:00",
                    "last_success_at": "2026-07-07T15:30:00+08:00",
                    "stale": False,
                    "quotes": {},
                    "diagnostic": {},
                }
            )
            return
        self.send_response(HTTPStatus.NOT_FOUND)
        self.end_headers()
        self.wfile.write(b"not found")

    def log_message(self, format: str, *args: object) -> None:
        return

    def _send_json(self, payload: dict[str, object]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path, content_type: str) -> None:
        body = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"fixture_dashboard_url: http://{args.host}:{args.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
