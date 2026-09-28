"""Static checks for ESS inputs (Gaussian, ORCA, Q-Chem, Molpro, Psi4, PySCF) and submit scripts.

    rag-drg check-input job.inp [more files] [--submit run.sh] [--json]
    rag-drg check-input --hook        # Claude Code PostToolUse hook (reads the hook JSON on stdin)

MCP tool: ``check_input(content, filename, submit_script_content=None)`` (content mode: the
shared server cannot read users' files).

Python API::

    from rag_drg.tools.inputcheck import check_input, Finding, ParsedInput, ParsedSubmit, EXTRA_CHECKS
    findings = check_input(path="job.inp")                       # file mode (finds run.sh next to it)
    findings = check_input(content=text, filename="job.inp", submit_content=script_text)

Findings are high precision: "error" only for things that fail or silently give wrong results,
"warning" for very likely mistakes, "info" when the checker is not sure. The rules come from
the curated cards in knowledge/ess/ and knowledge/hpc/templates/ (each finding's `ref`).

Extension point: other plugins append callables to ``EXTRA_CHECKS``::

    def my_check(inp: ParsedInput, sub: ParsedSubmit | None, cfg: Config | None) -> list[Finding]: ...
    EXTRA_CHECKS.append(my_check)

`inp.program` is None when only a submit script is checked. Exceptions raised by extra checks
are reported as "info" findings and never break the checker.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Callable

from ._inputcheck.common import (check_basis_coverage, check_distances, check_levels, check_parity,
                                 check_unknown_elements)
from ._inputcheck.model import Atom, Executable, Finding, ParsedInput, ParsedSubmit
from ._inputcheck.programs import ANALYZERS, detect_program, detect_submit
from ._inputcheck.submit import (SUBMIT_EXTS, check_submit_alone, cross_check, find_submit_script, parse_submit,
                                 referenced_inputs)

__all__ = ["Finding", "ParsedInput", "ParsedSubmit", "Atom", "Executable", "EXTRA_CHECKS", "check_input",
           "parse_input", "parse_submit", "detect_program", "format_findings", "hook_main"]

EXTRA_CHECKS: list[Callable[[ParsedInput, "ParsedSubmit | None", object], list[Finding]]] = []

SEV_ORDER = {"error": 0, "warning": 1, "info": 2}


def parse_input(content: str, filename: str | None = None, path: Path | None = None,
                program: str | None = None) -> tuple[ParsedInput, list[Finding]]:
    """Detect the program and parse `content`; returns (ParsedInput, structural findings)."""
    prog = program or detect_program(filename, content)
    inp = ParsedInput(program=prog, filename=filename, path=path, content=content, lines=content.splitlines())
    if prog is None:
        return inp, []
    try:
        findings = ANALYZERS[prog](inp)
    except Exception as e:  # noqa: BLE001 - a parser bug must not hide everything else
        findings = [Finding("info", "parser-error", f"The {prog} parser failed ({type(e).__name__}: {e}); "
                            "input not fully checked.")]
    return inp, findings


def _load_cfg(cfg):
    if cfg is not None:
        return cfg
    try:
        from ..config import load_config

        return load_config()
    except Exception:  # noqa: BLE001 - level checks are skipped without a config
        return None


def _extra(inp: ParsedInput, sub: ParsedSubmit | None, cfg) -> list[Finding]:
    out = []
    for fn in list(EXTRA_CHECKS):
        try:
            out += list(fn(inp, sub, cfg) or [])
        except Exception as e:  # noqa: BLE001
            out.append(Finding("info", "extra-check-failed", f"{getattr(fn, '__name__', fn)} failed: {e}"))
    return out


def _finish(findings: list[Finding]) -> list[Finding]:
    seen, out = set(), []
    for x in findings:
        key = (x.severity, x.code, x.message, x.line, x.file)
        if key not in seen:
            seen.add(key)
            out.append(x)
    return sorted(out, key=lambda x: (SEV_ORDER.get(x.severity, 3), x.file or "", x.line or 0))


def check_input(path: str | Path | None = None, content: str | None = None, filename: str | None = None,
                submit_script: str | Path | None = None, submit_content: str | None = None,
                cfg=None, program: str | None = None) -> list[Finding]:
    """Check an ESS input (or a submit script) and return findings.

    File mode: `path` (a submit script next to it that references the input is used automatically).
    Content mode: `content` + `filename` (+ `submit_content`). If the file itself is a submit
    script, it is checked on its own, and together with the inputs it references that exist on disk.
    """
    p = Path(path) if path else None
    if content is None:
        if p is None:
            raise ValueError("check_input needs `path` or `content`")
        content = p.read_text(errors="replace")
    filename = filename or (p.name if p else None)
    cfg = _load_cfg(cfg)
    prog = program or detect_program(filename, content)

    if prog is None and detect_submit(filename, content):
        return _check_submit_file(content, filename, p, cfg)
    if prog is None:
        return [Finding("info", "unknown-program", f"Could not tell which program {filename or 'this input'} is for; "
                        "not checked.")]

    inp, findings = parse_input(content, filename, p, prog)
    findings += check_unknown_elements(inp)
    if not inp.extra.get("parity_done"):
        findings += check_parity(inp)
    findings += check_distances(inp)
    findings += check_basis_coverage(inp)
    findings += check_levels(inp, cfg)

    sub = None
    if submit_content is None and submit_script:
        sp = Path(submit_script)
        submit_content = sp.read_text(errors="replace")
        sub = parse_submit(submit_content, sp.name, sp)
    elif submit_content is not None:
        name = Path(submit_script).name if submit_script else "submit script"
        sub = parse_submit(submit_content, name, Path(submit_script) if submit_script else None)
    elif p is not None:
        found = find_submit_script(p)
        if found:
            sub = parse_submit(found.read_text(errors="replace"), found.name, found)
            findings.append(Finding("info", "submit-found", f"Cross-checked with submit script {found.name}.", file=found.name))
    if sub is not None:
        findings += check_submit_alone(sub)
        findings += cross_check(inp, sub)
    findings += _extra(inp, sub, cfg)
    return _finish(findings)


def _check_submit_file(content: str, filename: str | None, p: Path | None, cfg) -> list[Finding]:
    sub = parse_submit(content, filename, p)
    findings = check_submit_alone(sub)
    checked = False
    if p is not None:
        for name in referenced_inputs(sub):
            ip = p.parent / name
            if ip.is_file() and ip.suffix.lower() not in SUBMIT_EXTS:
                text = ip.read_text(errors="replace")
                prog = detect_program(ip.name, text)
                if prog is None:
                    continue
                inp, _ = parse_input(text, ip.name, ip, prog)
                for x in cross_check(inp, sub):
                    x.file = x.file or ip.name
                    x.message = f"[{ip.name}] {x.message}"
                    findings.append(x)
                findings += _extra(inp, sub, cfg)
                checked = True
    if not checked:
        findings += _extra(ParsedInput(program=None), sub, cfg)
    for x in findings:
        x.file = x.file or filename
    return _finish(findings)


# ------------------------------------------------------------------ output


def format_findings(findings: list[Finding], name: str | None = None) -> str:
    head = f"{name}: " if name else ""
    if not findings:
        return f"{head}no problems found."
    n = {s: sum(1 for f in findings if f.severity == s) for s in SEV_ORDER}
    lines = [f"{head}{n['error']} error(s), {n['warning']} warning(s), {n['info']} note(s)"]
    lines += [f.format() for f in findings]
    return "\n".join(lines)


def is_checkable(path: Path, content: str | None = None) -> bool:
    """Is this an ESS input or a submit script (the hook ignores everything else)?"""
    try:
        if content is None:
            if not path.is_file() or path.stat().st_size > 5_000_000:
                return False
            content = path.read_text(errors="replace")
    except OSError:
        return False
    return detect_program(path.name, content) is not None or detect_submit(path.name, content)


def hook_main(stdin_text: str, cfg=None, err=None) -> int:
    """Claude Code PostToolUse hook: exit 2 + stderr summary on errors (Claude sees it and fixes
    the file), exit 0 otherwise (warnings printed to stderr). Non-ESS files: silent exit 0."""
    err = err or sys.stderr
    try:
        data = json.loads(stdin_text or "{}")
    except json.JSONDecodeError:
        return 0
    ti = data.get("tool_input") or {}
    fp = ti.get("file_path") or ti.get("path") or (data.get("tool_response") or {}).get("filePath")
    if not fp:
        return 0
    p = Path(fp)
    if not p.is_absolute() and data.get("cwd"):
        p = Path(data["cwd"]) / p
    if not is_checkable(p):
        return 0
    try:
        findings = check_input(path=p, cfg=cfg)
    except Exception as e:  # noqa: BLE001 - never block the agent because the checker broke
        print(f"rag-drg check-input: internal error on {p.name}: {e}", file=err)
        return 0
    errors = [f for f in findings if f.severity == "error"]
    warnings = [f for f in findings if f.severity == "warning"]
    if errors:
        print(f"rag-drg check-input found {len(errors)} error(s) in {p.name}; fix them:", file=err)
        for f in errors + warnings:
            print(f.format(), file=err)
        return 2
    if warnings:
        print(f"rag-drg check-input: {len(warnings)} warning(s) in {p.name}:", file=err)
        for f in warnings:
            print(f.format(), file=err)
    return 0


# ------------------------------------------------------------------ plugin hooks


def register_cli(subparsers):
    p = subparsers.add_parser("check-input", help="check ESS inputs / submit scripts for common mistakes")
    p.add_argument("paths", nargs="*", help="input files (Gaussian, ORCA, Q-Chem, Molpro, Psi4, PySCF) or submit scripts")
    p.add_argument("--submit", help="submit script to cross-check against (default: one next to the input that names it)")
    p.add_argument("--json", action="store_true")
    p.add_argument("--hook", action="store_true", help="Claude Code PostToolUse hook mode (JSON on stdin)")
    return {"check-input": _cli}


def _cli(args, cfg) -> int:
    if args.hook:
        return hook_main(sys.stdin.read(), cfg=cfg)
    if not args.paths:
        print("check-input: give one or more files (or --hook)", file=sys.stderr)
        return 2
    worst = 0
    report = {}
    for path in args.paths:
        p = Path(path)
        if not p.is_file():
            print(f"{path}: not found", file=sys.stderr)
            worst = max(worst, 2)
            continue
        findings = check_input(path=p, submit_script=args.submit, cfg=cfg)
        report[str(p)] = [f.to_dict() for f in findings]
        if not args.json:
            print(format_findings(findings, str(p)))
        if any(f.severity == "error" for f in findings):
            worst = max(worst, 1)
    if args.json:
        print(json.dumps(report, indent=2))
    return worst


_check_input = check_input  # the MCP tool below shadows the name inside register_mcp


def register_mcp(mcp, ctx) -> None:
    _check = _check_input

    @mcp.tool()
    def check_input(content: str, filename: str, submit_script_content: str | None = None) -> str:  # noqa: F811
        """Check an ESS input file for common mistakes before running it.

        Supports Gaussian (.gjf/.com), ORCA (.inp), Q-Chem, Molpro, Psi4 (psithon or Python) and
        PySCF scripts: charge/multiplicity vs electron count, element symbols, overlapping atoms,
        basis-set coverage (Basis Set Exchange), functional availability in that code, and the
        program's own gotchas (%maxcore, %mem units, blank lines, $end, 2S vs multiplicity, ...).
        With the submit script, memory/cores are cross-checked against the Slurm/PBS allocation.

        Args:
            content: The full text of the input file.
            filename: Its file name (the extension helps detect the program, e.g. job.inp).
            submit_script_content: Optional text of the Slurm/PBS script that runs it.
        """
        findings = _check(content=content, filename=filename, submit_content=submit_script_content, cfg=ctx.cfg)
        ctx.emit({"tool": "check_input", "args": {"filename": filename},
                  "n_results": len(findings), "results": [{"code": f.code, "severity": f.severity} for f in findings]})
        return format_findings(findings, filename)
