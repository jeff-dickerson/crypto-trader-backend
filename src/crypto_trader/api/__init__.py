"""The in-process REST API (Build Order step "Interfaces: backend REST API").

A small, dependency-free WSGI application over the paper machine's live in-process state and the
unified SQLite store. It serves the twelve routes the frontend's ApiClient needs behind an
`/api/v1` prefix, with one error envelope, cursor pagination on the append-heavy histories, and
Tailscale-only exposure as a bind-address choice.

Framework choice (documented per the project's documented-decision pattern, AGENTS.md): the
standard library only (a plain WSGI callable plus `wsgiref` for the runnable server). Rationale:

- The project keeps a deliberately minimal dependency footprint (only `requests`); FastAPI/Flask
  and an ASGI/WSGI server would pull a large transitive tree for a single-user, single-process,
  polling-only, in-process API that decision 1 puts behind the tailnet with no auth to negotiate.
- The API is in-process with the bot (hosting decision 5), so a synchronously-invoked WSGI
  callable fits the "no blocking I/O in any handler, a timeout wrapper on every outbound call"
  invariant, and is trivially testable offline by calling the app directly with no sockets and no
  network (conduct rule 6: no flakiness, tests never touch the network).
- `wsgiref.simple_server` gives the runnable HTTP server; the bind host is configurable so
  Tailscale-only exposure is purely a bind-address choice (no Tailscale provisioning is built
  here, that is deployment/infra, out of scope).
"""

from __future__ import annotations
