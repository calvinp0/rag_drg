# Authentication and deployment of the shared server

The shared server serves text from **licensed manuals** (ORCA, Gaussian, Q-Chem, Molpro), so
over HTTP it requires a per-person bearer token. The stdio transport (`rag-drg serve`, one
process per agent session on your own machine) is unaffected: no tokens there.

## Tokens

```bash
rag-drg tokens add alice      # prints the token ONCE (stdout); give it to alice
rag-drg tokens list           # names, created, last used (never the token)
rag-drg tokens revoke alice   # takes effect on the next request, no restart needed
```

* Tokens are stored as SHA-256 hashes in `index/tokens.yaml` (mode 0600; `index/` is
  git-ignored). Use another file with `auth: {tokens_file: /etc/rag-drg/tokens.yaml}` in
  `rag_drg.yaml` or a `conf.d/*.yaml` (relative paths are relative to the config file); on the
  server use the git-ignored `conf.d/zz-local.yaml` rather than a local commit, which would
  break the nightly `git pull --ff-only`.
* One token per person: the token name is who the server thinks you are. It is attached to
  every server event (`ctx.current_user()` in plugins, e.g. the query log) and is the natural
  author for recorded lessons.
* The server re-reads the file when it changes and stamps `last_used` (at most once a minute).
* Comparison is constant-time against every stored hash.
* Lost or leaked token: `revoke` it and `add` a new one.

## Running the server

```bash
rag-drg serve --transport http --host 0.0.0.0 --port 8765 \
    --allowed-host rag.chem.example.ac.il          # the name clients put in the URL
```

This runs one uvicorn server with:

| Path | What | Auth |
|---|---|---|
| `/mcp` | MCP, streamable HTTP (`--transport sse`: `/sse` + `/messages/` instead) | token |
| `/api/search`, `/api/level`, `/api/context` | REST API for non-MCP clients ([local-models.md](../integrations/local-models.md)) | token |
| `/api/health` | liveness check (`{"status": "ok"}`) | none |

Requests without a valid `Authorization: Bearer <token>` get `401` with a JSON body.

Options (`rag-drg serve --help`):

* `--auth token` (default for http/sse) or `--auth none`. The server refuses to start with
  `--auth token` and no tokens, and refuses `--auth none` on a non-loopback address unless
  you also pass `--allow-unauthenticated` (don't, for licensed content).
* `--allowed-host NAME` (repeatable, or `RAG_DRG_ALLOWED_HOSTS=a,b`, or
  `auth: {allowed_hosts: [...]}` in the config): the MCP SDK's DNS-rebinding protection checks
  the `Host` header against these names (with or without a port) plus loopback; other hosts get
  `421`. Without any, a server bound to `0.0.0.0` does not check `Host` (the SDK's own behaviour);
  the token is the protection that matters, since a DNS-rebinding web page has no token.
  `--allowed-host '*'` switches the check off explicitly. List every name people use
  (short name, FQDN, IP) if you set it.
* `--ssl-certfile/--ssl-keyfile`: serve HTTPS directly.
* `RAG_DRG_AUTH`, `RAG_DRG_HOST`, `RAG_DRG_PORT` environment defaults (handy in systemd).

`deploy/rag-drg.service` is a systemd unit with these settings.

### TLS

Behind the university firewall, plain HTTP on the intranet is acceptable, but tokens (and
manual text) then travel in clear text on the internal network. If you can, terminate TLS in
front of the server, e.g. with Caddy:

```
rag.chem.example.ac.il {
    reverse_proxy 127.0.0.1:8765
}
```

(nginx: `proxy_pass http://127.0.0.1:8765;` plus `proxy_buffering off;` and a long
`proxy_read_timeout`, because MCP streams responses.) Then bind the server to `127.0.0.1`,
add `--allowed-host rag.chem.example.ac.il`, and give people `https://` URLs. With an internal
CA, clients need that CA (`NODE_EXTRA_CA_CERTS` for Claude Code, `SSL_CERT_FILE` for Python).

## Clients

**Claude Code** (`--header` sets an HTTP header on every request to the server):

```bash
export RAG_DRG_TOKEN=rdg_...        # e.g. in ~/.bashrc
claude mcp add --scope user --transport http rag-drg http://rag.chem.example.ac.il:8765/mcp \
    --header "Authorization: Bearer $RAG_DRG_TOKEN"
```

This stores the expanded token in `~/.claude.json`. To keep it out of config files, use a
project `.mcp.json`, where Claude Code expands environment variables:

```json
{"mcpServers": {"rag-drg": {"type": "http", "url": "http://rag.chem.example.ac.il:8765/mcp",
  "headers": {"Authorization": "Bearer ${RAG_DRG_TOKEN}"}}}}
```

Check with `claude mcp list` (a 401 shows as a failed connection).

**Other MCP clients**: any client that can set HTTP headers. Python SDK 2.x:
`httpx2.AsyncClient(headers={"Authorization": f"Bearer {tok}"})` passed as `http_client=` to
`streamable_http_client(url, ...)`; SDK 1.x: `streamablehttp_client(url, headers={...})`.

**REST / scripts**:

```bash
curl -H "Authorization: Bearer $RAG_DRG_TOKEN" \
  "http://rag.chem.example.ac.il:8765/api/search?q=%25maxcore&software=orca&max_tokens=600"
```

## For plugin authors

`ServerContext.current_user()` returns the token name during an HTTP request (None over stdio
or with `--auth none`). It is backed by a contextvar set by `rag_drg.auth.BearerAuthMiddleware`
(MCP SDK 2.x carries it into tool calls); on SDK 1.x it falls back to the Starlette request
scope (`scope["rag_drg.user"]`). `ctx.emit()` fills `event["user"]` from it.
