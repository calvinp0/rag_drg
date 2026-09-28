"""Element data, geometry parsing and program-independent checks."""

from __future__ import annotations

import math
import re
from functools import lru_cache

from ..basis import ELEMENTS, Z_OF
from .model import REF, Atom, Finding, ParsedInput

BOHR_TO_ANG = 0.529177210903
MIN_DIST_ANG = 0.5

# Labels that are never atoms with electrons.
DUMMY_LABELS = {"x", "xx", "bq", "da", "q", "tv", "gh", "dummy"}


def is_float(s: str) -> bool:
    try:
        float(s.replace("d", "e").replace("D", "e"))
    except ValueError:
        return False
    return True


def to_float(s: str) -> float:
    return float(s.replace("d", "e").replace("D", "e"))


def is_int(s: str) -> bool:
    return re.fullmatch(r"[+-]?\d+", s.strip()) is not None


def parse_label(label: str) -> tuple[str | None, bool]:
    """Atom label as written in any of the supported codes -> (element or None, is_ghost).

    Handles C1, CL, Cl12, 6 (atomic number), C(Iso=13), C(Fragment=1), C(1), C-CA-0.5 (Gaussian
    MM types), H: (ORCA ghost), @H / Gh(H) (Q-Chem/Psi4 ghosts), H-Bq / Bq, X / DA / Q (dummies),
    ghost-H / X-H (PySCF), H@2 (PySCF labels). Returns (None, False) for unknown labels.
    """
    s = label.strip().strip("'\"")
    if not s:
        return None, False
    low = s.lower()
    m = re.fullmatch(r"gh\((\w+)\)", low)
    if m:
        return _element(m.group(1)), True
    if s.startswith("@"):
        return _element(s[1:]), True
    if low.startswith("ghost"):
        return _element(re.sub(r"^ghost[-_:]?", "", low)), True
    if s.endswith(":"):
        return _element(s[:-1]), True
    if low == "bq" or low.endswith("-bq") or low.startswith("bq"):
        return "", True
    if low.startswith("x-") or low.startswith("x_"):
        return _element(low[2:]), True
    base = re.split(r"[(\-@]", s, maxsplit=1)[0]
    if base.lower() in DUMMY_LABELS or re.fullmatch(r"(x|q|da)\d*", base.lower()):
        return "", True
    if base.isdigit():
        z = int(base)
        if z == 0:
            return "", True
        return (ELEMENTS[z - 1] if 0 < z <= len(ELEMENTS) else None), False
    return _element(base), False


def _element(s: str) -> str | None:
    s = re.sub(r"[\d_]+$", "", s.strip())
    return ELEMENTS[Z_OF[s.lower()] - 1] if s.lower() in Z_OF else None


def split_tokens(line: str) -> list[str]:
    return [t for t in re.split(r"[\s,]+", line.strip()) if t]


def parse_atom_line(line: str, lineno: int, allow_flag: bool = False) -> tuple[Atom | None, bool]:
    """Parse "El x y z [...]" (or "El flag x y z" when allow_flag, Gaussian freeze codes).

    Returns (Atom, recognised). A Z-matrix line ("H 1 1.09 2 104.5") gives an Atom without xyz.
    (None, False) when the first token is not an atom label at all.
    """
    tok = split_tokens(line)
    if not tok:
        return None, False
    sym, ghost = parse_label(tok[0])
    if sym is None:
        return None, False
    xyz = None
    nums = tok[1:]
    zmat = len(nums) in (2, 4, 6, 7) and is_int(nums[0]) and (len(nums) == 2 or is_int(nums[2]))
    if allow_flag and len(nums) >= 4 and nums[0] in ("0", "-1") and all(is_float(t) for t in nums[1:4]):
        xyz = tuple(to_float(t) for t in nums[1:4])  # Gaussian freeze flag: "C 0 x y z" / "C -1 x y z"
    elif len(nums) >= 3 and all(is_float(t) for t in nums[:3]) and not zmat:
        xyz = tuple(to_float(t) for t in nums[:3])
    return Atom(symbol=sym if not ghost else (sym or ""), xyz=xyz, line=lineno, ghost=ghost, label=tok[0]), True


def electrons(inp: ParsedInput) -> int | None:
    if not inp.geometry_complete or not inp.atoms or inp.charge is None:
        return None
    total = 0
    for a in inp.atoms:
        if a.ghost:
            continue
        if not a.symbol:
            return None
        total += Z_OF[a.symbol.lower()]
    return total - inp.charge


