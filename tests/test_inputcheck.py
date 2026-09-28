import io
import json
import shutil
import textwrap
from pathlib import Path

import pytest

try:
    import basis_set_exchange  # noqa: F401

    HAVE_BSE = True
except ImportError:  # optional `chem` extra
    HAVE_BSE = False

from rag_drg.config import load_config
from rag_drg.tools import inputcheck
from rag_drg.tools.inputcheck import (EXTRA_CHECKS, Finding, ParsedInput, check_input, detect_program, hook_main,
                                      parse_input, parse_submit)

REPO = Path(__file__).resolve().parent.parent
FIX = Path(__file__).parent / "fixtures" / "inputs"
VALID = FIX / "valid"


@pytest.fixture(scope="module")
def cfg():
    return load_config(REPO / "rag_drg.yaml")


def run(content, filename, cfg, submit=None):
    return check_input(content=textwrap.dedent(content), filename=filename, submit_content=textwrap.dedent(submit)
                       if submit else None, cfg=cfg)


def codes(findings, severity=None):
    return {f.code for f in findings if severity is None or f.severity == severity}


def serious(findings):
    return [f for f in findings if f.severity in ("error", "warning")]


# ------------------------------------------------------------------ valid inputs: no errors, no warnings

VALID_INPUTS = ["g16_opt.gjf", "orca_ts.inp", "qchem_freq_ts.in", "molpro_ccsdt.com", "psi4_opt.dat", "psi4_sp.py",
                "pyscf_uks.py"]


@pytest.mark.parametrize("name", VALID_INPUTS)
def test_valid_inputs_file_mode(name, cfg):
    findings = check_input(path=VALID / name, cfg=cfg)
    assert serious(findings) == [], "\n".join(f.format() for f in findings)
    assert "submit-found" in codes(findings)  # the matching run_*.sh was found and cross-checked


@pytest.mark.parametrize("name", VALID_INPUTS)
def test_valid_inputs_content_mode(name, cfg):
    findings = check_input(content=(VALID / name).read_text(), filename=name, cfg=cfg)
    assert serious(findings) == [], "\n".join(f.format() for f in findings)


@pytest.mark.parametrize("name", ["run_g16.sh", "run_orca.sh", "run_qchem.sh", "run_molpro.sh", "run_python.sh"])
def test_valid_submit_scripts(name, cfg):
    findings = check_input(path=VALID / name, cfg=cfg)
    assert serious(findings) == [], "\n".join(f.format() for f in findings)


def test_detection():
    assert detect_program("a.gjf", (VALID / "g16_opt.gjf").read_text()) == "gaussian"
    assert detect_program("a.com", (VALID / "molpro_ccsdt.com").read_text()) == "molpro"
    assert detect_program("a.inp", (VALID / "orca_ts.inp").read_text()) == "orca"
    assert detect_program("a.in", (VALID / "qchem_freq_ts.in").read_text()) == "qchem"
    assert detect_program("a.dat", (VALID / "psi4_opt.dat").read_text()) == "psi4"
    assert detect_program("a.py", (VALID / "psi4_sp.py").read_text()) == "psi4"
    assert detect_program("a.py", (VALID / "pyscf_uks.py").read_text()) == "pyscf"
    assert detect_program("notes.md", "# heading\nsome text\n") is None
    assert detect_program("x.py", "import numpy\nprint(1)\n") is None
    # ORCA without extension hints
    assert detect_program(None, "! B3LYP def2-SVP\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n") == "orca"


def test_parsed_fields(cfg):
    inp, _ = parse_input((VALID / "orca_ts.inp").read_text(), "orca_ts.inp")
    assert (inp.program, inp.charge, inp.multiplicity, inp.nprocs, inp.memory_per_core_mb) == ("orca", 0, 2, 16, 3000)
    assert inp.method == "wB97X-D3" and inp.job_type == "ts" and len(inp.atoms) == 7
    if HAVE_BSE:  # ORCA's `!` line: telling the basis from other keywords uses the BSE name list
        assert inp.basis == "def2-TZVP"
    inp, _ = parse_input((VALID / "g16_opt.gjf").read_text(), "g16_opt.gjf")
    assert (inp.method, inp.basis, inp.nprocs, inp.memory_total_mb) == ("wB97XD", "Def2TZVP", 16, 56 * 1024)
    inp, _ = parse_input((VALID / "molpro_ccsdt.com").read_text(), "m.com")
    assert inp.spin_2s == 1 and inp.memory_per_process_mb == pytest.approx(500e6 * 8 / 2**20)
    inp, _ = parse_input((VALID / "pyscf_uks.py").read_text(), "p.py")
    assert inp.spin_2s == 1 and inp.basis == "def2-tzvp" and inp.memory_total_mb == 56000


