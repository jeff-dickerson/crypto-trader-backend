"""The runnable HTTP server binds and serves over a real loopback socket.

This proves the deliverable is an actual running HTTP API (not only a WSGI callable): a wsgiref
server bound to 127.0.0.1:0 answers one real GET /api/v1/health. Loopback only, so it touches no
network and no live API; the bind host is a parameter, which is how Tailscale-only exposure is a
bind-address choice.
"""

from __future__ import annotations

import http.client
import json
import threading

from crypto_trader.api.server import make_http_server
from tests.api_helpers import build_test_context


def test_server_binds_and_serves_health_over_a_socket() -> None:
    ctx = build_test_context()
    server = make_http_server(ctx, host="127.0.0.1", port=0)
    host, port = server.server_address[0], server.server_address[1]
    worker = threading.Thread(target=server.handle_request, daemon=True)
    worker.start()
    try:
        conn = http.client.HTTPConnection(host, port, timeout=5)
        conn.request("GET", "/api/v1/health")
        resp = conn.getresponse()
        assert resp.status == 200
        body = json.loads(resp.read())
        assert body["status"] == "ok"
        conn.close()
    finally:
        worker.join(5)
        server.server_close()
