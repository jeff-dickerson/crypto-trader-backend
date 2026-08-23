"""The runnable HTTP server (wsgiref), bound to a configurable host.

Tailscale-only exposure (decision 1) is a bind-address choice, not code here: bind to the tailnet
interface (for example the host's Tailscale IP, or a loopback address for local development) and no
other interface can reach the API. No Tailscale provisioning is built here, that is deployment
work; this module only makes the single-process, static-file-capable hosting shape possible.
"""

from __future__ import annotations

from wsgiref.simple_server import WSGIServer, make_server

from crypto_trader.api.app import make_wsgi_app
from crypto_trader.api.context import ApiContext

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787


def make_http_server(
    ctx: ApiContext, *, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT
) -> WSGIServer:
    """Build (but do not start) a wsgiref server for `ctx`, bound to `host:port`.

    Returned unstarted so a caller can inspect the bound address (server.server_address) or run it
    with serve_forever(); binding to port 0 lets the OS choose a free port for a smoke test.
    """
    return make_server(host, port, make_wsgi_app(ctx))


def serve(ctx: ApiContext, *, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> None:
    """Serve the API forever on `host:port` (blocks). Bind `host` to the tailnet interface."""
    server = make_http_server(ctx, host=host, port=port)
    server.serve_forever()