# ------------------------------------------------------------------ common checks

G_ETHYL = """\
%nprocshared=4
%mem=8GB
#P B3LYP/6-31G(d) Opt

ethyl

0 {mult}
C    0.000000    0.000000    0.000000
C    1.490000    0.000000    0.000000
H   -0.380000    1.020000    0.000000
H   -0.380000   -0.510000    0.880000
H   -0.380000   -0.510000   -0.880000
H    2.050000    0.930000    0.000000
H    2.050000   -0.930000    0.000000

"""


def test_parity_error(cfg):
    assert serious(run(G_ETHYL.format(mult=2), "e.gjf", cfg)) == []
    f = run(G_ETHYL.format(mult=1), "e.gjf", cfg)
    assert "parity" in codes(f, "error")
    assert "multiplicity 1" in [x for x in f if x.code == "parity"][0].message


def test_unknown_element_and_close_atoms(cfg):
    bad = G_ETHYL.format(mult=2).replace("H    2.050000    0.930000", "Xy   2.050000    0.930000")
    assert "unknown-element" in codes(run(bad, "e.gjf", cfg), "error")
    dup = G_ETHYL.format(mult=2).replace("H    2.050000   -0.930000", "H    2.050000    0.930000")
    f = run(dup.replace("0 2", "0 2"), "e.gjf", cfg)
    assert "close-atoms" in codes(f, "error")


def test_bohr_units_scale_distances(cfg):
    # H2 at 1.4 bohr = 0.74 Å is fine when Units=Bohr is given
    g = "%mem=1GB\n#P HF/STO-3G Units=Bohr\n\nh2\n\n0 1\nH 0 0 0\nH 0 0 1.4\n\n"
    assert serious(run(g, "h2.gjf", cfg)) == []
    g = "%mem=1GB\n#P HF/STO-3G\n\nh2\n\n0 1\nH 0 0 0\nH 0 0 0.3\n\n"
    assert "close-atoms" in codes(run(g, "h2.gjf", cfg))


@pytest.mark.skipif(not HAVE_BSE, reason="needs the chem extra (basis_set_exchange)")
def test_basis_coverage(cfg):
    g = "%mem=1GB\n#P B3LYP/6-31G(d)\n\nmei\n\n0 1\nC 0 0 0\nI 2.14 0 0\nH -0.36 1.03 0\nH -0.36 -0.51 0.89\n" \
        "H -0.36 -0.51 -0.89\n\n"
    f = run(g, "mei.gjf", cfg)
    assert "basis-coverage" in codes(f, "warning")
    ok = g.replace("6-31G(d)", "Def2TZVP")
    assert "basis-coverage" not in codes(run(ok, "mei.gjf", cfg))


def test_without_bse_basis_checks_are_skipped(cfg, monkeypatch):
    from rag_drg.tools import basis

    monkeypatch.setattr(basis, "bse_available", lambda: False)
    g = "%mem=1GB\n#P B3LYP/6-31G(d)\n\nmei\n\n0 1\nC 0 0 0\nI 2.14 0 0\nH -0.36 1.03 0\nH -0.36 -0.51 0.89\n" \
        "H -0.36 -0.51 -0.89\n\n"
    assert serious(run(g, "mei.gjf", cfg)) == []
    assert serious(run(ORCA, "w.inp", cfg)) == []


def test_level_support(cfg):
    g = G_ETHYL.format(mult=2).replace("B3LYP/6-31G(d)", "wB97XV/Def2TZVP").replace("Opt", "")
    # wB97X-V is "no" for Gaussian in the levels table
    g = g.replace("wB97XV", "wB97X-V")
    assert "level-unsupported" in codes(run(g, "e.gjf", cfg), "error")
    o = "! wB97XD def2-TZVP\n%maxcore 2000\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n"
    f = run(o, "h2.inp", cfg)
    assert "level-variant" in codes(f, "warning")
    assert "wB97X-D3" in [x for x in f if x.code == "level-variant"][0].message