def check_parity(inp: ParsedInput) -> list[Finding]:
    """Electron count vs multiplicity (or 2S for Molpro/PySCF)."""
    n = electrons(inp)
    if n is None:
        return []
    ref = REF.get(inp.program or "", REF["capabilities"])
    line = inp.extra.get("charge_line")
    if n < 0:
        return [Finding("error", "charge", f"Charge {inp.charge} leaves {n} electrons.", line, ref=ref)]
    if inp.spin_2s is not None and inp.multiplicity is None:
        s = inp.spin_2s
        what = "spin (2S = N_alpha - N_beta)"
        if s < 0:
            s = -s
        if s > n:
            return [Finding("error", "parity", f"{what} = {inp.spin_2s} but there are only {n} electrons.", line,
                            ref=ref)]
        if (n - s) % 2:
            hint = ""
            if s >= 1 and (n - (s - 1)) % 2 == 0:
                hint = (f" It looks like the multiplicity ({s}) was given; {what} should be {s - 1}.")
            return [Finding(
                "error", "parity",
                f"{n} electrons (charge {inp.charge}) cannot have {what} = {inp.spin_2s}.{hint}", line,
                fix=f"spin = {n % 2} (lowest), or {n % 2 + 2}, ...: spin is 2S, NOT the multiplicity",
                ref=ref,
            )]
        return []
    mult = inp.multiplicity
    if mult is None:
        return []
    if mult < 1:
        return [Finding("error", "multiplicity", f"Multiplicity {mult} is invalid (must be >= 1).", line, ref=ref)]
    if mult - 1 > n:
        return [Finding("error", "parity", f"Multiplicity {mult} needs {mult - 1} unpaired electrons but there "
                        f"are only {n}.", line, ref=ref)]
    if (n - (mult - 1)) % 2:
        return [Finding(
            "error", "parity",
            f"{n} electrons (sum of Z minus charge {inp.charge}) cannot have multiplicity {mult}: "
            f"an {'odd' if n % 2 else 'even'} electron count needs an {'even' if n % 2 else 'odd'} multiplicity.",
            line, fix=f"check the charge, or use multiplicity {n % 2 + 1} (or {n % 2 + 3}, ...)", ref=ref,
        )]
    return []


def check_unknown_elements(inp: ParsedInput) -> list[Finding]:
    out = []
    for line, label in inp.extra.get("unknown_labels", [])[:5]:
        out.append(Finding("error", "unknown-element", f"'{label}' is not an element symbol.", line,
                           fix="use a periodic-table symbol (e.g. Cl, not CL1x)", ref=REF.get(inp.program or "")))
    return out


def check_distances(inp: ParsedInput) -> list[Finding]:
    pts = [a for a in inp.atoms if a.xyz is not None and not a.ghost]
    if len(pts) < 2 or len(pts) > 5000:
        return []
    scale = BOHR_TO_ANG if inp.units == "bohr" else 1.0
    import numpy as np

    xyz = np.array([a.xyz for a in pts], dtype=float) * scale
    d2 = ((xyz[:, None, :] - xyz[None, :, :]) ** 2).sum(-1)
    iu = np.triu_indices(len(pts), 1)
    close = np.where(d2[iu] < MIN_DIST_ANG ** 2)[0]
    out = []
    for k in close[:3]:
        i, j = iu[0][k], iu[1][k]
        d = math.sqrt(d2[i, j])
        a, b = pts[i], pts[j]
        what = "duplicate atom" if d < 0.01 else "too close"
        out.append(Finding(
            "error", "close-atoms",
            f"Atoms {a.label} (line {a.line}) and {b.label} (line {b.line}) are {d:.3f} Å apart ({what}; "
            f"units read as {inp.units}).",
            b.line, fix="remove the duplicate, or check Bohr vs Ångström units", ref=REF.get(inp.program or ""),
        ))
    if len(close) > 3:
        out.append(Finding("error", "close-atoms", f"{len(close) - 3} more atom pairs closer than {MIN_DIST_ANG} Å.",
                           None, ref=REF.get(inp.program or "")))
    return out


def element_set(inp: ParsedInput) -> list[str]:
    return sorted({a.symbol for a in inp.atoms if a.symbol and not a.ghost}, key=lambda s: Z_OF[s.lower()])


def check_basis_coverage(inp: ParsedInput) -> list[Finding]:
    """Basis (and ORCA aux basis) coverage via BSE; silent if BSE is missing or the name unknown."""
    els = element_set(inp)
    if not els:
        return []
    try:
        from ..basis import basis_elements, bse_available, resolve_basis
    except Exception:  # noqa: BLE001
        return []
    if not bse_available():
        return []
    out = []
    names = ([inp.basis] if inp.basis else []) + list(inp.aux_basis)
    for name in names:
        try:
            r = resolve_basis(name)
        except Exception:  # noqa: BLE001
            continue
        if not r["key"]:
            continue
        missing = [e for e in els if e not in set(basis_elements(r["key"]))]
        if missing:
            out.append(Finding(
                "warning", "basis-coverage",
                f"{r['name']} (from '{name}') is not defined for {', '.join(missing)} in the Basis Set Exchange. "
                "The program's own library may differ, but most likely the job stops with a missing-basis error.",
                inp.extra.get("basis_line"),
                fix="use a basis that covers these elements (e.g. def2 family; -PP sets for heavy elements), "
                    "or assign basis sets per element; check with `rag-drg basis`",
                ref=REF["capabilities"] + "#Basis set spelling",
            ))
    return out


