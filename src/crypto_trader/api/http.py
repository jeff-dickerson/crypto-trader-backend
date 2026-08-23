"""The tiny request/response records the router and handlers share.

Kept in its own module so handlers and the WSGI app can both import them without a cycle. A handler
is a plain function `(ApiContext, Request) -> Response`; the router (app.py) adapts the WSGI environ
into a Request and renders a Response (or an ApiError) as JSON with the standard envelope.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Request:
    """One parsed API request."""

    method: str
    path: str
    path_params: dict[str, str] = field(default_factory=dict)
    query: dict[str, str] = field(default_factory=dict)
    json_body: dict | None = None


@dataclass(frozen=True)
class Response:
    """One handler result: an HTTP status and a JSON-serialisable body."""

    status: int
    body: object
