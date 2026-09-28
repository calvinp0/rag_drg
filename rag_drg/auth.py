"""Bearer-token authentication for the HTTP/SSE server (see docs/auth.md).

Tokens live in a YAML file (default ``index/tokens.yaml``, git-ignored; override with
``auth: {tokens_file: ...}`` in rag_drg.yaml). Only SHA-256 hashes are stored::

    tokens:
      - name: alice
        sha256: 5e8f...
        created: 2026-09-28T10:00:00+00:00
        last_used: 2026-09-28T11:02:13+00:00

A token is shown once, by ``rag-drg tokens add NAME``. The server re-reads the file when it
changes, so adding or revoking a token needs no restart.

:class:`BearerAuthMiddleware` is a plain ASGI middleware: it answers 401 (JSON) unless the
request carries ``Authorization: Bearer <token>`` matching a stored hash, and otherwise
sets :data:`current_user_var` (and ``scope["rag_drg.user"]``) to the token's name, so MCP
tool events and lessons are attributed to the person the token was issued to.
"""

from __future__ import annotations

import contextlib
import contextvars
import datetime as _dt
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import secrets
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)

TOKEN_PREFIX = "rdg_"
LAST_USED_FLUSH_SECONDS = 60.0

#: Name of the token that authenticated the current request (None outside a request).
current_user_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("rag_drg_user", default=None)


def current_user() -> str | None:
    return current_user_var.get()


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def tokens_path(cfg) -> Path:
    """`auth.tokens_file` from the config (relative to the config dir), else index/tokens.yaml."""
    auth_cfg = (getattr(cfg, "extra", None) or {}).get("auth") or {}
    p = auth_cfg.get("tokens_file")
    if p:
        q = Path(os.path.expandvars(os.path.expanduser(str(p))))
        return q if q.is_absolute() else Path(cfg.root) / q
    return Path(cfg.index_path).parent / "tokens.yaml"


@contextlib.contextmanager
def _file_lock(path: Path):
    """Advisory lock on `<file>.lock` so the CLI and the server don't lose each other's writes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    with open(lock_path, "a+") as fh:
        try:
            import fcntl

            fcntl.flock(fh, fcntl.LOCK_EX)
        except (ImportError, OSError):  # pragma: no cover - non-POSIX
            pass
        try:
            yield
        finally:
            try:
                import fcntl

                fcntl.flock(fh, fcntl.LOCK_UN)
            except (ImportError, OSError):  # pragma: no cover
                pass


class TokenStore:
    """Read/write the tokens file. Thread-safe; cheap to call `verify` on every request."""

    def __init__(self, path: str | os.PathLike):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._entries: list[dict] = []
        self._mtime: float | None = None
        self._pending_last_used: dict[str, str] = {}
        self._last_flush = 0.0

    # --- file io -----------------------------------------------------------------------
    def _read(self) -> list[dict]:
        if not self.path.is_file():
            return []
        raw = yaml.safe_load(self.path.read_text()) or {}
        out = []
        for e in raw.get("tokens") or []:
            if isinstance(e, dict) and e.get("name") and e.get("sha256"):
                out.append(dict(e))
        return out

    def _write(self, entries: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        text = (
            "# rag-drg API tokens (SHA-256 hashes only). Manage with `rag-drg tokens add|list|revoke`.\n"
            + yaml.safe_dump({"tokens": entries}, sort_keys=False)
        )
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".tokens.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write(text)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise

    def _reload_if_changed(self) -> None:
        try:
            mtime = self.path.stat().st_mtime_ns
        except FileNotFoundError:
            mtime = None
        if mtime != self._mtime:
            try:
                self._entries = self._read()
            except Exception as e:  # noqa: BLE001 - keep serving with the last good copy
                log.warning("could not read %s: %s", self.path, e)
                return
            self._mtime = mtime

    # --- management (CLI) ----------------------------------------------------------------
    def entries(self) -> list[dict]:
        with self._lock:
            self._reload_if_changed()
            return [dict(e) for e in self._entries]

    def add(self, name: str) -> str:
        name = name.strip()
        if not name or any(c.isspace() for c in name):
            raise ValueError("token name must be non-empty and contain no whitespace")
        token = TOKEN_PREFIX + secrets.token_urlsafe(32)
        with self._lock, _file_lock(self.path):
            entries = self._read()
            if any(e["name"] == name for e in entries):
                raise ValueError(f"a token named '{name}' already exists (revoke it first)")
            entries.append({"name": name, "sha256": hash_token(token), "created": _now()})
            self._write(entries)
            self._mtime = None
        return token

    def revoke(self, name: str) -> bool:
        with self._lock, _file_lock(self.path):
            entries = self._read()
            keep = [e for e in entries if e["name"] != name]
            if len(keep) == len(entries):
                return False
            self._write(keep)
            self._mtime = None
        return True

    # --- request path ----------------------------------------------------------------------
    def verify(self, token: str | None) -> str | None:
        """Name of the token's owner, or None. Compares against every hash in constant time."""
        if not token:
            return None
        presented = hash_token(token)
        with self._lock:
            self._reload_if_changed()
            entries = list(self._entries)
        found = None
        for e in entries:
            if hmac.compare_digest(presented, str(e["sha256"])):
                found = e["name"]  # no early exit: timing does not depend on position
        if found:
            self._touch(found)
        return found

    def _touch(self, name: str) -> None:
        with self._lock:
            self._pending_last_used[name] = _now()
            due = time.monotonic() - self._last_flush >= LAST_USED_FLUSH_SECONDS
        if due:
            self.flush()

    def flush(self) -> None:
        """Write pending `last_used` stamps (merged into the current file, never overwriting adds)."""
        with self._lock:
            pending, self._pending_last_used = self._pending_last_used, {}
            self._last_flush = time.monotonic()
        if not pending:
            return
        try:
            with _file_lock(self.path):
                entries = self._read()
                changed = False
                for e in entries:
                    if e["name"] in pending:
                        e["last_used"] = pending[e["name"]]
                        changed = True
                if changed:
                    self._write(entries)
        except OSError as e:  # read-only deployment: last_used is nice-to-have
            log.warning("could not record token last_used in %s: %s", self.path, e)


