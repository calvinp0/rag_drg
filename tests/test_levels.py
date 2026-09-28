import shutil
from pathlib import Path

import yaml

from rag_drg.chunking import chunk_file
from rag_drg.ingest import ingest
from rag_drg.levels import lookup
from rag_drg.search import Searcher

REPO_TABLE = Path(__file__).resolve().parent.parent / "knowledge" / "ess" / "levels_of_theory.yaml"


def test_repo_table_is_well_formed():
    data = yaml.safe_load(REPO_TABLE.read_text())
    codes = set(data["codes"])
    assert {"gaussian", "orca", "qchem", "psi4", "molpro", "pyscf"} <= codes
    allowed = {"yes", "no", "variant", "partial", "unknown", True, False}
    names = set()
    for e in data["levels_of_theory"]:
        assert e["name"] not in names, f"duplicate {e['name']}"
        names.add(e["name"])
        assert set(e["codes"]) <= codes, e["name"]
        for code, c in e["codes"].items():
            assert c.get("support") in allowed, (e["name"], code)
            if c.get("support") in ("yes", True):
                assert c.get("keyword"), f"{e['name']}/{code}: 'yes' needs a keyword"


def test_lookup_matches_aliases_and_levels(project):
    shutil.copy(REPO_TABLE, project.root / "knowledge" / "ess" / "levels_of_theory.yaml")
    out = lookup(project, "wb97xd/def2tzvp", software="orca")
    assert "ωB97X-D" in out and "| orca | different method" in out
    assert "| gaussian |" not in out  # filtered to one code
    assert "Supported as-is in:" in out and "gaussian" in out.split("Supported as-is in:")[1]
    full = lookup(project, "DLPNO-CCSD(T)")
    assert "| gaussian | no |" in full and "| orca | yes |" in full
    assert "Did you mean" in lookup(project, "wb97")
    assert "not in the levels-of-theory table" in lookup(project, "zzzz-unknown-method")
    assert "Known levels of theory" in lookup(project, None)


def test_levels_table_is_indexed_one_chunk_per_method(project):
    dest = project.root / "knowledge" / "ess" / "levels_of_theory.yaml"
    shutil.copy(REPO_TABLE, dest)
    meta, chunks = chunk_file(dest, "ess/levels_of_theory.yaml")
    assert meta["domain"] == "ess" and meta["status"] == "draft"
    assert any(c.title == "Levels of theory > r2SCAN-3c" for c in chunks)
    ingest(project, progress=lambda *_: None)
    hits = Searcher(project).search("which codes support r2SCAN-3c", k=3)
    assert hits[0].chunk.title == "Levels of theory > r2SCAN-3c"


def test_version_filter_accepts_leading_zero(project):
    ingest(project, progress=lambda *_: None)
    s = Searcher(project)
    # The Gaussian card in the fixture is version "16"; add a "09" one.
    card = project.root / "knowledge" / "ess" / "gaussian" / "g09.md"
    card.write_text("---\ntitle: G09\ndomain: ess\nsoftware: gaussian\nversion: '09'\n---\n## Grid\nFineGrid default\n")
    ingest(project, progress=lambda *_: None)
    assert s.search("FineGrid", software="gaussian", version="9")
    assert s.search("FineGrid", software="gaussian", version="09")
    assert not s.search("FineGrid", software="gaussian", version="16")
