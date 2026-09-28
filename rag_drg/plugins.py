"""Feature plugins: every module in `rag_drg/tools/` is discovered automatically.

A plugin module may define any of:

    def register_cli(subparsers) -> dict[str, Callable[[argparse.Namespace, Config], int]]
        Add sub-commands; return {command_name: handler}. Handlers return an exit code.

    def register_mcp(mcp, ctx: ServerContext) -> None
        Add MCP tools with `@mcp.tool()`. `ctx` carries the config, store, searcher, lock,
        readonly flag and an event bus (`ctx.subscribe(fn)` / `ctx.emit(event)`).

    def lint(cfg: Config) -> list[str]
        Extra problems reported by `rag-drg lint` (and CI).

Keeping features in separate modules means they don't have to edit the CLI or server files.
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
import threading
from dataclasses import dataclass, field
from types import ModuleType
from typing import Any, Callable

log = logging.getLogger(__name__)


def plugin_modules() -> list[ModuleType]:
    from . import tools

    mods = []
    for info in sorted(pkgutil.iter_modules(tools.__path__), key=lambda i: i.name):
        if info.name.startswith("_"):
            continue
        try:
            mods.append(importlib.import_module(f"{tools.__name__}.{info.name}"))
        except Exception as e:  # noqa: BLE001 - a broken optional plugin must not break the CLI
            log.warning("Plugin %s failed to load: %s", info.name, e)
    return mods


@dataclass
class ServerContext:
    cfg: Any
    store: Any
    searcher: Any
    lock: threading.Lock
    readonly: bool = False
    listeners: list[Callable[[dict], None]] = field(default_factory=list)
    # Filled per request by the auth layer when token auth is on (see docs/auth.md).
    current_user: Callable[[], str | None] = lambda: None

    def subscribe(self, fn: Callable[[dict], None]) -> None:
        self.listeners.append(fn)

    def emit(self, event: dict) -> None:
        """Events: {"tool": name, "args": {...}, "results": [...], "n_results": int, ...}."""
        event.setdefault("user", self.current_user())
        for fn in list(self.listeners):
            try:
                fn(event)
            except Exception as e:  # noqa: BLE001 - listeners (e.g. query log) must never break a tool
                log.warning("event listener failed: %s", e)
