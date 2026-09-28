"""Tests for the ESS input composer (rag_drg/tools/compose_ess.py)."""

import json
import shutil
import textwrap
from pathlib import Path

import pytest

from rag_drg.config import load_config
from rag_drg.tools._compose.theory import PROGRAM_JOBS
from rag_drg.tools.basis import bse_available
from rag_drg.tools.compose_ess import compose_ess_job
from rag_drg.tools.inputcheck import check_input

REPO = Path(__file__).resolve().parent.parent

needs_bse = pytest.mark.skipif(not bse_available(), reason="basis_set_exchange not installed")

WATER = "3\nwater\nO 0.0 0.0 0.1173\nH 0.0 0.7572 -0.4692\nH 0.0 -0.7572 -0.4692\n"
CH3 = "C 0 0 0\nH 1.079 0 0\nH -0.5395 0.9344 0\nH -0.5395 -0.9344 0\n"
CH3I = "C 0.0 0.0 -1.8\nH 1.03 0.0 -2.15\nH -0.515 0.892 -2.15\nH -0.515 -0.892 -2.15\nI 0.0 0.0 0.34\n"
VERSION = {"orca": "6", "gaussian": "16", "qchem": "6.1", "psi4": None, "molpro": "2024", "pyscf": None}


@pytest.fixture(scope="module")
def cfg(tmp_path_factory):
    """A temp project whose servers.yaml is servers.example.yaml, using the repo's knowledge cards."""
    root = tmp_path_factory.mktemp("compose_project")
    (root / "rag_drg.yaml").write_text(textwrap.dedent(f"""\
        index_path: index/t.sqlite
        sources:
          - name: curated
            type: local
            path: {REPO / 'knowledge'}
            doc_type: card
        """))
    shutil.copy(REPO / "servers.example.yaml", root / "servers.yaml")
    return load_config(root / "rag_drg.yaml")


def spec(program="orca", job="sp", method="B3LYP", basis="def2-TZVP", xyz=WATER, mult=1, server=True, **kw):
    s = {"program": program, "version": VERSION.get(program), "job": job, "method": method, "basis": basis,
         "charge": 0, "multiplicity": mult, "molecule": {"xyz": xyz},
         "resources": {"server": "example", "cores": 8, "mem_gb": 32} if server else {"cores": 4, "mem_gb": 8}}
    s.update(kw)
    return s


def problems(res):
    return [(f["code"], f["message"]) for f in res["findings"] if f["severity"] in ("error", "warning")]


def assert_clean(res):
    assert res["ok"], res["errors"]
    assert problems(res) == []
    # re-check independently: the returned files must pass check_input with no errors/warnings
    again = check_input(content=res["input_text"], filename=res["input_name"], submit_content=res["submit_text"])
    assert [f for f in again if f.severity in ("error", "warning")] == []


# ------------------------------------------------------------------ every program x job


def _cases():
    for prog, jobs in PROGRAM_JOBS.items():
        for job in jobs:
            yield prog, job


@needs_bse
@pytest.mark.parametrize("program,job", list(_cases()))
@pytest.mark.parametrize("molecule,mult", [("water", 1), ("methyl", 2)])
def test_compose_all_programs_clean(cfg, program, job, molecule, mult):
    xyz = WATER if molecule == "water" else CH3
    res = compose_ess_job(spec(program, job, xyz=xyz, mult=mult), cfg=cfg)
    assert_clean(res)
    assert res["submit_text"].startswith("#!/bin/bash")
    assert res["submit_name"].endswith(".sh")


@needs_bse
@pytest.mark.parametrize("program", ["orca", "gaussian", "psi4", "molpro", "pyscf"])
def test_heavy_atom_ecp_noted(cfg, program):
    res = compose_ess_job(spec(program, "sp", xyz=CH3I), cfg=cfg)
    assert_clean(res)
    assert any("ECP" in n and "I (28 core electrons)" in n for n in res["notes"])
    if program == "pyscf":
        assert 'ecp="def2-tzvp"' in res["input_text"]
    if program == "gaussian":
        assert "B3LYP/Def2TZVP" in res["input_text"]


