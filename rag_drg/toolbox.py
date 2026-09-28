"""Tool profiles: how many tool definitions a client has to carry in its context.

* ``full`` (default): every tool is a normal MCP tool. Right for Claude Code, which defers MCP
  tool schemas and loads them on demand anyway.
* ``minimal``: only ``search_knowledge``, ``find_tool`` and ``run_tool`` are MCP tools; every
  other tool (core and plugin) stays reachable through ``find_tool(task)`` -> ``run_tool(name,
  args)``. For clients that load all schemas up front (local models with small contexts, most
  VS Code agents): a few hundred tokens instead of several thousand.

Every ``@mcp.tool()`` registration (core tools and plugins' ``register_mcp``) goes through
:class:`ToolRegistrar`, which records the function and decides whether to expose it.
"""

from __future__ import annotations

import inspect
import re
from dataclasses import dataclass, field
from typing import Any, Callable

PROFILES = ("full", "minimal")
MINIMAL_EXPOSED = {"search_knowledge"}

# Words a task description uses for each tool, beyond what its name and docstring say.
TASK_WORDS: dict[str, list[str]] = {
    "compose_ess_job": ["write", "generate", "create", "make", "compose", "prepare", "input", "single point",
                        "sp", "optimization", "optimisation", "frequency", "job"],
    "check_input": ["check", "validate", "verify", "lint", "input", "gjf", "inp", "submit", "script", "before submitting"],
    "check_arc_input": ["arc", "input.yml", "check", "validate", "yaml"],
    "compose_arc_run": ["arc", "run", "launch", "input.yml", "settings"],
    "diagnose_output": ["failed", "crash", "error", "log", "output", "diagnose", "why", "died", "termination"],
    "check_basis": ["basis", "element", "ecp", "coverage", "auxiliary"],
    "lookup_level_of_theory": ["functional", "method", "level", "theory", "supported", "spelling", "translate", "which code"],
    "render_submit_script": ["submit", "script", "pbs", "slurm", "qsub", "sbatch", "job script"],
    "check_resources": ["memory", "cores", "walltime", "limit", "fit", "queue", "partition"],
    "queue_access": ["queue", "access", "allowed", "permission", "partition"],
    "list_servers": ["cluster", "clusters", "servers", "which"],
    "server_info": ["cluster", "server", "paths", "installed", "partitions"],
    "record_lesson": ["corrected", "correction", "mistake", "lesson", "remember", "wrong"],
    "get_context": ["context", "more", "surrounding", "chunk"],
    "read_document": ["read", "document", "card", "template", "whole", "full"],
    "list_documents": ["list", "browse", "documents", "cards", "templates"],
    "list_knowledge_sources": ["sources", "index", "stats", "contents"],
}


# Strong signals: names of basis sets, methods, programs' error text... (+4 each).
TASK_PATTERNS: dict[str, re.Pattern] = {
    "check_basis": re.compile(r"\bbasis\b|\bdef2|\bcc-p|\baug-cc|\b6-31|\bsto-3g|\bma-def2|\becp\b|\bcover", re.I),
    "lookup_level_of_theory": re.compile(
        r"\b(b3lyp|wb97\w*|ωb97\w*|pbe0|m06\w*|r2scan\w*|b2plyp|dsd-\w+|dlpno\w*|ccsd\w*|mp2|casscf|nevpt2|"
        r"caspt2|mrci|cbs-qb3|g4|functional|dispersion|d3bj|d4|smd|pcm)\b|available in|supported (in|by)", re.I),
    "diagnose_output": re.compile(r"\b(fail\w*|crash\w*|died|killed|error termination|l\d{3,4}(\.exe)?|"
                                  r"not converg\w*|segfault|terminated abnormally)\b", re.I),
    "check_arc_input": re.compile(r"\binput\.yml\b|\barc\b.*\binput\b", re.I),
    "compose_arc_run": re.compile(r"\b(run|launch|submit)\b.*\barc\b|\barc\b.*\b(run|launch|submit)\b", re.I),
    "queue_access": re.compile(r"\b(can i use|allowed|access to)\b.*\b(queue|partition)|"
                               r"\b(queue|partition)\b.*\b(access|allowed|can i use|may i use)", re.I),
    "record_lesson": re.compile(r"\b(corrected|correction|lesson|remember (this|that)|was wrong|got it wrong)\b", re.I),
}


# Tools that only make sense when the task mentions their subject (else they are skipped).
TASK_REQUIRES: dict[str, re.Pattern] = {
    "compose_arc_run": re.compile(r"\barc\b|input\.yml", re.I),
    "check_arc_input": re.compile(r"\barc\b|input\.yml", re.I),
}


