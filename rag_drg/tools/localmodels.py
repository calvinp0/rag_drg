"""`rag-drg tools-schema`: function/tool specs for local-model frameworks (see
integrations/local-models.md).

Each tool maps 1:1 to a REST endpoint (``GET {base_url}/api/<endpoint>?<arguments>``), so a
dispatcher only needs the tool name -> endpoint table in ``--format json``. The argument
names are the query parameters.
"""

from __future__ import annotations

import json

SOFTWARE = "orca, gaussian, qchem, psi4, molpro, pyscf, arc, slurm, pbs"

TOOLS = [
    {
        "name": "search_knowledge",
        "endpoint": "search",
        "description": (
            "Search the research group's knowledge base (ESS manuals and curated notes for ORCA, "
            "Gaussian, Q-Chem, Psi4, Molpro, PySCF; the ARC code; HPC cluster usage; project notes). "
            "Call it BEFORE writing an ESS input file, ARC input or a submit script. Results tagged "
            "lesson/gotcha are corrections the group has made: follow them."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Question or exact keywords, e.g. '%maxcore' or 'ORCA TS optimisation'."},
                "software": {"type": "string", "description": f"Restrict to one program: {SOFTWARE}."},
                "version": {"type": "string", "description": "Program version, e.g. '6' (ORCA), '16' (Gaussian), '6.1' (Q-Chem)."},
                "domain": {"type": "string", "enum": ["ess", "arc", "hpc", "project", "literature", "lessons"]},
                "doc_type": {"type": "string", "enum": ["lesson", "gotcha", "card", "template", "schema",
                                                        "reference", "theory", "code", "paper"],
                             "description": "reference = keywords/syntax, theory = method background."},
                "k": {"type": "integer", "description": "Number of results (default 6, max 20)."},
                "max_tokens": {"type": "integer", "description": "Answer budget in tokens for a compact result (e.g. 800)."},
            },
            "required": ["query"],
        },
        "cli": ["rag-drg", "search", "{query}", "--software", "{software}", "--version", "{version}",
                "--domain", "{domain}", "--doc-type", "{doc_type}", "--k", "{k}", "--json"],
    },
    {
        "name": "lookup_level_of_theory",
        "endpoint": "level",
        "description": (
            "Which electronic-structure codes support a method/functional/basis/dispersion/solvation "
            "model and how each code writes it. Use before translating a level of theory between codes."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "e.g. 'wB97X-D/def2-TZVP' or 'DLPNO-CCSD(T)'."},
                "software": {"type": "string", "description": "Only this code's row: gaussian, orca, qchem, psi4, molpro, pyscf."},
            },
            "required": ["name"],
        },
        "cli": ["rag-drg", "level", "{name}", "--software", "{software}"],
    },
    {
        "name": "get_context",
        "endpoint": "context",
        "description": "Full text of a search result (by chunk_id) plus neighbouring chunks of the same document.",
        "parameters": {
            "type": "object",
            "properties": {
                "chunk_id": {"type": "integer", "description": "chunk_id from a search result."},
                "neighbors": {"type": "integer", "description": "Chunks before/after to include (default 1)."},
            },
            "required": ["chunk_id"],
        },
        "cli": None,
    },
]


def tools_schema(fmt: str = "openai", base_url: str = "http://localhost:8765") -> object:
    if fmt in ("openai", "ollama"):
        # Ollama's /api/chat `tools` and llama.cpp server's OpenAI endpoint use the same shape.
        return [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                                  "parameters": t["parameters"]}} for t in TOOLS]
    base = base_url.rstrip("/")
    return {
        "base_url": base,
        "auth_header": "Authorization: Bearer $RAG_DRG_TOKEN",
        "health": f"{base}/api/health",
        "tools": [
            {"name": t["name"], "description": t["description"], "parameters": t["parameters"],
             "http": {"method": "GET", "url": f"{base}/api/{t['endpoint']}",
                      "arguments": "query string (argument names as given)"},
             "cli": t["cli"]}
            for t in TOOLS
        ],
    }


def register_cli(sub):
    p = sub.add_parser("tools-schema", help="print tool/function specs for local-model frameworks (Ollama, llama.cpp, OpenAI-style)")
    p.add_argument("--format", choices=["openai", "ollama", "json"], default="openai",
                   help="openai/ollama: tools array for chat APIs; json: also REST endpoints and CLI equivalents")
    p.add_argument("--base-url", default="http://localhost:8765", help="server URL used in --format json")
    return {"tools-schema": _cmd}


def _cmd(args, cfg) -> int:
    print(json.dumps(tools_schema(args.format, args.base_url), indent=2))
    return 0