@needs_bse
def test_qchem_ecp_refused_unless_allowed(cfg):
    res = compose_ess_job(spec("qchem", "sp", xyz=CH3I), cfg=cfg)
    assert not res["ok"] and res["input_text"] is None
    assert "effective core potential" in res["errors"][0]
    res = compose_ess_job(spec("qchem", "sp", xyz=CH3I, allow_unverified=True), cfg=cfg)
    assert res["ok"]
    assert any("UNVERIFIED ECP" in n for n in res["notes"])


# ------------------------------------------------------------------ program syntax


@needs_bse
def test_orca_input_shape_and_memory_matches_submit(cfg):
    res = compose_ess_job(spec("orca", "opt+freq", method="wB97X-D3", xyz=CH3, mult=2,
                               scf={"convergence": "tight", "max_iter": 300}), cfg=cfg)
    assert_clean(res)
    t = res["input_text"]
    assert "! wB97X-D3 def2-TZVP" in t and "Opt Freq TightSCF" in t
    assert "%pal nprocs 8 end" in t
    assert "%maxcore 3072" in t  # 0.75 * 32 GB / 8 cores
    assert "* xyz 0 2" in t and "MaxIter 300" in t
    assert "#SBATCH --ntasks=8" in res["submit_text"] and "#SBATCH --mem=32G" in res["submit_text"]


@needs_bse
def test_gaussian_structure(cfg):
    res = compose_ess_job(spec("gaussian", "ts", method="wB97X-D", xyz=CH3, mult=2,
                               solvation={"model": "smd", "solvent": "Water"}, grid="ultrafine"), cfg=cfg)
    assert_clean(res)
    t = res["input_text"]
    lines = t.split("\n")
    assert lines[0] == "%nprocshared=8" and lines[1].startswith("%mem=") and lines[2] == "%chk=CH3_ts.chk"
    assert lines[3].startswith("#P wB97XD/Def2TZVP Opt=(TS,CalcFC,NoEigenTest) Freq")
    assert "SCRF=(SMD,Solvent=Water)" in lines[3] and "Int=UltraFine" in lines[3]
    assert lines[4] == "" and lines[6] == "" and lines[7] == "0 2"
    assert t.endswith("\n\n")


@needs_bse
def test_gaussian_g09_grid_spelling(cfg):
    res = compose_ess_job(spec("gaussian", "sp", version="09", grid="ultrafine"), cfg=cfg)
    assert_clean(res)
    assert "Int=Grid=UltraFine" in res["input_text"]
    assert "gaussian-09" in res["submit_text"]


@needs_bse
def test_qchem_multi_step_and_solvent(cfg):
    res = compose_ess_job(spec("qchem", "ts", method="B3LYP", dispersion="D3BJ",
                               solvation={"model": "pcm", "solvent": "Water"}), cfg=cfg)
    assert_clean(res)
    t = res["input_text"]
    assert t.count("@@@") == 2 and "GEOM_OPT_HESSIAN  read" in t
    assert "DFT_D" in t and "D3_BJ" in t and "$solvent" in t and "MEM_TOTAL" in t


@needs_bse
def test_psi4_and_pyscf_open_shell(cfg):
    res = compose_ess_job(spec("psi4", "sp", method="B3LYP", dispersion="D3BJ", xyz=CH3, mult=2), cfg=cfg)
    assert_clean(res)
    assert "reference uks" in res["input_text"] and "energy('b3lyp-d3bj')" in res["input_text"]
    assert any("dftd3" in n for n in res["notes"])
    res = compose_ess_job(spec("pyscf", "sp", method="PBE0", xyz=CH3, mult=2), cfg=cfg)
    assert_clean(res)
    assert "spin=1," in res["input_text"] and "dft.UKS" in res["input_text"] and 'mf.xc = "pbe0"' in res["input_text"]