# ------------------------------------------------------------------ Gaussian


def test_gaussian_final_blank_line(cfg):
    g = G_ETHYL.format(mult=2).rstrip("\n") + "\n"
    f = run(g, "e.gjf", cfg)
    assert "gaussian-final-blank" in codes(f, "error")


def test_gaussian_missing_title(cfg):
    g = "%mem=1GB\n#P HF/STO-3G\n\n0 1\nH 0 0 0\nH 0 0 0.74\n\n"
    assert codes(run(g, "h2.gjf", cfg), "error") & {"gaussian-title", "gaussian-charge-mult"}


def test_gaussian_mem_units(cfg):
    g = G_ETHYL.format(mult=2).replace("%mem=8GB", "%mem=8000")
    assert "gaussian-mem-units" in codes(run(g, "e.gjf", cfg), "warning")


def test_gaussian_cpu_and_nprocshared(cfg):
    g = G_ETHYL.format(mult=2).replace("%nprocshared=4", "%nprocshared=4\n%cpu=0-3")
    assert "gaussian-cpu-nproc" in codes(run(g, "e.gjf", cfg), "warning")


def test_gaussian_gpucpu(cfg):
    base = G_ETHYL.format(mult=2).replace("%nprocshared=4", "%cpu=0-15\n%gpucpu=0-1=0,1")
    assert serious(run(base, "e.gjf", cfg)) == []
    bad = base.replace("%gpucpu=0-1=0,1", "%gpucpu=0-1=16,17")
    assert "gaussian-gpucpu-cpu" in codes(run(bad, "e.gjf", cfg), "error")
    # cross-check with the allocation: 2 GPUs used, none requested
    sub = "#!/bin/bash\n#SBATCH --ntasks=1\n#SBATCH --cpus-per-task=16\n#SBATCH --mem=64G\n$G16 < e.gjf > e.log\n"
    assert "gaussian-gpu-alloc" in codes(run(base, "e.gjf", cfg, submit=sub), "error")
    ok_sub = sub.replace("#SBATCH --mem=64G", "#SBATCH --mem=64G\n#SBATCH --gres=gpu:2")
    assert "gaussian-gpu-alloc" not in codes(run(base, "e.gjf", cfg, submit=ok_sub))


def test_gaussian_double_dispersion(cfg):
    g = G_ETHYL.format(mult=2).replace("B3LYP/6-31G(d) Opt", "wB97XD/Def2TZVP EmpiricalDispersion=GD3BJ")
    assert "gaussian-double-dispersion" in codes(run(g, "e.gjf", cfg), "warning")


def test_gaussian_ts_hessian(cfg):
    g = G_ETHYL.format(mult=2).replace("Opt", "Opt=(TS,NoEigenTest)")
    assert "gaussian-ts-hessian" in codes(run(g, "e.gjf", cfg), "warning")
    g = G_ETHYL.format(mult=2).replace("Opt", "Opt=(TS,CalcFC,NoEigenTest) Freq")
    assert serious(run(g, "e.gjf", cfg)) == []


def test_gaussian_gen_basis(cfg):
    g = G_ETHYL.format(mult=2).replace("6-31G(d)", "Gen")
    assert "gaussian-gen-basis" in codes(run(g, "e.gjf", cfg), "error")
    g_ok = g + "C H 0\n6-31G(d)\n****\n\n"
    assert serious(run(g_ok, "e.gjf", cfg)) == []


def test_gaussian_allcheck(cfg):
    g = "%oldchk=opt.chk\n%chk=freq.chk\n%mem=8GB\n%nprocshared=4\n#P B3LYP/6-31G(d) Freq Geom=AllCheck Guess=Read\n\n"
    assert serious(run(g, "freq.gjf", cfg)) == []
    no_chk = g.replace("%oldchk=opt.chk\n%chk=freq.chk\n", "")
    assert "gaussian-chk-missing" in codes(run(no_chk, "freq.gjf", cfg), "error")


def test_gaussian_link1_and_zmatrix(cfg):
    g = textwrap.dedent("""\
        %chk=w.chk
        %mem=2GB
        #P HF/6-31G(d) Opt

        water zmatrix

        0 1
        O
        H 1 0.96
        H 1 0.96 2 104.5

        --Link1--
        %chk=w.chk
        %mem=2GB
        #P B3LYP/6-31G(d) Freq Geom=Check Guess=Read

        water freq

        0 1

        """)
    f = run(g, "w.gjf", cfg)
    assert serious(f) == [], "\n".join(x.format() for x in f)
    inp, _ = parse_input(g, "w.gjf")
    assert inp.jobs == 2 and [a.symbol for a in inp.atoms] == ["O", "H", "H"]


