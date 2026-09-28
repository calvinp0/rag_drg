"""`rag-drg tokens add|list|revoke`: API tokens for the HTTP server (see docs/auth.md)."""

from __future__ import annotations

import sys


def register_cli(sub):
    p = sub.add_parser("tokens", help="manage API tokens for `serve --transport http` (add / list / revoke)")
    tsub = p.add_subparsers(dest="tokens_cmd", required=True)
    a = tsub.add_parser("add", help="create a token for NAME and print it once")
    a.add_argument("name", help="who the token is for, e.g. a username; shown in logs and lessons")
    tsub.add_parser("list", help="list token names, creation and last-use times")
    r = tsub.add_parser("revoke", help="delete NAME's token")
    r.add_argument("name")
    return {"tokens": _tokens}


def _tokens(args, cfg) -> int:
    from ..auth import TokenStore, tokens_path

    store = TokenStore(tokens_path(cfg))
    if args.tokens_cmd == "add":
        try:
            token = store.add(args.name)
        except ValueError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        print(f"Token for '{args.name}' (shown only once; stored as a SHA-256 hash in {store.path}):",
              file=sys.stderr)
        print(token)
        print("Use it as:  export RAG_DRG_TOKEN=<token>\n"
              '  claude mcp add --transport http rag-drg http://<host>:8765/mcp '
              '--header "Authorization: Bearer $RAG_DRG_TOKEN"', file=sys.stderr)
        return 0
    if args.tokens_cmd == "list":
        entries = store.entries()
        if not entries:
            print(f"No tokens in {store.path}.")
            return 0
        for e in entries:
            print(f"{e['name']:20s} created {e.get('created', '-')}  last used {e.get('last_used') or 'never'}")
        return 0
    if args.tokens_cmd == "revoke":
        if store.revoke(args.name):
            print(f"Revoked '{args.name}'.")
            return 0
        print(f"error: no token named '{args.name}'", file=sys.stderr)
        return 1
    return 1