@needs_bse
def test_molpro_spin_and_memory(cfg):
    res = compose_ess_job(spec("molpro", "sp", method="CCSD(T)", basis="cc-pVTZ", xyz=CH3, mult=2), cfg=cfg)
    assert_clean(res)
    t = res["input_text"]
    assert "set,spin=1" in t and "{rhf}" in t and "{rccsd(t)}" in t and "symmetry,nosym" in t
    assert "memory,472,m" in t  # 0.88 * 32 GB / (8 B x 1e6 x 8 processes)
    assert '"$MOLPRO" -n 8' in res["submit_text"]


@needs_bse
def test_molpro_f12_needs_f12_basis(cfg):
    res = compose_ess_job(spec("molpro", "sp", method="CCSD(T)-F12", basis="cc-pVTZ"), cfg=cfg)
    assert not res["ok"] and "-F12" in res["errors"][0]
    res = compose_ess_job(spec("molpro", "sp", method="CCSD(T)-F12", basis="cc-pVTZ-F12"), cfg=cfg)
    assert_clean(res)
    assert "{ccsd(t)-f12}" in res["input_text"]


# ------------------------------------------------------------------ method mapping


@needs_bse
def test_wb97xd_spelling_gaussian_vs_orca(cfg):
    g = compose_ess_job(spec("gaussian", "sp", method="wB97X-D"), cfg=cfg)
    assert_clean(g)
    assert "#P wB97XD/Def2TZVP" in g["input_text"]
    o = compose_ess_job(spec("orca", "sp", method="wB97X-D"), cfg=cfg)
    assert not o["ok"] and o["input_text"] is None
    assert "variant" in o["errors"][0] and "DIFFERENT method" in o["errors"][0]
    o = compose_ess_job(spec("orca", "sp", method="wB97X-D", allow_unverified=True), cfg=cfg)
    assert o["ok"], o["errors"]
    assert any("UNVERIFIED" in n and "variant" in n for n in o["notes"])


@needs_bse
def test_method_spellings(cfg):
    g = compose_ess_job(spec("gaussian", "sp", method="PBE0"), cfg=cfg)
    assert "#P PBE1PBE/Def2TZVP" in g["input_text"]
    q = compose_ess_job(spec("qchem", "sp", method="M06-2X"), cfg=cfg)
    assert "METHOD" in q["input_text"] and "M06-2X" in q["input_text"]
    o = compose_ess_job(spec("orca", "sp", method="M06-2X"), cfg=cfg)
    assert "! M062X def2-TZVP" in o["input_text"]
    p = compose_ess_job(spec("psi4", "sp", method="HF"), cfg=cfg)
    assert "energy('scf')" in p["input_text"]


def test_unsupported_and_unknown_methods(cfg):
    r = compose_ess_job(spec("gaussian", "sp", method="DLPNO-CCSD(T)"), cfg=cfg)
    assert not r["ok"] and "not available in gaussian" in r["errors"][0]
    r = compose_ess_job(spec("gaussian", "sp", method="DLPNO-CCSD(T)", allow_unverified=True), cfg=cfg)
    assert not r["ok"]  # 'no' is never written
    r = compose_ess_job(spec("orca", "sp", method="CASSCF"), cfg=cfg)
    assert not r["ok"] and "active space" in r["errors"][0]
    r = compose_ess_job(spec("orca", "sp", method="SuperFunctional-2030"), cfg=cfg)
    assert not r["ok"] and "not in the levels-of-theory table" in r["errors"][0]