# --- ASGI middleware ------------------------------------------------------------------------


async def _send_json(send, status: int, body: dict, headers: list[tuple[bytes, bytes]] | None = None) -> None:
    data = json.dumps(body).encode()
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(data)).encode())]
        + (headers or []),
    })
    await send({"type": "http.response.body", "body": data})


def bearer_token(scope) -> str | None:
    for k, v in scope.get("headers") or []:
        if k.lower() == b"authorization":
            value = v.decode("latin-1").strip()
            scheme, _, cred = value.partition(" ")
            if scheme.lower() == "bearer" and cred.strip():
                return cred.strip()
    return None


class BearerAuthMiddleware:
    """Require a valid bearer token on every HTTP request except `public_paths`.

    Lifespan events pass straight through, so the wrapped app's startup (e.g. the MCP SDK's
    streamable-HTTP session manager) still runs.
    """

    def __init__(self, app, store: TokenStore, public_paths: tuple[str, ...] = ("/api/health",)):
        self.app = app
        self.store = store
        self.public_paths = public_paths

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        if scope.get("path") in self.public_paths:
            await self.app(scope, receive, send)
            return
        token = bearer_token(scope)
        user = self.store.verify(token) if token else None
        if user is None:
            if scope["type"] == "websocket":  # pragma: no cover - not used by MCP
                await send({"type": "websocket.close", "code": 1008})
                return
            await _send_json(
                send, 401,
                {"error": "unauthorized",
                 "detail": "missing bearer token" if not token else "invalid or revoked token",
                 "hint": "send 'Authorization: Bearer <token>'; ask the server admin for `rag-drg tokens add <you>`"},
                [(b"www-authenticate", b'Bearer realm="rag-drg"')],
            )
            return
        scope = dict(scope)
        scope["rag_drg.user"] = user
        reset = current_user_var.set(user)
        try:
            await self.app(scope, receive, send)
        finally:
            current_user_var.reset(reset)


def sdk_request_user(mcp: Any) -> str | None:
    """Fallback for SDKs that don't carry contextvars from the ASGI request into tool calls
    (MCP SDK 1.x): read the user the middleware put in the Starlette request scope."""
    try:
        req = mcp.get_context().request_context.request
        return (req.scope or {}).get("rag_drg.user") if req is not None else None
    except Exception:  # noqa: BLE001 - outside a request, or an SDK without get_context
        return None


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False
