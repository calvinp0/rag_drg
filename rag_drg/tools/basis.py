"""Basis-set checks against the Basis Set Exchange (BSE) library (offline data).

    rag-drg basis def2-TZVP --elements C,H,I
    rag-drg basis 6-31G(d,p) --smiles CCO
    rag-drg basis avtz --xyz geom.xyz --json

MCP tool: ``check_basis(basis, elements=None, smiles=None, xyz=None)``.

Program spellings are normalised before the lookup: case, ``Def2TZVP`` (Gaussian) =
``def2-TZVP``, ``6-31G(d)`` = ``6-31G*``, ``6-31G(d,p)`` = ``6-31G**``, Molpro shorthands
(``vdz``/``avtz``/``vtz-f12``/``vtz-pp``) and ORCA auxiliary suffixes (``/C``, ``/J``, ``/JK``,
``-CABS``), which are mapped to BSE's ``-RIFIT``/``-JFIT``/``-JKFIT``/``-OPTRI`` sets.

BSE is the reference. Programs ship their own basis libraries whose element coverage (and
occasionally contents) can differ, so a gap here means "check your program's library", not
necessarily "the job will fail".

Both ``basis_set_exchange`` and ``rdkit`` (for SMILES) are optional: ``pip install 'rag-drg[chem]'``.
"""

from __future__ import annotations

import difflib
import json
import re
import sys
from functools import lru_cache
from pathlib import Path

PROGRAM_NOTE = (
    "Programs ship their own basis-set libraries whose coverage can differ from BSE "
    "(BSE is the reference); check the program's library/manual when it matters."
)

ELEMENTS = (
    "H He Li Be B C N O F Ne Na Mg Al Si P S Cl Ar K Ca Sc Ti V Cr Mn Fe Co Ni Cu Zn Ga Ge As Se Br Kr "
    "Rb Sr Y Zr Nb Mo Tc Ru Rh Pd Ag Cd In Sn Sb Te I Xe Cs Ba La Ce Pr Nd Pm Sm Eu Gd Tb Dy Ho Er Tm "
    "Yb Lu Hf Ta W Re Os Ir Pt Au Hg Tl Pb Bi Po At Rn Fr Ra Ac Th Pa U Np Pu Am Cm Bk Cf Es Fm Md No Lr "
    "Rf Db Sg Bh Hs Mt Ds Rg Cn Nh Fl Mc Lv Ts Og"
).split()
Z_OF = {s.lower(): i + 1 for i, s in enumerate(ELEMENTS)}

# ORCA-style auxiliary suffix -> BSE auxiliary role.
AUX_SUFFIX = {"c": "rifit", "j": "jfit", "jk": "jkfit"}
AUX_LABEL = {"rifit": "/C (RI-MP2/correlation fitting)", "jfit": "/J (Coulomb fitting)",
             "jkfit": "/JK (Coulomb+exchange fitting)", "optri": "-CABS / OptRI (F12)",
             "admmfit": "ADMM fitting", "dftjfit": "DFT J fitting", "dftxfit": "DFT XC fitting"}
# ORCA's generic def2 fitting sets.
SPECIAL = {"def2/j": "def2-universal-jfit", "def2/jk": "def2-universal-jkfit"}
# Keywords that mean "the basis is given elsewhere" (not resolvable, not an error).
NON_BASIS = {"gen", "genecp", "chkbasis", "readbasis", "extrabasis", "mixed", "general", "gen/ecp"}


class BasisError(RuntimeError):
    """Raised for missing optional dependencies or unparseable element input."""


def _bse():
    try:
        import basis_set_exchange  # noqa: PLC0415
    except ImportError as e:
        raise BasisError(
            "basis_set_exchange is not installed: pip install 'rag-drg[chem]' (or basis_set_exchange)"
        ) from e
    return basis_set_exchange


def bse_available() -> bool:
    try:
        _bse()
    except BasisError:
        return False
    return True


