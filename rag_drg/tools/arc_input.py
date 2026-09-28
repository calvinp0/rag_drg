"""ARC input.yml: schema (generated from ARC's source) and a static checker.

    rag-drg arc schema [--arc-path P] [--out F]   # default: sources_cache/arc -> index/arc_input_schema.json
    rag-drg arc check input.yml [more.yml] [--json]  # exit 1 on errors

MCP tool: ``check_arc_input(content, filename="input.yml")``. The ESS input checker
(``rag-drg check-input`` / its PostToolUse hook / the ``check_input`` MCP tool) also routes ARC
inputs here: a ``*.yml``/``*.yaml`` whose top-level keys include ``project`` and ``species`` or
``reactions`` (see ``is_arc_input``).

Python API::

    from rag_drg.tools.arc_input import check_arc_input, load_schema, generate_schema
    findings = check_arc_input(text, "input.yml")      # list[rag_drg.tools.inputcheck.Finding]

The schema comes from, in order: an ARC clone (``sources_cache/arc``; regenerated when its commit
changes and cached in ``index/arc_input_schema.json``), else the committed snapshot
``knowledge/arc/input_schema.snapshot.yaml`` (e.g. in CI). Refresh the snapshot with
``rag-drg arc schema --out knowledge/arc/input_schema.snapshot.yaml`` after updating the clone.
Checks: docs/arc-input.md.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from ._arc.check import load_arc_yaml, run_checks
from ._arc.schema import (SNAPSHOT_REL, SchemaError, default_arc_path, default_json_path, generate_schema,
                          load_schema, read_schema_file, snapshot_path, write_schema)
from ._inputcheck.model import Finding

__all__ = ["check_arc_input", "is_arc_input", "load_schema", "generate_schema", "write_schema", "Finding"]

SEV_ORDER = {"error": 0, "warning": 1, "info": 2}
ARC_SUFFIXES = (".yml", ".yaml")


def _load_cfg(cfg):
    if cfg is not None:
        return cfg
    try:
        from ..config import load_config

        return load_config()
    except Exception:  # noqa: BLE001 - the checker works without a config (snapshot schema, no levels table)
        return None


def _server_names(cfg) -> set[str] | None:
    """Lower-case server names from servers.yaml; None when there is no (valid) servers.yaml."""
    if cfg is None:
        return None
    try:
        from .servers import load_servers, servers_path

        if not servers_path(cfg).is_file():
            return None
        return {str(n).lower() for n in load_servers(cfg)}
    except Exception:  # noqa: BLE001 - an invalid servers.yaml is reported by `rag-drg lint`, not here
        return None


def check_arc_input(content: str, filename: str = "input.yml", cfg=None, schema: dict | None = None) -> list[Finding]:
    """Check the text of an ARC input file; returns findings sorted by severity and line."""
    cfg = _load_cfg(cfg)
    if schema is None:
        try:
            schema = load_schema(cfg)
        except SchemaError as e:
            return [Finding("info", "arc-no-schema", f"ARC input not checked: {e}")]
    findings = run_checks(schema, content or "", filename, cfg=cfg, servers=_server_names(cfg))
    seen, out = set(), []
    for f in findings:
        key = (f.severity, f.code, f.message, f.line)
        if key not in seen:
            seen.add(key)
            out.append(f)
    return sorted(out, key=lambda f: (SEV_ORDER.get(f.severity, 3), f.line or 0))


_TOP_KEY = re.compile(r"^([A-Za-z_][\w]*)\s*:", re.M)


def is_arc_input(filename: str | None, content: str) -> bool:
    """A .yml/.yaml whose top-level keys include `project` and (`species` or `reactions`).
    Falls back to a line scan when the YAML does not parse (so the checker can report the error)."""
    if filename and not str(filename).lower().endswith(ARC_SUFFIXES):
        return False
    try:
        data = load_arc_yaml(content)
        keys = set(data) if isinstance(data, dict) else set()
    except Exception:  # noqa: BLE001
        keys = set(_TOP_KEY.findall(content or ""))
    return "project" in keys and bool(keys & {"species", "reactions"})


# ------------------------------------------------------------------ input-checker routing


def _route(filename: str | None, content: str):
    if is_arc_input(filename, content):
        return lambda text, name, cfg: check_arc_input(text, name or "input.yml", cfg=cfg)
    return None


def _register_router() -> None:
    from . import inputcheck

    routers = getattr(inputcheck, "INPUT_ROUTERS", None)
    if routers is not None and _route not in routers:
        routers.append(_route)


_register_router()


# ------------------------------------------------------------------ output


def format_findings(findings: list[Finding], name: str | None = None) -> str:
    from .inputcheck import format_findings as _fmt

    return _fmt(findings, name)


# ------------------------------------------------------------------ CLI


def register_cli(subparsers):
    p = subparsers.add_parser("arc", help="ARC input.yml: generate the input schema, check input files")
    asub = p.add_subparsers(dest="arc_cmd", required=True)
    q = asub.add_parser("schema", help="generate the ARC input schema from ARC's source (static; ARC is not imported)")
    q.add_argument("--arc-path", help="ARC checkout (default: the `arc` source in sources_cache/)")
    q.add_argument("--out", help="output file: .json, or .yaml for the snapshot format "
                                 f"(default: index/arc_input_schema.json; snapshot: {SNAPSHOT_REL})")
    q = asub.add_parser("check", help="check ARC input files (exit 1 on errors)")
    q.add_argument("paths", nargs="+")
    q.add_argument("--json", action="store_true")
    return {"arc": _cli}


def _cli(args, cfg) -> int:
    if args.arc_cmd == "schema":
        arc_path = Path(args.arc_path) if args.arc_path else default_arc_path(cfg)
        if arc_path is None or not arc_path.is_dir():
            print(f"No ARC checkout at {arc_path}; run `rag-drg fetch --source arc` or pass --arc-path.",
                  file=sys.stderr)
            return 2
        try:
            schema = generate_schema(arc_path)
        except SchemaError as e:
            print(f"arc schema: {e}", file=sys.stderr)
            return 2
        out = Path(args.out) if args.out else default_json_path(cfg)
        write_schema(schema, out)
        n = {k: len((schema.get(k) or {}).get("params") or {}) for k in ("arc", "species", "reaction", "level")}
        print(f"Wrote {out} from ARC {schema.get('arc_commit') or '(unknown commit)'}: {n['arc']} ARC arguments, "
              f"{n['species']} ARCSpecies / {n['reaction']} ARCReaction / {n['level']} Level arguments, "
              f"{len(schema['job_types']['keys'])} job types.")
        return 0
    worst = 0
    report = {}
    for path in args.paths:
        p = Path(path)
        if not p.is_file():
            print(f"{path}: not found", file=sys.stderr)
            worst = 2
            continue
        findings = check_arc_input(p.read_text(errors="replace"), p.name, cfg=cfg)
        report[str(p)] = [f.to_dict() for f in findings]
        if not args.json:
            print(format_findings(findings, str(p)))
        if any(f.severity == "error" for f in findings):
            worst = max(worst, 1)
    if args.json:
        print(json.dumps(report, indent=2))
    return worst


# ------------------------------------------------------------------ MCP


_check_arc_input = check_arc_input


def register_mcp(mcp, ctx) -> None:
    _check = _check_arc_input

    @mcp.tool()
    def check_arc_input(content: str, filename: str = "input.yml") -> str:  # noqa: F811
        """Check an ARC input.yml before running ARC.

        Validates against ARC's own source (generated schema): unknown top-level / species / reaction /
        level-dict keys (with did-you-mean), value types, `project`, species labels + structures, reactions
        referencing defined species, job_types keys and legacy aliases, specific_job_type pitfalls,
        level strings/dicts (and whether the ESS ARC routes a method to supports it), ess_settings ESS and
        server names, ts_adapters, and charge/multiplicity parity for SMILES species.

        Args:
            content: The full text of the ARC input file (YAML).
            filename: Its file name (only used in messages).
        """
        findings = _check(content, filename or "input.yml", cfg=ctx.cfg)
        ctx.emit({"tool": "check_arc_input", "args": {"filename": filename},
                  "n_results": len(findings), "results": [{"code": f.code, "severity": f.severity} for f in findings]})
        return format_findings(findings, filename)


# ------------------------------------------------------------------ lint


def lint(cfg) -> list[str]:
    snap = snapshot_path(cfg)
    if not snap.is_file():
        return [f"{SNAPSHOT_REL}: missing (run `rag-drg arc schema --out {SNAPSHOT_REL}`)"]
    data = read_schema_file(snap)
    if data is None:
        return [f"{SNAPSHOT_REL}: not a valid ARC input schema (regenerate with `rag-drg arc schema --out ...`)"]
    return []