@needs_bse
def test_dlpno_adds_correlation_aux_basis(cfg):
    res = compose_ess_job(spec("orca", "sp", method="DLPNO-CCSD(T)", basis="cc-pVTZ", xyz=CH3, mult=2), cfg=cfg)
    assert_clean(res)
    assert "! DLPNO-CCSD(T) cc-pVTZ cc-pVTZ/C" in res["input_text"]
    assert any("cc-pVTZ/C" in n and "added automatically" in n for n in res["notes"])
    res = compose_ess_job(spec("orca", "sp", method="RI-MP2", basis="def2-TZVP"), cfg=cfg)
    assert_clean(res)
    assert "! RI-MP2 def2-TZVP def2-TZVP/C" in res["input_text"]
    # Psi4's DLPNO is closed-shell only
    res = compose_ess_job(spec("psi4", "sp", method="DLPNO-CCSD(T)", basis="cc-pVTZ", xyz=CH3, mult=2), cfg=cfg)
    assert not res["ok"] and "closed-shell" in res["errors"][0]
    res = compose_ess_job(spec("psi4", "sp", method="DLPNO-CCSD(T)", basis="cc-pVTZ"), cfg=cfg)
    assert_clean(res)
    assert "energy('dlpno-ccsd(t)')" in res["input_text"]


@needs_bse
def test_cc_geometry_jobs_refused(cfg):
    r = compose_ess_job(spec("orca", "opt", method="DLPNO-CCSD(T)", basis="cc-pVTZ"), cfg=cfg)
    assert not r["ok"] and "single point" in r["errors"][0]


@needs_bse
def test_dispersion_rules(cfg):
    r = compose_ess_job(spec("orca", "sp", method="wB97X-D3", dispersion="D3BJ"), cfg=cfg)
    assert not r["ok"] and "twice" in r["errors"][0]
    r = compose_ess_job(spec("gaussian", "sp", method="B3LYP-D3BJ"), cfg=cfg)
    assert_clean(r)
    assert "EmpiricalDispersion=GD3BJ" in r["input_text"]
    r = compose_ess_job(spec("gaussian", "sp", method="B3LYP", dispersion="D4"), cfg=cfg)
    assert not r["ok"]


@needs_bse
def test_r2scan3c_has_no_basis(cfg):
    r = compose_ess_job(spec("orca", "opt", method="r2SCAN-3c", basis=None), cfg=cfg)
    assert_clean(r)
    assert "! r2SCAN-3c\n" in r["input_text"]
    r = compose_ess_job(spec("orca", "opt", method="r2SCAN-3c"), cfg=cfg)
    assert not r["ok"] and "3c" in r["errors"][0]


# ------------------------------------------------------------------ spec validation


def test_multiplicity_parity_error(cfg):
    r = compose_ess_job(spec("orca", "sp", xyz=CH3, mult=1), cfg=cfg)
    assert not r["ok"] and r["input_text"] is None and r["submit_text"] is None
    assert "cannot have multiplicity 1" in r["errors"][0]
    r = compose_ess_job(spec("orca", "sp", mult=None), cfg=cfg)
    assert not r["ok"] and "multiplicity is required" in r["errors"][0]


def test_bad_fields_reported_together(cfg):
    r = compose_ess_job({"program": "nwchem", "job": "md", "method": "", "bogus": 1}, cfg=cfg)
    assert not r["ok"]
    joined = " ".join(r["errors"])
    for word in ("program", "job", "method", "bogus", "resources", "molecule"):
        assert word in joined


def test_job_not_supported_by_program(cfg):
    r = compose_ess_job(spec("psi4", "irc"), cfg=cfg)
    assert not r["ok"] and "not generated for psi4" in r["errors"][0]
    r = compose_ess_job(spec("pyscf", "ts"), cfg=cfg)
    assert not r["ok"]


def test_xyz_file_refused_without_allow_files(cfg, tmp_path):
    f = tmp_path / "w.xyz"
    f.write_text(WATER)
    s = spec("orca", "sp")
    s["molecule"] = {"xyz_file": str(f)}
    r = compose_ess_job(s, cfg=cfg)
    assert not r["ok"] and "never reads files" in r["errors"][0]