def _pople(s: str) -> str:
    """6-31G(d) -> 6-31G*, 6-31+G(d,p) -> 6-31+G** (Gaussian treats them as identical)."""
    m = re.match(r"^(\d-\d+\+{0,2}g)\((d|d,p)\)$", s)
    if m:
        return m.group(1) + ("*" if m.group(2) == "d" else "**")
    return s


def _norm(name: str) -> str:
    s = name.strip().lower().replace(" ", "").replace("_st_", "*")
    s = _pople(s)
    return s.replace("-", "").replace("_", "")


def _molpro(s: str) -> str | None:
    """Molpro shorthands: vdz, avtz, vtz-f12, avtz-pp, wcvtz -> cc-pVnZ family names (lower case)."""
    m = re.match(r"^(a)?(wc|c)?v([dtq56])z(-f12|-pp)?$", s.strip().lower())
    if not m:
        return None
    aug, core, n, suf = m.groups()
    core = {"wc": "wc", "c": "c", None: ""}[core]
    return f"{'aug-' if aug else ''}cc-p{core}v{n}z{suf or ''}"


@lru_cache(maxsize=1)
def _tables() -> tuple[dict, dict[str, str]]:
    """(BSE metadata, normalised name -> BSE key). Built once."""
    md = _bse().get_metadata()
    lookup: dict[str, str] = {}
    # Prefer the '*' spellings for Pople sets (6-31G** over BSE's separate 6-31G(d,p) entry),
    # then primary names, then other_names.
    keys = sorted(md, key=lambda k: (0 if "_st_" in k else 1, k))
    for k in keys:
        lookup.setdefault(_norm(k), k)
        lookup.setdefault(_norm(md[k].get("display_name", k)), k)
    for k in keys:
        for other in md[k].get("other_names") or []:
            lookup.setdefault(_norm(other), k)
    return md, lookup


def _first(v):
    return v[0] if isinstance(v, list) and v else v


def resolve_basis(name: str) -> dict:
    """Map a program spelling to a BSE entry.

    Returns {"input", "key" (BSE key or None), "name" (display name), "role", "via" (how the
    name was interpreted), "suggestions", "error"}.
    """
    out: dict = {"input": name, "key": None, "name": None, "role": None, "via": None,
                 "suggestions": [], "error": None}
    raw = (name or "").strip().strip("\"'")
    if not raw:
        out["error"] = "empty basis name"
        return out
    if raw.lower() in NON_BASIS:
        out["error"] = f"'{raw}' means the basis is given in the input (not a library basis)"
        return out
    md, lookup = _tables()

    def hit(key: str, via: str | None = None) -> dict:
        out.update(key=key, name=md[key]["display_name"], role=md[key].get("role"), via=via)
        return out

    low = raw.lower()
    if low in SPECIAL:
        return hit(SPECIAL[low], f"ORCA {raw} = {md[SPECIAL[low]]['display_name']}")
    # ORCA auxiliary suffix: X/C, X/J, X/JK
    m = re.match(r"^(.+)/(c|j|jk)$", low)
    if m:
        base = resolve_basis(m.group(1))
        role = AUX_SUFFIX[m.group(2)]
        if base["key"]:
            aux = _first((md[base["key"]].get("auxiliaries") or {}).get(role))
            if aux and aux in md:
                return hit(aux, f"ORCA {raw} = {md[aux]['display_name']} ({role} of {base['name']})")
            out["error"] = (f"BSE lists no {role} auxiliary set for {base['name']}; your program may "
                            f"still ship '{raw}'")
            return out
        out["suggestions"] = base["suggestions"]
        out["error"] = f"unknown orbital basis '{m.group(1)}' in '{raw}'"
        return out
    # F12 complementary auxiliary basis: cc-pVTZ-F12-CABS -> cc-pVTZ-F12-OptRI
    m = re.match(r"^(.+-f12)-cabs$", low)
    if m and f"{m.group(1)}-optri" in md:
        key = f"{m.group(1)}-optri"
        return hit(key, f"{raw} = {md[key]['display_name']}")
    mol = _molpro(low)
    if mol and _norm(mol) in lookup:
        key = lookup[_norm(mol)]
        return hit(key, f"Molpro shorthand {raw} = {md[key]['display_name']}")
    key = lookup.get(_norm(raw))
    if key:
        via = None if _norm(raw) == _norm(md[key]["display_name"]) and raw == md[key]["display_name"] else (
            f"{raw} = {md[key]['display_name']}")
        return hit(key, via)
    close = difflib.get_close_matches(_norm(raw), list(lookup), n=6, cutoff=0.6)
    out["suggestions"] = list(dict.fromkeys(md[lookup[c]]["display_name"] for c in close))
    out["error"] = f"'{raw}' is not in the Basis Set Exchange"
    return out


