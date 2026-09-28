"""One `rag-drg arc ...` command shared by several plugins.

Each plugin calls :func:`arc_group` to get the `arc` sub-command group (created on first use) and
:func:`add_arc_command` to add its own sub-commands with a handler; `rag-drg arc <cmd>` then
dispatches to the right plugin whatever order plugins are loaded in.
"""

from __future__ import annotations

from typing import Callable

_HANDLERS: dict[str, Callable] = {}


def arc_group(subparsers):
    existing = subparsers.choices.get("arc")
    if existing is not None and hasattr(existing, "_rag_drg_arc_sub"):
        return existing._rag_drg_arc_sub
    p = subparsers.add_parser("arc", help="ARC: input schema, input.yml checks, composing a run on a cluster")
    sub = p.add_subparsers(dest="arc_cmd", required=True)
    p._rag_drg_arc_sub = sub
    return sub


def add_arc_command(name: str, handler: Callable) -> None:
    _HANDLERS[name] = handler


def dispatch(args, cfg) -> int:
    handler = _HANDLERS.get(args.arc_cmd)
    if handler is None:
        raise SystemExit(f"unknown `rag-drg arc` command {args.arc_cmd!r}")
    return int(handler(args, cfg) or 0)
