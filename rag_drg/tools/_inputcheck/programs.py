"""Program detection, parsers and program-specific checks.

Each ``analyze_<program>(inp)`` fills the ParsedInput fields it can and returns findings.
Rules follow the curated cards in knowledge/ess/<program>/ (see model.REF). When a rule is not
certain the finding is "info", never "error".
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from .common import fmt_mb, is_float, is_int, mem_to_mb, parse_atom_line, parse_label, split_tokens
from .model import REF, Atom, Finding, ParsedInput

G, O, QC, MP, P4, PY = (REF[k] for k in ("gaussian", "orca", "qchem", "molpro", "psi4", "pyscf"))

# Files with these extensions are never ESS inputs (docs, data, code, outputs, shell scripts).
NON_INPUT_EXTS = {".md", ".markdown", ".rst", ".txt", ".yaml", ".yml", ".json", ".toml", ".cfg", ".ini", ".log",
                  ".out", ".csv", ".tsv", ".html", ".htm", ".xml", ".tex", ".bib", ".c", ".cc", ".cpp", ".h", ".hpp",
                  ".js", ".ts", ".tsx", ".jsx", ".rs", ".go", ".java", ".f", ".f90", ".sh", ".bash", ".zsh", ".slurm",
                  ".pbs", ".sbatch", ".ipynb", ".xyz", ".pdb", ".sdf", ".mol2", ".cif", ".chk", ".fchk", ".gbw",
                  ".hess", ".npy", ".npz", ".pdf", ".png", ".jpg", ".svg", ".lock", ".gitignore", ".css", ".r"}


# ================================================================== detection


def detect_program(filename: str | None, content: str) -> str | None:
    """gaussian | orca | qchem | molpro | psi4 | pyscf | None (content first, extension as a hint)."""
    ext = Path(filename or "").suffix.lower()
    text = content or ""
    if ext in NON_INPUT_EXTS:
        return None
    if ext == ".py" or re.search(r"^\s*(import|from)\s+(psi4|pyscf)\b", text, re.M):
        has_pyscf = re.search(r"^\s*(import|from)\s+pyscf\b", text, re.M) or re.search(r"\bgto\.(M|Mole)\(", text)
        has_psi4 = re.search(r"^\s*(import|from)\s+psi4\b", text, re.M) or "psi4.geometry(" in text
        if has_pyscf and not ("psi4.geometry(" in text and not re.search(r"\bgto\.(M|Mole)\(", text)):
            return "pyscf"
        if has_psi4:
            return "psi4"
        return None
    if re.search(r"^\s*\$molecule\b", text, re.M | re.I) and re.search(r"^\s*\$rem\b", text, re.M | re.I):
        return "qchem"
    if (re.search(r"^\s*\*\*\*\s*,", text, re.M) or re.search(r"^\s*geometry\s*=\s*[{\w]", text, re.M | re.I)
            or re.search(r"^\s*memory\s*,\s*\d", text, re.M | re.I)):
        return "molpro"
    if (re.search(r"^\s*molecule\s*\w*\s*\{", text, re.M) and re.search(
            r"^\s*(set\s*\{|set\s+\w+|energy\(|optimize\(|opt\(|gradient\(|frequenc(y|ies)\(|properties\()", text, re.M)):
        return "psi4"
    g = o = 0
    if ext in (".gjf", ".com", ".gau", ".gjc"):
        g += 2
    if ext == ".inp":
        o += 1
    if re.search(r"^\s*%(mem|nprocshared|nprocs?|chk|oldchk|cpu|gpucpu|rwf|lindaworkers|nproclinda)\s*=", text,
                 re.M | re.I):
        g += 3
    first = next((ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("%")), "")
    if re.match(r"^\s*#[pntPNT]?(\s|$)", first):
        g += 2
    if re.search(r"^\s*!\s*[A-Za-z]", text, re.M):
        o += 2
    if re.search(r"^\s*\*\s*(xyz|xyzfile|int|internal|gzmt|pdbfile|gzmtfile)\b", text, re.M | re.I):
        o += 3
    if re.search(r"^\s*%(maxcore|pal|scf|geom|method|basis|cpcm|output|mdci|casscf|tddft|freq)\b(?!\s*=)", text,
                 re.M | re.I):
        o += 2
    if max(g, o) < 3:
        return None
    return "gaussian" if g > o else "orca"


def detect_submit(filename: str | None, content: str) -> bool:
    head = (content or "")[:20000]
    return bool(re.search(r"^#(SBATCH|PBS)\b", head, re.M))


# ================================================================== Gaussian


def _route_tokens(route: str) -> list[str]:
    """Split a route at top-level whitespace (parentheses may contain spaces)."""
    out, cur, depth = [], "", 0
    for ch in route:
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth = max(0, depth - 1)
        if ch.isspace() and depth == 0:
            if cur:
                out.append(cur)
            cur = ""
        else:
            cur += ch
    if cur:
        out.append(cur)
    return out


def _route_options(tokens: list[str]) -> dict[str, set[str]]:
    """{keyword: {options}} e.g. Opt=(TS,CalcFC) -> {"opt": {"ts", "calcfc"}}."""
    opts: dict[str, set[str]] = {}
    for t in tokens:
        m = re.match(r"^([A-Za-z][\w-]*)\s*(?:=\s*\(?|\()?(.*?)\)?$", t)
        if not m:
            continue
        key = m.group(1).lower()
        val = m.group(2) if ("=" in t or "(" in t) else ""
        parts = {p.strip().lower() for p in re.split(r"[,\s]+", val) if p.strip()}
        opts.setdefault(key, set()).update(parts)
    return opts


def _cpu_list(spec: str) -> list[int] | None:
    out: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        m = re.fullmatch(r"(\d+)-(\d+)(?:/(\d+))?", part)
        if m:
            step = int(m.group(3) or 1)
            out += list(range(int(m.group(1)), int(m.group(2)) + 1, step))
        elif part.isdigit():
            out.append(int(part))
        else:
            return None
    return out


def analyze_gaussian(inp: ParsedInput) -> list[Finding]:
    f: list[Finding] = []
    lines = inp.lines
    # Split --Link1-- jobs
    starts = [0] + [i + 1 for i, ln in enumerate(lines) if re.fullmatch(r"\s*--link1--\s*", ln, re.I)]
    inp.jobs = len(starts)
    link0_all: dict[str, tuple[str, int]] = {}
    for j, s in enumerate(starts):
        e = starts[j + 1] - 1 if j + 1 < len(starts) else len(lines)
        f += _gaussian_job(inp, lines[s:e], s, first=(j == 0), link0_all=link0_all)
    # The whole file must end with a blank line.
    text = inp.content
    if text.strip() and not re.search(r"\n[ \t\r]*\n\s*$", text):
        last = max(i for i, ln in enumerate(lines, 1) if ln.strip())
        f.append(Finding("error", "gaussian-final-blank", "The input does not end with a blank line; Gaussian "
                         "needs an empty line after the last section (classic 'End of file' error).", last,
                         fix="add an empty line at the end of the file", ref=G + "#Input skeleton (blank lines matter)"))
    return f


def _gaussian_job(inp: ParsedInput, lines: list[str], off: int, first: bool, link0_all: dict) -> list[Finding]:
    f: list[Finding] = []
    i, n = 0, len(lines)
    ln_no = lambda k: off + k + 1  # noqa: E731
    while i < n and not lines[i].strip():
        i += 1
    link0: dict[str, tuple[str, int]] = {}
    while i < n and (lines[i].lstrip().startswith(("%", "!")) or not lines[i].strip()):
        s = lines[i].strip()
        if s.startswith("%"):
            m = re.match(r"%\s*([A-Za-z]+)\s*(?:=\s*(.*))?$", s)
            if m:
                link0[m.group(1).lower()] = ((m.group(2) or "").split("!")[0].strip(), ln_no(i))
        i += 1
    if first:
        link0_all.update(link0)
    if i >= n or not lines[i].lstrip().startswith("#"):
        f.append(Finding("error", "gaussian-route", "No route section: after the Link0 (%) lines the next line "
                         "must start with '#' (e.g. '#P B3LYP/6-31G(d) Opt').", ln_no(min(i, n - 1)) if n else None,
                         ref=G + "#Input skeleton (blank lines matter)"))
        return f
    route_start = i
    route_lines = []
    while i < n and lines[i].strip():
        route_lines.append(lines[i].split("!")[0])
        i += 1
    route = " ".join(route_lines).strip()
    route = re.sub(r"^#[pntPNT]?(?=\s)", "", route).lstrip("#").strip()
    tokens = _route_tokens(route)
    opts = _route_options(tokens)
    rl = route.lower()
    if first:
        inp.extra["link0"] = {k: v[0] for k, v in link0.items()}
        inp.extra["route"] = route
    # method/basis
    for t in tokens:
        if "/" in t and "=" not in t and ":" not in t and not t.lower().startswith(("oniom", "iop")):
            parts = t.split("/")
            if first and inp.method is None:
                inp.method, inp.basis = parts[0], parts[1] if len(parts) > 1 else None
                inp.extra["basis_line"] = ln_no(route_start)
            inp.extra.setdefault("method_tokens", []).append((parts[0], ln_no(route_start)))
    geom = opts.get("geom", set()) | opts.get("geometry", set())
    allcheck = bool(geom & {"allcheck", "allchk"})
    geomcheck = bool(geom & {"check", "checkpoint"}) or allcheck
    uses_gen = bool(re.search(r"(^|[\s/])gen(ecp)?(\s|$|/)", rl)) or (inp.basis or "").lower() in ("gen", "genecp")
    if first:
        inp.job_type = ("ts" if "ts" in opts.get("opt", set()) else "opt" if "opt" in opts else
                        "irc" if "irc" in opts else "freq" if "freq" in opts else "sp")
        if "units" in opts and opts["units"] & {"au", "bohr"}:
            inp.units = "bohr"

    # ---------------- Link0 checks (first job only; later jobs inherit nothing but are usually similar)
    L = link0 if link0 else {}
    if "mem" in L:
        val, line = L["mem"]
        m = re.fullmatch(r"([\d.]+)\s*([A-Za-z]*)", val)
        if not m:
            f.append(Finding("error", "gaussian-mem", f"Cannot read %mem={val}.", line, fix="%mem=16GB",
                             ref=G + "#Memory and cores (gotcha)"))
        else:
            unit = m.group(2).lower()
            if not unit:
                f.append(Finding("warning", "gaussian-mem-units",
                                 f"%mem={val} has no unit, so Gaussian reads it as 8-byte words "
                                 f"({fmt_mb(float(m.group(1)) * 8 / 2**20)}).", line,
                                 fix=f"write the unit, e.g. %mem={m.group(1)}MB or %mem=16GB",
                                 ref=G + "#Memory and cores (gotcha)"))
            elif unit not in ("kb", "mb", "gb", "tb", "kw", "mw", "gw", "tw"):
                f.append(Finding("warning", "gaussian-mem-units",
                                 f"%mem unit '{m.group(2)}' is not one of KB, MB, GB, TB, KW, MW, GW, TW.", line,
                                 fix=f"%mem={m.group(1)}GB", ref=G + "#Memory and cores (gotcha)"))
            if first:
                inp.memory_total_mb = mem_to_mb(float(m.group(1)), unit or "w")
    nps = L.get("nprocshared") or L.get("nproc") or L.get("nprocs")
    cpu = L.get("cpu")
    if nps and cpu:
        f.append(Finding("warning", "gaussian-cpu-nproc", "Both %cpu and %nprocshared are set; use only one "
                         "(%cpu pins cores and implies the count).", cpu[1],
                         fix="keep %cpu=... (GPU jobs) or %nprocshared=N, not both", ref=G + "#Memory and cores (gotcha)"))
    cpus = _cpu_list(cpu[0]) if cpu else None
    if first:
        if nps and is_int(nps[0]):
            inp.nprocs = int(nps[0])
        elif cpus:
            inp.nprocs = len(cpus)
    if "gpucpu" in L:
        val, line = L["gpucpu"]
        m = re.fullmatch(r"\s*([\d,\-/]+)\s*=\s*([\d,\-/]+)\s*", val)
        if not m:
            f.append(Finding("warning", "gaussian-gpucpu", f"Cannot read %gpucpu={val}; expected gpu-list=cpu-list, "
                             "e.g. %gpucpu=0-1=0,1.", line, ref=G + "#GPUs (G16 GPU build only; G09 has no GPU support)"))
        else:
            gl, cl = _cpu_list(m.group(1)) or [], _cpu_list(m.group(2)) or []
            if first:
                inp.gpus = len(gl)
            if len(gl) != len(cl):
                f.append(Finding("warning", "gaussian-gpucpu", f"%gpucpu lists {len(gl)} GPU(s) but {len(cl)} "
                                 "controlling CPU(s); each GPU needs one controlling core.", line,
                                 ref=G + "#GPUs (G16 GPU build only; G09 has no GPU support)"))
            if cpus is None:
                f.append(Finding("warning", "gaussian-gpucpu-cpu", "%gpucpu is used without %cpu; the controlling "
                                 "cores must be listed in %cpu.", line, fix="add %cpu=0-15 (the job's cores)",
                                 ref=G + "#GPUs (G16 GPU build only; G09 has no GPU support)"))
            else:
                outside = [c for c in cl if c not in cpus]
                if outside:
                    f.append(Finding("error", "gaussian-gpucpu-cpu", f"GPU controlling core(s) {outside} are not in "
                                     f"%cpu={cpu[0]}.", line, fix="the cores after '=' in %gpucpu must be part of %cpu",
                                     ref=G + "#GPUs (G16 GPU build only; G09 has no GPU support)"))
    # ---------------- route checks
    if re.search(r"(^|[\s/])[ru]?wb97xd(\s|/|$)", rl) and "empiricaldispersion" in opts:
        f.append(Finding("warning", "gaussian-double-dispersion", "wB97XD already contains its own empirical "
                         "dispersion; adding EmpiricalDispersion counts dispersion twice.", ln_no(route_start),
                         fix="remove EmpiricalDispersion=... (or use wB97X with a dispersion model deliberately)",
                         ref=G + "#Solvation, dispersion, basis"))
    optset = opts.get("opt", set()) | opts.get("optimization", set())
    if "ts" in optset and not optset & {"calcfc", "readfc", "calcall", "rcfc", "calchffc", "readcartesianfc"}:
        f.append(Finding("warning", "gaussian-ts-hessian", "Opt=TS without CalcFC/ReadFC/CalcAll starts from a "
                         "guessed Hessian and often fails to find the TS.", ln_no(route_start),
                         fix="Opt=(TS,CalcFC,NoEigenTest) (or ReadFC with %oldchk of a freq job)",
                         ref=G + "#Optimisations, TS, IRC"))
    chk = "chk" in link0_all or "oldchk" in link0_all or "chk" in L or "oldchk" in L
    reads_chk = geomcheck or "read" in opts.get("guess", set()) or bool(optset & {"readfc", "rcfc"})
    if reads_chk and not chk:
        f.append(Finding("error" if allcheck else "warning", "gaussian-chk-missing",
                         "The route reads from a checkpoint (Geom=Check/AllCheck, Guess=Read or ReadFC) but no %chk or "
                         "%oldchk is given.", ln_no(route_start), fix="%oldchk=previous.chk", ref=G + "#Optimisations, TS, IRC"))

    # ---------------- sections after the route
    if i >= n:
        if not allcheck:
            f.append(Finding("error", "gaussian-sections", "Missing blank line + title + blank line + charge/"
                             "multiplicity after the route section.", ln_no(n - 1),
                             ref=G + "#Input skeleton (blank lines matter)"))
        return f
    i += 1  # blank after route
    sections: list[tuple[int, list[str]]] = []
    while i < n:
        while i < n and not lines[i].strip():
            i += 1
        if i >= n:
            break
        s0, block = i, []
        while i < n and lines[i].strip():
            block.append(lines[i])
            i += 1
        sections.append((s0, block))
    if allcheck:
        if sections and len(sections[0][1]) >= 1 and _looks_like_charge_line(sections[0][1][0]) is None and len(
                sections) >= 2 and _looks_like_charge_line(sections[1][1][0]) is not None:
            f.append(Finding("warning", "gaussian-allcheck-sections", "Geom=AllCheck takes the title, charge/"
                             "multiplicity and geometry from the checkpoint; remove the title and molecule sections.",
                             ln_no(sections[0][0]), ref=G + "#Optimisations, TS, IRC"))
        extra_sections = sections
    else:
        if not sections:
            f.append(Finding("error", "gaussian-sections", "Missing title and charge/multiplicity sections after the "
                             "route.", ln_no(i - 1), ref=G + "#Input skeleton (blank lines matter)"))
            return f
        title_idx, title = sections[0]
        if _looks_like_charge_line(title[0]) is not None and len(title) > 1:
            f.append(Finding("error", "gaussian-title", "The section after the route looks like the charge/"
                             "multiplicity + geometry: the title line and its blank lines are missing.",
                             ln_no(title_idx), fix="route, blank line, title, blank line, charge mult, coordinates",
                             ref=G + "#Input skeleton (blank lines matter)"))
            return f
        if len(sections) < 2:
            f.append(Finding("error", "gaussian-sections", "No charge/multiplicity section after the title.",
                             ln_no(title_idx), ref=G + "#Input skeleton (blank lines matter)"))
            return f
        mol_idx, mol = sections[1]
        cm = _looks_like_charge_line(mol[0])
        if cm is None:
            f.append(Finding("error", "gaussian-charge-mult", f"Expected 'charge multiplicity' (e.g. '0 1'), got "
                             f"'{mol[0].strip()}'. Is the title section (or a blank line) missing or split?",
                             ln_no(mol_idx), ref=G + "#Input skeleton (blank lines matter)"))
            return f
        if first:
            inp.charge, inp.multiplicity = cm[0], cm[1]
            inp.extra["charge_line"] = ln_no(mol_idx)
        atoms_lines = mol[1:]
        if geomcheck:
            extra_sections = sections[2:]
            if first:
                inp.geometry_complete = False
        else:
            if not atoms_lines:
                f.append(Finding("error", "gaussian-geometry", "No atoms after the charge/multiplicity line.",
                                 ln_no(mol_idx), ref=G + "#Input skeleton (blank lines matter)"))
            if first:
                ok = True
                for k, ln in enumerate(atoms_lines):
                    atom, rec = parse_atom_line(ln, ln_no(mol_idx + 1 + k), allow_flag=True)
                    if not rec:
                        tok = split_tokens(ln)
                        if tok and re.match(r"^[A-Za-z]{1,3}\d*$", tok[0]):
                            inp.extra.setdefault("unknown_labels", []).append((ln_no(mol_idx + 1 + k), tok[0]))
                        ok = False
                        continue
                    inp.atoms.append(atom)
                inp.geometry_complete = ok and bool(inp.atoms)
            extra_sections = sections[2:]
    if uses_gen:
        has_basis = any(any(re.fullmatch(r"\s*\*{4}\s*", ln) for ln in blk) for _, blk in extra_sections)
        if not has_basis:
            f.append(Finding("error", "gaussian-gen-basis", "Gen/GenECP in the route but no basis-set block (atoms "
                             "line, basis, '****') after the geometry.", ln_no(route_start),
                             fix="add the basis block after the blank line that ends the geometry",
                             ref=G + "#Solvation, dispersion, basis"))
    return f


def _looks_like_charge_line(line: str) -> tuple[int, int] | None:
    tok = split_tokens(line)
    if len(tok) >= 2 and len(tok) % 2 == 0 and all(is_int(t) for t in tok):
        return int(tok[0]), int(tok[1])
    return None


# ================================================================== ORCA

ORCA_JOB_WORDS = {"opt", "optts", "freq", "numfreq", "sp", "engrad", "irc", "neb-ts", "neb", "copt", "tightopt",
                  "looseopt", "verytightopt", "tightscf", "verytightscf", "normalscf", "loosescf", "slowconv",
                  "veryslowconv", "kdiis", "soscf", "nori", "nocosx", "rijcosx", "rijk", "ri", "moread", "largeprint",
                  "miniprint", "normalprint", "defgrid1", "defgrid2", "defgrid3", "autoaux", "tightpno", "normalpno",
                  "loosepno", "bohrs", "uhf", "uks", "rhf", "rks", "rohf", "roks", "d3bj", "d3zero", "d4", "d3"}
ORCA_SINGLE_LINE = {"maxcore", "moinp", "base", "pointcharges", "id"}
ORCA_SUBBLOCKS = {"constraints", "scan", "newgto", "newecp", "newauxjgto", "newauxcgto", "newauxjkgto",
                  "addgto", "modify_internal", "coords", "invertconstraints_block", "tsmode", "hybrid_hess"}
ORCA_DOUBLE_HYBRIDS = re.compile(r"^(ri-)?(b2plyp|b2gp-plyp|mpw2plyp|b2k-plyp|b2t-plyp|pwpb95|dsd-\S+|revdsd-\S+|"
                                 r"wb2plyp|wb2gp-plyp|wb97x-2|pbe0-dh|pbe-qidh|pbe0-2|rsx-qidh|rsx-0dh|wpr2scan50|"
                                 r"pr2scan50|pr2scan69|kpr2scan50|b2nc-plyp|wb88pp86|wpbepp86|scs-\S+|sos-\S+)(-d\S*)?$")


def analyze_orca(inp: ParsedInput) -> list[Finding]:
    f: list[Finding] = []
    lines = inp.lines
    clean = [ln.split("#")[0] for ln in lines]
    simple: list[tuple[str, int]] = []
    blocks: dict[str, tuple[str, int]] = {}
    depth, open_stack = 0, []
    in_coords = False
    coord_start = None
    coord_hdr = None
    atoms_lines: list[tuple[str, int]] = []
    for k, ln in enumerate(clean, 1):
        s = ln.strip()
        if not s:
            continue
        if in_coords:
            if s == "*" or s.startswith("*") and not s[1:].strip():
                in_coords = False
                continue
            if inp.jobs == 1:  # only the first job of a $new_job chain is checked in detail
                atoms_lines.append((s, k))
            continue
        if s.startswith("!"):
            if inp.jobs == 1:
                simple += [(t, k) for t in s[1:].split()]
            continue
        m = re.match(r"^\*\s*(xyz|xyzfile|int|internal|gzmt|gzmtfile|pdbfile)\b(.*)$", s, re.I)
        if m and depth == 0:
            if inp.jobs == 1:
                coord_hdr = (m.group(1).lower(), m.group(2).split(), k)
            coord_start = k
            if m.group(1).lower() in ("xyz", "int", "internal", "gzmt"):
                in_coords = True
            continue
        if s.startswith("$new_job"):
            inp.jobs += 1
            continue
        if s.startswith("%"):
            m = re.match(r"^%\s*(\w+)\s*(.*)$", s)
            if not m:
                continue
            name, rest = m.group(1).lower(), m.group(2).strip()
            if depth == 0:
                blocks.setdefault(name, (rest, k))
            if name in ORCA_SINGLE_LINE or re.fullmatch(r"(\"[^\"]*\"|'[^']*'|[\d.eE+-]+)", rest):
                continue
            toks = rest.split()
            depth += 1
            open_stack.append((name, k))
            for t in toks:
                tl = t.lower()
                if tl in ORCA_SUBBLOCKS:
                    depth += 1
                    open_stack.append((tl, k))
                elif tl == "end":
                    depth -= 1
                    if open_stack:
                        open_stack.pop()
            # Keep the block text for later look-ups.
            if depth > 0:
                blocks[name] = (rest, k)
            continue
        if depth > 0:
            toks = s.split()
            first_tok = toks[0].lower()
            if first_tok in ORCA_SUBBLOCKS:
                depth += 1
                open_stack.append((first_tok, k))
                toks = toks[1:]
            for t in toks:
                if t.lower() == "end":
                    depth -= 1
                    if open_stack:
                        open_stack.pop()
            if open_stack and depth > 0:
                name = open_stack[0][0]
                txt, l0 = blocks.get(name, ("", k))
                blocks[name] = (txt + "\n" + s, l0)
            if depth < 0:
                f.append(Finding("info", "orca-extra-end", "More 'end' lines than open blocks.", k, ref=O + "#Input skeleton"))
                depth = 0
            continue
    if depth > 0 and open_stack:
        name, k = open_stack[0]
        f.append(Finding("error", "orca-block-end", f"%{name} block opened on line {k} is not closed with 'end'.", k,
                         fix="finish every %block with a line 'end'", ref=O + "#Input skeleton"))
    if in_coords:
        f.append(Finding("error", "orca-coords-end", "The '* xyz' coordinate block is not closed with a line '*'.",
                         coord_start, ref=O + "#Input skeleton"))
    inp.extra["blocks"] = {k: v[0] for k, v in blocks.items()}
    kw = [(t, k) for t, k in simple]
    kwl = {t.lower() for t, _ in kw}
    inp.extra["keywords"] = [t for t, _ in kw]
    first_bang = kw[0][1] if kw else None
    # ---------------- geometry
    if coord_hdr:
        typ, args, k = coord_hdr
        if len(args) >= 2 and is_int(args[0]) and is_int(args[1]):
            inp.charge, inp.multiplicity = int(args[0]), int(args[1])
            inp.extra["charge_line"] = k
        else:
            f.append(Finding("error", "orca-charge-mult", f"'* {typ}' must be followed by charge and multiplicity "
                             f"(e.g. '* {typ} 0 1{' geom.xyz' if typ == 'xyzfile' else ''}').", k, ref=O + "#Input skeleton"))
        if typ == "xyzfile":
            if len(args) < 3:
                f.append(Finding("error", "orca-xyzfile", "'* xyzfile charge mult' needs a file name.", k,
                                 ref=O + "#Input skeleton"))
            elif inp.path is not None:
                p = (inp.path.parent / args[2].strip("\"'"))
                if p.is_file():
                    _xyz_atoms(inp, p.read_text(errors="replace"))
                else:
                    f.append(Finding("warning", "orca-xyzfile", f"Geometry file {args[2]} not found next to the "
                                     "input (fine if the submit script copies it).", k, ref=O + "#Input skeleton"))
        elif typ == "xyz":
            ok = True
            for s, ln in atoms_lines:
                a, rec = parse_atom_line(s, ln)
                if not rec:
                    tok = split_tokens(s)
                    if tok and re.match(r"^[A-Za-z]{1,3}\d*$", tok[0]):
                        inp.extra.setdefault("unknown_labels", []).append((ln, tok[0]))
                    ok = False
                    continue
                inp.atoms.append(a)
            inp.geometry_complete = ok and bool(inp.atoms)
            if not atoms_lines:
                f.append(Finding("error", "orca-geometry", "Empty '* xyz' block.", k, ref=O + "#Input skeleton"))
        else:
            for s, ln in atoms_lines:
                a, rec = parse_atom_line(s, ln)
                if rec:
                    a.xyz = None
                    inp.atoms.append(a)
            inp.geometry_complete = False
    elif "coords" in blocks:
        txt = blocks["coords"][0]
        mc = re.search(r"\bcharge\s+(-?\d+)", txt, re.I)
        mm = re.search(r"\bmult\s+(\d+)", txt, re.I)
        if mc and mm:
            inp.charge, inp.multiplicity = int(mc.group(1)), int(mm.group(1))
    elif not re.search(r"\$new_job", inp.content):
        f.append(Finding("error", "orca-geometry", "No geometry: expected '* xyz charge mult' ... '*' or "
                         "'* xyzfile charge mult file.xyz'.", None, ref=O + "#Input skeleton"))
    if "bohrs" in kwl or re.search(r"\bunits\s+bohrs?\b", blocks.get("coords", ("", 0))[0], re.I):
        inp.units = "bohr"
    # ---------------- method / basis / aux basis
    from ..basis import bse_available, resolve_basis

    have_bse = bse_available()
    for t, k in kw:
        tl = t.lower()
        if re.search(r"/(c|j|jk)$", tl) or tl.endswith("-cabs"):
            inp.aux_basis.append(t)
            continue
        inp.extra.setdefault("method_tokens", []).append((t, k))
        if inp.basis is None and have_bse:
            try:
                r = resolve_basis(t)
            except Exception:  # noqa: BLE001
                r = {"key": None}
            if r.get("key") and r.get("role") == "orbital":
                inp.basis = t
                inp.extra["basis_line"] = k
    # ---------------- memory / cores
    mc = blocks.get("maxcore")
    pal = blocks.get("pal")
    pal_n = None
    if pal:
        m = re.search(r"\bnprocs\s*=?\s*(\d+)", pal[0], re.I)
        if m:
            pal_n = int(m.group(1))
    bang_n = None
    bang_line = None
    for t, k in kw:
        m = re.fullmatch(r"pal(\d+)", t.lower())
        if m:
            bang_n, bang_line = int(m.group(1)), k
    if pal_n and bang_n and pal_n != bang_n:
        f.append(Finding("error", "orca-nprocs-conflict", f"%pal nprocs {pal_n} and !PAL{bang_n} disagree.", bang_line,
                         fix="keep only one of them (%pal nprocs N end always works)", ref=O + "#Memory and cores (gotcha)"))
    inp.nprocs = pal_n or bang_n or 1
    if mc is None:
        f.append(Finding("warning", "orca-maxcore-missing", "No %maxcore: ORCA falls back to its small default "
                         "memory per core.", None,
                         fix="%maxcore 3000   (MB PER CORE, ~75% of the memory per core you request)",
                         ref=O + "#Memory and cores (gotcha)"))
    else:
        m = re.match(r"^([\d.]+)", mc[0])
        if not m:
            f.append(Finding("error", "orca-maxcore", f"Cannot read %maxcore {mc[0]}.", mc[1], ref=O + "#Memory and cores (gotcha)"))
        else:
            v = float(m.group(1))
            inp.memory_per_core_mb = v
            inp.memory_total_mb = v * inp.nprocs
            if v < 250:
                f.append(Finding("warning", "orca-maxcore-small", f"%maxcore {m.group(1)} is only {v:.0f} MB per core; "
                                 "%maxcore is in MB (was GB meant?).", mc[1], fix=f"%maxcore {int(v * 1000) if v < 64 else 3000}",
                                 ref=O + "#Memory and cores (gotcha)"))
            elif v >= 32000 and inp.nprocs > 1:
                f.append(Finding("warning", "orca-maxcore-total", f"%maxcore {m.group(1)} MB is PER CORE: with "
                                 f"{inp.nprocs} processes ORCA may use {fmt_mb(v * inp.nprocs)}. Was the total memory meant?",
                                 mc[1], fix=f"%maxcore {int(v / inp.nprocs * 0.75)}  (total/nprocs x 0.75)",
                                 ref=O + "#Memory and cores (gotcha)"))
    # ---------------- aux basis for correlation
    corr = [t for t, _ in kw if re.match(r"^(dlpno-|ri-mp2|ri-scs-mp2|ri-sos-mp2|rijk-mp2|local-mp2|dlpno)", t.lower())]
    dh = [t for t, _ in kw if ORCA_DOUBLE_HYBRIDS.match(t.lower()) and not t.lower().startswith(("scs-", "sos-"))]
    has_c = any(t.lower().endswith("/c") for t in inp.aux_basis) or "autoaux" in kwl or re.search(
        r"\bauxc\b", blocks.get("basis", ("", 0))[0], re.I)
    if corr and not has_c:
        f.append(Finding("error", "orca-aux-c", f"{corr[0]} needs a correlation auxiliary basis (/C) but none is given.",
                         first_bang, fix="add e.g. cc-pVTZ/C or def2-TZVP/C to the ! line (matching the orbital basis), "
                                         "or AutoAux", ref=O + "#Coupled cluster"))
    elif dh and not has_c:
        f.append(Finding("warning", "orca-aux-c", f"Double hybrid {dh[0]} needs a /C auxiliary basis for its RI-MP2 part.",
                         first_bang, fix="add e.g. def2-TZVP/C (matching the orbital basis)", ref=REF["levels"] + "#B2PLYP"))
    # ---------------- MORead
    if "moread" in kwl:
        mo = blocks.get("moinp")
        if not mo:
            f.append(Finding("warning", "orca-moread", "MORead without %moinp \"file.gbw\".", first_bang,
                             ref=O + "#Restarting / reading orbitals and Hessians"))
        elif inp.filename:
            gbw = Path(mo[0].strip().strip("\"'")).name
            if Path(gbw).stem == Path(inp.filename).stem:
                f.append(Finding("error", "orca-moinp-same-name", f"%moinp {mo[0]} has the same basename as this input "
                                 f"({inp.filename}); ORCA overwrites {Path(inp.filename).stem}.gbw when the job starts.",
                                 mo[1], fix=f"rename the old file (e.g. {Path(gbw).stem}_guess.gbw) and point %moinp to it",
                                 ref=O + "#Restarting / reading orbitals and Hessians"))
    # ---------------- ORCA 4 grid keywords
    for t, k in kw:
        if re.fullmatch(r"(final|cosx|grid)?grid\d|finalgridx?\d|gridx\d", t.lower()):
            f.append(Finding("warning", "orca-old-grid", f"'{t}' is an ORCA 4 grid keyword; ORCA 5/6 use "
                             "DefGrid1/DefGrid2/DefGrid3.", k, fix="replace with DefGrid2 (default) or DefGrid3",
                             ref=O + "#DFT defaults that changed"))
    # ---------------- job type
    inp.job_type = ("ts" if kwl & {"optts", "neb-ts", "scants"} else "irc" if "irc" in kwl else
                    "opt" if kwl & {"opt", "copt", "zopt", "gdiis-opt", "tightopt", "looseopt", "verytightopt"} else
                    "freq" if kwl & {"freq", "numfreq", "anfreq"} else "sp")
    inp.method = next((t for t, _ in inp.extra.get("method_tokens", [])
                       if t != inp.basis and t.lower() not in ORCA_JOB_WORDS and not re.fullmatch(r"pal\d+", t.lower())),
                      None)
    return f


def _xyz_atoms(inp: ParsedInput, text: str) -> None:
    ok = True
    atoms = []
    rows = text.splitlines()
    if rows and is_int(rows[0].strip() or "x"):
        rows = rows[2:]
    for s in rows:
        if not s.strip():
            continue
        a, rec = parse_atom_line(s, 0)
        if not rec or a.xyz is None:
            ok = False
            continue
        a.line = None
        atoms.append(a)
    inp.atoms = atoms
    inp.geometry_complete = ok and bool(atoms)


# ================================================================== Q-Chem

QCHEM_JOBTYPES = {"sp", "opt", "ts", "freq", "force", "rpath", "nmr", "issc", "bsse", "eda", "pes_scan", "aimd",
                  "aifdem", "fsm", "gsm", "mm", "xsas", "optimization", "frequency", "frequencies", "gradient",
                  "polarizability", "static_polarizability", "freezing_string", "dpes", "ispes", "lr_mm"}
QCHEM_WRONG_JOBTYPES = {"energy": "sp", "single_point": "sp", "singlepoint": "sp", "optts": "ts",
                        "transition_state": "ts", "hessian": "freq"}
QCHEM_SELF_DISP = re.compile(r"^(wb97x-d|wb97x-v|wb97m-v)$")


def analyze_qchem(inp: ParsedInput) -> list[Finding]:
    f: list[Finding] = []
    lines = inp.lines
    jobs: list[list[tuple[int, str]]] = [[]]
    for k, ln in enumerate(lines, 1):
        if re.fullmatch(r"\s*@@@\s*", ln):
            jobs.append([])
        else:
            jobs[-1].append((k, ln))
    inp.jobs = len(jobs)
    for j, job in enumerate(jobs):
        f += _qchem_job(inp, job, j)
    return f


def _qchem_job(inp: ParsedInput, job: list[tuple[int, str]], j: int) -> list[Finding]:
    f: list[Finding] = []
    sections: dict[str, tuple[int, list[tuple[int, str]]]] = {}
    cur, cur_line, body = None, None, []
    for k, raw in job:
        ln = raw.split("!")[0].rstrip()
        s = ln.strip()
        if not s:
            continue
        if s.startswith("$"):
            name = s[1:].split()[0].lower() if len(s) > 1 else ""
            if name == "end":
                if cur is None:
                    f.append(Finding("error", "qchem-section", "$end without an open $section.", k, ref=QC + "#Input skeleton"))
                else:
                    sections[cur] = (cur_line, body)
                    cur = None
                continue
            if cur is not None:
                f.append(Finding("error", "qchem-section", f"${cur} (line {cur_line}) is not closed with $end before "
                                 f"${name}.", k, fix=f"add $end after the ${cur} section", ref=QC + "#Input skeleton"))
                sections[cur] = (cur_line, body)
            cur, cur_line, body = name, k, []
            continue
        if cur is not None:
            body.append((k, s))
    if cur is not None:
        f.append(Finding("error", "qchem-section", f"${cur} (line {cur_line}) is not closed with $end.", cur_line,
                         fix=f"add $end after the ${cur} section", ref=QC + "#Input skeleton"))
        sections[cur] = (cur_line, body)
    first_line = job[0][0] if job else None
    label = f" (job {j + 1})" if inp.jobs > 1 else ""
    if not job or not any(ln.strip() for _, ln in job):
        f.append(Finding("error", "qchem-empty-job", f"Empty job{label} (stray @@@?).", first_line, ref=QC + "#Multi-step jobs (@@@)"))
        return f
    # $molecule
    if "molecule" not in sections:
        f.append(Finding("error", "qchem-molecule", f"No $molecule section{label} (later @@@ jobs use "
                         "'$molecule / read / $end').", first_line, ref=QC + "#Multi-step jobs (@@@)"))
    else:
        mline, mbody = sections["molecule"]
        if not mbody:
            f.append(Finding("error", "qchem-molecule", f"Empty $molecule section{label}.", mline, ref=QC + "#Input skeleton"))
        elif mbody[0][1].lower() == "read":
            if j == 0:
                f.append(Finding("info", "qchem-read-first", "'$molecule read' in the first job reads a saved scratch "
                                 "directory (qchem -save / -nt ... savedir); make sure it exists.", mbody[0][0],
                                 ref=QC + "#Multi-step jobs (@@@)"))
        elif j == 0:
            tok = split_tokens(mbody[0][1])
            if len(tok) == 2 and all(is_int(t) for t in tok):
                inp.charge, inp.multiplicity = int(tok[0]), int(tok[1])
                inp.extra["charge_line"] = mbody[0][0]
                ok, frag = True, False
                for k, s in mbody[1:]:
                    if s.startswith("--"):
                        frag = True
                        continue
                    t2 = split_tokens(s)
                    if len(t2) == 2 and all(is_int(t) for t in t2):
                        continue  # fragment charge/mult
                    a, rec = parse_atom_line(s, k)
                    if not rec:
                        if t2 and re.match(r"^[A-Za-z]{1,3}\d*$", t2[0]) and "=" not in s:
                            inp.extra.setdefault("unknown_labels", []).append((k, t2[0]))
                        ok = False
                        continue
                    inp.atoms.append(a)
                inp.geometry_complete = ok and bool(inp.atoms)
                inp.extra["fragments"] = frag
            else:
                f.append(Finding("error", "qchem-charge-mult", "The first line of $molecule must be 'charge "
                                 f"multiplicity' (or 'read'), got '{mbody[0][1]}'.", mbody[0][0], ref=QC + "#Input skeleton"))
    # $rem
    if "rem" not in sections:
        f.append(Finding("error", "qchem-rem", f"No $rem section{label}.", first_line, ref=QC + "#Input skeleton"))
        return f
    rline, rbody = sections["rem"]
    rem: dict[str, tuple[str, int]] = {}
    for k, s in rbody:
        m = re.match(r"^(\w+)\s*(?:=\s*|\s+)(\S.*)$", s)
        if m:
            rem[m.group(1).upper()] = (m.group(2).strip(), k)
        else:
            f.append(Finding("info", "qchem-rem-line", f"Cannot read $rem line '{s}' (expected 'KEYWORD value').", k,
                             ref=QC + "#Input skeleton"))
    if j == 0:
        inp.extra["rem"] = {k: v[0] for k, v in rem.items()}
    sev = "error" if j == 0 else "warning"
    method = rem.get("METHOD") or rem.get("EXCHANGE")
    if not method:
        f.append(Finding(sev, "qchem-method", f"$rem{label} has neither METHOD nor EXCHANGE.", rline,
                         fix="METHOD  wB97X-D  (or EXCHANGE/CORRELATION)", ref=QC + "#Input skeleton"))
    basis = rem.get("BASIS")
    if not basis and not (method and re.search(r"-3c$", method[0], re.I)):
        f.append(Finding(sev, "qchem-basis", f"$rem{label} has no BASIS.", rline, fix="BASIS  def2-TZVP",
                         ref=QC + "#Input skeleton"))
    if basis and basis[0].lower() in ("gen", "mixed") and "basis" not in sections:
        f.append(Finding("error", "qchem-gen-basis", f"BASIS {basis[0]} needs a $basis section.", basis[1],
                         ref=QC + "#Input skeleton"))
    jt = rem.get("JOBTYPE")
    if jt:
        v = jt[0].lower()
        if v in QCHEM_WRONG_JOBTYPES:
            f.append(Finding("warning", "qchem-jobtype", f"JOBTYPE {jt[0]} is not a Q-Chem job type; use "
                             f"{QCHEM_WRONG_JOBTYPES[v]}.", jt[1], fix=f"JOBTYPE {QCHEM_WRONG_JOBTYPES[v]}",
                             ref=QC + "#Input skeleton"))
        elif v not in QCHEM_JOBTYPES:
            f.append(Finding("info", "qchem-jobtype", f"JOBTYPE {jt[0]} is not one of the common job types (sp, opt, "
                             "ts, freq, force, rpath, ...); check the manual.", jt[1], ref=QC + "#Input skeleton"))
    if j == 0:
        if method:
            inp.method = method[0]
            inp.extra.setdefault("method_tokens", []).append((method[0], method[1]))
        if basis:
            inp.basis = basis[0]
            inp.extra["basis_line"] = basis[1]
        inp.job_type = (jt[0].lower() if jt else "sp")
        if "INPUT_BOHR" in rem and rem["INPUT_BOHR"][0].lower() in ("true", "1"):
            inp.units = "bohr"
    mem = rem.get("MEM_TOTAL")
    if mem:
        if is_float(mem[0]):
            if j == 0 or inp.memory_total_mb is None:
                inp.memory_total_mb = float(mem[0])
        else:
            f.append(Finding("error", "qchem-mem", f"MEM_TOTAL {mem[0]} must be a number (MB).", mem[1], ref=QC + "#Memory and cores (gotcha)"))
    elif j == 0:
        f.append(Finding("warning", "qchem-mem-missing", "No MEM_TOTAL in $rem; Q-Chem's default total memory is "
                         "small.", rline, fix="MEM_TOTAL  56000   (MB, TOTAL for the job; ~85-90% of the allocation)",
                         ref=QC + "#Memory and cores (gotcha)"))
    dftd = rem.get("DFT_D")
    if dftd and method and QCHEM_SELF_DISP.match(method[0].lower()) and dftd[0].lower() not in ("false", "0", "none"):
        f.append(Finding("warning", "qchem-double-dispersion", f"{method[0]} already includes its own dispersion/"
                         f"non-local term; DFT_D {dftd[0]} adds it twice.", dftd[1], fix="remove DFT_D",
                         ref=QC + "#DFT, dispersion, solvation"))
    if j == 0:
        for key in ("SCF_GUESS", "GEOM_OPT_HESSIAN"):
            if key in rem and rem[key][0].lower() == "read":
                f.append(Finding("info", "qchem-read-first", f"{key} read in the first job needs a saved scratch "
                                 "directory from a previous run.", rem[key][1], ref=QC + "#Multi-step jobs (@@@)"))
    solv = rem.get("SOLVENT_METHOD")
    if solv and solv[0].lower() == "smd" and "smx" not in sections:
        f.append(Finding("info", "qchem-smx", "SOLVENT_METHOD SMD without a $smx section (solvent defaults to water).",
                         solv[1], fix="$smx / solvent water / $end", ref=QC + "#DFT, dispersion, solvation"))
    return f


# ================================================================== Molpro


def analyze_molpro(inp: ParsedInput) -> list[Finding]:
    f: list[Finding] = []
    lines = [ln.split("!")[0] for ln in inp.lines]
    text = "\n".join(lines)
    low = text.lower()
    # memory
    m = None
    for k, ln in enumerate(lines, 1):
        m = re.match(r"^\s*memory\s*,\s*([\d.]+)\s*(?:,\s*([a-z]+))?", ln, re.I)
        if m:
            n = float(m.group(1))
            unit = (m.group(2) or "").lower()
            scale = {"": 1, "k": 1e3, "m": 1e6, "g": 1e9, "t": 1e12}.get(unit)
            if scale is None:
                f.append(Finding("info", "molpro-memory", f"Unrecognised memory unit '{m.group(2)}'.", k, ref=MP + "#Memory (gotcha)"))
                break
            mb = n * scale * 8 / 2**20
            inp.memory_per_process_mb = mb
            if n * scale * 8 >= 64e9:  # >= 64 GB per process: memory,8000,m (= 64 GB) is the classic "meant 8 GB"
                f.append(Finding("warning", "molpro-memory-large",
                                 f"memory,{m.group(1)},{m.group(2) or ''} = {fmt_mb(mb)} PER PROCESS (units are 8-byte "
                                 "words). Was GB meant?", k,
                                 fix=f"memory,{int(n / 8)},{unit or 'm'} gives {fmt_mb(mb / 8)} per process",
                                 ref=MP + "#Memory (gotcha)"))
            break
    else:
        f.append(Finding("info", "molpro-memory-missing", "No 'memory,N,m' card: Molpro uses its small default "
                         "(or the -m command-line value).", None, ref=MP + "#Memory (gotcha)"))
    # geometry
    gm = re.search(r"^\s*geometry\s*=\s*\{(.*?)\}", text, re.M | re.S | re.I)
    if gm:
        start_line = text[: gm.start(1)].count("\n") + 1
        body = gm.group(1).splitlines()
        rows = [(start_line + i, r.strip()) for i, r in enumerate(body) if r.strip()]
        if rows and is_int(rows[0][1]):
            rows = rows[2:]
        ok = True
        for k, s in rows:
            for part in [p for p in s.split(";") if p.strip()]:
                pl = part.strip().lower()
                if pl in ("angstrom", "bohr", "nosym", "noorient", "symmetry,nosym") or "=" in pl:
                    if pl == "bohr":
                        inp.units = "bohr"
                    continue
                a, rec = parse_atom_line(part, k)
                if not rec:
                    ok = False
                    continue
                inp.atoms.append(a)
        inp.geometry_complete = ok and bool(inp.atoms)
    else:
        gf = re.search(r"^\s*geometry\s*=\s*([^\s{;]+)", text, re.M | re.I)
        if gf and inp.path is not None:
            p = inp.path.parent / gf.group(1).strip("\"'")
            if p.is_file() and p.suffix.lower() == ".xyz":
                _xyz_atoms(inp, p.read_text(errors="replace"))
    # basis
    bm = re.search(r"^\s*basis\s*=\s*([^\s;{!,]+)", text, re.M | re.I) or re.search(
        r"^\s*basis\s*=\s*\{[^}]*?default\s*=\s*([^\s;,}]+)", text, re.M | re.I | re.S)
    if bm:
        inp.basis = bm.group(1)
        inp.extra["basis_line"] = text[: bm.start()].count("\n") + 1
    # charge / spin / wf
    charge = spin = nelec = None
    line_cs = None
    for k, ln in enumerate(lines, 1):
        for part in ln.split(";"):
            pl = part.strip().lower()
            mc = re.match(r"^set\s*,\s*charge\s*=\s*([+-]?\d+)", pl)
            if mc:
                charge, line_cs = int(mc.group(1)), k
            ms = re.match(r"^set\s*,\s*spin\s*=\s*(\d+)", pl)
            if ms:
                spin, line_cs = int(ms.group(1)), k
            mw = re.search(r"(?:^|[{\s])wf\s*,\s*([^}]*)", pl)
            if mw:
                args = [a.strip() for a in mw.group(1).split(",")]
                pos = [a for a in args if a and "=" not in a]
                for a in args:
                    if a.startswith("spin="):
                        spin, line_cs = int(re.sub(r"\D", "", a) or 0), k
                    if a.startswith("charge="):
                        charge, line_cs = int(a.split("=")[1]), k
                    if a.startswith(("nelec=", "elec=")):
                        nelec = int(a.split("=")[1])
                if len(pos) >= 3 and all(is_int(x) for x in pos[:3]):
                    nelec, spin, line_cs = int(pos[0]), int(pos[2]), k
    inp.extra["charge_line"] = line_cs
    inp.charge = charge if charge is not None else 0
    if spin is not None:
        inp.spin_2s = spin
        inp.multiplicity = None
        if nelec is not None and (nelec - spin) % 2:
            f.append(Finding("error", "parity", f"wf card: {nelec} electrons cannot have spin (2S) = {spin}.", line_cs,
                             fix="spin in wf/set,spin is 2S (doublet 1, triplet 2), not the multiplicity",
                             ref=MP + "#Spin and symmetry (gotcha)"))
            inp.extra["parity_done"] = True
        elif nelec is not None:
            inp.extra["parity_done"] = True  # explicit nelec already consistent
    # method tokens: {rks,b3lyp} etc. are not checked against the levels table (support mostly unknown).
    m2 = re.findall(r"\{\s*([a-z][\w()\-]*)", low)
    if m2:
        inp.method = m2[-1]
    inp.job_type = "opt" if "optg" in low else "freq" if "frequencies" in low else "sp"
    return f


# ================================================================== Psi4


def _psi4_mol_block(inp: ParsedInput, body: str, start_line: int, f: list[Finding]) -> None:
    rows = body.splitlines()
    ok, frag, charge_set = True, False, False
    for i, raw in enumerate(rows):
        k = start_line + i
        s = raw.split("#")[0].strip()
        if not s:
            continue
        sl = s.lower()
        if sl.startswith("--"):
            frag = True
            continue
        if re.match(r"^(units|unit)\s+(bohr|au|a\.u\.)", sl):
            inp.units = "bohr"
            continue
        if re.match(r"^(units|unit|symmetry|no_com|nocom|no_reorient|noreorient|pubchem|efp|fix_com|fix_orientation)\b",
                    sl) or "=" in s:
            if sl.startswith("pubchem"):
                ok = False
            continue
        tok = split_tokens(s)
        if len(tok) == 2 and all(is_int(t) for t in tok):
            if not charge_set and not inp.atoms:
                inp.charge, inp.multiplicity = int(tok[0]), int(tok[1])
                inp.extra["charge_line"] = k
                charge_set = True
            continue
        a, rec = parse_atom_line(s, k)
        if not rec:
            if tok and re.match(r"^[A-Za-z]{1,3}\d*$", tok[0]):
                inp.extra.setdefault("unknown_labels", []).append((k, tok[0]))
            ok = False
            continue
        inp.atoms.append(a)
    if frag:
        inp.extra["fragments"] = True
        ok = False  # per-fragment charges/multiplicities: skip the parity check
    if not charge_set:
        inp.charge = 0 if inp.charge is None else inp.charge
        inp.extra["default_charge_mult"] = True
    inp.geometry_complete = ok and bool(inp.atoms) and charge_set


def _arith(expr: str) -> float | None:
    """Evaluate a constant arithmetic expression like 1024 * 1024 * 1024 or int(5e8) (else None)."""
    try:
        node = ast.parse(expr.strip(), mode="eval").body
    except SyntaxError:
        return None

    def ev(n):
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)):
            return float(n.value)
        if isinstance(n, ast.BinOp) and isinstance(n.op, (ast.Mult, ast.Add, ast.Sub, ast.Pow, ast.Div)):
            a, b = ev(n.left), ev(n.right)
            return {ast.Mult: a * b, ast.Add: a + b, ast.Sub: a - b, ast.Div: a / b if b else 0.0,
                    ast.Pow: a ** b if b < 20 else 0.0}[type(n.op)]
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in ("int", "float") and n.args:
            return ev(n.args[0])
        raise ValueError

    try:
        return ev(node)
    except (ValueError, TypeError, KeyError, OverflowError):
        return None


def analyze_psi4(inp: ParsedInput) -> list[Finding]:
    f: list[Finding] = []
    text = inp.content
    is_py =bool(re.search(r"^\s*(import|from)\s+psi4\b", text, re.M))
    # geometry
    if is_py:
        m = re.search(r"geometry\(\s*[rRuU]?(\"\"\"|''')(.*?)\1", text, re.S)
        if m:
            _psi4_mol_block(inp, m.group(2), text[: m.start(2)].count("\n") + 1, f)
    else:
        m = re.search(r"^\s*molecule\s*\w*\s*\{(.*?)^\s*\}", text, re.S | re.M)
        if m:
            _psi4_mol_block(inp, m.group(1), text[: m.start(1)].count("\n") + 1, f)
    # memory
    mem_line = None
    mm = re.search(r"^\s*memory\s+([\d.]+)\s*([a-z]*)", text, re.M | re.I) if not is_py else None
    mp = re.search(r"set_memory\(([^)]*\)?)\)", text, re.I)
    if mm:
        inp.memory_total_mb = mem_to_mb(float(mm.group(1)), mm.group(2) or "b")
        mem_line = text[: mm.start()].count("\n") + 1
    elif mp:
        arg = mp.group(1).split(",")[0].strip()
        ms = re.fullmatch(r"[\"']\s*([\d.]+)\s*([a-zA-Z]*)\s*[\"']", arg)
        if ms:
            inp.memory_total_mb = mem_to_mb(float(ms.group(1)), ms.group(2) or "b")
        else:
            nbytes = _arith(arg)
            inp.memory_total_mb = nbytes / 2**20 if nbytes is not None else None
        mem_line = text[: mp.start()].count("\n") + 1
    else:
        f.append(Finding("warning", "psi4-memory-missing", "Memory is not set; Psi4's default is only ~500 MiB.",
                         None, fix="psi4.set_memory(\"16 GB\")  /  memory 16 GB (psithon)", ref=P4 + "#PsiAPI (Python) skeleton"))
    if inp.memory_total_mb is not None and inp.memory_total_mb < 1 and mem_line:
        f.append(Finding("warning", "psi4-memory-small", "Psi4 memory below 1 MB: a bare number is bytes.", mem_line,
                         fix="psi4.set_memory(\"16 GB\")", ref=P4 + "#PsiAPI (Python) skeleton"))
    # threads
    mt = re.search(r"set_num_threads\(\s*(\d+)", text)
    if mt:
        inp.nprocs = int(mt.group(1))
    # reference
    refs = [m_.group(1).lower() for m_ in re.finditer(r"reference[\"']?\s*[:,=\s]\s*[\"']?(\w+)", text, re.I)]
    reference = next((r for r in refs if r in ("uhf", "uks", "rohf", "roks", "cuhf", "rhf", "rks")), None)
    inp.extra["reference"] = reference
    if inp.multiplicity and inp.multiplicity > 1 and reference not in ("uhf", "uks", "rohf", "roks", "cuhf"):
        f.append(Finding("warning", "psi4-reference", f"Open-shell molecule (multiplicity {inp.multiplicity}) but "
                         f"reference is {reference or 'not set (default RHF/RKS)'}.", inp.extra.get("charge_line"),
                         fix="set reference uhf (or uks / rohf)", ref=P4 + "#Gotchas"))
    # basis / method
    bm = (re.search(r"[\"']basis[\"']\s*:\s*[\"']([^\"']+)", text, re.I)
          or re.search(r"^\s*(?:set\s+)?basis\s+([^\s{}]+)", text, re.M | re.I))
    calls = list(re.finditer(r"\b(energy|optimize|opt|gradient|frequency|frequencies|freq|hessian|properties|prop)\(\s*"
                             r"[\"']([^\"']+)[\"']", text))
    if calls:
        name = calls[0].group(2)
        meth, _, b = name.partition("/")
        inp.method = meth
        if b and not bm:
            inp.basis = b
        for c in calls:
            inp.extra.setdefault("method_tokens", []).append((c.group(2).split("/")[0], text[: c.start()].count("\n") + 1))
        fn = calls[0].group(1)
        inp.job_type = {"optimize": "opt", "opt": "opt", "frequency": "freq", "frequencies": "freq", "freq": "freq",
                        "hessian": "freq", "gradient": "force"}.get(fn, "sp")
    if bm:
        inp.basis = bm.group(1)
        inp.extra["basis_line"] = text[: bm.start()].count("\n") + 1
    if re.search(r"[\"']?opt_type[\"']?\s*[:\s]\s*[\"']?ts", text, re.I):
        inp.job_type = "ts"
    return f


# ================================================================== PySCF


def _literal(node):
    try:
        return ast.literal_eval(node)
    except Exception:  # noqa: BLE001
        return _Unknown


class _Unknown:  # noqa: D101 - sentinel
    pass


def analyze_pyscf(inp: ParsedInput) -> list[Finding]:
    f: list[Finding] = []
    try:
        tree = ast.parse(inp.content)
    except SyntaxError as e:
        return [Finding("error", "python-syntax", f"Python syntax error: {e.msg}.", e.lineno, ref=PY + "#Skeleton")]
    mol_kw: dict[str, tuple[object, int]] = {}
    attrs: dict[str, tuple[object, int]] = {}  # mol.spin = ... etc. (applied after the gto.M keywords)
    xc: list[tuple[str, int]] = []
    if re.search(r"\bpyscf\.pbc\b|\bfrom\s+pyscf\s+import\s+.*\bpbc\b|\bpbc\.gto\b", inp.content):
        inp.extra["pbc"] = True  # periodic cells: k-points/smearing, no molecular electron-count rules
        return f
    mol_names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            fn = node.value.func
            name = fn.attr if isinstance(fn, ast.Attribute) else fn.id if isinstance(fn, ast.Name) else ""
            if name in ("M", "Mole"):
                mol_names |= {t.id for t in node.targets if isinstance(t, ast.Name)}
                if any(kw.arg == "a" for kw in node.value.keywords):
                    inp.extra["pbc"] = True
                    return f
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else fn.id if isinstance(fn, ast.Name) else ""
            owner = fn.value.id if isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name) else ""
            if (name in ("M", "Mole") and owner in ("gto", "pyscf", "")) and "_call" not in mol_kw:
                for kw in node.keywords:
                    if kw.arg:
                        mol_kw[kw.arg] = (_literal(kw.value), node.lineno)
                if node.args and name == "M" and "atom" not in mol_kw:
                    mol_kw["atom"] = (_literal(node.args[0]), node.lineno)
                mol_kw.setdefault("_call", (None, node.lineno))
            if name in ("RKS", "UKS", "ROKS", "KS", "GKS"):
                for kw in node.keywords:
                    if kw.arg == "xc":
                        v = _literal(kw.value)
                        if isinstance(v, str):
                            xc.append((v, node.lineno))
            if name == "num_threads" and node.args:
                v = _literal(node.args[0])
                if isinstance(v, int):
                    inp.nprocs = v
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Attribute):
                    v = _literal(node.value)
                    if t.attr == "xc" and isinstance(v, str):
                        xc.append((v, node.lineno))
                    elif t.attr in ("atom", "spin", "charge", "basis", "unit", "max_memory") and isinstance(
                            t.value, ast.Name) and (t.value.id in mol_names or not mol_names or t.attr == "max_memory"):
                        if t.attr not in attrs or node.lineno > attrs[t.attr][1]:
                            attrs[t.attr] = (v, node.lineno)
    mol_kw.update(attrs)
    if "_call" not in mol_kw and "atom" not in mol_kw:
        return f
    call_line = mol_kw.get("_call", (None, None))[1]
    charge = mol_kw.get("charge", (0, None))[0]
    spin = mol_kw.get("spin", (0, None))[0]
    inp.charge = charge if isinstance(charge, int) else None
    inp.spin_2s = spin if isinstance(spin, int) else None
    inp.extra["charge_line"] = mol_kw.get("spin", mol_kw.get("charge", (None, call_line)))[1]
    unit = mol_kw.get("unit", ("angstrom", None))[0]
    if isinstance(unit, str) and unit.lower() in ("b", "bohr", "au", "a.u."):
        inp.units = "bohr"
    basis = mol_kw.get("basis", (None, None))
    if isinstance(basis[0], str):
        inp.basis = basis[0]
        inp.extra["basis_line"] = basis[1]
    atom = mol_kw.get("atom", (None, None))
    _pyscf_atoms(inp, atom[0], atom[1] or call_line)
    mm = mol_kw.get("max_memory")
    if mm is not None and isinstance(mm[0], (int, float)):
        inp.memory_total_mb = float(mm[0])
    else:
        f.append(Finding("info", "pyscf-max-memory", "mol.max_memory is not set; PySCF's default is 4000 MB.",
                         call_line, fix="mol.max_memory = 16000  # MB (~85-90% of the allocation)", ref=PY + "#Skeleton"))
    for v, ln in xc:
        inp.extra.setdefault("method_tokens", []).append((v, ln))
    if xc:
        inp.method = xc[0][0]
    if inp.spin_2s is None and "spin" not in mol_kw:
        inp.spin_2s = 0
    return f


def _pyscf_atoms(inp: ParsedInput, atom, line: int | None) -> None:
    if atom is None or atom is _Unknown:
        return
    rows: list = []
    if isinstance(atom, str):
        if re.search(r"\.(xyz|zmat)$", atom.strip(), re.I) and "\n" not in atom:
            p = (inp.path.parent / atom.strip()) if inp.path else None
            if p is not None and p.is_file():
                _xyz_atoms(inp, p.read_text(errors="replace"))
            return
        rows = [r for r in re.split(r"[;\n]", atom) if r.strip()]
        ok = True
        for r in rows:
            a, rec = parse_atom_line(r, line)
            if not rec or a.xyz is None:
                tok = split_tokens(r)
                if tok and not rec and re.match(r"^[A-Za-z]{1,3}\d*$", tok[0]):
                    inp.extra.setdefault("unknown_labels", []).append((line, tok[0]))
                ok = False
                if a is not None and rec:
                    inp.atoms.append(a)
                continue
            inp.atoms.append(a)
        inp.geometry_complete = ok and bool(inp.atoms)
        return
    if isinstance(atom, (list, tuple)):
        ok = True
        for item in atom:
            if not isinstance(item, (list, tuple)) or not item or not isinstance(item[0], (str, int)):
                ok = False
                continue
            lab = str(item[0])
            coords = item[1] if len(item) == 2 and isinstance(item[1], (list, tuple)) else item[1:4]
            sym, ghost = parse_label(lab)
            if sym is None:
                inp.extra.setdefault("unknown_labels", []).append((line, lab))
                ok = False
                continue
            try:
                xyz = tuple(float(c) for c in coords)
            except (TypeError, ValueError):
                xyz = None
            inp.atoms.append(Atom(symbol=sym, xyz=xyz if xyz and len(xyz) == 3 else None, line=line, ghost=ghost,
                                  label=lab))
        inp.geometry_complete = ok and bool(inp.atoms)


ANALYZERS = {
    "gaussian": analyze_gaussian,
    "orca": analyze_orca,
    "qchem": analyze_qchem,
    "molpro": analyze_molpro,
    "psi4": analyze_psi4,
    "pyscf": analyze_pyscf,
}
