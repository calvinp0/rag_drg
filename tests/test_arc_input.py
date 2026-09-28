"""ARC input schema generator + input.yml checker (rag_drg/tools/arc_input.py, rag_drg/tools/_arc/)."""

import io
import json
import re
import textwrap
from pathlib import Path

import pytest

from rag_drg.config import load_config
from rag_drg.tools._arc.schema import (doc_sections, generate_schema, git_commit, load_schema, read_schema_file,
                                       schema_sections, snapshot_path, write_schema)
from rag_drg.tools.arc_input import check_arc_input, is_arc_input

REPO = Path(__file__).resolve().parent.parent
FIX = Path(__file__).parent / "fixtures" / "arc"
CLONE = REPO / "sources_cache" / "arc"
HAS_CLONE = (CLONE / "arc" / "main.py").is_file()
needs_clone = pytest.mark.skipif(not HAS_CLONE, reason="no ARC clone in sources_cache/arc")


@pytest.fixture(scope="module")
def cfg():
    return load_config(REPO / "rag_drg.yaml")


@pytest.fixture(scope="module")
def snap():
    data = read_schema_file(snapshot_path())
    assert data is not None
    return data


@pytest.fixture(scope="module")
def generated():
    if not HAS_CLONE:
        pytest.skip("no ARC clone in sources_cache/arc")
    return generate_schema(CLONE)


def codes(findings, severity=None):
    return {f.code for f in findings if severity is None or f.severity == severity}


def errors(findings):
    return [f for f in findings if f.severity == "error"]


def check(text, cfg, snap, **kw):
    return check_arc_input(textwrap.dedent(text), "input.yml", cfg=cfg, schema=snap, **kw)


# ------------------------------------------------------------------ schema


def _assert_schema_shape(s):
    arc = s["arc"]["params"]
    assert len(arc) >= 45
    for key in ("project", "species", "reactions", "level_of_theory", "job_memory", "max_job_time", "job_types",
                "ess_settings", "specific_job_type", "n_confs", "e_confs", "ts_adapters", "adaptive_levels"):
        assert key in arc, key
    assert s["arc"]["required"] == ["project"]
    assert arc["n_confs"]["default"] == 10 and arc["n_confs"]["type"] == "int"
    assert arc["e_confs"]["default"] == 5.0
    assert arc["verbose"]["default_is_literal"] is False and arc["verbose"]["default_repr"] == "logging.INFO"
    assert "GB" in arc["job_memory"]["description"]
    assert arc["job_memory"]["default_from_settings"] == "default_job_settings['job_total_memory_gb']"
    for key in ("label", "smiles", "inchi", "adjlist", "xyz", "multiplicity", "charge", "is_ts", "bdes"):
        assert key in s["species"]["params"], key
    assert {"label", "reactants", "products", "ts_xyz_guess", "multiplicity"} <= set(s["reaction"]["params"])
    assert "year" in s["level"]["dict_keys"] and "method" in s["level"]["dict_keys"]
    assert "repr" not in s["level"]["dict_keys"]
    jt = s["job_types"]
    assert {"conf_opt", "opt", "fine", "freq", "sp", "rotors", "irc", "stability", "onedmin", "bde"} <= set(jt["keys"])
    assert jt["legacy_aliases"] == {"fine_grid": "fine", "lennard_jones": "onedmin"} or \
        {"fine_grid": "fine", "lennard_jones": "onedmin"}.items() <= jt["legacy_aliases"].items()
    assert jt["renamed_error"].get("1d_rotors") == "rotors"
    assert {"gaussian", "orca", "qchem", "molpro"} <= set(s["supported_ess"])
    assert {"heuristics", "gaussian"} <= set(s["job_adapters"])
    assert s["levels_ess"]["orca"] == ["dlpno"]
    assert re.fullmatch(r"[0-9a-f]{40}", s["arc_commit"])


def test_snapshot_is_valid(snap):
    _assert_schema_shape(snap)