# ------------------------------------------------------------------ ORCA

ORCA = """\
! B3LYP D3BJ def2-SVP Opt
%pal nprocs 4 end
%maxcore 2000
* xyz 0 1
O   0.000000   0.000000   0.117300
H   0.000000   0.757200  -0.469200
H   0.000000  -0.757200  -0.469200
*
"""


def test_orca_valid_variants(cfg):
    assert serious(run(ORCA, "w.inp", cfg)) == []
    with_constraints = ORCA.replace("%maxcore 2000", "%maxcore 2000\n%geom\n  Constraints\n    {B 0 1 C}\n  end\nend")
    assert serious(run(with_constraints, "w.inp", cfg)) == []
    scan = ORCA.replace("%maxcore 2000", "%maxcore 2000\n%geom Scan\n  B 0 1 = 0.9, 1.1, 5\n  end\nend")
    assert serious(run(scan, "w.inp", cfg)) == []
    xyzfile = ORCA.split("* xyz")[0] + "* xyzfile 0 1 geom.xyz\n"
    assert serious(run(xyzfile, "w.inp", cfg)) == []
    bang = ORCA.replace("%pal nprocs 4 end\n", "").replace("Opt", "Opt PAL4")
    assert serious(run(bang, "w.inp", cfg)) == []


def test_orca_maxcore(cfg):
    assert "orca-maxcore-missing" in codes(run(ORCA.replace("%maxcore 2000\n", ""), "w.inp", cfg), "warning")
    assert "orca-maxcore-small" in codes(run(ORCA.replace("%maxcore 2000", "%maxcore 4"), "w.inp", cfg), "warning")
    assert "orca-maxcore-total" in codes(run(ORCA.replace("%maxcore 2000", "%maxcore 64000"), "w.inp", cfg), "warning")


def test_orca_pal_conflict(cfg):
    f = run(ORCA.replace("Opt", "Opt PAL8"), "w.inp", cfg)
    assert "orca-nprocs-conflict" in codes(f, "error")


def test_orca_unclosed_block_and_coords(cfg):
    f = run(ORCA.replace("%pal nprocs 4 end", "%scf\n  MaxIter 200\n%pal nprocs 4 end"), "w.inp", cfg)
    assert "orca-block-end" in codes(f, "error")
    f = run(ORCA.rstrip().rsplit("\n", 1)[0] + "\n", "w.inp", cfg)
    assert "orca-coords-end" in codes(f, "error")
    f = run(ORCA.replace("* xyz 0 1", "* xyz"), "w.inp", cfg)
    assert "orca-charge-mult" in codes(f, "error")


def test_orca_dlpno_needs_aux(cfg):
    bad = ORCA.replace("B3LYP D3BJ def2-SVP Opt", "DLPNO-CCSD(T) cc-pVTZ TightPNO")
    assert "orca-aux-c" in codes(run(bad, "w.inp", cfg), "error")
    good = ORCA.replace("B3LYP D3BJ def2-SVP Opt", "DLPNO-CCSD(T) cc-pVTZ cc-pVTZ/C TightPNO")
    assert serious(run(good, "w.inp", cfg)) == []
    dh = ORCA.replace("B3LYP D3BJ def2-SVP Opt", "B2PLYP def2-TZVP")
    assert "orca-aux-c" in codes(run(dh, "w.inp", cfg), "warning")


def test_orca_moread_same_basename(cfg):
    o = ORCA.replace("! B3LYP", "! MORead B3LYP") + '%moinp "w.gbw"\n'
    assert "orca-moinp-same-name" in codes(run(o, "w.inp", cfg), "error")
    o2 = ORCA.replace("! B3LYP", "! MORead B3LYP") + '%moinp "w_guess.gbw"\n'
    assert serious(run(o2, "w.inp", cfg)) == []


def test_orca_old_grid(cfg):
    assert "orca-old-grid" in codes(run(ORCA.replace("Opt", "Opt Grid4 FinalGrid5"), "w.inp", cfg), "warning")