@needs_bse
def test_xyz_file_allowed_locally(cfg, tmp_path):
    f = tmp_path / "w.xyz"
    f.write_text(WATER)
    s = spec("orca", "sp")
    s["molecule"] = {"xyz_file": str(f)}
    assert compose_ess_job(s, cfg=cfg, allow_files=True)["ok"]


@needs_bse
def test_smiles_is_flagged_rough(cfg):
    pytest.importorskip("rdkit")
    s = spec("orca", "opt", mult=None)
    s["molecule"] = {"smiles": "[CH3]"}
    r = compose_ess_job(s, cfg=cfg)
    assert_clean(r)
    assert "* xyz 0 2" in r["input_text"]
    assert any("ROUGH STARTING GEOMETRY" in n for n in r["notes"])


@needs_bse
def test_standalone_no_submit_script(cfg):
    r = compose_ess_job(spec("gaussian", "sp", server=False), cfg=cfg)
    assert_clean(r)
    assert r["submit_text"] is None and r["submit_name"] is None
    assert "%nprocshared=4" in r["input_text"] and "%mem=7GB" in r["input_text"]


@needs_bse
def test_unknown_server(cfg):
    s = spec("orca", "sp")
    s["resources"]["server"] = "nope"
    r = compose_ess_job(s, cfg=cfg)
    assert not r["ok"] and "unknown server" in r["errors"][0]


@needs_bse
def test_ambiguous_software_needs_version(cfg):
    s = spec("orca", "sp", version=None)
    r = compose_ess_job(s, cfg=cfg)
    assert not r["ok"] and "several orca installs" in r["errors"][0]


@needs_bse
def test_submit_limits_propagate(cfg):
    s = spec("orca", "sp")
    s["resources"]["cores"] = 4096
    r = compose_ess_job(s, cfg=cfg)
    assert not r["ok"] and "cores per node" in r["errors"][0]


# ------------------------------------------------------------------ protocols


PROTOCOL = {
    "program": "orca", "version": "6", "method": "B3LYP", "basis": "def2-SVP", "dispersion": "D3BJ",
    "scf": {"convergence": "tight"},
    "resources": {"server": "example", "cores": 8, "mem_gb": 32},
    "steps": {
        "opt": {"job": "opt+freq"},
        "sp": {"job": "sp", "method": "DLPNO-CCSD(T)", "basis": "cc-pVTZ", "dispersion": None,
               "scf": {"convergence": "verytight"}},
    },
}


@needs_bse
def test_protocol_merge_precedence(cfg):
    explicit = {"charge": 0, "multiplicity": 1, "molecule": {"xyz": WATER}, "resources": {"cores": 4}}
    r = compose_ess_job(explicit, protocol=PROTOCOL, step="opt", cfg=cfg)
    assert_clean(r)
    t = r["input_text"]
    assert "! B3LYP def2-SVP D3BJ" in t and "Opt Freq TightSCF" in t
    assert "%pal nprocs 4 end" in t  # explicit cores win over the protocol's 8
    assert "#SBATCH --mem=32G" in r["submit_text"]  # protocol memory kept (deep merge)
    # the step overrides the protocol defaults; the explicit spec overrides the step
    sp = dict(explicit, basis="aug-cc-pVTZ")
    sp["dispersion"] = None
    r = compose_ess_job(sp, protocol=PROTOCOL, step="sp", cfg=cfg)
    assert r["ok"], r["errors"]
    assert "! DLPNO-CCSD(T) aug-cc-pVTZ aug-cc-pVTZ/C" in r["input_text"]
    assert "VeryTightSCF" in r["input_text"]
    assert any("protocol step 'sp'" in n for n in r["notes"])


def test_protocol_unknown_step(cfg):
    r = compose_ess_job({"multiplicity": 1, "molecule": {"xyz": WATER}}, protocol=PROTOCOL, step="nope", cfg=cfg)
    assert not r["ok"] and "no step 'nope'" in r["errors"][0]


