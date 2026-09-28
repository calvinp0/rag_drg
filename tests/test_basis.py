import builtins
import json
from pathlib import Path

import pytest

pytest.importorskip("basis_set_exchange")

from rag_drg.tools import basis  # noqa: E402
from rag_drg.tools.basis import check_basis, elements_from_xyz, format_report, resolve_basis  # noqa: E402

REPO = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize("spelling, canonical", [
    ("def2-TZVP", "def2-TZVP"),
    ("Def2TZVP", "def2-TZVP"),            # Gaussian
    ("def2tzvp", "def2-TZVP"),
    ("DEF2-svp", "def2-SVP"),
    ("6-31G(d)", "6-31G*"),
    ("6-31G*", "6-31G*"),
    ("6-31g(d,p)", "6-31G**"),
    ("6-31G**", "6-31G**"),
    ("6-31+G(d)", "6-31+G*"),
    ("6-311++G(d,p)", "6-311++G**"),
    ("6-311+G(2d,p)", "6-311+G(2d,p)"),
    ("cc-pvtz", "cc-pVTZ"),
    ("vdz", "cc-pVDZ"),                   # Molpro shorthands
    ("vtz", "cc-pVTZ"),
    ("VQZ", "cc-pVQZ"),
    ("avdz", "aug-cc-pVDZ"),
    ("avtz", "aug-cc-pVTZ"),
    ("avqz", "aug-cc-pVQZ"),
    ("vtz-f12", "cc-pVTZ-F12"),
    ("def2-TZVP/C", "def2-TZVP-RIFIT"),   # ORCA auxiliary suffixes
    ("cc-pVTZ/C", "cc-pVTZ-RIFIT"),
    ("aug-cc-pVTZ/C", "aug-cc-pVTZ-RIFIT"),
    ("cc-pVTZ/JK", "cc-pVTZ-JKFIT"),
    ("def2/J", "def2-universal-JFIT"),
    ("def2/JK", "def2-universal-JKFIT"),
    ("def2-SVP/J", "def2-universal-JFIT"),
    ("cc-pVTZ-F12-CABS", "cc-pVTZ-F12-OPTRI"),
    ("LANL2DZ", "LANL2DZ"),
])
def test_normalisation(spelling, canonical):
    r = resolve_basis(spelling)
    assert r["name"] == canonical, r


def test_common_keywords_are_not_basis_sets():
    for kw in ["Opt", "Freq", "TightSCF", "D3BJ", "RIJCOSX", "OptTS", "UKS", "HF", "MP2", "PAL8", "DefGrid3"]:
        assert resolve_basis(kw)["key"] is None, kw


def test_unknown_gives_suggestions():
    r = check_basis("defTZVP")
    assert not r["found"] and "def2-TZVP" in r["suggestions"]
    assert "Did you mean" in r["message"]
    assert not check_basis("Gen")["found"]


def test_coverage_ecp_and_aux():
    r = check_basis("def2-TZVP", elements=["C", "H", "I"])
    assert r["found"] and r["missing"] == [] and r["ecp"] == {"I": 28}
    assert any("def2-TZVP-RIFIT" in v for v in r["auxiliaries"].values())
    assert any("def2-universal-JKFIT" in v for v in r["auxiliaries"].values())
    r = check_basis("cc-pVTZ", elements="C,H,I")
    assert r["missing"] == ["I"] and "MISSING" in r["message"]
    r = check_basis("6-31G(d,p)", elements="C,H,Br,I")
    assert r["canonical"] == "6-31G**" and r["missing"] == ["I"]
    r = check_basis("LANL2DZ", elements=["Pt"])
    assert r["ecp"]["Pt"] == 60
    assert "Programs ship their own" in r["note"]


def test_elements_from_xyz_and_errors():
    xyz = "3\nwater\nO 0 0 0\nH 0 0.757 0.587\nH 0 -0.757 0.587\n"
    assert elements_from_xyz(xyz) == ["H", "O"]
    assert check_basis("cc-pVDZ", xyz=xyz)["covered"] == ["H", "O"]
    r = check_basis("cc-pVDZ", elements=["Xx"])
    assert "error" in r and "Xx" in r["message"]


def test_smiles():
    pytest.importorskip("rdkit")
    r = check_basis("def2-SVP", smiles="CCI")
    assert r["elements"] == ["H", "C", "I"] and r["ecp"] == {"I": 28}
    assert check_basis("cc-pVDZ", smiles="C(")["error"]


def test_smiles_without_rdkit(monkeypatch):
    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name.startswith("rdkit"):
            raise ImportError("no rdkit")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    r = check_basis("def2-SVP", smiles="CCO")
    assert "RDKit" in r["error"] and "rag-drg[chem]" in r["error"]


def test_program_spelling():
    assert check_basis("def2-TZVP", software="gaussian")["program_spelling"] == "Def2TZVP"
    assert check_basis("cc-pVTZ", software="psi4")["program_spelling"] == "cc-pvtz"


def test_format_report():
    out = format_report(check_basis("avtz", elements=["O", "H"]))
    assert "aug-cc-pVTZ" in out and "Molpro shorthand" in out and "Missing:   -" in out


def test_cli(tmp_path, capsys):
    from rag_drg.cli import main

    cfg = str(REPO / "rag_drg.yaml")
    assert main(["--config", cfg, "basis", "def2-TZVP", "--elements", "C,H,I"]) == 0
    assert main(["--config", cfg, "basis", "cc-pVTZ", "--elements", "C,I"]) == 1
    xyz = tmp_path / "m.xyz"
    xyz.write_text("2\n\nC 0 0 0\nI 2.1 0 0\n")
    capsys.readouterr()
    assert main(["--config", cfg, "basis", "def2-SVP", "--xyz", str(xyz), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["ecp"] == {"I": 28}
    assert main(["--config", cfg, "basis", "def2-SVP", "--elements", "Qq"]) == 2


def test_mcp_tool():
    class FakeMCP:
        tools = {}

        def tool(self, *a, **k):
            def deco(fn):
                self.tools[fn.__name__] = fn
                return fn
            return deco

    class Ctx:
        def emit(self, e):
            self.e = e

    mcp = FakeMCP()
    basis.register_mcp(mcp, Ctx())
    out = mcp.tools["check_basis"]("Def2TZVP", elements=["C", "I"])
    assert "def2-TZVP" in out and "I (28 core e-)" in out