@needs_clone
def test_generator_on_clone(generated, snap):
    _assert_schema_shape(generated)
    assert generated["arc_commit"] == git_commit(CLONE)
    assert len(generated["arc"]["params"]) == 51 or generated["arc_commit"] != snap["arc_commit"]
    if generated["arc_commit"] == snap["arc_commit"]:
        # the committed snapshot is exactly what the generator produces for that commit
        assert json.loads(json.dumps(generated)) == {k: v for k, v in snap.items() if k != "_origin"}


@needs_clone
def test_write_and_reload(generated, tmp_path):
    for name in ("s.json", "s.yaml"):
        out = write_schema(generated, tmp_path / name)
        again = read_schema_file(out)
        assert again["arc"]["params"].keys() == generated["arc"]["params"].keys()


@needs_clone
def test_load_schema_prefers_clone_and_caches(cfg, tmp_path):
    s = load_schema(cfg)
    assert s["_origin"] in ("clone", "index-cache")
    assert (Path(cfg.index_path).parent / "arc_input_schema.json").is_file()


def test_load_schema_falls_back_to_snapshot(tmp_path):
    s = load_schema(None, arc_path=tmp_path / "no-clone")
    assert s["_origin"] == "snapshot"


def test_doc_sections():
    doc = textwrap.dedent("""\
        Summary.

        Args:
            a (int, optional): First line
                               continued.
            b: No type.
                Note:
                    nested note stays in b.

        Attributes:
            c (str): An attribute.
        """)
    d = doc_sections(doc)
    assert d["Args"]["a"] == {"type": "int, optional", "description": "First line continued."}
    assert "nested note" in d["Args"]["b"]["description"]
    assert d["Attributes"]["c"]["description"] == "An attribute."


def test_snapshot_chunks_one_per_parameter(snap):
    from rag_drg.chunking import chunk_file

    meta, chunks = chunk_file(snapshot_path(), "arc/input_schema.snapshot.yaml")
    assert meta["doc_type"] == "schema" and meta["software"] == "arc" and meta["domain"] == "arc"
    titles = [c.title for c in chunks]
    assert "ARC input > level_of_theory" in titles
    assert "ARC input > job_memory" in titles
    assert "ARC input > species > smiles" in titles
    assert "ARC input > level dict > method" in titles
    jm = next(c for c in chunks if c.title == "ARC input > job_memory")
    assert "GB" in jm.text and "Default" in jm.text and "int" in jm.text
    assert len([t for t in titles if t.count(" > ") == 1]) == len(snap["arc"]["params"]) + 1
    assert len(schema_sections(snap)) == len(chunks)


# ------------------------------------------------------------------ valid inputs (false-positive guard)


@pytest.mark.parametrize("path", sorted((FIX / "examples").rglob("*.yml")), ids=lambda p: p.parent.name)
def test_fixture_examples_have_no_errors(path, cfg, snap):
    findings = check_arc_input(path.read_text(), path.name, cfg=cfg, schema=snap)
    assert errors(findings) == []
    assert not [f for f in findings if f.severity == "warning"]


@needs_clone
def test_all_clone_examples_have_no_errors(cfg, snap, generated):
    paths = sorted((CLONE / "examples").rglob("*.yml"))
    assert paths
    for schema in (snap, generated):
        for p in paths:
            findings = check_arc_input(p.read_text(), p.name, cfg=cfg, schema=schema)
            assert errors(findings) == [], (p, [f.format() for f in errors(findings)])


def test_minimal_is_clean(cfg, snap):
    assert check("""\
        project: minimal
        species:
          - label: H2
            smiles: '[H][H]'
        """, cfg, snap) == []


# ------------------------------------------------------------------ invalid inputs, one per check