# ------------------------------------------------------------------ levels of theory


def _lnorm(s: str) -> str:
    s = s.lower().replace("ω", "w").replace("omega", "w")
    return re.sub(r"[^a-z0-9]", "", s)


@lru_cache(maxsize=4)
def _levels_index(root: str) -> dict:
    from ...config import load_config
    from ...levels import load_levels

    cfg = load_config(root)
    entries, _ = load_levels(cfg)
    keys: dict[str, dict] = {}
    for e in entries:
        for k in [e["name"], *(e.get("aliases") or [])]:
            keys.setdefault(_lnorm(str(k)), e)
    return keys


def levels_index(cfg) -> dict:
    if cfg is None:
        return {}
    try:
        cfg_file = cfg.root / "rag_drg.yaml"
        return _levels_index(str(cfg_file)) if cfg_file.is_file() else _levels_from_cfg(cfg)
    except Exception:  # noqa: BLE001 - the level check is optional
        return {}


def _levels_from_cfg(cfg) -> dict:
    from ...levels import load_levels

    entries, _ = load_levels(cfg)
    keys: dict[str, dict] = {}
    for e in entries:
        for k in [e["name"], *(e.get("aliases") or [])]:
            keys.setdefault(_lnorm(str(k)), e)
    return keys


def _support(c: dict) -> str:
    v = c.get("support", "unknown")
    if v is True:
        return "yes"
    if v is False:
        return "no"
    return str(v).lower()


def lookup_method(keys: dict, name: str, program: str) -> dict | None:
    k = _lnorm(name)
    if k in keys:
        return keys[k]
    if program == "gaussian":  # R/U/RO prefixes
        for pre in ("ro", "u", "r"):
            if k.startswith(pre) and k[len(pre):] in keys:
                return keys[k[len(pre):]]
    return None


def check_levels(inp: ParsedInput, cfg) -> list[Finding]:
    keys = levels_index(cfg)
    if not keys or not inp.program:
        return []
    out = []
    seen = set()
    for name, line in inp.extra.get("method_tokens", []):
        e = lookup_method(keys, name, inp.program)
        if e is None or e["name"] in seen:
            continue
        seen.add(e["name"])
        c = (e.get("codes") or {}).get(inp.program)
        if not c:
            continue
        sup = _support(c)
        how = f" Write it as: {c['keyword']}." if c.get("keyword") else ""
        notes = f" ({c['notes']})" if c.get("notes") else ""
        alts = [k for k, v in (e.get("codes") or {}).items() if _support(v) == "yes"]
        ref = REF["levels"] + f"#{e['name']}"
        if sup == "no":
            out.append(Finding("error", "level-unsupported",
                               f"{e['name']} ('{name}') is not available in {inp.program}{notes}. "
                               f"Supported as-is in: {', '.join(alts) or 'none listed'}.", line,
                               fix="choose another method or code (lookup_level_of_theory)", ref=ref))
        elif sup == "variant":
            out.append(Finding("warning", "level-variant",
                               f"'{name}' ({e['name']}) is not the same method in {inp.program}: the similar "
                               f"{inp.program} keyword is a different parametrisation.{how}{notes}", line,
                               fix=f"use {inp.program}'s own name deliberately (and report it), or run "
                                   f"{e['name']} in {', '.join(alts) or 'another code'}", ref=ref))
        elif sup in ("partial", "unknown"):
            out.append(Finding("info", f"level-{sup}",
                               f"Support for {e['name']} in {inp.program} is marked '{sup}' in the levels table."
                               f"{how}{notes}", line, ref=ref))
    return out


# ------------------------------------------------------------------ units


def mem_to_mb(value: float, unit: str | None, default: str = "mb") -> float | None:
    """Convert memory with unit (b, kb, mb, gb, tb, k, m, g, t, kib, mib, gib, tib, words kw/mw/gw/tw)
    to MiB (2^20 bytes). Units are case-insensitive."""
    u = (unit or default).lower().strip()
    table = {
        "b": 1 / 2**20, "k": 1 / 1024, "kb": 1 / 1024, "kib": 1 / 1024, "m": 1, "mb": 1, "mib": 1,
        "g": 1024, "gb": 1024, "gib": 1024, "t": 1024**2, "tb": 1024**2, "tib": 1024**2,
        "w": 8 / 2**20, "kw": 8 / 1024, "mw": 8, "gw": 8 * 1024, "tw": 8 * 1024**2,
    }
    f = table.get(u)
    return None if f is None else value * f


def fmt_mb(mb: float | None) -> str:
    if mb is None:
        return "?"
    return f"{mb / 1024:.1f} GB" if mb >= 1024 else f"{mb:.0f} MB"
