# rag-drg repository notes for agents

- Python package in `rag_drg/`; config in `rag_drg.yaml`; curated content in `knowledge/`.
- Setup: `python -m venv .venv && .venv/bin/pip install -e '.[mcp,pdf,dev]'`.
- Before committing: `.venv/bin/rag-drg lint` and `.venv/bin/python -m pytest -q`.
- Knowledge cards must have front matter (`title`, `domain`, `doc_type`, `status`, and
  `software` for ESS cards). Only write facts you can back with the manual or a calculation;
  new cards start as `status: draft`. Never mark a card `verified` yourself; that's for a human reviewer.
- `index/`, `sources_cache/`, and PDFs under `sources/` and `papers/` are git-ignored; never commit them.
- The MCP server supports both MCP Python SDK 1.x (`FastMCP`) and 2.x (`MCPServer`); keep both paths working.