def basis_elements(key: str) -> list[str]:
    """Element symbols covered by the latest version of a BSE basis."""
    md, _ = _tables()
    entry = md[key]
    ver = entry["versions"][entry["latest_version"]]
    return [ELEMENTS[int(z) - 1] for z in ver["elements"]]


def ecp_info(key: str, elements: list[str]) -> dict[str, int]:
    """{element: ECP core electrons} for the requested elements that use an ECP in this basis."""
    md, _ = _tables()
    if "scalar_ecp" not in (md[key].get("function_types") or []) and not any(
        "ecp" in t for t in md[key].get("function_types") or []
    ):
        return {}
    covered = set(basis_elements(key))
    els = [e for e in elements if e in covered]
    if not els:
        return {}
    data = _bse().get_basis(key, elements=els, header=False)
    out = {}
    for z, e in data["elements"].items():
        if e.get("ecp_electrons"):
            out[ELEMENTS[int(z) - 1]] = int(e["ecp_electrons"])
    return out


def _ranges(symbols: list[str]) -> str:
    """Compact element coverage like 'H-Kr, Rb-Xe'."""
    zs = sorted(Z_OF[s.lower()] for s in symbols)
    if not zs:
        return "none"
    parts, start, prev = [], zs[0], zs[0]
    for z in zs[1:] + [None]:
        if z is not None and z == prev + 1:
            prev = z
            continue
        parts.append(ELEMENTS[start - 1] if start == prev else f"{ELEMENTS[start - 1]}-{ELEMENTS[prev - 1]}")
        if z is not None:
            start = prev = z
    return ", ".join(parts)


def normalize_element(sym: str) -> str | None:
    s = re.sub(r"[^A-Za-z]", "", sym or "")
    return ELEMENTS[Z_OF[s.lower()] - 1] if s.lower() in Z_OF else None


def elements_from_smiles(smiles: str) -> list[str]:
    """Elements of a SMILES string, implicit hydrogens included (needs RDKit)."""
    try:
        from rdkit import Chem, RDLogger  # noqa: PLC0415
    except ImportError as e:
        raise BasisError("SMILES input needs RDKit: pip install 'rag-drg[chem]' (or rdkit)") from e
    RDLogger.DisableLog("rdApp.*")
    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        raise BasisError(f"RDKit could not parse SMILES '{smiles}'")
    mol = Chem.AddHs(mol)
    return sorted({a.GetSymbol() for a in mol.GetAtoms()}, key=lambda s: Z_OF.get(s.lower(), 999))


def elements_from_xyz(text: str) -> list[str]:
    """Elements from xyz text (with or without the count/comment header) or a path to an .xyz file."""
    if "\n" not in text and len(text) < 4096 and Path(text).expanduser().is_file():
        text = Path(text).expanduser().read_text(errors="replace")
    found = []
    for line in text.splitlines():
        tok = line.replace(",", " ").split()
        if len(tok) < 4:
            continue
        try:
            [float(t) for t in tok[1:4]]
        except ValueError:
            continue
        sym = tok[0]
        if sym.isdigit() and 0 < int(sym) <= len(ELEMENTS):
            sym = ELEMENTS[int(sym) - 1]
        el = normalize_element(re.sub(r"\d+$", "", sym))
        if el is None:
            raise BasisError(f"unknown element '{tok[0]}' in xyz line: {line.strip()}")
        found.append(el)
    if not found:
        raise BasisError("no atoms found in the xyz input")
    return sorted(set(found), key=lambda s: Z_OF[s.lower()])


