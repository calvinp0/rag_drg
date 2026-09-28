"""Diagnose a failed (or finished, or still running) ESS output file without reading all of it.

Agents burn many tokens paging through multi-megabyte Gaussian/ORCA/Q-Chem/Molpro/Psi4 outputs to find
out why a job died. ``diagnose_output`` reads only the head (program, version, route) and the tail of the
file, decides whether the job succeeded, failed or never finished, matches the curated failure signatures
in ``knowledge/ess/errors.yaml`` in priority order and returns the meaning, ordered fixes, a short excerpt
and pointers to the relevant knowledge cards.

Plugin hooks (see ``rag_drg/plugins.py``):

* CLI ``rag-drg diagnose FILE... [--json] [--software X] [--tail-lines N]``
  (exit 0 = all succeeded, 1 = at least one failed, 2 = otherwise incomplete / unknown)
* MCP tool ``diagnose_output(content, filename="", software=None)`` (content mode: the shared server cannot
  read the user's files)
* ``lint`` validates every errors-database YAML (unique ids, patterns compile, required fields, known software)

The errors database is also indexed for search, one chunk per entry (``errors_sections``, used by
``rag_drg.chunking.chunk_file``).
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

import yaml

KNOWN_SOFTWARE = ("gaussian", "orca", "qchem", "molpro", "psi4", "pyscf", "scheduler")
SEVERITIES = ("error", "fatal", "warning")
REQUIRED_FIELDS = ("software", "id", "pattern", "meaning", "fixes", "sources")
DEFAULT_PRIORITY = 50

HEAD_LINES = 300          # program banner, version, route / input echo
TAIL_LINES = 2000         # errors are almost always within the last few hundred lines
FULL_READ_BYTES = 4 << 20  # smaller files are simply read whole
FULL_SCAN_MAX_BYTES = 256 << 20
MAX_CONTENT_CHARS = 2 << 20
EXCERPT_CONTEXT = 4
EXCERPT_MAX_LINES = 30
EXCERPT_LINE_CHARS = 200

REPO_ERRORS_FILE = Path(__file__).resolve().parents[2] / "knowledge" / "ess" / "errors.yaml"

CARDS = {
    "gaussian": ["knowledge/ess/gaussian/gaussian-essentials.md (Common error terminations)",
                 "knowledge/ess/gaussian/g09-vs-g16.md"],
    "orca": ["knowledge/ess/orca/orca-essentials.md"],
    "qchem": ["knowledge/ess/qchem/qchem-essentials.md"],
    "molpro": ["knowledge/ess/molpro/molpro-essentials.md"],
    "psi4": ["knowledge/ess/psi4/psi4-essentials.md"],
    "pyscf": ["knowledge/ess/pyscf/pyscf-essentials.md"],
}
COMMON_CARDS = ["knowledge/ess/capabilities.md (Normal-termination markers)", "knowledge/ess/errors.yaml"]
ARC_CARD = "knowledge/arc/arc-essentials.md"

INCOMPLETE_ADVICE = (
    "No normal-termination marker and no known error message: the job is still running, or it was killed "
    "from outside (walltime, out-of-memory kill, node failure, manual scancel). Check the scheduler: "
    "`sacct -j <jobid> --format=JobID,State,ExitCode,Elapsed,Timelimit,MaxRSS,ReqMem` (Slurm), "
    "`qstat -xf <jobid>` (PBS) or `condor_history -l <jobid>` (HTCondor), and the job's stderr file. "
    "State TIMEOUT -> restart from the last geometry/checkpoint with more time; OUT_OF_MEMORY -> more memory "
    "or a smaller program memory setting."
)


# --------------------------------------------------------------------------- #
# Errors database
# --------------------------------------------------------------------------- #

@dataclass
class ErrorEntry:
    software: str
    id: str
    pattern: str
    meaning: str
    fixes: list[str]
    sources: list[str]
    links: list[str] = field(default_factory=list)
    priority: int = DEFAULT_PRIORITY
    severity: str = "error"
    versions: list[str] = field(default_factory=list)
    regex: re.Pattern | None = field(default=None, repr=False, compare=False)

    @property
    def fallback(self) -> bool:
        return self.priority <= 0


def is_errors_file(data: Any) -> bool:
    return isinstance(data, dict) and isinstance(data.get("errors"), list)


def _entries_from_data(data: dict) -> list[ErrorEntry]:
    out = []
    for e in data.get("errors") or []:
        if not isinstance(e, dict) or not all(e.get(k) is not None for k in ("software", "id", "pattern")):
            continue
        try:
            rx = re.compile(str(e["pattern"]))
        except re.error:
            continue  # reported by lint
        out.append(ErrorEntry(
            software=str(e["software"]).lower(), id=str(e["id"]), pattern=str(e["pattern"]),
            meaning=str(e.get("meaning") or "").strip(),
            fixes=[str(f) for f in (e.get("fixes") or [])], sources=[str(s) for s in (e.get("sources") or [])],
            links=[str(x).lower() for x in (e.get("links") or [])],
            priority=int(e.get("priority", DEFAULT_PRIORITY)), severity=str(e.get("severity") or "error"),
            versions=[str(v) for v in (e.get("versions") or [])], regex=rx,
        ))
    return out


def errors_files(cfg=None) -> list[Path]:
    """errors-database YAML files: `errors*.yaml` in the local sources of `cfg`, else the repository copy."""
    files: list[Path] = []
    if cfg is not None:
        for s in getattr(cfg, "sources", []) or []:
            if getattr(s, "type", None) != "local" or not getattr(s, "path", None) or not Path(s.path).is_dir():
                continue
            for pat in ("errors*.yaml", "errors*.yml"):
                files += sorted(Path(s.path).rglob(pat))
    if not files and REPO_ERRORS_FILE.is_file():
        files = [REPO_ERRORS_FILE]
    return files


_DB_CACHE: dict[tuple, list[ErrorEntry]] = {}


def load_error_db(cfg=None, paths: Iterable[Path] | None = None) -> list[ErrorEntry]:
    files = [Path(p) for p in paths] if paths is not None else errors_files(cfg)
    key = tuple((str(f), f.stat().st_mtime_ns) for f in files if f.is_file())
    if key in _DB_CACHE:
        return _DB_CACHE[key]
    entries: list[ErrorEntry] = []
    for f in files:
        try:
            data = yaml.safe_load(f.read_text())
        except (OSError, yaml.YAMLError):
            continue
        if is_errors_file(data):
            entries += _entries_from_data(data)
    _DB_CACHE[key] = entries
    return entries


# --------------------------------------------------------------------------- #
# Search index: one chunk per entry
# --------------------------------------------------------------------------- #

def readable_pattern(pattern: str) -> str:
    """A rough plain-text version of a regex, so the literal message words are keyword-searchable."""
    s = re.sub(r"\(\?[a-zA-Z]+\)", "", pattern)
    s = re.sub(r"\\s[*+?]?|\\n", " ", s)
    s = re.sub(r"\\d[*+?]?|\[\^?[^\]]*\][*+?]?|\\S[*+?]?|\.\*\??|\.\+\??", " ", s)
    s = s.replace("(?:", "(").replace("^", "").replace("$", "")
    s = re.sub(r"\\(.)", r"\1", s)
    s = re.sub(r"\{\d+(,\d*)?\}\??", "", s)
    return re.sub(r"\s+", " ", s).strip()


def format_entry(e: dict) -> str:
    sw = str(e.get("software", ""))
    lines = [f"{sw} error {e.get('id')}: {readable_pattern(str(e.get('pattern', '')))}"]
    if e.get("links"):
        lines.append("Gaussian link(s): " + ", ".join(e["links"]) + " (Error termination via Lnk1e in .../"
                     + "/".join(f"{x}.exe" for x in e["links"]) + ")")
    lines.append(f"Output pattern (regex): {e.get('pattern')}")
    lines.append(f"Meaning: {str(e.get('meaning', '')).strip()}")
    fixes = e.get("fixes") or []
    if fixes:
        lines.append("Fixes, in order:")
        lines += [f"{i}. {f}" for i, f in enumerate(fixes, 1)]
    if e.get("severity") and e["severity"] != "error":
        lines.append(f"Severity: {e['severity']}")
    if e.get("sources"):
        lines.append("Sources: " + "; ".join(str(s) for s in e["sources"]))
    lines.append("Diagnose a whole output file with `rag-drg diagnose FILE` or the diagnose_output MCP tool.")
    return "\n".join(lines)


def errors_sections(data: dict) -> list[tuple[list[str], str]]:
    return [
        (["ESS errors", str(e["software"]), str(e["id"])], format_entry(e))
        for e in data["errors"]
        if isinstance(e, dict) and e.get("id") and e.get("software")
    ]


# --------------------------------------------------------------------------- #
# Reading
# --------------------------------------------------------------------------- #

def _tail_bytes(f, size: int, n_lines: int) -> bytes:
    block, data, pos = 1 << 16, b"", size
    while pos > 0 and data.count(b"\n") <= n_lines:
        step = min(block, pos)
        pos -= step
        f.seek(pos)
        data = f.read(step) + data
    parts = data.split(b"\n")
    return b"\n".join(parts[-(n_lines + 1):])


def read_head_tail(path: Path, tail_lines: int = TAIL_LINES, head_lines: int = HEAD_LINES) -> tuple[str, bool]:
    """(text, complete). Small files are read whole; big ones as head + marker + tail."""
    size = path.stat().st_size
    if size <= FULL_READ_BYTES:
        return path.read_text(errors="replace"), True
    with path.open("rb") as f:
        head_parts = []
        for _ in range(head_lines):
            line = f.readline()
            if not line:
                break
            head_parts.append(line)
        head = b"".join(head_parts)
        tail = _tail_bytes(f, size, tail_lines)
    skipped = size - len(head) - len(tail)
    if skipped <= 0:
        return path.read_text(errors="replace"), True
    marker = f"\n [... rag-drg: {skipped} bytes in the middle of the file not read ...]\n"
    return head.decode(errors="replace") + marker + tail.decode(errors="replace"), False


def _shrink_content(content: str, tail_lines: int) -> tuple[str, bool]:
    if len(content) <= MAX_CONTENT_CHARS:
        return content, True
    lines = content.splitlines()
    head, tail = lines[:HEAD_LINES], lines[-tail_lines:]
    return "\n".join(head + [" [... rag-drg: middle of the content not read ...]"] + tail), False


# --------------------------------------------------------------------------- #
# Program, version, status
# --------------------------------------------------------------------------- #

_SIGNATURES: dict[str, list[tuple[str, int]]] = {
    "gaussian": [(r"Entering Gaussian System", 5), (r"Normal termination of Gaussian", 5),
                 (r"Error termination via Lnk1e", 5), (r"Gaussian, Inc\.", 2), (r"Gaussian \d\d, Revision", 3),
                 (r"\(Enter \S+/l\d+\.exe\)", 3), (r"Leave Link\s+\d+", 2)],
    "orca": [(r"\*\s+O\s+R\s+C\s+A\s+\*", 6), (r"ORCA TERMINATED NORMALLY", 6), (r"ORCA finished by error termination", 6),
             (r"\[file orca_\w+/", 3), (r"ORCA TERMINATED ABNORMALLY", 5), (r"\bORCA\b", 1)],
    "qchem": [(r"Welcome to Q-Chem", 6), (r"Thank you very much for using Q-Chem", 6), (r"Q-Chem begins on", 5),
              (r"Q-Chem, Inc\.", 2), (r"\$rem\b", 1)],
    "molpro": [(r"PROGRAM SYSTEM MOLPRO", 6), (r"Molpro calculation terminated", 6), (r"GLOBAL ERROR fehler", 5),
               (r"Variable memory set to", 3), (r"(?m)^\s*\? Error\s*$", 3), (r"SETTING BASIS", 2),
               (r"(?i)\bmolpro\b", 1)],
    "psi4": [(r"Psi4: An Open-Source Ab Initio", 6), (r"Psi4 exiting successfully", 6),
             (r"Psi4 encountered an error", 6), (r"psi4\.driver", 3), (r"\bPsi4\b", 1)],
    "pyscf": [(r"\bpyscf\b", 3), (r"converged SCF energy =", 3)],
    "scheduler": [(r"Job submitted from host", 3), (r"Job executing on host", 3), (r"slurmstepd", 2),
                  (r"Job was held\.", 3), (r"PBS: job killed", 3)],
}
_SIG_RX = {sw: [(re.compile(p), w) for p, w in sigs] for sw, sigs in _SIGNATURES.items()}


def detect_software(text: str) -> str | None:
    sample = text if len(text) <= 400_000 else text[:200_000] + text[-200_000:]
    scores = {sw: sum(w for rx, w in sigs if rx.search(sample)) for sw, sigs in _SIG_RX.items()}
    best = max(scores, key=lambda k: scores[k])
    if scores[best] < 3:
        return None
    if best == "scheduler":
        # A merged stdout with a slurmstepd line: prefer the program that wrote the rest.
        others = {k: v for k, v in scores.items() if k != "scheduler" and v >= 5}
        if others:
            return max(others, key=lambda k: others[k])
    return best


_VERSION_RX = {
    "gaussian": [(r"(Gaussian \d\d, Revision [A-Z]\.\d+)", "{0}"),
                 (r"Gaussian (\d\d):\s+\S+-G\d\dRev([A-Z]\.\d+)", "Gaussian {0}, Revision {1}")],
    "orca": [(r"(Program Version \d+\.\d+\.\d+)", "{0}")],
    "qchem": [(r"Q-Chem (\d+\.\d+(?:\.\d+)?) for", "Q-Chem {0}"), (r"Q-Chem (\d+\.\d+), Q-Chem, Inc\.", "Q-Chem {0}")],
    "molpro": [(r"PROGRAM SYSTEM MOLPRO[\s*]*\n\s*(?:Copyright[^\n]*\n\s*)?Version (\d{4}\.\d+)", "Molpro {0}"),
               (r"Version (\d{4}\.\d+) linked", "Molpro {0}")],
    "psi4": [(r"Psi4 (\d+\.\d+(?:\.\d+)?(?:\w+)?) release", "Psi4 {0}"), (r"Psi4 (\d+\.\d+\S*)", "Psi4 {0}")],
    "pyscf": [(r"(?i)pyscf version (\S+)", "PySCF {0}")],
}


def detect_version(text: str, software: str | None) -> str | None:
    head = text[:300_000]
    for pat, fmt in _VERSION_RX.get(software or "", []):
        m = re.search(pat, head)
        if m:
            return fmt.format(*m.groups())
    return None


def _gaussian_route(text: str) -> str | None:
    """The route section of the last job step (Gaussian wraps it at a fixed width between dashed lines)."""
    route = None
    for m in re.finditer(r"(?m)^ -{5,}\n( #[^\n]*\n(?: [^\n]*\n){0,6}?) -{5,}$", text):
        route = "".join(l[1:] if l.startswith(" ") else l for l in m.group(1).splitlines()).strip()
    return route


def _orca_keywords(text: str) -> str | None:
    kws = re.findall(r"(?m)^\|\s*\d+>\s*(!.*?)\s*$", text[:400_000])
    return " ".join(kws) if kws else None


_GAUSSIAN_TERM = re.compile(r"(?m)^ (Normal termination of Gaussian|Error termination)[^\n]*$")
_GAUSSIAN_LINK = re.compile(r"Error termination via Lnk1e in (?:\S*/)?(l\d+)\.exe")
_GAUSSIAN_NEXT_STEP = re.compile(r"Link1:\s+Proceeding to internal job step|Entering Gaussian System|Entering Link 1 =")

_SIMPLE_STATUS = {
    "orca": (r"\*\*\*\*ORCA TERMINATED NORMALLY\*\*\*\*|ORCA TERMINATED NORMALLY",
             r"ORCA finished by error termination|ORCA TERMINATED ABNORMALLY|\.\.\.\. aborting the run"),
    "qchem": (r"Thank you very much for using Q-Chem", r"Q-Chem fatal error occurred"),
    "psi4": (r"Psi4 exiting successfully", r"Psi4 encountered an error"),
    "molpro": (r"Molpro calculation terminated", r"(?m)^\s*\? Error\s*$|GLOBAL ERROR fehler"),
    "pyscf": (r"a^", r"Traceback \(most recent call last\)"),
    "scheduler": (r"a^", r"a^"),
}


@dataclass
class _Status:
    status: str             # success | failed | incomplete | unknown
    reason: str
    segment_start: int = 0  # offset of the text that belongs to the final job step
    failing_link: str | None = None


def _gaussian_status(text: str) -> _Status:
    link = None
    links = _GAUSSIAN_LINK.findall(text)
    terms = list(_GAUSSIAN_TERM.finditer(text))
    if not terms:
        return _Status("incomplete", "no 'Normal termination' or 'Error termination' line")
    last = terms[-1]
    if last.group(1).startswith("Normal"):
        after = text[last.end():]
        nxt = _GAUSSIAN_NEXT_STEP.search(after)
        if nxt:
            return _Status("incomplete", "a later job step started after the last 'Normal termination' and has not "
                                         "finished", segment_start=last.end())
        n_ok = sum(1 for t in terms if t.group(1).startswith("Normal"))
        return _Status("success", f"'Normal termination of Gaussian' ({n_ok} step(s))")
    # Error: the final block may be two lines ("request processed by link 9999" + "via Lnk1e").
    i = len(terms) - 1
    while i > 0 and text.count("\n", terms[i - 1].end(), terms[i].start()) <= 3 \
            and terms[i - 1].group(1).startswith("Error"):
        i -= 1
    start = terms[i - 1].end() if i > 0 else 0
    if links:
        link = links[-1]
    elif "processed by link 9999" in last.group(0):
        link = "l9999"
    n_ok = sum(1 for t in terms[:i] if t.group(1).startswith("Normal"))
    extra = f" after {n_ok} normally terminated step(s)" if n_ok else ""
    return _Status("failed", f"'Error termination'{extra}" + (f" in link {link}" if link else ""),
                   segment_start=start, failing_link=link)


def determine_status(text: str, software: str | None) -> _Status:
    if software == "gaussian":
        return _gaussian_status(text)
    if software in _SIMPLE_STATUS:
        ok, bad = _SIMPLE_STATUS[software]
        ok_m = list(re.finditer(ok, text))
        bad_m = list(re.finditer(bad, text))
        if software == "molpro" and ok_m and not bad_m:
            return _Status("success", "'Molpro calculation terminated' and no error trailer")
        if software != "molpro" and ok_m and (not bad_m or ok_m[-1].start() > bad_m[-1].start()):
            return _Status("success", f"normal-termination marker {ok_m[-1].group(0)!r}")
        if bad_m:
            return _Status("failed", f"error marker {bad_m[-1].group(0).strip()!r}")
        if software == "scheduler":
            return _Status("unknown", "scheduler log without a known failure message")
        if software == "pyscf":
            return _Status("unknown", "PySCF prints no termination marker; check mf.converged in the script")
        return _Status("incomplete", "no normal-termination marker")
    return _Status("unknown", "could not tell which program wrote this output")


# --------------------------------------------------------------------------- #
# Progress (cheap, optional)
# --------------------------------------------------------------------------- #

def _last(rx: str, text: str, flags: int = 0):
    last = None
    for last in re.finditer(rx, text, flags):
        pass
    return last


def progress_info(text: str, software: str | None) -> dict:
    info: dict = {}
    if software == "gaussian":
        if m := _last(r"Step number\s+(\d+) out of a maximum of\s+(\d+)", text):
            info["last_opt_step"] = f"{m.group(1)} of max {m.group(2)}"
        if m := _last(r"SCF Done:\s+E\((\S+)\)\s+=\s+(\S+)\s+A\.U\. after\s+(\d+) cycles", text):
            info["last_scf"] = f"E({m.group(1)}) = {m.group(2)} after {m.group(3)} cycles"
        if m := _last(r"(?m)^ Maximum Force.*\n^ RMS     Force.*\n^ Maximum Displacement.*\n^ RMS     Displacement.*$",
                      text):
            yes = m.group(0).count("YES")
            info["last_convergence_check"] = f"{yes}/4 criteria met"
        if m := _last(r"Pt\s+(\d+) Step number\s+(\d+) out of a maximum of\s+(\d+)", text):
            info["last_irc_point"] = f"point {m.group(1)}, step {m.group(2)} of {m.group(3)}"
    elif software == "orca":
        if m := _last(r"GEOMETRY OPTIMIZATION CYCLE\s+(\d+)", text):
            info["last_opt_cycle"] = m.group(1)
        if m := _last(r"FINAL SINGLE POINT ENERGY\s+(\S+)", text):
            info["last_energy"] = m.group(1)
    elif software == "qchem":
        if m := _last(r"Optimization Cycle:\s+(\d+)", text):
            info["last_opt_cycle"] = m.group(1)
        if m := _last(r"Total energy in the final basis set =\s+(\S+)", text):
            info["last_energy"] = m.group(1)
    elif software == "psi4":
        if m := _last(r"@\S+ iter\s+(\S+):\s+(\S+)", text):
            info["last_scf_iteration"] = f"{m.group(1)}: {m.group(2)}"
    return info


# --------------------------------------------------------------------------- #
# Diagnosis
# --------------------------------------------------------------------------- #

@dataclass
class Diagnosis:
    file: str | None
    software: str | None
    version: str | None
    status: str
    reason: str
    failing_link: str | None = None
    route: str | None = None
    errors: list[dict] = field(default_factory=list)
    excerpt: str = ""
    progress: dict = field(default_factory=dict)
    cards: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    complete_read: bool = True

    def to_dict(self) -> dict:
        return asdict(self)

    def format_text(self, max_errors: int = 3) -> str:
        head = f"{self.file}: " if self.file else ""
        sw = self.version or self.software or "unknown program"
        out = [f"{head}{self.status.upper()} ({sw}) - {self.reason}"]
        if self.route:
            out.append(f"Route/input: {self.route}")
        for i, e in enumerate(self.errors[:max_errors]):
            label = "Most likely cause" if i == 0 else "Also matched"
            out.append(f"\n{label}: [{e['software']}:{e['id']}] {e['meaning']}")
            out.append(f"  matched: {e['matched']!r}")
            if i == 0 or len(self.errors) <= 2:
                out += [f"  fix {n}. {f}" for n, f in enumerate(e["fixes"], 1)]
            if e.get("sources"):
                out.append("  sources: " + "; ".join(e["sources"]))
        if len(self.errors) > max_errors:
            out.append(f"(+{len(self.errors) - max_errors} weaker matches: "
                       + ", ".join(e["id"] for e in self.errors[max_errors:]) + ")")
        if self.progress:
            out.append("\nProgress: " + "; ".join(f"{k}: {v}" for k, v in self.progress.items()))
        if self.excerpt:
            out.append("\nExcerpt ('>' marks matched lines):\n" + self.excerpt)
        for n in self.notes:
            out.append(f"\nNote: {n}")
        if self.cards:
            out.append("\nSee: " + "; ".join(self.cards))
        return "\n".join(out)


def _line_bounds(text: str, pos: int) -> tuple[int, int]:
    s = text.rfind("\n", 0, pos) + 1
    e = text.find("\n", pos)
    return s, (len(text) if e == -1 else e)


def _excerpt(text: str, positions: list[int], extra_positions: list[int] = ()) -> str:
    """Matched lines +- EXCERPT_CONTEXT lines (plus the termination line), capped."""
    if not text:
        return ""
    starts = [0] + [m.end() for m in re.finditer("\n", text)]
    import bisect

    def line_no(pos):
        return bisect.bisect_right(starts, pos) - 1

    lines = text.split("\n")
    marked = {line_no(p) for p in positions}
    wanted: set[int] = set()
    for ln in sorted(marked) + [line_no(p) for p in extra_positions]:
        wanted.update(range(max(0, ln - EXCERPT_CONTEXT), min(len(lines), ln + EXCERPT_CONTEXT + 1)))
    if not wanted:
        # No match: the last non-empty lines usually say what happened.
        nonempty = [i for i, l in enumerate(lines) if l.strip()]
        wanted = set(nonempty[-15:])
    chosen = sorted(wanted)
    if len(chosen) > EXCERPT_MAX_LINES:
        keep = sorted(marked)[:1]
        around = set()
        for ln in keep:
            around.update(range(max(0, ln - EXCERPT_CONTEXT), ln + EXCERPT_CONTEXT + 1))
        chosen = sorted((around | set(chosen[-(EXCERPT_MAX_LINES - len(around)):])) & set(chosen))[-EXCERPT_MAX_LINES:]
    out, prev = [], None
    for i in chosen:
        if prev is not None and i != prev + 1:
            out.append("  ...")
        line = lines[i].rstrip()
        if len(line) > EXCERPT_LINE_CHARS:
            line = line[:EXCERPT_LINE_CHARS] + " [...]"
        out.append(("> " if i in marked else "  ") + line)
        prev = i
    return "\n".join(out)


def _version_ok(entry: ErrorEntry, version: str | None) -> bool:
    if not entry.versions or not version:
        return True
    return any(re.search(rf"(?<![\d.]){re.escape(v)}(?![\d])", version) for v in entry.versions)


def _match_entries(text: str, segment_start: int, software: str | None, status: str, version: str | None,
                   failing_link: str | None, db: list[ErrorEntry]) -> list[tuple]:
    scope = {"scheduler"} | ({software} if software else set())
    found = []
    for order, e in enumerate(db):
        if e.software not in scope or not _version_ok(e, version):
            continue
        if status == "success" and e.severity == "error":
            continue
        # Scheduler lines may follow the program output: search the whole text for them.
        seg = text if e.software == "scheduler" else text[segment_start:]
        base = 0 if e.software == "scheduler" else segment_start
        last = None
        for last in e.regex.finditer(seg):
            pass
        if last is None:
            continue
        score = float(e.priority)
        if failing_link and e.links:
            score += 15 if failing_link in e.links else -40
        m_pos = base + last.start()
        found.append((score, order, e, m_pos, last))
    specific = [f for f in found if not f[2].fallback]
    if specific:
        found = specific
    elif status == "success":
        found = []
    found.sort(key=lambda f: (-f[0], f[1]))
    return found


def diagnose_output(
    path: str | Path | None = None,
    content: str | None = None,
    filename: str | None = None,
    software: str | None = None,
    tail_lines: int | None = None,
    cfg=None,
    db: list[ErrorEntry] | None = None,
) -> Diagnosis:
    """Diagnose an ESS output given a `path` (read efficiently) or its `content` (e.g. head + tail)."""
    tail_lines = int(tail_lines or TAIL_LINES)
    notes: list[str] = []
    if path is not None:
        p = Path(path)
        text, complete = read_head_tail(p, tail_lines)
        name = filename or str(path)
    else:
        text, complete = _shrink_content(content or "", tail_lines)
        name = filename or None
        if content and ("[..." in content or "lines omitted" in content or "not read" in content):
            complete = False
    if not text.strip():
        return Diagnosis(file=name, software=software, version=None, status="unknown",
                         reason="empty output", notes=["The output is empty: the program never started "
                                                       "(check the submit script / scheduler stderr)."])
    db = db if db is not None else load_error_db(cfg)
    sw = (software or "").lower() or detect_software(text)
    if sw and sw not in KNOWN_SOFTWARE:
        notes.append(f"Unknown software {software!r}; known: {', '.join(KNOWN_SOFTWARE)}.")
    diag = _diagnose_text(text, sw, db)
    # A big file whose tail explains nothing: look at the whole final step once.
    if (path is not None and not complete and diag.status in ("failed", "incomplete")
            and not [e for e in diag.errors if e["priority"] > 0]
            and Path(path).stat().st_size <= FULL_SCAN_MAX_BYTES):
        text = Path(path).read_text(errors="replace")
        complete = True
        diag = _diagnose_text(text, sw, db)
        notes.append("The tail did not explain the failure, so the whole file was scanned.")
    diag.file = name
    diag.complete_read = complete
    if not complete:
        notes.append("Only the beginning and end of the output were read; line context is relative to that.")
    diag.notes = notes + diag.notes
    return diag


def _diagnose_text(text: str, sw: str | None, db: list[ErrorEntry]) -> Diagnosis:
    version = detect_version(text, sw)
    st = determine_status(text, sw)
    status = st.status
    found = _match_entries(text, st.segment_start, sw, status, version, st.failing_link, db)
    notes: list[str] = []
    reason = st.reason
    decisive = [f for f in found if f[2].severity != "warning"]
    if decisive and status in ("success", "incomplete", "unknown"):
        first = decisive[0][2]
        if status == "success" and first.severity == "fatal":
            reason = f"{reason}, but {first.id} matched (it is fatal even after a normal-termination marker)"
            status = "failed"
        elif status in ("incomplete", "unknown"):
            reason = f"{reason}; matched {first.id}"
            status = "failed"
    if status == "incomplete":
        notes.append(INCOMPLETE_ADVICE)
    if status == "failed" and not found:
        notes.append("No known error signature matched. Read the excerpt (the lines just before the termination "
                     "usually name the cause), search the knowledge base with the last message line, and consider "
                     "adding an entry to knowledge/ess/errors.yaml.")
    errors = []
    for score, _order, e, pos, m in found:
        s, en = _line_bounds(text, pos)
        errors.append({
            "id": e.id, "software": e.software, "meaning": e.meaning, "fixes": e.fixes, "sources": e.sources,
            "severity": e.severity, "priority": e.priority, "score": score,
            "matched": text[s:en].strip()[:EXCERPT_LINE_CHARS],
            "lines_from_end": text.count("\n", pos),
        })
    extra = []
    if sw == "gaussian" and status == "failed":
        tm = _last(_GAUSSIAN_TERM.pattern, text)
        if tm:
            extra.append(tm.start())
    positions = [f[3] for f in found[:2]]
    seg = text[st.segment_start:] if not positions else text
    offset = 0 if positions else st.segment_start
    excerpt = _excerpt(seg, [p - offset for p in positions], [p - offset for p in extra])
    cards = list(CARDS.get(sw or "", [])) + COMMON_CARDS
    if errors:
        cards.append(f"search_knowledge(\"{errors[0]['id']}\") for the full entry")
    if any("(ARC" in f for e in errors for f in e["fixes"]):
        cards.append(ARC_CARD)
    route = _gaussian_route(text) if sw == "gaussian" else _orca_keywords(text) if sw == "orca" else None
    return Diagnosis(
        file=None, software=sw, version=version, status=status, reason=reason, failing_link=st.failing_link,
        route=route, errors=errors, excerpt=excerpt, progress=progress_info(text[st.segment_start:], sw),
        cards=cards, notes=notes,
    )


# --------------------------------------------------------------------------- #
# Lint
# --------------------------------------------------------------------------- #

def lint_errors_data(data: Any, where: str, seen_ids: dict[str, str] | None = None) -> list[str]:
    from ..lint import DOC_TYPES, DOMAINS, STATUSES

    problems: list[str] = []
    seen_ids = {} if seen_ids is None else seen_ids
    if not is_errors_file(data):
        return [f"{where}: expected a top-level 'errors:' list"]
    meta = data.get("meta")
    if not isinstance(meta, dict):
        problems.append(f"{where}: missing 'meta:' mapping (title, domain, doc_type, status)")
    else:
        if not meta.get("title"):
            problems.append(f"{where}: meta is missing 'title'")
        if meta.get("domain") not in DOMAINS:
            problems.append(f"{where}: meta domain {meta.get('domain')!r} not in {sorted(DOMAINS)}")
        if meta.get("doc_type") not in DOC_TYPES:
            problems.append(f"{where}: meta doc_type {meta.get('doc_type')!r} not in {sorted(DOC_TYPES)}")
        if meta.get("status") not in STATUSES:
            problems.append(f"{where}: meta status {meta.get('status')!r} not in {sorted(STATUSES)}")
    for i, e in enumerate(data["errors"]):
        tag = f"{where}: errors[{i}]"
        if not isinstance(e, dict):
            problems.append(f"{tag}: not a mapping")
            continue
        tag = f"{where}: {e.get('id') or f'errors[{i}]'}"
        for k in REQUIRED_FIELDS:
            if e.get(k) in (None, "", []):
                problems.append(f"{tag}: missing '{k}'")
        sw = str(e.get("software") or "").lower()
        if sw and sw not in KNOWN_SOFTWARE:
            problems.append(f"{tag}: software {e.get('software')!r} not in {list(KNOWN_SOFTWARE)}")
        eid = e.get("id")
        if eid:
            if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", str(eid)):
                problems.append(f"{tag}: id should be lower-case letters, digits and '-'")
            if str(eid) in seen_ids:
                problems.append(f"{tag}: duplicate id (also in {seen_ids[str(eid)]})")
            seen_ids[str(eid)] = where
        if e.get("pattern") is not None:
            try:
                rx = re.compile(str(e["pattern"]))
                if rx.search(""):
                    problems.append(f"{tag}: pattern matches the empty string")
            except re.error as err:
                problems.append(f"{tag}: pattern does not compile: {err}")
        for k in ("fixes", "sources", "links", "versions"):
            if e.get(k) is not None and not (isinstance(e[k], list) and all(isinstance(x, (str, int, float))
                                                                              for x in e[k])):
                problems.append(f"{tag}: '{k}' must be a list of strings")
        if e.get("links"):
            if sw != "gaussian":
                problems.append(f"{tag}: 'links' is only meaningful for gaussian entries")
            bad = [x for x in e["links"] if not re.fullmatch(r"l\d+", str(x))]
            if bad:
                problems.append(f"{tag}: links must look like 'l502', got {bad}")
        if e.get("priority") is not None and not isinstance(e["priority"], int):
            problems.append(f"{tag}: 'priority' must be an integer")
        if e.get("severity") is not None and e["severity"] not in SEVERITIES:
            problems.append(f"{tag}: severity {e['severity']!r} not in {list(SEVERITIES)}")
    return problems


def lint(cfg) -> list[str]:
    problems: list[str] = []
    seen: dict[str, str] = {}
    for f in errors_files(cfg):
        rel = f.relative_to(cfg.root) if f.is_relative_to(cfg.root) else f
        try:
            data = yaml.safe_load(f.read_text())
        except yaml.YAMLError as err:
            problems.append(f"{rel}: invalid YAML: {err}")
            continue
        if not is_errors_file(data):
            continue  # some other errors*.yaml
        problems += lint_errors_data(data, str(rel), seen)
    return problems


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def register_cli(sub) -> dict:
    p = sub.add_parser(
        "diagnose",
        help="why did an ESS job fail? reads head + tail of Gaussian/ORCA/Q-Chem/Molpro/Psi4 outputs",
        description="Diagnose ESS output files. Exit code: 0 all succeeded, 1 at least one failed, "
                    "2 otherwise incomplete (still running / killed) or unknown.",
    )
    p.add_argument("files", nargs="+", metavar="FILE")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.add_argument("--software", "-s", choices=[s for s in KNOWN_SOFTWARE if s != "scheduler"],
                   help="skip program detection")
    p.add_argument("--tail-lines", type=int, default=TAIL_LINES, help=f"lines read from the end (default {TAIL_LINES})")
    return {"diagnose": _cli_diagnose}


def _cli_diagnose(args, cfg) -> int:
    db = load_error_db(cfg)
    results = []
    for f in args.files:
        path = Path(f)
        if not path.is_file():
            results.append(Diagnosis(file=f, software=None, version=None, status="unknown",
                                     reason="file not found"))
            continue
        results.append(diagnose_output(path=path, software=args.software, tail_lines=args.tail_lines, db=db))
    if args.json:
        payload = [r.to_dict() for r in results]
        print(json.dumps(payload[0] if len(payload) == 1 else payload, indent=1))
    else:
        print("\n\n".join(r.format_text() for r in results))
    statuses = {r.status for r in results}
    if "failed" in statuses:
        return 1
    if statuses - {"success"}:
        return 2
    return 0


# --------------------------------------------------------------------------- #
# MCP
# --------------------------------------------------------------------------- #

def register_mcp(mcp, ctx) -> None:
    @mcp.tool()
    def diagnose_output(content: str, filename: str = "", software: str | None = None) -> str:
        """Diagnose a failed/finished ESS output (Gaussian, ORCA, Q-Chem, Molpro, Psi4, plus Slurm/PBS/HTCondor
        messages): status (success / failed / incomplete), program + version, the matched error with its meaning
        and ordered fixes, a short excerpt and knowledge-card pointers.

        Use this INSTEAD of reading a whole output file. The server cannot see your files, so pass the text.
        For big files pass only the first ~100 lines plus the LAST ~300 lines, e.g.
        `(head -n 100 job.log; echo '[... lines omitted ...]'; tail -n 300 job.log)`. For multi-step Gaussian
        jobs the tail is what matters. If the result says 'incomplete', the job was probably killed (walltime /
        memory) or is still running: check the scheduler (sacct / qstat) and its stderr file.

        Args:
            content: The output text (or head + tail of it).
            filename: Optional file name, only echoed back.
            software: Optional: gaussian, orca, qchem, molpro, psi4, pyscf (skips detection).
        """
        diag = diagnose_output_impl(content=content, filename=filename or None, software=software or None,
                                    cfg=ctx.cfg)
        ctx.emit({"tool": "diagnose_output",
                  "args": {"filename": filename, "software": software, "chars": len(content or "")},
                  "n_results": len(diag.errors), "status": diag.status,
                  "results": [{"id": e["id"], "score": e["score"]} for e in diag.errors[:5]]})
        return diag.format_text()


# The MCP tool shadows the module-level name inside register_mcp; keep an alias for it.
diagnose_output_impl = diagnose_output
