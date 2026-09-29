"""Deterministic graders: each check is pass/fail with a one-line reason.

A check is a mapping with `type` plus arguments. Paths are relative to the task's work directory
and may be globs; a check on a glob that matches nothing fails (the agent did not write it).

    file_exists    {path}
    regex          {path, pattern, [flags: "i"]}          some matching file contains the pattern
    not_regex      {path, pattern, [flags]}               no matching file contains it (0 files: pass)
    number         {path, pattern, [min], [max]}          pattern's first group is a number in range
    answer_regex   {pattern, [flags]}                     the agent's final answer contains it
    answer_not_regex {pattern, [flags]}
    check_input    {path, [submit], [max_warnings]}       rag-drg's ESS input checker: no errors
    arc_check      {path}                                 rag-drg's ARC input.yml checker: no errors

Graders never call a model: they are cheap, repeatable and can gate CI. Add a type here (and a
test) rather than an LLM judge until a criterion really cannot be checked in code.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str

    def to_dict(self) -> dict:
        return {"name": self.name, "passed": self.passed, "detail": self.detail}


def _flags(spec: dict) -> int:
    f = 0
    for c in str(spec.get("flags", "")):
        f |= {"i": re.I, "m": re.M, "s": re.S}.get(c, 0)
    return f | re.M


def _files(work: Path, pattern: str) -> list[Path]:
    if any(ch in pattern for ch in "*?["):
        return sorted(p for p in work.glob(pattern) if p.is_file())
    p = work / pattern
    return [p] if p.is_file() else []


def _read(p: Path) -> str:
    return p.read_text(errors="replace")


def _label(spec: dict) -> str:
    return spec.get("name") or f"{spec['type']}:{spec.get('path', spec.get('pattern', ''))}"


def grade_check(spec: dict, work: Path, answer: str, cfg=None) -> CheckResult:
    t = spec["type"]
    name = _label(spec)
    if t == "file_exists":
        files = _files(work, spec["path"])
        return CheckResult(name, bool(files), ", ".join(f.name for f in files) or "missing")
    if t in ("regex", "not_regex", "number"):
        files = _files(work, spec["path"])
        rx = re.compile(spec["pattern"], _flags(spec))
        if t == "not_regex":
            hits = [f.name for f in files if rx.search(_read(f))]
            return CheckResult(name, not hits, f"found in {', '.join(hits)}" if hits else "absent")
        if not files:
            return CheckResult(name, False, f"no file matches {spec['path']}")
        if t == "regex":
            hits = [f.name for f in files if rx.search(_read(f))]
            return CheckResult(name, bool(hits), f"in {', '.join(hits)}" if hits else "pattern not found")
        vals = []
        for f in files:
            m = rx.search(_read(f))
            if m:
                try:
                    vals.append(float(m.group(1)))
                except (IndexError, ValueError):
                    pass
        if not vals:
            return CheckResult(name, False, "no number found")
        v = vals[0]
        lo, hi = spec.get("min"), spec.get("max")
        ok = (lo is None or v >= lo) and (hi is None or v <= hi)
        return CheckResult(name, ok, f"{v:g} (allowed {lo if lo is not None else '-inf'}..{hi if hi is not None else 'inf'})")
    if t in ("answer_regex", "answer_not_regex"):
        found = re.search(spec["pattern"], answer or "", _flags(spec)) is not None
        ok = found if t == "answer_regex" else not found
        return CheckResult(name, ok, ("found" if found else "not found") + " in the final answer")
    if t == "check_input":
        from ..inputcheck import check_input

        files = _files(work, spec["path"])
        if not files:
            return CheckResult(name, False, f"no file matches {spec['path']}")
        sub = _files(work, spec["submit"]) if spec.get("submit") else []
        if spec.get("submit") and not sub:
            return CheckResult(name, False, f"no submit script matches {spec['submit']}")
        findings = check_input(files[0], submit_script=sub[0] if sub else None, cfg=cfg)
        errs = [f for f in findings if f.severity == "error"]
        warns = [f for f in findings if f.severity == "warning"]
        max_w = spec.get("max_warnings")
        ok = not errs and (max_w is None or len(warns) <= max_w)
        shown = errs or (warns if max_w is not None and len(warns) > max_w else [])
        detail = "; ".join(f"[{f.code}] {f.message}"[:160] for f in shown[:3]) or f"0 errors, {len(warns)} warnings"
        return CheckResult(name, ok, detail)
    if t == "arc_check":
        from ..arc_input import check_arc_input

        files = _files(work, spec["path"])
        if not files:
            return CheckResult(name, False, f"no file matches {spec['path']}")
        findings = check_arc_input(_read(files[0]), files[0].name, cfg)
        errs = [f for f in findings if f.severity == "error"]
        return CheckResult(name, not errs,
                           "; ".join(f"[{f.code}] {f.message}"[:160] for f in errs[:3]) or "0 errors")
    raise ValueError(f"unknown check type {t!r}")


def grade(checks: list[dict], work: Path, answer: str, cfg=None) -> list[CheckResult]:
    out = []
    for spec in checks:
        try:
            out.append(grade_check(spec, work, answer, cfg))
        except Exception as e:  # noqa: BLE001 - a broken check must show up as a failure, not a crash
            out.append(CheckResult(_label(spec), False, f"grader error: {type(e).__name__}: {e}"))
    return out


CHECK_TYPES = ("file_exists", "regex", "not_regex", "number", "answer_regex", "answer_not_regex",
               "check_input", "arc_check")