def _parse_elements(elements) -> list[str]:
    if isinstance(elements, str):
        elements = re.split(r"[,\s]+", elements.strip())
    out = []
    for e in elements or []:
        if not e:
            continue
        el = normalize_element(e)
        if el is None:
            raise BasisError(f"unknown element symbol '{e}'")
        out.append(el)
    return sorted(set(out), key=lambda s: Z_OF[s.lower()])


def program_spelling(display: str, software: str | None) -> str | None:
    if not software:
        return None
    sw = software.lower()
    if sw == "gaussian" and display.lower().startswith("def2-"):
        return "Def2" + display[5:].replace("-", "")
    if sw in ("psi4", "pyscf"):
        return display.lower()
    if sw in ("orca", "qchem", "molpro", "gaussian"):
        return display
    return None


def check_basis(basis: str, elements: list[str] | str | None = None, smiles: str | None = None,
                xyz: str | None = None, software: str | None = None) -> dict:
    """Resolve a basis name in BSE and check element coverage / ECPs / auxiliary sets.

    Elements come from `elements` (list or "C,H,I"), `xyz` (text or path) or `smiles` (RDKit).
    """
    res: dict = {"input": basis, "found": False, "canonical": None, "bse_key": None, "role": None,
                 "interpreted_as": None, "suggestions": [], "elements": [], "covered": [], "missing": [],
                 "basis_coverage": None, "ecp": {}, "auxiliaries": {}, "program_spelling": None,
                 "message": "", "note": PROGRAM_NOTE}
    try:
        els: list[str] = []
        if elements:
            els += _parse_elements(elements)
        if xyz:
            els += elements_from_xyz(xyz)
        if smiles:
            els += elements_from_smiles(smiles)
        els = sorted(set(els), key=lambda s: Z_OF[s.lower()])
        res["elements"] = els
        r = resolve_basis(basis)
    except BasisError as e:
        res["message"] = f"Error: {e}"
        res["error"] = str(e)
        return res
    res["suggestions"] = r["suggestions"]
    if not r["key"]:
        msg = f"{r['error'] or 'not found'}."
        if r["suggestions"]:
            msg += f" Did you mean: {', '.join(r['suggestions'])}?"
        res["message"] = msg
        return res
    md, _ = _tables()
    key = r["key"]
    entry = md[key]
    covered_all = basis_elements(key)
    res.update(found=True, canonical=r["name"], bse_key=key, role=r["role"], interpreted_as=r["via"],
               basis_coverage=_ranges(covered_all), description=entry.get("description"),
               family=entry.get("family"))
    for role, aux in (entry.get("auxiliaries") or {}).items():
        names = aux if isinstance(aux, list) else [aux]
        res["auxiliaries"][AUX_LABEL.get(role, role)] = [md[a]["display_name"] if a in md else a for a in names]
    res["program_spelling"] = program_spelling(r["name"], software)
    parts = [f"{r['name']} (BSE role: {r['role']}) covers {res['basis_coverage']}."]
    if r["via"]:
        parts.insert(0, f"Interpreted {r['via']}.")
    if els:
        cov = set(covered_all)
        res["covered"] = [e for e in els if e in cov]
        res["missing"] = [e for e in els if e not in cov]
        try:
            res["ecp"] = ecp_info(key, res["covered"])
        except Exception as e:  # noqa: BLE001 - ECP info is a bonus
            res["ecp_error"] = str(e)
        if res["missing"]:
            parts.append(f"MISSING for: {', '.join(res['missing'])} (BSE has no {r['name']} for these elements; "
                         "pick another basis or mix basis sets per element).")
        else:
            parts.append(f"All requested elements covered ({', '.join(els)}).")
        if res["ecp"]:
            parts.append("ECP used for: " + ", ".join(f"{k} ({v} core electrons)" for k, v in res["ecp"].items())
                         + ". Make sure the program applies the matching ECP.")
    if res["program_spelling"] and res["program_spelling"] != r["name"]:
        parts.append(f"In {software} write it as '{res['program_spelling']}'.")
    res["message"] = " ".join(parts)
    return res


