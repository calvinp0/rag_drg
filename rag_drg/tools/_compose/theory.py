"""Method, dispersion, solvation and basis resolution for the composer.

Methods are resolved through the levels-of-theory table (knowledge/ess/levels_of_theory.yaml):
the program's own spelling is taken from the table's `keyword`, and its `support` decides
whether the composer may write it (yes / partial: write it; variant / unknown: only with
`allow_unverified`; no: never). Basis sets go through the Basis Set Exchange check in
`rag_drg.tools.basis` (program spelling, element coverage, ECPs, auxiliary sets).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .spec import ComposeError

# Method classes the writers know how to write.
HF, DFT, DH, C3, MP2, CC, DLPNO, F12 = "hf", "dft", "dh", "c3", "mp2", "ccsd_t", "dlpno", "f12"
CLASS_LABEL = {HF: "Hartree-Fock", DFT: "DFT", DH: "double-hybrid DFT", C3: "DFT composite (3c)", MP2: "MP2",
               CC: "CCSD(T)", DLPNO: "DLPNO-CCSD(T)", F12: "CCSD(T)-F12"}

# (program -> method classes the composer writes)
IMPLEMENTED = {
    "orca": {HF, DFT, DH, C3, MP2, CC, DLPNO, F12},
    "gaussian": {HF, DFT, DH, MP2, CC},
    "qchem": {HF, DFT, MP2, CC},
    "psi4": {HF, DFT, DH, MP2, CC, DLPNO},
    "molpro": {HF, DFT, MP2, CC, F12},
    "pyscf": {HF, DFT, MP2, CC},
}
# (program -> job types the composer writes; each documented in knowledge/ess/<program>/*-essentials.md)
PROGRAM_JOBS = {
    "orca": ("sp", "opt", "freq", "opt+freq", "ts", "irc"),
    "gaussian": ("sp", "opt", "freq", "opt+freq", "ts", "irc"),
    "qchem": ("sp", "opt", "freq", "opt+freq", "ts", "irc"),
    "psi4": ("sp", "opt", "freq", "opt+freq", "ts"),
    "molpro": ("sp", "opt", "freq", "opt+freq", "ts"),
    "pyscf": ("sp", "opt", "freq", "opt+freq"),
}
WHY_NO_JOB = {
    ("psi4", "irc"): "the Psi4 card documents no IRC driver",
    ("molpro", "irc"): "the Molpro card documents no IRC",
    ("pyscf", "ts"): "the PySCF card documents no TS search",
    ("pyscf", "irc"): "the PySCF card documents no IRC",
}
GEOMETRY_JOBS = ("opt", "freq", "opt+freq", "ts", "irc")


def jobs_for(program: str, cls: str) -> tuple[str, ...]:
    """Job types the composer writes for this program and method class."""
    jobs = PROGRAM_JOBS[program]
    if cls in (CC, DLPNO, F12):
        return ("sp",)
    if cls == DH and program == "psi4":
        return ("sp",)
    if cls == MP2 and program == "pyscf":
        return ("sp",)
    return jobs


@dataclass
class Level:
    requested: str
    name: str                 # table name (or the request when not in the table)
    cls: str
    keyword: str              # how this program spells it
    support: str              # yes | partial | variant | unknown
    in_table: bool = True
    self_dispersion: bool = False
    ri: bool = False          # RI-MP2 explicitly requested
    notes: list[str] = field(default_factory=list)


def _norm(s: str) -> str:
    s = s.lower().replace("ω", "w").replace("omega", "w")
    return re.sub(r"[^a-z0-9]", "", s)


def _support(c: dict | None) -> str:
    if not c:
        return "unknown"
    v = c.get("support", "unknown")
    if v is True:
        return "yes"
    if v is False:
        return "no"
    return str(v).lower()


def _class_of(category: str) -> str | None:
    c = (category or "").lower()
    if "multireference" in c:
        return "multireference"
    if "composite thermochemistry" in c:
        return "composite"
    if "dispersion correction" in c:
        return "dispersion"
    if "solvation" in c:
        return "solvation"
    if "double hybrid" in c:
        return DH
    if "dft composite" in c:
        return C3
    if "dft" in c:
        return DFT
    if "scf" in c:
        return HF
    if "explicitly correlated" in c:
        return F12
    if "local coupled cluster" in c:
        return DLPNO
    if "coupled cluster" in c:
        return CC
    if "correlation" in c:
        return MP2
    return None


SELF_DISPERSION = {"wb97xd", "wb97xd3", "wb97xv", "wb97mv", "r2scan3c", "b973c", "pbeh3c", "hf3c", "wb97xd4",
                   "wb97xd3bj"}


def _index(entries: list[dict]) -> dict[str, dict]:
    keys: dict[str, dict] = {}
    for e in entries:
        for k in [e["name"], *(e.get("aliases") or [])]:
            keys.setdefault(_norm(str(k)), e)
    return keys


def _extract_keyword(program: str, kw: str | None, cls: str) -> str | None:
    """The program's spelling of the method from the table's (prose-ish) `keyword` field."""
    if not kw:
        return None
    kw = str(kw)
    m = None
    if program == "orca":
        m = re.search(r"!\s*([^\s!]+)", kw)
    elif program == "gaussian":
        m = re.match(r"\s*([^\s(/]+(?:\([^)]*\))?)", kw)
    elif program == "qchem":
        m = re.search(r"\bMETHOD\s+(\S+)", kw)
    elif program == "psi4":
        m = re.search(r"energy\('([^']+)'\)", kw) or re.match(r"\s*([\w\-()+]+)", kw)
    elif program == "molpro":
        if cls in (DFT, DH):
            m = re.search(r"\{\s*[ru]ks\s*,\s*([^}\s]+)\s*\}", kw)
    elif program == "pyscf":
        if cls in (DFT, DH):
            m = re.search(r"xc\s*=\s*'([^']+)'", kw)
    if not m:
        return None
    tok = m.group(1).strip().rstrip("?")
    return tok or None


def _split_dispersion(method: str) -> tuple[str, str] | None:
    m = re.fullmatch(r"(.+?)-?(d3\(?bj\)?|d3zero|d3\(0\)|d3|d4)", method.strip(), re.I)
    if not m:
        return None
    return m.group(1), m.group(2)


def resolve_method(entries: list[dict], program: str, method: str, dispersion: str | None,
                   allow_unverified: bool) -> tuple[Level, str | None]:
    """-> (Level, dispersion). Raises ComposeError with the reason when it cannot be written."""
    keys = _index(entries)
    requested = method.strip()
    notes: list[str] = []
    entry = keys.get(_norm(requested))
    if entry is None:
        split = _split_dispersion(requested)
        if split and _norm(split[0]) in keys:
            if dispersion and _norm(dispersion) != _norm(split[1]):
                raise ComposeError(f"method {requested!r} carries dispersion {split[1]} but dispersion: {dispersion} "
                                   "was also given; give it once")
            notes.append(f"'{requested}' read as method {split[0]} + dispersion {split[1]}")
            requested, dispersion = split[0], split[1]
            entry = keys[_norm(requested)]
    ri = _norm(requested) in ("rimp2", "dfmp2")
    if entry is None:
        n = _norm(requested)
        if re.search(r"(ccsd|mp[234]|cisd|casscf|caspt|nevpt|mrci|cbs|g[1-4]|eom)", n) and not n.endswith("3c"):
            raise ComposeError(f"{requested!r} is not in the levels-of-theory table and looks like a wavefunction/"
                               "composite method the composer cannot classify; add it to "
                               "knowledge/ess/levels_of_theory.yaml first (lookup_level_of_theory)")
        if not allow_unverified:
            raise ComposeError(f"{requested!r} is not in the levels-of-theory table, so its {program} spelling and "
                               f"availability are unverified; check the {program} manual (search_knowledge) and set "
                               "allow_unverified: true to write it as given (treated as a DFT functional), or add it "
                               "to knowledge/ess/levels_of_theory.yaml")
        cls = C3 if n.endswith("3c") else DFT
        kw = requested.lower() if program in ("psi4", "pyscf", "molpro") else requested
        notes.append(f"UNVERIFIED: {requested!r} is not in the levels-of-theory table; written as given ('{kw}') "
                     "because allow_unverified is true")
        lvl = Level(requested=requested, name=requested, cls=cls, keyword=kw, support="unknown", in_table=False,
                    self_dispersion=n in SELF_DISPERSION or n.endswith("3c"), ri=ri, notes=notes)
        return lvl, dispersion

    cls = _class_of(entry.get("category", ""))
    if cls in ("dispersion", "solvation"):
        raise ComposeError(f"{entry['name']} is not a method; put it in the `{cls}` field of the spec")
    if cls == "multireference":
        raise ComposeError(f"{entry['name']} is a multireference method: it needs an active space (and state "
                           "averaging) that the spec cannot express; the composer does not generate it - write the "
                           "input by hand from the card and check it with check_input")
    if cls == "composite":
        raise ComposeError(f"{entry['name']} is a composite thermochemistry protocol (it fixes its own geometry, "
                           "basis and job steps); the composer does not generate it")
    if cls is None:
        raise ComposeError(f"cannot tell what kind of method {entry['name']} is (category {entry.get('category')!r})")
    c = (entry.get("codes") or {}).get(program)
    sup = _support(c)
    tnotes = f" ({c['notes']})" if c and c.get("notes") else ""
    alts = [k for k, v in (entry.get("codes") or {}).items() if _support(v) == "yes"]
    kw = _extract_keyword(program, (c or {}).get("keyword"), cls)
    if sup == "no":
        raise ComposeError(f"{entry['name']} is not available in {program} according to the levels-of-theory table"
                           f"{tnotes}. Supported as-is in: {', '.join(alts) or 'none listed'}")
    if sup in ("variant", "unknown") and not allow_unverified:
        why = (f"{program}'s similarly named method is a DIFFERENT method" + (f" (the table suggests '{kw}')" if kw else "")
               if sup == "variant" else f"support in {program} is not checked yet")
        raise ComposeError(f"{entry['name']} ('{requested}') in {program}: the levels-of-theory table says "
                           f"'{sup}': {why}{tnotes}. Use a code where it is supported as-is "
                           f"({', '.join(alts) or 'none listed'}), pick {program}'s own method deliberately, or set "
                           "allow_unverified: true to write it anyway")
    if sup == "variant":
        # Write what was asked for (the user asserts their build has it); never substitute silently.
        kw = requested if program in ("orca", "gaussian", "qchem") else requested.lower()
        notes.append(f"UNVERIFIED (allow_unverified): the table marks {entry['name']} in {program} as 'variant' "
                     f"(a different method with a similar name){tnotes}; written as requested ('{kw}'). Make sure your "
                     f"{program} version has exactly this method")
    elif sup == "unknown":
        kw = kw or (requested.lower() if program in ("psi4", "pyscf", "molpro") else requested)
        notes.append(f"UNVERIFIED (allow_unverified): support for {entry['name']} in {program} is 'unknown' in the "
                     f"levels table; written as '{kw}'. Check the manual")
    elif sup == "partial":
        notes.append(f"{entry['name']} in {program} is 'partial' in the levels table{tnotes}")
    if kw is None:
        kw = requested.lower() if program in ("psi4", "pyscf", "molpro") else requested
    if ri and program == "orca":
        kw = "RI-MP2"
    elif ri:
        notes.append(f"RI-MP2 requested: {program} is given plain MP2 "
                     + ("(Psi4 uses density-fitted MP2 by default)" if program == "psi4" else
                        "(the composer does not write RI-MP2 auxiliary settings for this code)"))
    if entry.get("notes") and cls in (DFT, DH):
        notes.append(f"{entry['name']}: {str(entry['notes']).strip()}")
    n = _norm(entry["name"])
    lvl = Level(requested=requested, name=entry["name"], cls=cls, keyword=kw, support=sup,
                self_dispersion=n in SELF_DISPERSION or cls == C3, ri=ri, notes=notes)
    return lvl, dispersion


# ------------------------------------------------------------------ dispersion

DISP_CANON = {"d3bj": "d3bj", "d3(bj)": "d3bj", "gd3bj": "d3bj", "d3-bj": "d3bj", "d3_bj": "d3bj",
              "d4": "d4", "dftd4": "d4",
              "d3": "d3zero", "d3zero": "d3zero", "d3(0)": "d3zero", "gd3": "d3zero", "d3_zero": "d3zero"}
DISP_SYNTAX = {
    "orca": {"d3bj": "D3BJ", "d4": "D4", "d3zero": "D3ZERO"},
    "gaussian": {"d3bj": "EmpiricalDispersion=GD3BJ", "d3zero": "EmpiricalDispersion=GD3"},
    "qchem": {"d3bj": "D3_BJ", "d3zero": "D3_ZERO"},
    "psi4": {"d3bj": "-d3bj", "d4": "-d4"},
    "pyscf": {"d3bj": "d3bj", "d4": "d4"},
    "molpro": {},
}
DISP_PARTIAL = {("psi4", "d3bj"): "needs the dftd3 / s-dftd3 program installed",
                ("psi4", "d4"): "needs dftd4 installed",
                ("pyscf", "d3bj"): "needs the pyscf-dispersion package",
                ("pyscf", "d4"): "needs the pyscf-dispersion package"}


def resolve_dispersion(program: str, disp: str | None, level: Level) -> tuple[str | None, list[str]]:
    if not disp:
        return None, []
    key = DISP_CANON.get(disp.strip().lower().replace(" ", ""))
    if key is None:
        raise ComposeError(f"unknown dispersion {disp!r}; use D3BJ, D3 (zero damping) or D4")
    if level.cls not in (DFT, DH):
        raise ComposeError(f"dispersion {disp} only goes with DFT; {level.name} is {CLASS_LABEL.get(level.cls, level.cls)}")
    if level.self_dispersion:
        raise ComposeError(f"{level.name} already includes its own dispersion/non-local correction; adding {disp} "
                           "would count it twice")
    syntax = DISP_SYNTAX[program].get(key)
    if syntax is None:
        raise ComposeError(f"{disp} dispersion is not available / not documented for {program} in the levels table "
                           "and cards; the composer does not write it")
    notes = [f"dispersion {disp} -> {syntax!r}"]
    if (program, key) in DISP_PARTIAL:
        notes.append(f"{program} dispersion {syntax}: {DISP_PARTIAL[(program, key)]}")
    return syntax, notes


# ------------------------------------------------------------------ solvation

def resolve_solvation(program: str, solv: dict | None) -> tuple[dict | None, list[str]]:
    if not solv:
        return None, []
    model, solvent = solv["model"], solv["solvent"]
    notes: list[str] = []
    if program in ("psi4", "molpro", "pyscf"):
        raise ComposeError(f"implicit solvation is not generated for {program}: the levels table marks it no/"
                           "partial/unknown there (Psi4 needs PCMSolver and a pcm block, PySCF needs the dielectric "
                           "constant); write it by hand or run the solvated step in ORCA/Gaussian/Q-Chem")
    if program == "qchem" and model == "cpcm":
        raise ComposeError("Q-Chem: use solvation model pcm (SOLVENT_METHOD PCM) or smd; C-PCM options are not "
                           "documented in the Q-Chem card")
    if program == "orca" and model == "pcm":
        notes.append("ORCA's PCM is C-PCM: pcm written as CPCM")
    if program == "gaussian" and model == "pcm":
        notes.append("Gaussian SCRF=PCM is IEF-PCM")
    notes.append(f"solvation: {model.upper()} in {solvent}")
    return {"model": model, "solvent": solvent}, notes


# ------------------------------------------------------------------ basis


@dataclass
class BasisChoice:
    orbital: str | None          # program spelling
    aux: list[str] = field(default_factory=list)
    ecp: dict = field(default_factory=dict)
    canonical: str | None = None
    notes: list[str] = field(default_factory=list)


def _is_def2(name: str) -> bool:
    return bool(re.match(r"^(ma-)?def2", name.strip().lower()))


def resolve_basis(program: str, basis: str | None, elements: list[str], level: Level,
                  allow_unverified: bool) -> BasisChoice:
    from ..basis import bse_available, check_basis, resolve_basis as bse_resolve

    if level.cls == C3:
        if basis:
            raise ComposeError(f"{level.name} is a '3c' composite with its own fixed basis set; leave basis empty")
        return BasisChoice(orbital=None, notes=[f"{level.name} uses its built-in basis (no basis keyword)"])
    if not basis:
        raise ComposeError("basis is required (e.g. def2-TZVP)")
    out = BasisChoice(orbital=basis)
    if re.search(r"(/c|/j|/jk|-cabs|-rifit|-jkfit|-jfit)$", basis.strip().lower()):
        raise ComposeError(f"{basis} is an auxiliary basis; give the orbital basis (the composer adds the auxiliary "
                           "sets that are needed)")
    if not bse_available():
        if not allow_unverified:
            raise ComposeError("basis_set_exchange is not installed, so the basis cannot be checked (spelling, element "
                               "coverage, ECPs): pip install 'rag-drg[chem]', or set allow_unverified: true")
        out.notes.append(f"UNVERIFIED: basis {basis} written as given (basis_set_exchange not installed)")
        return out
    res = check_basis(basis, elements=elements, software=program)
    if not res.get("found"):
        if not allow_unverified:
            raise ComposeError(f"basis {basis!r}: {res.get('message')}")
        out.notes.append(f"UNVERIFIED: basis {basis!r} is not in the Basis Set Exchange; written as given")
        return out
    if res.get("role") not in (None, "orbital"):
        raise ComposeError(f"{basis} is a {res.get('role')} (auxiliary) basis in BSE; give the orbital basis")
    if res.get("missing"):
        raise ComposeError(f"{res['canonical']} does not cover {', '.join(res['missing'])} (BSE coverage: "
                           f"{res['basis_coverage']}); pick a basis that covers all elements")
    out.canonical = res["canonical"]
    out.orbital = res.get("program_spelling") or basis
    if out.orbital != basis:
        out.notes.append(f"basis {basis} written as '{out.orbital}' for {program}")
    out.ecp = dict(res.get("ecp") or {})
    canon = out.canonical
    low = canon.lower()
    if program == "gaussian" and re.search(r"-pp$", low):
        raise ComposeError(f"{canon} is not a built-in Gaussian basis: its ECP must be given in a GenECP block, which "
                           "the composer does not generate; use def2 basis sets (built-in ECPs) or write GenECP by hand")
    if level.cls == F12 and "f12" not in low:
        raise ComposeError(f"{level.name} needs an -F12 orbital basis (e.g. cc-pVTZ-F12), not {canon}")
    if out.ecp:
        ecp_txt = ", ".join(f"{k} ({v} core electrons)" for k, v in out.ecp.items())
        if _is_def2(canon) and program in ("orca", "gaussian", "molpro", "psi4"):
            out.notes.append(f"ECP: {canon} uses an effective core potential for {ecp_txt}; {program} applies the "
                             "matching def2 ECP automatically for def2 basis names (check the output's basis summary)")
        elif _is_def2(canon) and program == "pyscf":
            out.notes.append(f"ECP: {canon} uses an effective core potential for {ecp_txt}; written explicitly as "
                             "gto.M(ecp=...)")
        elif allow_unverified:
            out.notes.append(f"UNVERIFIED ECP: {canon} uses an ECP for {ecp_txt}; the composer did not add ECP input "
                             f"for {program} - check that {program} applies it")
        else:
            raise ComposeError(f"{canon} uses an effective core potential for {ecp_txt}, and the composer cannot "
                               f"confirm {program} applies it automatically (not in the {program} card); add the ECP "
                               "by hand, or set allow_unverified: true")
    # auxiliary basis sets (ORCA)
    if program == "orca":
        need_c = level.cls in (DLPNO, DH) or level.ri
        if level.cls == F12:
            base = re.sub(r"-f12$", "", canon, flags=re.I)
            cabs = f"{canon}-CABS"
            c_aux = f"{base}/C"
            for a in (cabs, c_aux):
                if not bse_resolve(a).get("key"):
                    raise ComposeError(f"no auxiliary set {a} in BSE for {canon}; write the F12 input by hand")
            out.aux = [cabs, c_aux]
            out.notes.append(f"auxiliary basis for {level.name}: {cabs} (CABS) and {c_aux} (correlation fitting), "
                             "as in the ORCA card")
        elif need_c:
            c_aux = f"{out.orbital}/C"
            if bse_resolve(c_aux).get("key"):
                out.aux = [c_aux]
                out.notes.append(f"auxiliary basis {c_aux} added automatically ({level.name} needs a /C correlation "
                                 "fitting basis)")
            else:
                out.aux = ["AutoAux"]
                out.notes.append(f"BSE has no /C set for {canon}; AutoAux added so ORCA generates the correlation "
                                 "fitting basis")
        elif level.cls == DFT:
            out.notes.append("ORCA 5+ turns on RI-J (GGA) / RIJCOSX (hybrids) with the def2/J auxiliary basis "
                             "automatically; add NoRI / NoCOSX via extra_keywords for exact integrals")
    return out
