"""The HTTP server behind ``rag-drg serve --transport http|sse``: one ASGI app with

* the MCP endpoint (``/mcp`` streamable HTTP, or ``/sse`` + ``/messages/``) from the MCP SDK,
* the REST API under ``/api`` (see :mod:`rag_drg.tools.rest_api`),
* bearer-token auth in front of both (``/api/health`` is public), see :mod:`rag_drg.auth`.

Works with MCP Python SDK 2.x (``MCPServer``) and 1.x (``FastMCP``). The SDK's lifespan
(which starts the streamable-HTTP session manager) is passed through untouched.
"""

from __future__ import annotations

import logging
import sys

from .auth import BearerAuthMiddleware, TokenStore, is_loopback

log = logging.getLogger(__name__)

LOOPBACK_HOSTS = ["127.0.0.1", "localhost", "[::1]"]


def transport_security(host: str, allowed_hosts: list[str] | None):
    """DNS-rebinding protection settings for the SDK (Host/Origin header checks).

    * `allowed_hosts` given: protection on, allowing those names (with or without a port)
      plus loopback. Use the name clients put in their URL, e.g. rag.chem.example.ac.il.
      `["*"]` switches the check off.
    * none given, loopback bind: the SDK default (loopback names only).
    * none given, other bind (0.0.0.0): check off, as the SDK itself does; the bearer token
      is what protects the server (a DNS-rebinding page in a browser has no token).
    """
    try:
        from mcp.server.transport_security import TransportSecuritySettings
    except ImportError:  # pragma: no cover - very old SDK
        return None
    hosts = [h.strip() for h in (allowed_hosts or []) if h and h.strip()]
    if "*" in hosts:
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)
    if not hosts:
        if is_loopback(host):
            hosts = []
        else:
            return TransportSecuritySettings(enable_dns_rebinding_protection=False)
    if host not in ("0.0.0.0", "::", "") and host not in hosts:
        hosts.append(host)
    names: list[str] = []
    for h in hosts + LOOPBACK_HOSTS:
        has_port = "]:" in h if h.startswith("[") else h.count(":") == 1
        for v in (h,) if has_port else (h, f"{h}:*"):  # Host: name, or name:port
            if v not in names:
                names.append(v)
    origins = []
    for h in names:
        for scheme in ("http", "https"):
            origins.append(f"{scheme}://{h}")
    return TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=names,
                                     allowed_origins=origins)


def _mcp_asgi_app(mcp, transport: str, host: str, security, stateless: bool = True):
    """`stateless` (streamable HTTP only): no MCP session IDs. rag-drg keeps no per-session state,
    and without sessions a restarted server doesn't strand connected agents on "unknown or expired
    session ID" (404) until they reconnect."""
    if getattr(mcp, "_rag_drg_sdk_major", 2) >= 2:
        if transport == "sse":
            return mcp.sse_app(transport_security=security, host=host)
        return mcp.streamable_http_app(transport_security=security, host=host, stateless_http=stateless)
    # SDK 1.x: options live on mcp.settings
    settings = getattr(mcp, "settings", None)
    if settings is not None and security is not None and hasattr(settings, "transport_security"):
        settings.transport_security = security
    if settings is not None and hasattr(settings, "stateless_http"):
        settings.stateless_http = stateless
    return mcp.sse_app() if transport == "sse" else mcp.streamable_http_app()


def _allowed(value: str, patterns: list[str]) -> bool:
    for p in patterns:
        if value == p or (p.endswith(":*") and value.rsplit(":", 1)[0] == p[:-2] and ":" in value):
            return True
    return False


async def _reject(send, status: int, message: str):
    body = message.encode()
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"text/plain"), (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})


class _Router:
    """`/api/...` -> REST app, everything else (and lifespan) -> the MCP app.

    The SDK's Host/Origin (DNS-rebinding) checks only guard its own endpoints, so the same
    settings are applied to /api here."""

    def __init__(self, mcp_app, rest_app, security=None):
        self.mcp_app = mcp_app
        self.rest_app = rest_app
        self.security = security

    def _host_problem(self, scope) -> tuple[int, str] | None:
        sec = self.security
        if sec is None or not getattr(sec, "enable_dns_rebinding_protection", False):
            return None
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers") or []}
        if not _allowed(headers.get("host", ""), list(sec.allowed_hosts or [])):
            return 421, "Invalid Host header"
        origin = headers.get("origin")
        if origin and not _allowed(origin, list(sec.allowed_origins or [])):
            return 403, "Invalid Origin header"
        return None

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            path = scope.get("path", "")
            if path == "/api" or path.startswith("/api/"):
                problem = self._host_problem(scope)
                if problem:
                    await _reject(send, *problem)
                    return
                scope = dict(scope)
                scope["root_path"] = scope.get("root_path", "") + "/api"
                scope["path"] = path[4:] or "/"
                await self.rest_app(scope, receive, send)
                return
        await self.mcp_app(scope, receive, send)


def attach_user(mcp) -> None:
    """Make `ctx.current_user()` return the authenticated token name during a request.

    SDK 2.x carries the contextvar set by the auth middleware into tool calls; 1.x does not,
    so fall back to the Starlette request scope the SDK hands to the tool context."""
    from .auth import current_user, sdk_request_user

    mcp._rag_drg_ctx.current_user = lambda: current_user() or sdk_request_user(mcp)


def build_http_app(mcp, transport: str = "http", host: str = "127.0.0.1",
                   allowed_hosts: list[str] | None = None, token_store: TokenStore | None = None,
                   stateless: bool = True):
    """ASGI app: MCP + REST, behind bearer-token auth when `token_store` is given."""
    from .tools.rest_api import build_rest_app

    ctx = mcp._rag_drg_ctx
    security = transport_security(host, allowed_hosts)
    app = _Router(_mcp_asgi_app(mcp, transport, host, security, stateless), build_rest_app(ctx), security)
    if token_store is not None:
        app = BearerAuthMiddleware(app, token_store, public_paths=("/api/health",))
    return app


def run_http(mcp, transport: str, host: str, port: int, allowed_hosts: list[str] | None,
             token_store: TokenStore | None, ssl_certfile: str | None = None, ssl_keyfile: str | None = None,
             stateless: bool = True):
    import uvicorn

    app = build_http_app(mcp, transport, host, allowed_hosts, token_store, stateless=stateless)
    scheme = "https" if ssl_certfile else "http"
    shown = f"[{host}]" if ":" in host else host
    path = "/sse" if transport == "sse" else "/mcp"
    auth = f"token ({len(token_store.entries())} token(s))" if token_store else "NONE"
    print(f"rag-drg: MCP at {scheme}://{shown}:{port}{path}, REST at {scheme}://{shown}:{port}/api, auth: {auth}",
          file=sys.stderr)
    config = uvicorn.Config(app, host=host, port=port, log_level="info",
                            ssl_certfile=ssl_certfile, ssl_keyfile=ssl_keyfile)
    try:
        uvicorn.Server(config).run()
    finally:
        if token_store is not None:
            token_store.flush()