@needs_bse
def test_example_protocol_file_composes(cfg):
    import yaml

    proto = yaml.safe_load((REPO / "examples" / "protocols" / "example_protocol.yaml").read_text())
    for step in proto["steps"]:
        r = compose_ess_job({"charge": 0, "multiplicity": 1, "molecule": {"xyz": WATER},
                             "resources": {"server": "example"}}, protocol=proto, step=step, cfg=cfg)
        assert r["ok"], (step, r["errors"])
        assert problems(r) == []


# ------------------------------------------------------------------ CLI and MCP


@needs_bse
def test_cli_writes_files(cfg, tmp_path, capsys):
    from rag_drg.cli import main

    xyz = tmp_path / "ch3.xyz"
    xyz.write_text(CH3)
    out = tmp_path / "run"
    rc = main(["--config", str(cfg.root / "rag_drg.yaml"), "compose", "--program", "orca", "--version", "6",
               "--job", "opt", "--method", "B3LYP", "--basis", "def2-SVP", "--charge", "0", "--mult", "2",
               "--xyz", str(xyz), "--server", "example", "--cores", "8", "--mem", "32", "--out-dir", str(out)])
    assert rc == 0, capsys.readouterr()
    assert (out / "CH3_opt.inp").is_file() and (out / "CH3_opt.sh").is_file()
    capsys.readouterr()
    rc = main(["--config", str(cfg.root / "rag_drg.yaml"), "compose", "--program", "orca", "--version", "6",
               "--job", "opt", "--method", "B3LYP", "--basis", "def2-SVP", "--charge", "0", "--mult", "1",
               "--xyz", str(xyz), "--server", "example", "--json"])
    assert rc == 1
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is False and "multiplicity 1" in data["errors"][0]


@needs_bse
def test_cli_spec_file_with_protocol(cfg, tmp_path, capsys):
    from rag_drg.cli import main

    (tmp_path / "w.xyz").write_text(WATER)
    (tmp_path / "proto.yaml").write_text(json.dumps(PROTOCOL))
    (tmp_path / "job.yaml").write_text(textwrap.dedent("""\
        protocol: proto.yaml
        step: opt
        charge: 0
        multiplicity: 1
        molecule: {xyz_file: w.xyz}
        """))
    rc = main(["--config", str(cfg.root / "rag_drg.yaml"), "compose", str(tmp_path / "job.yaml"), "--json"])
    data = json.loads(capsys.readouterr().out)
    assert rc == 0, data["errors"]
    assert "Opt Freq" in data["input_text"]


class _FakeMCP:
    def __init__(self):
        self.tools = {}

    def tool(self, name=None):
        def deco(fn):
            self.tools[name or fn.__name__] = fn
            return fn
        return deco


class _Ctx:
    def __init__(self, cfg):
        self.cfg = cfg
        self.events = []

    def emit(self, e):
        self.events.append(e)


@needs_bse
def test_mcp_tool_never_reads_files(cfg, tmp_path):
    from rag_drg.tools import compose_ess

    mcp, ctx = _FakeMCP(), _Ctx(cfg)
    compose_ess.register_mcp(mcp, ctx)
    tool = mcp.tools["compose_ess_job"]
    secret = tmp_path / "w.xyz"
    secret.write_text(WATER)
    s = spec("orca", "sp")
    s["molecule"] = {"xyz_file": str(secret)}
    out = json.loads(tool(spec=s))
    assert out["ok"] is False and "never reads files" in out["errors"][0]
    out = json.loads(tool(spec=spec("orca", "sp"), protocol=str(tmp_path / "p.yaml")))
    assert out["ok"] is False and "does not read protocol files" in out["errors"][0]
    out = json.loads(tool(spec=spec("orca", "sp")))
    assert out["ok"] is True and out["input_text"].startswith("#")
    assert ctx.events[-1]["tool"] == "compose_ess_job"
