"""Per-program input writers. Syntax follows the curated cards in knowledge/ess/<program>/.

Each writer takes a `Job` (everything already resolved and validated) and returns the input
text. Anything a writer cannot express raises ComposeError instead of guessing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .molecule import Molecule
from .spec import ComposeError, Spec
from .theory import C3, CC, DFT, DH, DLPNO, F12, HF, MP2, BasisChoice, Level

EXTENSIONS = {"orca": ".inp", "gaussian": ".gjf", "qchem": ".in", "psi4": ".dat", "molpro": ".in", "pyscf": ".py"}


@dataclass
class Job:
    spec: Spec
    mol: Molecule
    level: Level
    basis: BasisChoice
    disp: str | None                 # program syntax of the dispersion correction
    solv: dict | None                # {model, solvent}
    mem_lines: list[str]             # in-input memory/cores lines from servers.input_lines
    cores: int
    mem_gb: float
    input_name: str
    title: str
    version: str | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def open_shell(self) -> bool:
        return self.mol.multiplicity > 1

    @property
    def stem(self) -> str:
        return self.input_name.rsplit(".", 1)[0]


def _no_grid(job: Job, program: str) -> None:
    if job.spec.grid:
        raise ComposeError(f"grid: the {program} card documents no integration-grid keyword; pass the program's own "
                           "option through extra_keywords if you need one")


def _dft_only_grid(job: Job) -> None:
    if job.spec.grid and job.level.cls not in (DFT, DH, C3):
        raise ComposeError(f"grid only applies to DFT; {job.level.name} is not DFT")


# ================================================================== ORCA

ORCA_SCF = {"loose": "LooseSCF", "normal": "NormalSCF", "tight": "TightSCF", "verytight": "VeryTightSCF"}
ORCA_GRID = {"defgrid1": "DefGrid1", "defgrid2": "DefGrid2", "defgrid3": "DefGrid3", "coarse": "DefGrid1",
             "fine": "DefGrid2", "default": "DefGrid2", "ultrafine": "DefGrid3"}
ORCA_JOB = {"sp": [], "opt": ["Opt"], "freq": ["Freq"], "opt+freq": ["Opt", "Freq"], "ts": ["OptTS", "Freq"],
            "irc": ["IRC"]}


def write_orca(job: Job) -> str:
    s, lvl = job.spec, job.level
    first = [lvl.keyword] + ([job.basis.orbital] if job.basis.orbital else []) + job.basis.aux
    if job.disp:
        first.append(job.disp)
    second = list(ORCA_JOB[s.job])
    if s.scf.get("convergence"):
        second.append(ORCA_SCF[s.scf["convergence"]])
    if s.grid:
        _dft_only_grid(job)
        g = ORCA_GRID.get(s.grid.lower())
        if g is None:
            raise ComposeError(f"ORCA grid {s.grid!r}: use DefGrid1/DefGrid2/DefGrid3 (ORCA 5+; the ORCA 4 GridX "
                               "keywords no longer work)")
        second.append(g)
    if job.solv:
        second.append(f"CPCM({job.solv['solvent']})")
    raw_blocks = [x for x in s.extra_keywords if x.lstrip().startswith("%")]
    second += [x for x in s.extra_keywords if not x.lstrip().startswith("%")]
    lines = [f"# {job.title}", "# composed by rag-drg (compose_ess_job); check the notes before running",
             "! " + " ".join(first)]
    if second:
        lines.append("! " + " ".join(second))
    lines += [ln for ln in job.mem_lines]
    if s.scf.get("max_iter"):
        lines += ["%scf", f"  MaxIter {s.scf['max_iter']}", "end"]
    if s.job == "ts":
        lines += ["%geom", "  Calc_Hess true      # exact Hessian at the first step",
                  "  Recalc_Hess 5       # recompute every 5 steps", "end"]
    if s.job == "irc":
        lines += ["%irc", "  MaxIter 50", "end"]
    if job.solv and job.solv["model"] == "smd":
        lines += ["%cpcm", "  smd true", f'  SMDsolvent "{job.solv["solvent"]}"', "end"]
    lines += raw_blocks
    lines.append(f"* xyz {job.mol.charge} {job.mol.multiplicity}")
    lines += ["  " + ln for ln in job.mol.xyz_lines()]
    lines.append("*")
    return "\n".join(lines) + "\n"


# ================================================================== Gaussian

G_JOB = {"sp": [], "opt": ["Opt"], "freq": ["Freq"], "opt+freq": ["Opt", "Freq"],
         "ts": ["Opt=(TS,CalcFC,NoEigenTest)", "Freq"], "irc": ["IRC=(CalcFC,MaxPoints=50,StepSize=10)"]}
G_SCF = {"tight": "Tight", "verytight": "VeryTight", "loose": "Conver=6"}
G_GRID = {"fine": "FineGrid", "finegrid": "FineGrid", "ultrafine": "UltraFine", "superfine": "SuperFine"}


def _is_g09(version: str | None) -> bool:
    return str(version or "").lower().lstrip("g") in ("09", "9")


def write_gaussian(job: Job) -> str:
    s, lvl = job.spec, job.level
    route = [f"{lvl.keyword}/{job.basis.orbital}"] + G_JOB[s.job]
    if job.disp:
        route.append(job.disp)
    scf = []
    if s.scf.get("convergence") and s.scf["convergence"] != "normal":
        scf.append(G_SCF[s.scf["convergence"]])
    if s.scf.get("max_iter"):
        scf.append(f"MaxCycle={s.scf['max_iter']}")
    if scf:
        route.append("SCF=" + (scf[0] if len(scf) == 1 else "(" + ",".join(scf) + ")"))
    if s.grid:
        _dft_only_grid(job)
        g = G_GRID.get(s.grid.lower())
        if g is None:
            raise ComposeError(f"Gaussian grid {s.grid!r}: use fine, ultrafine or superfine")
        route.append(f"Int=Grid={g}" if _is_g09(job.version) else f"Int={g}")
    if job.solv:
        route.append(f"SCRF=({job.solv['model'].upper()},Solvent={job.solv['solvent']})")
    route += s.extra_keywords
    link0 = [ln for ln in job.mem_lines] + [f"%chk={job.stem}.chk"]
    title = re.sub(r"\s+", " ", job.title).strip()
    lines = link0 + ["#P " + " ".join(route), "", title, "", f"{job.mol.charge} {job.mol.multiplicity}"]
    lines += job.mol.xyz_lines()
    return "\n".join(lines) + "\n\n"


# ================================================================== Q-Chem

QC_JOB = {"sp": [("sp", False)], "opt": [("opt", False)], "freq": [("freq", False)],
          "opt+freq": [("opt", False), ("freq", True)],
          "ts": [("freq", False), ("ts", True), ("freq", True)],
          "irc": [("freq", False), ("rpath", True)]}
QC_SCF = {"loose": "5", "tight": "8", "verytight": "10"}


def write_qchem(job: Job) -> str:
    s, lvl = job.spec, job.level
    _no_grid(job, "Q-Chem")
    extra = []
    for x in s.extra_keywords:
        m = re.match(r"^\s*(\w+)\s*(?:=\s*|\s+)(\S.*)$", x)
        if not m:
            raise ComposeError(f"Q-Chem extra_keywords must be '$rem KEYWORD value' pairs; got {x!r}")
        extra.append((m.group(1).upper(), m.group(2).strip()))
    blocks = []
    for i, (jt, read) in enumerate(QC_JOB[s.job]):
        mol = ["$molecule", "read", "$end"] if read else (
            ["$molecule", f"{job.mol.charge} {job.mol.multiplicity}"] + ["   " + ln for ln in job.mol.xyz_lines()]
            + ["$end"])
        rem = [("JOBTYPE", jt), ("METHOD", lvl.keyword), ("BASIS", job.basis.orbital)]
        if job.open_shell:
            rem.append(("UNRESTRICTED", "true"))
        if job.disp:
            rem.append(("DFT_D", job.disp))
        if s.scf.get("convergence") in QC_SCF:
            rem.append(("SCF_CONVERGENCE", QC_SCF[s.scf["convergence"]]))
        if s.scf.get("max_iter"):
            rem.append(("MAX_SCF_CYCLES", str(s.scf["max_iter"])))
        if job.solv:
            rem.append(("SOLVENT_METHOD", "SMD" if job.solv["model"] == "smd" else "PCM"))
        if read:
            rem.append(("SCF_GUESS", "read"))
            if jt in ("ts", "rpath") and QC_JOB[s.job][i - 1][0] == "freq":
                rem.append(("GEOM_OPT_HESSIAN", "read"))
        for ln in job.mem_lines:
            k, _, v = ln.partition(" ")
            rem.append((k, v.strip()))
        rem += extra
        width = max(len(k) for k, _ in rem) + 2
        block = mol + ["", "$rem"] + [f"   {k.ljust(width)}{v}" for k, v in rem] + ["$end"]
        if job.solv and job.solv["model"] == "smd":
            block += ["", "$smx", f"   solvent {job.solv['solvent']}", "$end"]
        elif job.solv:
            block += ["", "$solvent", f"   SolventName {job.solv['solvent']}", "$end"]
        blocks.append("\n".join(block))
    head = f"! {job.title}\n! composed by rag-drg (compose_ess_job)\n"
    return head + "\n\n@@@\n\n".join(blocks) + "\n"


# ================================================================== Psi4 (psithon)

P4_CONV = {"loose": "1e-5", "tight": "1e-8", "verytight": "1e-10"}


def write_psi4(job: Job) -> str:
    s, lvl = job.spec, job.level
    _no_grid(job, "Psi4")
    if lvl.cls == DLPNO and job.open_shell:
        raise ComposeError("Psi4's DLPNO-CCSD(T) is closed-shell RHF only (levels table); use ORCA for open shells")
    if lvl.cls in (DFT, DH, C3):
        ref = "uks" if job.open_shell else "rks"
    else:
        ref = "uhf" if job.open_shell else "rhf"
    opts = [("basis", job.basis.orbital), ("reference", ref)]
    if lvl.cls == CC:
        opts.append(("scf_type", "pk"))
    if s.scf.get("convergence") in P4_CONV:
        c = P4_CONV[s.scf["convergence"]]
        opts += [("e_convergence", c), ("d_convergence", c)]
    if s.scf.get("max_iter"):
        opts.append(("maxiter", str(s.scf["max_iter"])))
    if s.job == "ts":
        opts += [("opt_type", "ts"), ("full_hess_every", "0")]
    for x in s.extra_keywords:
        m = re.match(r"^\s*(\w+)\s+(\S.*)$", x)
        if not m:
            raise ComposeError(f"Psi4 extra_keywords must be 'option value' pairs for the set block; got {x!r}")
        opts.append((m.group(1).lower(), m.group(2).strip()))
    name = lvl.keyword + (job.disp or "")
    calls = {"sp": [f"energy('{name}')"], "opt": [f"optimize('{name}')"], "freq": [f"frequency('{name}')"],
             "opt+freq": [f"optimize('{name}')", f"frequency('{name}')"],
             "ts": [f"optimize('{name}')", f"frequency('{name}')"]}[s.job]
    width = max(len(k) for k, _ in opts) + 1
    lines = [f"# {job.title}", "# composed by rag-drg (compose_ess_job)", ""]
    lines += [ln for ln in job.mem_lines]
    lines += ["", "molecule {", f"{job.mol.charge} {job.mol.multiplicity}"]
    lines += ["  " + ln for ln in job.mol.xyz_lines()]
    lines += ["symmetry c1", "}", "", "set {"] + [f"  {k.ljust(width)}{v}" for k, v in opts] + ["}", ""]
    lines += calls
    return "\n".join(lines) + "\n"


# ================================================================== Molpro

MP_CONV = {"loose": "1.d-5", "tight": "1.d-8", "verytight": "1.d-10"}


def write_molpro(job: Job) -> str:
    s, lvl = job.spec, job.level
    _no_grid(job, "Molpro")
    if s.scf.get("max_iter"):
        raise ComposeError("scf.max_iter: the Molpro card documents no SCF iteration option; pass it through "
                           "extra_keywords (e.g. inside your own {hf;...} card) if needed")
    title = re.sub(r"[,;!]", " ", job.title)
    lines = [f"***,{title}", "! composed by rag-drg (compose_ess_job)"]
    lines += [ln for ln in job.mem_lines]
    if s.scf.get("convergence") in MP_CONV:
        lines.append(f"gthresh,energy={MP_CONV[s.scf['convergence']]}")
    lines += ["symmetry,nosym", "geometry={"] + job.mol.xyz_lines() + ["}"]
    lines += [f"basis={job.basis.orbital}", f"set,charge={job.mol.charge}",
              f"set,spin={job.mol.spin_2s}          ! 2S (unpaired electrons), NOT the multiplicity"]
    lines += s.extra_keywords
    osh = job.open_shell
    if lvl.cls in (DFT, DH):
        lines.append(f"{{{'uks' if osh else 'rks'},{lvl.keyword}}}")
    elif lvl.cls == HF:
        lines.append("{uhf}" if osh else "{hf}")
    elif lvl.cls == MP2:
        lines += ["{rhf}", "{rmp2}"] if osh else ["{hf}", "{mp2}"]
    elif lvl.cls == CC:
        lines += ["{rhf}", "{rccsd(t)}"] if osh else ["{hf}", "{ccsd(t)}"]
    elif lvl.cls == F12:
        lines += ["{rhf}", "{rccsd(t)-f12}"] if osh else ["{hf}", "{ccsd(t)-f12}"]
    else:
        raise ComposeError(f"Molpro: {lvl.name} is not written by the composer")
    if s.job in ("opt", "opt+freq"):
        lines.append("{optg}")
    if s.job == "ts":
        lines.append("{optg,root=2}")
    if s.job in ("freq", "opt+freq", "ts"):
        lines.append("{frequencies}")
    return "\n".join(lines) + "\n"


# ================================================================== PySCF

PY_CONV = {"loose": "1e-7", "tight": "1e-10", "verytight": "1e-12"}


def _pyscf_mem_lines(job: Job) -> tuple[str | None, str | None]:
    mem = thr = None
    for ln in job.mem_lines:
        m = re.search(r"max_memory\s*=\s*(\d+)", ln)
        if m:
            mem = m.group(1)
        m = re.search(r"num_threads\((\d+)\)", ln)
        if m:
            thr = m.group(1)
    return mem, thr


def write_pyscf(job: Job) -> str:
    s, lvl = job.spec, job.level
    _no_grid(job, "PySCF")
    mem, thr = _pyscf_mem_lines(job)
    if mem is None or thr is None:
        raise ComposeError("internal: no PySCF memory/thread suggestion from the servers module")
    osh = job.open_shell
    is_dft = lvl.cls in (DFT, DH, C3)
    mods = ["gto", "dft" if is_dft else "scf"]
    if lvl.cls == MP2:
        mods.append("mp")
    if lvl.cls == CC:
        mods.append("cc")
    atoms = "\n".join(job.mol.xyz_lines("{:<2s} {:14.8f} {:14.8f} {:14.8f}"))
    L = [f'"""{job.title}', "", f"Composed by rag-drg (compose_ess_job). Run: python {job.input_name}", '"""',
         f"from pyscf import {', '.join(mods)}",
         f"from pyscf import lib; lib.num_threads({thr})",
         "",
         "mol = gto.M(",
         f'    atom="""\n{atoms}\n""",',
         f'    basis="{job.basis.orbital}",']
    if job.basis.ecp:
        L.append(f'    ecp="{job.basis.orbital}",')
    L += [f"    charge={job.mol.charge},",
          f"    spin={job.mol.spin_2s},  # 2S = N_alpha - N_beta, NOT the multiplicity",
          '    unit="Angstrom",',
          "    verbose=4,",
          ")",
          f"mol.max_memory = {mem}  # MB, TOTAL",
          ""]
    cls_name = ("UKS" if osh else "RKS") if is_dft else ("UHF" if osh else "RHF")
    mod = "dft" if is_dft else "scf"
    L += ["", "def make_mf(m):", f"    mf = {mod}.{cls_name}(m)"]
    if is_dft:
        L.append(f'    mf.xc = "{lvl.keyword}"')
    if job.disp:
        L.append(f'    mf.disp = "{job.disp}"')
    if s.scf.get("convergence") in PY_CONV:
        L.append(f"    mf.conv_tol = {PY_CONV[s.scf['convergence']]}")
    if s.scf.get("max_iter"):
        L.append(f"    mf.max_cycle = {s.scf['max_iter']}")
    L += ["    " + x for x in s.extra_keywords]
    L += ["    return mf", "", "", "def run_scf(m):", "    mf = make_mf(m)", "    mf.kernel()",
          "    if not mf.converged:",
          '        raise SystemExit("SCF did not converge (PySCF does not stop on its own); try mf.newton()")',
          "    return mf", "", ""]
    if s.job == "sp":
        L += ["mf = run_scf(mol)", 'print("E(SCF) =", mf.e_tot)']
        if lvl.cls == MP2:
            L += ["pt = mp.MP2(mf).run()", 'print("E(MP2) =", pt.e_tot)']
        if lvl.cls == CC:
            L += ["mycc = cc.CCSD(mf).run()", "et = mycc.ccsd_t()", 'print("E(CCSD) =", mycc.e_tot)',
                  'print("E(CCSD(T)) =", mycc.e_tot + et)']
    else:
        if lvl.cls not in (HF, DFT, DH, C3):
            raise ComposeError(f"PySCF {s.job} is only written for SCF/DFT")
        geo = "mol"
        if s.job in ("opt", "opt+freq"):
            L += ["# geometry optimisation with geomeTRIC (pip install geometric)",
                  "from pyscf.geomopt.geometric_solver import optimize", "",
                  "mol_eq = optimize(make_mf(mol), maxsteps=100)",
                  'print("Optimised geometry (Angstrom):")',
                  "for i in range(mol_eq.natm):",
                  '    print(mol_eq.atom_symbol(i), *(f"{c:.8f}" for c in mol_eq.atom_coord(i, unit="Angstrom")))']
            geo = "mol_eq"
        if s.job in ("freq", "opt+freq"):
            L += ["", "# harmonic frequencies and thermochemistry", "from pyscf.hessian import thermo", "",
                  f"mf_eq = run_scf({geo})",
                  "hess = mf_eq.Hessian().kernel()",
                  f"freq = thermo.harmonic_analysis({geo}, hess)",
                  f"thermo.dump_normal_mode({geo}, freq)",
                  'tdata = thermo.thermo(mf_eq, freq["freq_au"], 298.15, 101325)',
                  f"thermo.dump_thermo({geo}, tdata)"]
        else:
            L += [f"mf_eq = run_scf({geo})", 'print("E(SCF) at the optimised geometry =", mf_eq.e_tot)']
    return "\n".join(L) + "\n"


WRITERS = {"orca": write_orca, "gaussian": write_gaussian, "qchem": write_qchem, "psi4": write_psi4,
           "molpro": write_molpro, "pyscf": write_pyscf}

__all__ = ["EXTENSIONS", "Job", "WRITERS", "C3", "CC", "DFT", "DH", "DLPNO", "F12", "HF", "MP2"]
