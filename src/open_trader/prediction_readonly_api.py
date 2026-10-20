"""Loopback-only display proxy. No runtime, database or trading credentials."""
from __future__ import annotations

import argparse
from datetime import date
import hashlib
import hmac
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import os
from pathlib import Path
import re
import stat
from urllib.parse import parse_qs, urlsplit

from .frontend_gateway import _require_loopback


PREFIX = '/api/prediction-arbitrage'
READ_PATHS = frozenset({
    '/healthz', PREFIX + '/venues', PREFIX + '/lp/dashboard',
    PREFIX + '/lp/account/trades', PREFIX + '/lp/auto/state',
    PREFIX + '/lp/auto/reservations', PREFIX + '/lp/auto/reports',
})
MAX_RESPONSE_BYTES = 64 * 1024 * 1024


def read_private_file(path: Path) -> str:
    """Pin a private regular file; never follow a substituted symlink."""
    path = path.absolute()
    for parent in path.parents:
        if parent.is_symlink():
            raise ValueError('private file path must not contain symlinks')
    parent = path.parent.stat()
    if parent.st_uid != os.getuid() or stat.S_IMODE(parent.st_mode) != 0o700:
        raise ValueError('private file parent must be owned by current user with mode 0700')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'r', encoding='utf-8') as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600):
            raise ValueError('private file must be owned by current user with mode 0600')
        value = stream.read(65537)
        if len(value) > 65536:
            raise ValueError('private file is too large')
        return value


def client_token(path: Path) -> str:
    token = read_private_file(path).strip()
    if not re.fullmatch(r'[A-Za-z0-9_-]{32,512}', token):
        raise ValueError('invalid private client token')
    return token


def require_tailnet_host(host: str) -> None:
    try:
        valid = ipaddress.ip_address(host) in ipaddress.ip_network('100.64.0.0/10')
    except ValueError:
        valid = bool(re.fullmatch(r'(?=.{1,253}$)[a-zA-Z0-9](?:[a-zA-Z0-9.-]*[a-zA-Z0-9])?\.ts\.net', host))
    if not valid:
        raise ValueError('read-only transport requires a tailnet IPv4 address or .ts.net name')


def allowed_read_target(target: str) -> bool:
    # Canonical ASCII only: no percent/double decoding, dot segments or URL forwarding.
    if not re.fullmatch(r'/[A-Za-z0-9/_?=&.-]*', target) or target.startswith('//'):
        return False
    parsed = urlsplit(target)
    path = parsed.path
    if path == PREFIX + '/lp/account/trades':
        try:
            query = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
        except ValueError:
            return False
        return (set(query) <= {'offset', 'limit'}
                and all(len(values) == 1 and re.fullmatch(r'[0-9]{1,9}', values[0]) for values in query.values())
                and 1 <= int(query.get('limit', ['100'])[0]) <= 100)
    if parsed.query:
        return False
    if path in READ_PATHS:
        return True
    for prefix in (PREFIX + '/lp/auto/reports/', PREFIX + '/lp/reports/'):
        if path.startswith(prefix):
            value = path[len(prefix):]
            try:
                return date.fromisoformat(value).isoformat() == value
            except ValueError:
                return False
    return False


def strip_auth_capabilities(value):
    if isinstance(value, dict):
        return {key: strip_auth_capabilities(item) for key, item in value.items()
                if key.lower().replace('_', '').replace('-', '') not in
                {'csrftoken', 'sessiontoken', 'session', 'otpredictionsession', 'csrf', 'cookie', 'setcookie', 'authorization'}}
    if isinstance(value, list):
        return [strip_auth_capabilities(item) for item in value]
    return value


def create_prediction_readonly_api(*, token_file: Path, host: str = '127.0.0.1',
                                   port: int = 8770, upstream_port: int = 8769,
                                   timeout: float = 30) -> ThreadingHTTPServer:
    _require_loopback(host, 'host')
    if not 0 <= port <= 65535 or not 1 <= upstream_port <= 65535 or timeout <= 0:
        raise ValueError('invalid adapter port or timeout')
    read_private_file(token_file)  # Fail startup on unsafe permissions; re-read for revocation.

    class Handler(BaseHTTPRequestHandler):
        def _json(self, status, value):
            body = json.dumps(value, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            try:
                entries = json.loads(read_private_file(token_file))
                if (not isinstance(entries, dict) or len(entries) > 100
                        or any(not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', name)
                               or not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest)
                               for name, digest in entries.items())):
                    raise ValueError('invalid token registry')
                headers = self.headers.get_all('Authorization', [])
                token = headers[0][7:] if len(headers) == 1 and headers[0].startswith('Bearer ') else ''
                digest = hashlib.sha256(token.encode()).hexdigest()
                matched = False
                for expected in entries.values():
                    matched |= hmac.compare_digest(digest, expected)
                if not 32 <= len(token) <= 512 or not matched:
                    raise ValueError('unauthorized')
            except (OSError, ValueError, TypeError):
                self._json(401, {'code': 'readonly_unauthorized'})
                return
            if not allowed_read_target(self.requestline.split()[1]):
                self._json(403, {'code': 'readonly_path_forbidden'})
                return
            connection = http.client.HTTPConnection('127.0.0.1', upstream_port, timeout=timeout)
            try:
                connection.request('GET', self.path, headers={'Accept': 'application/json',
                                   'X-Open-Trader-Read-Only': 'true'})
                response = connection.getresponse()
                body = response.read(MAX_RESPONSE_BYTES + 1)
                if 300 <= response.status < 400 or len(body) > MAX_RESPONSE_BYTES:
                    raise ValueError('invalid upstream response')
                payload = strip_auth_capabilities(json.loads(body))
                if not isinstance(payload, dict):
                    raise ValueError("invalid upstream JSON object")
                payload["access"] = "read_only"
                self._json(response.status, payload)
            except (OSError, ValueError, http.client.HTTPException):
                self._json(503, {'code': 'prediction_service_unavailable'})
            finally:
                connection.close()

        def _deny(self):
            self._json(405, {'code': 'readonly_method_forbidden'})

        def __getattr__(self, name):
            if name.startswith('do_'):
                return self._deny
            raise AttributeError(name)

        def log_message(self, *args):
            pass  # Never log request paths, headers or bearer tokens.

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    return server


def main(argv=None):
    parser = argparse.ArgumentParser(prog='open-trader prediction-readonly-api')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8770)
    parser.add_argument('--token-file', type=Path, required=True)
    args = parser.parse_args(argv)
    server = create_prediction_readonly_api(token_file=args.token_file, host=args.host, port=args.port)
    try:
        server.serve_forever()
    finally:
        server.server_close()
    return 0