ORCA_SLURM = """\
#!/bin/bash
#SBATCH --ntasks=4
#SBATCH --cpus-per-task=1
#SBATCH --mem-per-cpu=4G
#SBATCH --time=1-00:00:00
ORCA_BIN=/opt/orca_6_0_1/orca
"$ORCA_BIN" w.inp > w.out
"""


def test_orca_submit_crosschecks(cfg):
    assert serious(run(ORCA, "w.inp", cfg, submit=ORCA_SLURM)) == []
    f = run(ORCA, "w.inp", cfg, submit=ORCA_SLURM.replace('"$ORCA_BIN" w.inp', 'mpirun -np 4 "$ORCA_BIN" w.inp'))
    assert "orca-mpirun" in codes(f, "error")
    f = run(ORCA, "w.inp", cfg, submit=ORCA_SLURM.replace('"$ORCA_BIN" w.inp', "orca w.inp"))
    assert "orca-path" in codes(f, "error")
    f = run(ORCA, "w.inp", cfg, submit=ORCA_SLURM.replace("--ntasks=4", "--ntasks=1").replace(
        "--cpus-per-task=1", "--cpus-per-task=4"))
    assert "orca-nprocs-alloc" in codes(f, "warning")
    f = run(ORCA, "w.inp", cfg, submit=ORCA_SLURM.replace("--ntasks=4", "--ntasks=2"))
    assert "orca-nprocs-alloc" in codes(f, "error")
    f = run(ORCA.replace("%maxcore 2000", "%maxcore 4000"), "w.inp", cfg, submit=ORCA_SLURM)
    assert "orca-maxcore-alloc" in codes(f, "warning")   # 16 GB of 16 GB
    f = run(ORCA.replace("%maxcore 2000", "%maxcore 6000"), "w.inp", cfg, submit=ORCA_SLURM)
    assert "orca-maxcore-alloc" in codes(f, "error")


# ------------------------------------------------------------------ Q-Chem

QCHEM = """\
$molecule
0 1
O   0.000000   0.000000   0.117300
H   0.000000   0.757200  -0.469200
H   0.000000  -0.757200  -0.469200
$end

$rem
   JOBTYPE     opt
   METHOD      B3LYP
   BASIS       def2-SVP
   MEM_TOTAL   8000
$end
"""


def test_qchem_valid(cfg):
    assert serious(run(QCHEM, "w.in", cfg)) == []


def test_qchem_sections(cfg):
    assert "qchem-section" in codes(run(QCHEM.replace("$end\n\n$rem", "\n$rem", 1), "w.in", cfg), "error")
    assert "qchem-section" in codes(run(QCHEM.rstrip().rsplit("\n", 1)[0] + "\n", "w.in", cfg), "error")


def test_qchem_rem(cfg):
    assert "qchem-basis" in codes(run(QCHEM.replace("   BASIS       def2-SVP\n", ""), "w.in", cfg), "error")
    assert "qchem-method" in codes(run(QCHEM.replace("   METHOD      B3LYP\n", ""), "w.in", cfg), "error")
    assert "qchem-jobtype" in codes(run(QCHEM.replace("JOBTYPE     opt", "JOBTYPE     energy"), "w.in", cfg), "warning")
    assert "qchem-mem-missing" in codes(run(QCHEM.replace("   MEM_TOTAL   8000\n", ""), "w.in", cfg), "warning")
    q = QCHEM.replace("B3LYP", "wB97X-D").replace("MEM_TOTAL   8000", "MEM_TOTAL   8000\n   DFT_D       D3_BJ")
    assert "qchem-double-dispersion" in codes(run(q, "w.in", cfg), "warning")


def test_qchem_multijob(cfg):
    second = "\n@@@\n\n$rem\n   JOBTYPE freq\n   METHOD B3LYP\n   BASIS def2-SVP\n   MEM_TOTAL 8000\n$end\n"
    assert "qchem-molecule" in codes(run(QCHEM + second, "w.in", cfg), "error")
    ok = second.replace("$rem", "$molecule\nread\n$end\n\n$rem", 1)
    assert serious(run(QCHEM + ok, "w.in", cfg)) == []