BAD = [
    ("yaml", "project: x\nspecies:\n  - label: a\n   smiles: C\n", "arc-yaml-error", "error"),
    ("unknown-top", "project: x\njob_memmory: 14\nspecies: [{label: a, smiles: C}]\n", "arc-unknown-key", "error"),
    ("species-key-at-top", "project: x\nsmiles: C\nspecies: [{label: a, smiles: C}]\n", "arc-unknown-key", "error"),
    ("project-missing", "species: [{label: a, smiles: C}]\n", "arc-project-missing", "error"),
    ("project-chars", "project: my project\nspecies: [{label: a, smiles: C}]\n", "arc-project-name", "error"),
    ("number-type", "project: x\njob_memory: 14 GB\nspecies: [{label: a, smiles: C}]\n", "arc-type", "error"),
    ("number-quoted", "project: x\nn_confs: '10'\nspecies: [{label: a, smiles: C}]\n", "arc-type", "error"),
    ("species-not-list", "project: x\nspecies:\n  label: a\n  smiles: C\n", "arc-species-type", "error"),
    ("species-entry-str", "project: x\nspecies: [CCO]\n", "arc-species-type", "error"),
    ("species-unknown-key", "project: x\nspecies:\n  - label: a\n    smiles: C\n    multiplicty: 3\n",
     "arc-species-unknown-key", "error"),
    ("species-no-label", "project: x\nspecies:\n  - smiles: C\n", "arc-species-label", "error"),
    ("species-label-type", "project: x\nspecies:\n  - label: 1\n    smiles: C\n", "arc-species-label", "error"),
    ("species-dup", "project: x\nspecies:\n  - {label: a, smiles: C}\n  - {label: a, smiles: CC}\n",
     "arc-species-duplicate", "error"),
    ("species-no-structure", "project: x\nspecies:\n  - label: a\n    charge: 0\n", "arc-species-no-structure", "error"),
    ("species-TS-name", "project: x\nspecies:\n  - {label: TS1, smiles: C}\n", "arc-species-label", "error"),
    ("species-mult-type", "project: x\nspecies:\n  - {label: a, smiles: C, multiplicity: triplet}\n", "arc-type",
     "error"),
    ("ts-no-source", "project: x\nspecies:\n  - {label: ts, is_ts: true}\n", "arc-ts-no-source", "warning"),
    ("reactions-not-list", "project: x\nspecies: [{label: a, smiles: C}]\nreactions: {label: a <=> a}\n",
     "arc-reaction-type", "error"),
    ("reaction-unknown-key", "project: x\nspecies: [{label: a, smiles: C}, {label: b, smiles: CC}]\n"
     "reactions:\n  - label: a <=> b\n    ts_guess: x\n", "arc-reaction-unknown-key", "error"),
    ("reaction-arrow", "project: x\nspecies: [{label: a, smiles: C}, {label: b, smiles: CC}]\n"
     "reactions:\n  - label: a => b\n", "arc-reaction-label", "error"),
    ("reaction-arrow-spaces", "project: x\nspecies: [{label: a, smiles: C}, {label: b, smiles: CC}]\n"
     "reactions:\n  - label: a<=>b\n", "arc-reaction-label", "error"),
    ("reaction-missing-species", "project: x\nspecies: [{label: a, smiles: C}]\nreactions:\n  - label: a <=> b\n",
     "arc-reaction-species", "error"),
    ("reaction-reactants-missing", "project: x\nspecies: [{label: a, smiles: C}]\n"
     "reactions:\n  - {reactants: [a], products: [c]}\n", "arc-reaction-species", "error"),
    ("reaction-empty", "project: x\nspecies: [{label: a, smiles: C}]\nreactions:\n  - {multiplicity: 1}\n",
     "arc-reaction-label", "error"),
    ("reaction-ts-label", "project: x\nspecies: [{label: a, smiles: C}, {label: b, smiles: CC}]\n"
     "reactions:\n  - {label: a <=> b, ts_label: TSx}\n", "arc-reaction-ts", "warning"),
    ("job-types-type", "project: x\nspecies: [{label: a, smiles: C}]\njob_types: [opt, freq]\n",
     "arc-job-types-type", "error"),
    ("job-type-unknown", "project: x\nspecies: [{label: a, smiles: C}]\njob_types: {rotor: false}\n",
     "arc-job-type-unknown", "error"),
    ("job-type-yes-no", "project: x\nspecies: [{label: a, smiles: C}]\njob_types: {rotors: no}\n",
     "arc-job-type-value", "error"),
    ("job-type-legacy", "project: x\nspecies: [{label: a, smiles: C}]\njob_types: {fine_grid: true}\n",
     "arc-job-type-legacy", "info"),
    ("specific-stability", "project: x\nspecies: [{label: a, smiles: C}]\nspecific_job_type: stability\n",
     "arc-specific-stability", "error"),
    ("specific-unknown", "project: x\nspecies: [{label: a, smiles: C}]\nspecific_job_type: optfreq\n",
     "arc-specific-job-type", "error"),
    ("specific-with-job-types", "project: x\nspecies: [{label: a, smiles: C}]\nspecific_job_type: opt\n"
     "job_types: {rotors: false}\n", "arc-specific-with-job-types", "warning"),
    ("specific-fine", "project: x\nspecies: [{label: a, smiles: C}]\nspecific_job_type: fine\n",
     "arc-specific-job-type-alias", "warning"),
    ("level-conflict", "project: x\nspecies: [{label: a, smiles: C}]\nlevel_of_theory: b3lyp/6-31g\n"
     "opt_level: b3lyp/6-31g\n", "arc-level-conflict", "error"),
    ("level-lot-dict", "project: x\nspecies: [{label: a, smiles: C}]\nlevel_of_theory: {method: b3lyp}\n",
     "arc-level-type", "error"),
    ("level-lot-slashes", "project: x\nspecies: [{label: a, smiles: C}]\nlevel_of_theory: a/b//c/d//e/f\n",
     "arc-level-format", "error"),
    ("level-spaces", "project: x\nspecies: [{label: a, smiles: C}]\nsp_level: dlpno-ccsd(t)/def2-tzvp def2-tzvp/c\n",
     "arc-level-format", "error"),
    ("level-two-slashes", "project: x\nspecies: [{label: a, smiles: C}]\nsp_level: dlpno-ccsd(t)/def2-tzvp/c\n",
     "arc-level-format", "error"),
    ("level-unknown-key", "project: x\nspecies: [{label: a, smiles: C}]\nopt_level: {method: b3lyp, basis_set: x}\n",
     "arc-level-unknown-key", "error"),
    ("level-no-method", "project: x\nspecies: [{label: a, smiles: C}]\nopt_level: {basis: def2svp}\n",
     "arc-level-method", "error"),
    ("level-solvation", "project: x\nspecies: [{label: a, smiles: C}]\nopt_level: {method: b3lyp, solvent: water}\n",
     "arc-level-solvation", "error"),
    ("level-year-non-arkane", "project: x\nspecies: [{label: a, smiles: C}]\nsp_level: {method: b97d3, year: 2023}\n",
     "arc-level-year", "warning"),
    ("level-year-in-method", "project: x\nspecies: [{label: a, smiles: C}]\nopt_level: b97d32023/def2svp\n",
     "arc-level-year-in-method", "warning"),
    ("level-bad-software", "project: x\nspecies: [{label: a, smiles: C}]\nopt_level: {method: b3lyp, software: gausian}\n",
     "arc-level-software", "error"),
    ("level-variant-orca", "project: x\nspecies: [{label: a, smiles: C}]\nsp_level: {method: wb97xd, basis: def2tzvp, "
     "software: orca}\n", "arc-level-variant", "warning"),
    ("adaptive", "project: x\nspecies: [{label: a, smiles: C}]\nadaptive_levels: [{levels: {opt: b3lyp/6-31g}}]\n",
     "arc-adaptive-levels", "error"),
    ("ess-unknown", "project: x\nspecies: [{label: a, smiles: C}]\ness_settings: {gausian: local}\n",
     "arc-ess-unknown", "error"),
    ("ess-type", "project: x\nspecies: [{label: a, smiles: C}]\ness_settings: [gaussian]\n",
     "arc-ess-settings-type", "error"),
    ("ts-adapters-type", "project: x\nspecies: [{label: a, smiles: C}]\nts_adapters: heuristics\n",
     "arc-ts-adapters-type", "error"),
    ("ts-adapter-unknown", "project: x\nspecies: [{label: a, smiles: C}]\nts_adapters: [heuristic]\n",
     "arc-ts-adapter-unknown", "error"),
    ("statmech", "project: x\nspecies: [{label: a, smiles: C}]\nthermo_adapter: mess\n", "arc-statmech-adapter",
     "error"),
    ("dup-key", "project: x\nproject: y\nspecies: [{label: a, smiles: C}]\n", "arc-duplicate-key", "warning"),
    ("not-mapping", "- project: x\n", "arc-not-mapping", "error"),
]


