"""Exercise HTTP handlers with real HTTP parsing and in-memory socket boundaries."""
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
import json


class WireSocket:
    def __init__(self, data):
        self.input = BytesIO(data)
        self.output = bytearray()
    def makefile(self, *args):
        return self.input
    def sendall(self, data):
        self.output.extend(data)


class MemoryHTTP:
    def __init__(self, monkeypatch):
        self.servers = {}
        self.calls = []
        self.monkeypatch = monkeypatch
        owner = self
        class Connection:
            def __init__(self, host, port, timeout=30):
                assert host in ('127.0.0.1', 'localhost')
                self.port = port
            def request(self, method, path, body=None, headers=None):
                self.response = owner.request(self.port, method, path, headers, body)
            def getresponse(self):
                return self.response
            def close(self):
                pass
        monkeypatch.setattr(http.client, 'HTTPConnection', Connection)

    def patch_server(self, module, name='ThreadingHTTPServer'):
        cls = getattr(module, name)
        def create(address, handler):
            port = address[1] or 20000 + len(self.servers)
            server = cls((address[0], port), handler, bind_and_activate=False)
            server.server_name, server.server_port = address[0], port
            self.servers[port] = server
            return server
        self.monkeypatch.setattr(module, name, create)

    def request(self, port, method='GET', path='/', headers=None, body=None):
        if port not in self.servers:
            raise ConnectionRefusedError('offline fixture disconnected')
        headers = {'Host': f'127.0.0.1:{port}', **(headers or {})}
        if body is not None:
            headers['Content-Length'] = str(len(body))
        self.calls.append((port, method, path, headers, body))
        raw = f'{method} {path} HTTP/1.0\r\n' + ''.join(f'{k}: {v}\r\n' for k, v in headers.items()) + '\r\n'
        wire = WireSocket(raw.encode() + (body or b''))
        server = self.servers[port]
        server.RequestHandlerClass(wire, ('127.0.0.1', 12345), server)
        response = http.client.HTTPResponse(WireSocket(bytes(wire.output)))
        response.begin()
        return response

    def backend(self, payload, port=8769):
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                body = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header('Set-Cookie', 'session=must-remove')
                self.send_header('X-CSRF-Token', 'must-remove')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            do_POST = do_GET
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(('127.0.0.1', port), Handler, bind_and_activate=False)
        owner.servers[port] = server
        return server

    def close(self):
        for server in self.servers.values():
            server.server_close()