def test_qchem_submit(cfg):
    sub = "#!/bin/bash\n#SBATCH --ntasks=1\n#SBATCH --cpus-per-task=8\n#SBATCH --mem=8G\nqchem -nt 8 w.in w.out\n"
    assert "qchem-mem-alloc" in codes(run(QCHEM, "w.in", cfg, submit=sub), "warning")  # 8000 of 8192 MB
    f = run(QCHEM.replace("MEM_TOTAL   8000", "MEM_TOTAL   16000"), "w.in", cfg, submit=sub)
    assert "qchem-mem-alloc" in codes(f, "error")
    f = run(QCHEM.replace("MEM_TOTAL   8000", "MEM_TOTAL   7000"), "w.in", cfg, submit=sub.replace("-nt 8", "-nt 16"))
    assert "threads-exceed" in codes(f, "error")


# ------------------------------------------------------------------ Molpro

MOLPRO = """\
***,oh radical
memory,{mem},m
geometry={{
O 0.0 0.0 0.0
H 0.0 0.0 0.97
}}
basis=avtz
set,charge=0
set,spin={spin}
{{rhf}}
{{rccsd(t)}}
"""


def test_molpro(cfg):
    assert serious(run(MOLPRO.format(mem=500, spin=1), "oh.com", cfg)) == []
    f = run(MOLPRO.format(mem=500, spin=2), "oh.com", cfg)
    assert "parity" in codes(f, "error")
    assert "multiplicity" in [x for x in f if x.code == "parity"][0].message
    assert "molpro-memory-large" in codes(run(MOLPRO.format(mem=8000, spin=1), "oh.com", cfg), "warning")


def test_molpro_wf_card(cfg):
    m = MOLPRO.format(mem=500, spin=1).replace("set,charge=0\nset,spin=1\n{rhf}", "{rhf; wf,9,1,2}")
    assert "parity" in codes(run(m, "oh.com", cfg), "error")
    m = MOLPRO.format(mem=500, spin=1).replace("set,charge=0\nset,spin=1\n{rhf}", "{rhf; wf,9,1,1}")
    assert serious(run(m, "oh.com", cfg)) == []


def test_molpro_submit(cfg):
    sub = "#!/bin/bash\n#SBATCH --ntasks=16\n#SBATCH --mem=32G\n/opt/molpro/bin/molpro -n 16 oh.com\n"
    f = run(MOLPRO.format(mem=500, spin=1), "oh.com", cfg, submit=sub)
    assert "molpro-mem-alloc" in codes(f, "error")   # 16 x 3.7 GiB > 32 GiB
    ok = sub.replace("--mem=32G", "--mem=72G")
    assert serious(run(MOLPRO.format(mem=500, spin=1), "oh.com", cfg, submit=ok)) == []


# ------------------------------------------------------------------ PySCF / Psi4

PYSCF = """\
from pyscf import gto, scf
mol = gto.M(atom="O 0 0 0; H 0 0 0.97", basis="cc-pvdz", spin={spin}, charge=0)
mol.max_memory = 4000
mf = scf.UHF(mol).run()
"""


def test_pyscf_spin(cfg):
    assert serious(run(PYSCF.format(spin=1), "oh.py", cfg)) == []
    f = run(PYSCF.format(spin=2), "oh.py", cfg)
    assert "parity" in codes(f, "error")
    msg = [x for x in f if x.code == "parity"][0].message
    assert "multiplicity" in msg and "should be 1" in msg


def test_pyscf_misc(cfg):
    f = run(PYSCF.format(spin=1).replace("mol.max_memory = 4000\n", ""), "oh.py", cfg)
    assert "pyscf-max-memory" in codes(f, "info") and serious(f) == []
    f = run("from pyscf import gto\nmol = gto.M(atom='H 0 0 0'\n", "x.py", cfg)
    assert "python-syntax" in codes(f, "error")
    # attribute style + spin default 0 with an odd electron count
    s = "from pyscf import gto\nmol = gto.Mole()\nmol.atom = [['O', (0, 0, 0)], ['H', (0, 0, 0.97)]]\n" \
        "mol.basis = 'cc-pvdz'\nmol.max_memory = 2000\nmol.build()\n"
    assert "parity" in codes(run(s, "x.py", cfg), "error")
    assert serious(run(s.replace("mol.build()", "mol.spin = 1\nmol.build()"), "x.py", cfg)) == []


PSI4 = """\
memory 8 GB
molecule {{
0 {mult}
O 0.0 0.0 0.0
H 0.0 0.0 0.97
}}
set basis cc-pvdz
{ref}
energy('scf')
"""