@pytest.mark.parametrize("text,code,severity", [b[1:] for b in BAD], ids=[b[0] for b in BAD])
def test_invalid_inputs(text, code, severity, cfg, snap):
    findings = check_arc_input(text, "input.yml", cfg=cfg, schema=snap)
    got = {(f.code, f.severity) for f in findings}
    assert (code, severity) in got, [f.format() for f in findings]


def test_suggestions_and_lines(cfg, snap):
    findings = check("""\
        project: x
        job_memmory: 14
        species:
          - label: a
            smiles: C
            multiplicty: 1
        opt_level: {method: b3lyp, sovlent: water}
        job_types:
          1d_rotors: true
        """, cfg, snap)
    by = {f.code: f for f in findings}
    assert "'job_memory'" in by["arc-unknown-key"].message and by["arc-unknown-key"].line == 2
    assert "'multiplicity'" in by["arc-species-unknown-key"].message and by["arc-species-unknown-key"].line == 6
    assert "'solvent'" in by["arc-level-unknown-key"].message
    assert "'rotors'" in by["arc-job-type-unknown"].message and by["arc-job-type-unknown"].line == 9


def test_yaml_error_has_line(cfg, snap):
    f = check_arc_input("project: x\nspecies:\n  - label: a\n   smiles: C\n", cfg=cfg, schema=snap)
    assert f[0].code == "arc-yaml-error" and f[0].line == 4