@dataclass
class ToolSpec:
    name: str
    fn: Callable[..., Any]
    description: str
    exposed: bool = False

    @property
    def summary(self) -> str:
        return (self.description.strip().split("\n\n")[0].replace("\n", " ").strip()) or self.name

    def params(self) -> list[dict]:
        """Parameters from the signature, with descriptions from the docstring's Args section."""
        docs = _arg_docs(self.description)
        out = []
        for p in inspect.signature(self.fn).parameters.values():
            if p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD):
                continue
            ann = p.annotation
            typ = ann if isinstance(ann, str) else getattr(ann, "__name__", str(ann)).replace("typing.", "")
            if typ == "_empty":
                typ = "any"
            out.append({"name": p.name, "type": str(typ).replace(" | None", "?"),
                        "required": p.default is p.empty,
                        "default": None if p.default is p.empty else p.default,
                        "doc": docs.get(p.name, "")})
        return out

    def usage(self) -> str:
        lines = [f"{self.name}: {self.summary}"]
        for p in self.params():
            req = "required" if p["required"] else f"default {p['default']!r}"
            doc = f" - {p['doc']}" if p["doc"] else ""
            lines.append(f"  {p['name']} ({p['type']}, {req}){doc}")
        return "\n".join(lines)


def _arg_docs(doc: str) -> dict[str, str]:
    out: dict[str, str] = {}
    m = re.search(r"\n\s*Args:\s*\n(.*?)(\n\s*\n\s*[A-Z][a-z]+:|\Z)", doc or "", re.S)
    if not m:
        return out
    cur = None
    for line in m.group(1).splitlines():
        am = re.match(r"\s{2,}(\w+)(?:\s*\([^)]*\))?:\s*(.*)", line)
        if am and (cur is None or len(line) - len(line.lstrip()) <= _indent(m.group(1))):
            cur = am.group(1)
            out[cur] = am.group(2).strip()
        elif cur and line.strip():
            out[cur] += " " + line.strip()
    return {k: v[:200] for k, v in out.items()}


def _indent(block: str) -> int:
    for line in block.splitlines():
        if line.strip():
            return len(line) - len(line.lstrip())
    return 0


class ToolRegistrar:
    """Stands in for the MCP server object during tool registration."""

    def __init__(self, mcp, profile: str = "full"):
        if profile not in PROFILES:
            raise ValueError(f"unknown profile {profile!r}; use one of {PROFILES}")
        self._mcp = mcp
        self.profile = profile
        self.specs: dict[str, ToolSpec] = {}

    def tool(self, name: str | None = None, description: str | None = None, **kwargs):
        def deco(fn):
            tool_name = name or fn.__name__
            exposed = self.profile == "full" or tool_name in MINIMAL_EXPOSED
            self.specs[tool_name] = ToolSpec(tool_name, fn, description or inspect.getdoc(fn) or "", exposed)
            if exposed:
                reg_kwargs = dict(kwargs)
                if name:
                    reg_kwargs["name"] = name
                if description:
                    reg_kwargs["description"] = description
                self._mcp.tool(**reg_kwargs)(fn)
            return fn

        return deco

    def __getattr__(self, item):  # everything else (settings, run, ...) goes to the real server
        return getattr(self._mcp, item)

    # ------------------------------------------------------------------ discovery
    def find(self, task: str, k: int = 3) -> list[tuple[float, ToolSpec]]:
        words = set(re.findall(r"[a-z0-9_.]+", task.lower()))
        text = task.lower()
        scored = []
        for spec in self.specs.values():
            if spec.name in ("find_tool", "run_tool"):
                continue
            need = TASK_REQUIRES.get(spec.name)
            if need is not None and not need.search(task):
                continue
            hay = set(re.findall(r"[a-z0-9_.]+", f"{spec.name.replace('_', ' ')} {spec.summary}".lower()))
            score = len(words & hay) * 1.0
            for w in TASK_WORDS.get(spec.name, []):
                if (" " in w and w in text) or w in words:
                    score += 1.5
            if spec.name.replace("_", " ") in text or spec.name in text:
                score += 5
            pat = TASK_PATTERNS.get(spec.name)
            if pat is not None and pat.search(task):
                score += 4
            if score > 0:
                scored.append((score, spec))
        scored.sort(key=lambda t: (-t[0], t[1].name))
        return scored[:k]

    def run(self, name: str, args: dict | None) -> Any:
        spec = self.specs.get(name)
        if spec is None or name in ("find_tool", "run_tool"):
            import difflib

            close = difflib.get_close_matches(name, [n for n in self.specs if n not in ("find_tool", "run_tool")], n=3)
            return f"No tool named {name!r}." + (f" Did you mean: {', '.join(close)}?" if close else
                                                  " Use find_tool(task) to look one up.")
        args = dict(args or {})
        try:
            inspect.signature(spec.fn).bind(**args)
        except TypeError as e:
            return f"Bad arguments for {name}: {e}\n\n{spec.usage()}"
        return spec.fn(**args)