def test_psi4(cfg):
    assert serious(run(PSI4.format(mult=2, ref="set reference uhf"), "oh.dat", cfg)) == []
    assert "psi4-reference" in codes(run(PSI4.format(mult=2, ref=""), "oh.dat", cfg), "warning")
    assert "parity" in codes(run(PSI4.format(mult=1, ref=""), "oh.dat", cfg), "error")
    no_mem = PSI4.format(mult=2, ref="set reference uhf").replace("memory 8 GB\n", "")
    assert "psi4-memory-missing" in codes(run(no_mem, "oh.dat", cfg), "warning")


# ------------------------------------------------------------------ submit scripts


def test_parse_slurm_and_pbs():
    s = parse_submit(textwrap.dedent("""\
        #!/bin/bash
        #SBATCH -n 4 -c 2
        #SBATCH --mem-per-cpu=2000
        #SBATCH -t 1-12:00:00
        #SBATCH -p long
        #SBATCH --gres=gpu:a100:2
        srun hostname
        """))
    assert (s.scheduler, s.mpi_tasks, s.cpus_per_task, s.total_cores) == ("slurm", 4, 2, 8)
    assert s.mem_total_mb == 16000 and s.walltime_s == 36 * 3600 and s.partition == "long" and s.gpus == 2
    p = parse_submit((REPO / "knowledge/hpc/templates/pbs_orca.sh").read_text(), "pbs_orca.sh")
    assert (p.scheduler, p.total_cores, p.mpi_tasks, p.mem_total_mb, p.walltime_s) == ("pbs", 16, 16, 64 * 1024, 86400)
    assert [e.program for e in p.executables] == ["orca"] and p.executables[0].absolute
    t = parse_submit("#!/bin/bash\n#PBS -l nodes=2:ppn=8,walltime=10:00:00\n#PBS -q short\n#PBS -l mem=32gb\n")
    assert (t.total_cores, t.mpi_tasks, t.mem_total_mb, t.partition) == (16, 16, 32 * 1024, "short")


def test_submit_program_mismatch(cfg):
    sub = "#!/bin/bash\n#SBATCH --ntasks=1\n#SBATCH --cpus-per-task=4\n#SBATCH --mem=16G\n/opt/g16/g16 < w.inp\n"
    assert "submit-program" in codes(run(ORCA, "w.inp", cfg, submit=sub), "warning")


def test_gaussian_submit_crosschecks(cfg):
    sub = "#!/bin/bash\n#SBATCH --ntasks=1\n#SBATCH --cpus-per-task=2\n#SBATCH --mem=4G\n$G16 < e.gjf > e.log\n"
    f = run(G_ETHYL.format(mult=2), "e.gjf", cfg, submit=sub)
    assert {"gaussian-mem-alloc", "gaussian-nproc-alloc"} <= codes(f, "error")


def test_submit_file_mode_checks_referenced_inputs(tmp_path, cfg):
    (tmp_path / "w.inp").write_text(ORCA)
    (tmp_path / "run.sh").write_text(ORCA_SLURM.replace("--ntasks=4", "--ntasks=2"))
    f = check_input(path=tmp_path / "run.sh", cfg=cfg)
    assert "orca-nprocs-alloc" in codes(f, "error")
    # and from the input's side, the script is found automatically
    f = check_input(path=tmp_path / "w.inp", cfg=cfg)
    assert "orca-nprocs-alloc" in codes(f, "error") and "submit-found" in codes(f)


# ------------------------------------------------------------------ extension point


def test_extra_checks(cfg):
    seen = []

    def my_check(inp: ParsedInput, sub, cfg_):
        seen.append((inp.program, sub.partition if sub else None))
        return [Finding("warning", "cluster-limit", "partition limit exceeded")]

    def broken(inp, sub, cfg_):
        raise RuntimeError("boom")

    EXTRA_CHECKS.extend([my_check, broken])
    try:
        f = run(ORCA, "w.inp", cfg, submit=ORCA_SLURM.replace("#SBATCH --ntasks=4", "#SBATCH --ntasks=4 -p short"))
    finally:
        EXTRA_CHECKS.remove(my_check)
        EXTRA_CHECKS.remove(broken)
    assert seen == [("orca", "short")]
    assert "cluster-limit" in codes(f, "warning") and "extra-check-failed" in codes(f, "info")


# ------------------------------------------------------------------ hook mode + CLI


