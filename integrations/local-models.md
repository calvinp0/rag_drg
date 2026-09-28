# Using rag-drg with local models

## MCP-capable clients

Most agent front-ends for local models support MCP (stdio or streamable HTTP). Point them at:

* **stdio**: command `rag-drg`, args `["serve"]`, env `RAG_DRG_CONFIG=/path/to/rag_drg/rag_drg.yaml`
* **HTTP** (shared group server): `http://<host>:8765/mcp`

Then copy the instructions from `integrations/claude-code/CLAUDE.md.snippet` into the
client's system prompt / rules file. Smaller models follow "call `search_knowledge` before
writing an input file" much more reliably when it is in the system prompt.

## No MCP: use the CLI as a tool

Expose this as a function/tool in your agent framework:

```bash
rag-drg search "ORCA NEB-TS input" --software orca --version 6 --k 5 --json
```

It prints a JSON list of hits (`title`, `text`, `source`, `path`, `url`, `software`,
`version`, `doc_type`, `status`, `score`). Record corrections with:

```bash
rag-drg lesson --title "..." --mistake "..." --correction "..." --domain ess --software orca --version 6
```

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