MINIMAL_INSTRUCTIONS = """\
Research-group knowledge base for quantum-chemistry software (ORCA, Gaussian, Q-Chem, Psi4,
Molpro, PySCF), ARC, and HPC clusters. Search it before writing ESS/ARC inputs or submit
scripts. For actions (check or compose an input, diagnose a failed job, check a basis, submit
scripts, cluster limits, record a correction): find_tool(task) -> run_tool(name, args).
Follow results tagged lesson/gotcha.
"""

MINIMAL_SEARCH_DESCRIPTION = """Search the group knowledge base (keywords work best, e.g. "ORCA %maxcore").

Args:
    query: question or exact keywords.
    software: orca | gaussian | qchem | psi4 | molpro | pyscf | arc | pbs | slurm.
    version: e.g. "6", "16", "6.1".
    max_tokens: answer budget for small context windows (e.g. 800).
"""


def register_discovery_tools(registrar: ToolRegistrar, ctx) -> None:
    """find_tool / run_tool: always recorded; exposed as MCP tools in the minimal profile."""

    mcp = registrar._mcp

    def find_tool(task: str, k: int = 2) -> str:
        """Find the right tool for a task and show how to call it.

        Args:
            task: what you want to do, e.g. "compose an ORCA single point input" or "why did my Gaussian job fail".
            k: how many tools to return (default 2).
        """
        hits = registrar.find(task, k=max(1, min(int(k), 5)))
        ctx.emit({"tool": "find_tool", "args": {"task": task}, "n_results": len(hits),
                  "results": [{"title": s.name} for _, s in hits]})
        if not hits:
            return "No matching tool. Use search_knowledge for questions; tools: " + ", ".join(
                n for n in sorted(registrar.specs) if n not in ("find_tool", "run_tool"))
        return "\n\n".join(s.usage() for _, s in hits) + "\n\nCall with run_tool(name, args={...})."

    def run_tool(name: str, args: dict | None = None) -> str:
        """Run a tool found with find_tool.

        Args:
            name: tool name, e.g. "check_input".
            args: its arguments as an object, e.g. {"content": "...", "filename": "job.inp"}.
        """
        out = registrar.run(name, args)
        return out if isinstance(out, str) else str(out)

    for fn in (find_tool, run_tool):
        registrar.specs[fn.__name__] = ToolSpec(fn.__name__, fn, inspect.getdoc(fn) or "", registrar.profile == "minimal")
        if registrar.profile == "minimal":
            mcp.tool()(fn)


# ---------------------------------------------------------------------- search hints
HINT_RULES: list[tuple[re.Pattern, str, str]] = [
    (re.compile(r"\b(write|writing|generate|create|make|compose|prepare)\b.*\b(input|job|single point|sp|opt)", re.I),
     "compose_ess_job", "to generate a checked input + submit script"),
    (re.compile(r"\b(check|validate|verify|correct|wrong with)\b.*\b(input|gjf|inp|com|script)", re.I),
     "check_input", "to check an input file (+ submit script)"),
    (re.compile(r"\b(fail|failed|crash|crashed|died|error termination|l\d{3,4}\.exe|not converg)", re.I),
     "diagnose_output", "with the head and tail of the output file"),
    (re.compile(r"\barc\b.*\b(input\.yml|input file|yaml)\b|\binput\.yml\b", re.I),
     "check_arc_input", "to validate an ARC input.yml"),
    (re.compile(r"\b(submit script|qsub|sbatch|pbs script|slurm script|job script)\b", re.I),
     "render_submit_script", "for a ready-to-run script with the cluster's real paths and limits"),
    (re.compile(r"\bbasis\b.*\b(element|atom|ecp|cover|iodine|heavy)", re.I),
     "check_basis", "to check basis coverage for your elements"),
]


def tool_hints(query: str, available: set[str]) -> list[str]:
    hints = []
    for pattern, tool, why in HINT_RULES:
        if tool in available and pattern.search(query) and tool not in [h.split("`")[1] for h in hints]:
            hints.append(f"`{tool}` {why}")
    return hints[:2]
