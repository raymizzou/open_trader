"""HTTP proxy handshake cleanup for the SDK's websockets 15.0.1 dependency."""

from __future__ import annotations

import asyncio

import websockets
from websockets.asyncio import client


async def _connect_http_proxy(proxy, ws_uri, user_agent_header=None, **kwargs):
    # Resolve the private protocol only on the version this patch supports.
    class HTTPProxyConnection(client.HTTPProxyConnection):
        def data_received(self, data: bytes) -> None:
            if not self.response.done():
                super().data_received(data)

        def eof_received(self) -> None:
            if not self.response.done() and not self.reader.eof:
                super().eof_received()

        def connection_lost(self, exc: Exception | None) -> None:
            if self.response.done():
                return
            if exc is not None:
                self.response.set_exception(exc)
            else:
                self.eof_received()

    transport, protocol = await asyncio.get_running_loop().create_connection(
        lambda: HTTPProxyConnection(ws_uri, proxy, user_agent_header),
        proxy.host,
        proxy.port,
        **kwargs,
    )
    try:
        await protocol.response
    except BaseException:
        # 15.0.1 misses CancelledError, leaving a live proxy transport behind.
        transport.abort()
        raise
    return transport


def install_proxy_cleanup() -> None:
    # ponytail: one version-scoped SDK compatibility seam; remove after the SDK
    # permits a dependency with cancellation, late-response and EOF tests green.
    # Install before Prediction starts its loops; never alter proxy/TLS routing.
    if websockets.__version__ == "15.0.1":
        client.connect_http_proxy = _connect_http_proxy