def format_report(res: dict) -> str:
    lines = [f"Basis: {res['input']}"]
    if res.get("found"):
        lines.append(f"BSE name: {res['canonical']} (key {res['bse_key']}, role {res['role']})")
        if res.get("interpreted_as"):
            lines.append(f"Interpreted: {res['interpreted_as']}")
        lines.append(f"Coverage: {res['basis_coverage']}")
        if res["elements"]:
            lines.append(f"Requested: {', '.join(res['elements'])}")
            lines.append(f"Covered:   {', '.join(res['covered']) or '-'}")
            lines.append(f"Missing:   {', '.join(res['missing']) or '-'}")
        if res["ecp"]:
            lines.append("ECP:       " + ", ".join(f"{k} ({v} core e-)" for k, v in res["ecp"].items()))
        if res["auxiliaries"]:
            lines.append("Auxiliary sets listed by BSE:")
            lines += [f"  {role}: {', '.join(v)}" for role, v in res["auxiliaries"].items()]
    lines.append(res["message"])
    lines.append(f"Note: {res['note']}")
    return "\n".join(lines)


# ------------------------------------------------------------------ plugin hooks


def register_cli(subparsers):
    p = subparsers.add_parser("basis", help="check a basis set in the Basis Set Exchange (coverage, ECPs, aux sets)")
    p.add_argument("name", help="e.g. def2-TZVP, Def2TZVP, 6-31G(d,p), avtz, def2-TZVP/C")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--elements", "-e", help="comma-separated element symbols, e.g. C,H,I")
    g.add_argument("--smiles", help="SMILES (implicit H included; needs RDKit)")
    g.add_argument("--xyz", help="xyz file")
    p.add_argument("--software", "-s", help="show the spelling for this program")
    p.add_argument("--json", action="store_true")
    return {"basis": _cli}


def _cli(args, cfg) -> int:
    xyz = None
    if args.xyz:
        try:
            xyz = Path(args.xyz).read_text(errors="replace")
        except OSError as e:
            print(f"cannot read {args.xyz}: {e}", file=sys.stderr)
            return 2
    res = check_basis(args.name, elements=args.elements, smiles=args.smiles, xyz=xyz, software=args.software)
    print(json.dumps(res, indent=2) if args.json else format_report(res))
    if res.get("error"):
        return 2
    return 0 if res["found"] and not res["missing"] else 1


_check_basis = check_basis  # the MCP tool below shadows the name inside register_mcp


def register_mcp(mcp, ctx) -> None:
    _check = _check_basis

    @mcp.tool()
    def check_basis(basis: str, elements: list[str] | None = None, smiles: str | None = None,  # noqa: F811
                    xyz: str | None = None) -> str:
        """Check a basis set against the Basis Set Exchange: canonical name, which elements it
        covers (and which of yours are missing), which elements use an ECP, and the auxiliary
        (/C, /J, /JK) sets that go with it.

        Program spellings are understood: Def2TZVP, 6-31G(d,p), avtz, def2-TZVP/C, def2/J, ...
        Programs ship their own basis libraries whose coverage can differ; BSE is the reference.

        Args:
            basis: Basis set name as written in the input.
            elements: Element symbols, e.g. ["C", "H", "I"].
            smiles: SMILES of the molecule (implicit hydrogens are counted).
            xyz: xyz-format geometry text.
        """
        res = _check(basis, elements=elements, smiles=smiles, xyz=xyz)
        ctx.emit({"tool": "check_basis", "args": {"basis": basis}, "n_results": int(bool(res.get("found")))})
        return format_report(res)
