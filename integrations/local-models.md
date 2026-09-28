# Using rag-drg with local models

## MCP-capable clients

Most agent front-ends for local models support MCP (stdio or streamable HTTP). Point them at:

* **stdio**: command `rag-drg`, args `["serve"]`, env `RAG_DRG_CONFIG=/path/to/rag_drg/rag_drg.yaml`
* **HTTP** (shared group server): `http://<host>:8765/mcp` with the header
  `Authorization: Bearer <your token>` (ask the admin for `rag-drg tokens add <you>`; see
  [`docs/auth.md`](../docs/auth.md))

Then copy the instructions from `integrations/claude-code/CLAUDE.md.snippet` into the
client's system prompt / rules file. Smaller models follow "call `search_knowledge` before
writing an input file" much more reliably when it is in the system prompt.

## Small context windows: `max_tokens`

`search_knowledge(query, ..., max_tokens=800)` (MCP) or `&max_tokens=800` (REST) returns a
compact answer of roughly that many tokens (estimated at 4 characters per token):

* lessons, gotchas and curated cards first, then manual chunks in rank order;
* each result cut to the lines that mention the query terms, plus the code/input block that
  follows such a line (gaps shown as `…`), instead of blind truncation;
* every result keeps its citation (`src: source:path chunk_id=N`); fetch the full text with
  `get_context(chunk_id)` when needed.

Without `max_tokens` the output is unchanged. 400-1000 is a good range for 8k-context models.

## No MCP: REST API on the shared server

The HTTP server (`rag-drg serve --transport http`) also answers plain JSON under `/api`, in
the same process and with the same token:

| Endpoint | Parameters |
|---|---|
| `GET /api/search` | `q` (or `query`), `software`, `version`, `domain`, `doc_type`, `k` (max 20), `max_tokens`, `format=text` |
| `GET /api/level` | `name`, `software` |
| `GET /api/context` | `chunk_id`, `neighbors` |
| `GET /api/health` | none (no token needed) |

```bash
curl -s -H "Authorization: Bearer $RAG_DRG_TOKEN" \
  "http://<host>:8765/api/search?q=NEB-TS&software=orca&version=6&max_tokens=600" | jq -r .text
```

`/api/search` returns `{"query", "results": [{id, title, text, source, path, url, software,
version, doc_type, status, score, ...}]}`; with `max_tokens` the results are the compact
selection (trimmed `text`) and a top-level `text` holds a ready-to-paste rendering.
`format=text` returns just that text (`text/plain`).

On your own machine, `rag-drg serve --transport http --auth none` (it binds 127.0.0.1 by
default) gives the same API without tokens.

### Tool specs for function calling: `rag-drg tools-schema`

```bash
rag-drg tools-schema                      # OpenAI-style "tools" array (llama.cpp server, vLLM, LM Studio)
rag-drg tools-schema --format ollama      # the same shape, for Ollama's /api/chat "tools"
rag-drg tools-schema --format json --base-url http://<host>:8765   # adds REST URL + CLI command per tool
```

Tools: `search_knowledge`, `lookup_level_of_theory`, `get_context`. Each maps to
`GET /api/<search|level|context>` with the arguments as query parameters (the `json` format
lists the URLs), so a dispatcher is a few lines. Ollama example:

```python
import json, os, subprocess, requests

BASE = "http://<host>:8765"
HDR = {"Authorization": f"Bearer {os.environ['RAG_DRG_TOKEN']}"}
TOOLS = json.loads(subprocess.check_output(["rag-drg", "tools-schema", "--format", "ollama"]))
ENDPOINT = {"search_knowledge": "search", "lookup_level_of_theory": "level", "get_context": "context"}

def call_tool(name, args):
    if name == "search_knowledge":
        args.setdefault("max_tokens", 800)          # keep answers small for local models
    r = requests.get(f"{BASE}/api/{ENDPOINT[name]}", params=args, headers=HDR, timeout=60)
    r.raise_for_status()
    d = r.json()
    return d.get("text") or json.dumps(d)[:4000]

messages = [{"role": "user", "content": "Write an ORCA 6 NEB-TS input for ..."}]
while True:
    resp = requests.post("http://localhost:11434/api/chat", json={
        "model": "qwen3:14b", "messages": messages, "tools": TOOLS, "stream": False}).json()
    msg = resp["message"]
    messages.append(msg)
    if not msg.get("tool_calls"):
        print(msg["content"])
        break
    for tc in msg["tool_calls"]:
        f = tc["function"]
        messages.append({"role": "tool", "tool_name": f["name"], "content": call_tool(f["name"], f["arguments"])})
```

**llama.cpp**: start `llama-server --jinja -m model.gguf` (tool calling needs `--jinja`), send
the same `tools` array to `POST /v1/chat/completions`; tool calls arrive in
`choices[0].message.tool_calls` with `function.arguments` as a JSON string (`json.loads` it),
and results go back as `{"role": "tool", "tool_call_id": ..., "content": ...}`.

**Open WebUI**: recent versions can connect to the MCP endpoint directly (streamable HTTP with a
bearer-token header); otherwise add a Python "Tool" whose method does the `requests.get` above.

## No server: use the CLI as a tool

Expose this as a function/tool in your agent framework:

```bash
rag-drg search "ORCA NEB-TS input" --software orca --version 6 --k 5 --json
```

It prints a JSON list of hits (`title`, `text`, `source`, `path`, `url`, `software`,
`version`, `doc_type`, `status`, `score`). Record corrections with:

```bash
rag-drg lesson --title "..." --mistake "..." --correction "..." --domain ess --software orca --version 6
```

(`tools-schema --format json` lists the CLI command for each tool that has one. The CLI
`search` has no `max_tokens`; use the REST API or MCP for compact answers.)

## Embeddings from a local server

To add semantic search with a local embedding model (e.g. Ollama `nomic-embed-text`), set in
`rag_drg.yaml`:

```yaml
embeddings:
  provider: openai
  base_url: http://localhost:11434/v1
  model: nomic-embed-text
```

and run `rag-drg ingest` (only new/changed chunks are embedded on later runs).

## Optional reranker

A cross-encoder can re-order the best fused candidates (better precision on paraphrased
questions; costs roughly 50-200 ms per query on a CPU). Off by default; enable it in
`rag_drg.yaml` (or a `conf.d/*.yaml`):

```yaml
rerank:
  model: cross-encoder/ms-marco-MiniLM-L-6-v2
  top_n: 30          # how many fused candidates to rerank
```

It needs `pip install 'rag-drg[st]'` (sentence-transformers) and applies to the MCP
`search_knowledge` tool and `/api/search` (not to the `rag-drg search` CLI). The reranker score
sets the order, with the curated/status boosts kept as a small prior, so lessons and cards
still win near-ties. If the package or model is unavailable, the server logs a warning and
searches without it. In code: `Searcher.search(..., rerank=fn)` with `fn(query, texts) -> scores`.