def test_yes_no_stay_strings_like_arc(cfg, snap):
    # ARC's loader keeps NO as a string: a species labelled NO is fine and can be referenced.
    findings = check("""\
        project: x
        species:
          - {label: NO, smiles: '[N]=O'}
          - {label: HNO, smiles: N=O}
          - {label: H, smiles: '[H]'}
        reactions:
          - label: HNO <=> NO + H
        """, cfg, snap)
    assert errors(findings) == []


def test_restart_file_without_project_is_not_an_error(cfg, snap):
    findings = check("species: [{label: a, smiles: C}]\nrunning_jobs: {}\noutput: {}\n", cfg, snap)
    assert "arc-project-missing" not in codes(findings)


def test_level_routing_via_levels_ess_is_a_warning(cfg, snap):
    # 'dlpno' routes to ORCA before levels_ess: certain; a levels_ess phrase match is only a warning.
    findings = check("""\
        project: x
        species: [{label: a, smiles: C}]
        opt_level: wb97xd/def2svp
        """, cfg, snap)
    assert errors(findings) == []


def test_arkane_level_is_not_routed(cfg, snap):
    findings = check("""\
        project: x
        species: [{label: a, smiles: C}]
        arkane_level_of_theory: {method: b97d3, basis: def2tzvp, year: 2023}
        """, cfg, snap)
    assert findings == []


def test_parity_with_rdkit(cfg, snap):
    pytest.importorskip("rdkit")
    findings = check("""\
        project: x
        species:
          - {label: OH, smiles: '[OH]', multiplicity: 1}
          - {label: CH3, smiles: '[CH3]', charge: 1}
          - {label: O2, smiles: '[O][O]', multiplicity: 3}
        """, cfg, snap)
    assert ("arc-parity", "warning") in {(f.code, f.severity) for f in findings}
    assert "arc-charge" in codes(findings)
    assert not [f for f in findings if "O2" in f.message]


