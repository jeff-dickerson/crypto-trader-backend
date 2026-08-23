"""The router and the WSGI application.

A tiny segment-matching router maps (method, path) to one of the twelve handlers, all under the
`/api/v1` prefix so v1 stays live if a v2 is ever added. It renders a Response as JSON and any
ApiError (or unexpected exception) as the standard error envelope with the right status. There is
no third-party framework: `make_wsgi_app` returns a plain WSGI callable, and `dispatch` is exposed
so tests can drive the router directly, both offline and without sockets (see api/__init__.py for
the framework rationale).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from urllib.parse import parse_qs

from crypto_trader.api.context import ApiContext
from crypto_trader.api.errors import ApiError, ErrorCode
from crypto_trader.api.handlers import (
    handle_approval_decision,
    handle_approvals,
    handle_dashboard,
    handle_health,
    handle_journal,
    handle_kill_switch_arm,
    handle_kill_switch_get,
    handle_kill_switch_rearm,
    handle_risk_get,
    handle_risk_patch,
    handle_signals,
    handle_terminal,
)
from crypto_trader.api.http import Request, Response

Handler = Callable[[ApiContext, Request], Response]

API_PREFIX = "/api/v1"

# (method, path template, handler). A `{name}` segment captures a path parameter.
ROUTES: list[tuple[str, str, Handler]] = [
    ("GET", f"{API_PREFIX}/health", handle_health),
    ("GET", f"{API_PREFIX}/dashboard", handle_dashboard),
    ("GET", f"{API_PREFIX}/approvals", handle_approvals),
    ("POST", f"{API_PREFIX}/approvals/{{id}}/decision", handle_approval_decision),
    ("GET", f"{API_PREFIX}/signals", handle_signals),
    ("GET", f"{API_PREFIX}/journal", handle_journal),
    ("GET", f"{API_PREFIX}/terminal", handle_terminal),
    ("GET", f"{API_PREFIX}/risk", handle_risk_get),
    ("PATCH", f"{API_PREFIX}/risk", handle_risk_patch),
    ("GET", f"{API_PREFIX}/kill-switch", handle_kill_switch_get),
    ("POST", f"{API_PREFIX}/kill-switch/arm", handle_kill_switch_arm),
    ("POST", f"{API_PREFIX}/kill-switch/rearm", handle_kill_switch_rearm),
]


def _normalise(path: str) -> str:
    if len(path) > 1 and path.endswith("/"):
        return path.rstrip("/")
    return path


def _match(template: str, path: str) -> dict[str, str] | None:
    t_parts = template.strip("/").split("/")
    p_parts = path.strip("/").split("/")
    if len(t_parts) != len(p_parts):
        return None
    params: dict[str, str] = {}
    for t, p in zip(t_parts, p_parts):
        if t.startswith("{") and t.endswith("}"):
            params[t[1:-1]] = p
        elif t != p:
            return None
    return params


def dispatch(ctx: ApiContext, request: Request) -> Response:
    """Route one already-parsed Request to its handler, rendering every error as an envelope."""
    path = _normalise(request.path)
    path_matched = False
    for method, template, handler in ROUTES:
        params = _match(template, path)
        if params is None:
            continue
        path_matched = True
        if method != request.method:
            continue
        bound = Request(
            method=request.method,
            path=path,
            path_params=params,
            query=request.query,
            json_body=request.json_body,
        )
        try:
            return handler(ctx, bound)
        except ApiError as exc:
            return Response(exc.http_status, exc.to_envelope())
        except Exception as exc:  # noqa: BLE001 - never leak a stack trace to the client
            err = ApiError(ErrorCode.INTERNAL, f"internal error: {exc}")
            return Response(err.http_status, err.to_envelope())

    code = ErrorCode.NOT_FOUND
    msg = (
        f"no route for {request.method} {path}"
        if not path_matched
        else f"method {request.method} not allowed for {path}"
    )
    err = ApiError(code, msg)
    return Response(err.http_status, err.to_envelope())


def build_request(
    method: str, path: str, query_string: str = "", body_bytes: bytes = b""
) -> Request:
    """Parse method/path/query/body into a Request; raises ApiError(VALIDATION) on bad JSON."""
    query = {k: v[-1] for k, v in parse_qs(query_string, keep_blank_values=True).items()}
    json_body: dict | None = None
    if body_bytes:
        try:
            parsed = json.loads(body_bytes.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ApiError(ErrorCode.VALIDATION, f"malformed JSON body: {exc}") from None
        if not isinstance(parsed, dict):
            raise ApiError(ErrorCode.VALIDATION, "JSON body must be an object")
        json_body = parsed
    return Request(method=method, path=path, query=query, json_body=json_body)


def make_wsgi_app(ctx: ApiContext) -> Callable[[dict, Callable], Iterable[bytes]]:
    """Return a WSGI callable serving the API over `ctx`. No third-party framework."""

    def application(environ: dict, start_response: Callable) -> Iterable[bytes]:
        method = environ.get("REQUEST_METHOD", "GET")
        path = environ.get("PATH_INFO", "/") or "/"
        query_string = environ.get("QUERY_STRING", "")
        body_bytes = _read_body(environ)
        try:
            request = build_request(method, path, query_string, body_bytes)
            response = dispatch(ctx, request)
        except ApiError as exc:
            response = Response(exc.http_status, exc.to_envelope())
        payload = json.dumps(response.body).encode("utf-8")
        start_response(
            _status_line(response.status),
            [
                ("Content-Type", "application/json"),
                ("Content-Length", str(len(payload))),
            ],
        )
        return [payload]

    return application


def _read_body(environ: dict) -> bytes:
    try:
        length = int(environ.get("CONTENT_LENGTH") or 0)
    except (TypeError, ValueError):
        length = 0
    if length <= 0:
        return b""
    return environ["wsgi.input"].read(length)


_STATUS_TEXT = {
    200: "200 OK",
    400: "400 Bad Request",
    404: "404 Not Found",
    409: "409 Conflict",
    500: "500 Internal Server Error",
    503: "503 Service Unavailable",
}


def _status_line(status: int) -> str:
    return _STATUS_TEXT.get(status, f"{status} Status")