def _hook(path, cfg, tool="Write"):
    err = io.StringIO()
    code = hook_main(json.dumps({"tool_name": tool, "tool_input": {"file_path": str(path)}}), cfg=cfg, err=err)
    return code, err.getvalue()


def test_hook_mode(tmp_path, cfg):
    readme = tmp_path / "README.md"
    readme.write_text("# notes\n")
    assert _hook(readme, cfg) == (0, "")
    assert _hook(tmp_path / "missing.inp", cfg) == (0, "")
    assert hook_main("not json", cfg=cfg, err=io.StringIO()) == 0
    assert hook_main("{}", cfg=cfg, err=io.StringIO()) == 0

    good = tmp_path / "w.inp"
    good.write_text(ORCA)
    assert _hook(good, cfg) == (0, "")

    warn = tmp_path / "nomaxcore.inp"
    warn.write_text(ORCA.replace("%maxcore 2000\n", ""))
    code, err = _hook(warn, cfg, tool="Edit")
    assert code == 0 and "orca-maxcore-missing" in err

    bad = tmp_path / "bad.gjf"
    bad.write_text(G_ETHYL.format(mult=1))
    code, err = _hook(bad, cfg, tool="MultiEdit")
    assert code == 2 and "parity" in err and "bad.gjf" in err

    script = tmp_path / "run.sh"
    script.write_text(ORCA_SLURM.replace('"$ORCA_BIN" w.inp', 'mpirun "$ORCA_BIN" w.inp'))
    code, err = _hook(script, cfg)
    assert code == 2 and "orca-mpirun" in err


def test_hook_relative_path_uses_cwd(tmp_path, cfg):
    (tmp_path / "bad.gjf").write_text(G_ETHYL.format(mult=1))
    err = io.StringIO()
    payload = {"cwd": str(tmp_path), "tool_input": {"file_path": "bad.gjf"}}
    assert hook_main(json.dumps(payload), cfg=cfg, err=err) == 2


def test_cli(tmp_path, capsys):
    from rag_drg.cli import main

    shutil.copy(VALID / "orca_ts.inp", tmp_path / "orca_ts.inp")
    assert main(["--config", str(REPO / "rag_drg.yaml"), "check-input", str(tmp_path / "orca_ts.inp")]) == 0
    (tmp_path / "bad.gjf").write_text(G_ETHYL.format(mult=1))
    capsys.readouterr()
    assert main(["--config", str(REPO / "rag_drg.yaml"), "check-input", "--json", str(tmp_path / "bad.gjf")]) == 1
    out = json.loads(capsys.readouterr().out)
    assert any(x["code"] == "parity" and x["severity"] == "error" for x in out[str(tmp_path / "bad.gjf")])
    assert main(["--config", str(REPO / "rag_drg.yaml"), "check-input", "--submit",
                 str(VALID / "run_orca.sh"), str(tmp_path / "orca_ts.inp")]) == 0


def test_cli_hook_reads_stdin(tmp_path, monkeypatch, capsys):
    from rag_drg.cli import main

    (tmp_path / "bad.gjf").write_text(G_ETHYL.format(mult=1))
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"tool_input": {"file_path": str(tmp_path / "bad.gjf")}})))
    assert main(["--config", str(REPO / "rag_drg.yaml"), "check-input", "--hook"]) == 2
    assert "parity" in capsys.readouterr().err


# ------------------------------------------------------------------ MCP registration (SDK-independent)


class FakeMCP:
    def __init__(self):
        self.tools = {}

    def tool(self, *a, **kw):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


class FakeCtx:
    def __init__(self, cfg):
        self.cfg = cfg
        self.events = []

    def emit(self, e):
        self.events.append(e)


def test_mcp_tool(cfg):
    mcp, ctx = FakeMCP(), FakeCtx(cfg)
    inputcheck.register_mcp(mcp, ctx)
    tool = mcp.tools["check_input"]
    out = tool(content=G_ETHYL.format(mult=1), filename="e.gjf")
    assert "parity" in out and "1 error" in out
    out = tool(content=ORCA, filename="w.inp", submit_script_content=ORCA_SLURM.replace("--ntasks=4", "--ntasks=2"))
    assert "orca-nprocs-alloc" in out
    assert ctx.events[-1]["tool"] == "check_input"
    assert "no problems" in tool(content=ORCA, filename="w.inp")