def test_ess_servers_checked_against_servers_yaml(tmp_path, snap):
    (tmp_path / "rag_drg.yaml").write_text("index_path: index/x.sqlite\nsources: []\n")
    (tmp_path / "servers.yaml").write_text(textwrap.dedent("""\
        servers:
          zeus:
            scheduler: slurm
            host: zeus.example.org
            partitions:
              main: {max_walltime: "24:00:00", cores_per_node: 8, mem_per_node_gb: 32, default: true}
        """))
    tcfg = load_config(tmp_path / "rag_drg.yaml")
    text = "project: x\nspecies: [{label: a, smiles: C}]\ness_settings: {gaussian: [local, zeus], orca: zues}\n"
    findings = check_arc_input(text, cfg=tcfg, schema=snap)
    bad = [f for f in findings if f.code == "arc-ess-server"]
    assert len(bad) == 1 and "'zues'" in bad[0].message and "'zeus'" in bad[0].message
    (tmp_path / "servers.yaml").unlink()
    assert "arc-ess-server" not in codes(check_arc_input(text, cfg=tcfg, schema=snap))


# ------------------------------------------------------------------ detection, hook, CLI, MCP


def test_is_arc_input():
    assert is_arc_input("input.yml", "project: x\nspecies: []\n")
    assert is_arc_input("run.yaml", "project: x\nreactions: []\n")
    assert is_arc_input("input.yml", "project: x\nspecies:\n  - label: a\n   smiles: C\n")  # unparsable
    assert not is_arc_input("input.yml", "project: x\n")
    assert not is_arc_input("servers.yaml", "servers: {}\n")
    assert not is_arc_input("job.inp", "project: x\nspecies: []\n")


def test_check_input_routes_arc_files(tmp_path, cfg):
    from rag_drg.tools.inputcheck import check_input, hook_main, is_checkable

    p = tmp_path / "input.yml"
    p.write_text("project: x\njob_memmory: 14\nspecies: [{label: a, smiles: C}]\n")
    assert is_checkable(p)
    assert "arc-unknown-key" in codes(check_input(path=p, cfg=cfg))
    err = io.StringIO()
    assert hook_main(json.dumps({"tool_input": {"file_path": str(p)}}), cfg=cfg, err=err) == 2
    assert "job_memory" in err.getvalue()
    p.write_text((FIX / "examples" / "minimal" / "input.yml").read_text())
    assert hook_main(json.dumps({"tool_input": {"file_path": str(p)}}), cfg=cfg, err=io.StringIO()) == 0
    other = tmp_path / "config.yml"
    other.write_text("foo: 1\n")
    assert not is_checkable(other)


def test_cli(tmp_path, capsys):
    from rag_drg.cli import main

    conf = ["--config", str(REPO / "rag_drg.yaml")]
    assert main(conf + ["arc", "check", str(FIX / "examples" / "minimal" / "input.yml")]) == 0
    assert main(conf + ["arc", "check", "--json", str(FIX / "invalid_many.yml")]) == 1
    out = capsys.readouterr().out
    report = json.loads(out[out.index("{"):])
    assert any(f["code"] == "arc-unknown-key" for f in report[str(FIX / "invalid_many.yml")])
    if HAS_CLONE:
        assert main(conf + ["arc", "schema", "--out", str(tmp_path / "s.json")]) == 0
        assert read_schema_file(tmp_path / "s.json") is not None
    assert main(conf + ["arc", "schema", "--arc-path", str(tmp_path / "nope")]) == 2


def test_mcp_tool(cfg):
    from rag_drg.tools import arc_input

    tools = {}

    class FakeMCP:
        def tool(self):
            def deco(fn):
                tools[fn.__name__] = fn
                return fn
            return deco

    events = []

    class Ctx:
        def __init__(self):
            self.cfg = cfg

        def emit(self, e):
            events.append(e)

    arc_input.register_mcp(FakeMCP(), Ctx())
    out = tools["check_arc_input"]("project: x\njob_memmory: 1\nspecies: [{label: a, smiles: C}]\n")
    assert "job_memory" in out and events[0]["tool"] == "check_arc_input"
    assert "no problems" in tools["check_arc_input"]((FIX / "examples" / "minimal" / "input.yml").read_text())


def test_lint_clean(cfg):
    from rag_drg.tools.arc_input import lint

    assert lint(cfg) == []
