import json
import textwrap

import pytest
import yaml

from rag_drg.cli import main
from rag_drg.ingest import ingest
from rag_drg.search import Searcher
from rag_drg.tools import evaluation as ev


def quiet(*_):
    pass


QA = textwrap.dedent("""\
    - id: orca-maxcore
      q: "Is ORCA %maxcore total memory or per core?"
      filters: {software: orca}
      expect: ["curated:ess/orca/orca-essentials"]   # wrong path on purpose: fixture file is orca.md
      title_contains: "Memory"
      tags: [orca, memory]
    - id: gaussian-ts
      q: "Gaussian TS optimisation keywords"
      filters: {software: gaussian}
      expect: ["curated:ess/gaussian/"]
      tags: [gaussian]
    - id: psi4-dconv
      q: "density convergence of the SCF"
      expect: [{source: manual, path: scf.rst}]
      tags: [psi4]
    - id: miss
      q: "slurm submit script for orca"
      expect: ["curated:ess/gaussian/"]
      tags: [hpc]
    - id: needs-arc
      q: "ARC output.yml schema"
      expect: ["arc:arc/schemas/"]
      tags: [arc]
    - id: todo
      q: "something from the query log"
      expect: [TODO]
    """)


@pytest.fixture
def qa_project(project):
    ingest(project, progress=quiet)
    (project.root / "eval").mkdir()
    (project.root / "eval" / "qa.yaml").write_text(QA)
    return project


def test_parse_items_expect_forms_and_requires():
    items = ev.parse_items(yaml.safe_load(QA))
    by = {it.id: it for it in items}
    assert by["orca-maxcore"].expect == [
        {"source": "curated", "path": "ess/orca/orca-essentials"}, {"title_contains": "Memory"}
    ]
    assert by["orca-maxcore"].filters == {"software": "orca"}
    assert by["psi4-dconv"].requires == ["manual"]  # derived from expect
    assert by["needs-arc"].requires == ["arc"]
    assert by["todo"].todo and by["todo"].expect == []


@pytest.mark.parametrize("bad, msg", [
    ([{"id": "a", "q": "x", "expect": ["a:b"]}, {"id": "a", "q": "y", "expect": ["a:b"]}], "duplicate"),
    ([{"id": "a", "expect": ["a:b"]}], "missing question"),
    ([{"id": "a", "q": "x"}], "needs 'expect'"),
    ([{"id": "a", "q": "x", "expect": ["no-colon"]}], "source:path"),
    ([{"id": "a", "q": "x", "expect": ["a:b"], "filters": {"lang": "en"}}], "filters"),
    ({"id": "a"}, "YAML list"),
])
def test_parse_items_rejects(bad, msg):
    with pytest.raises(ValueError, match=msg):
        ev.parse_items(bad)


def test_metrics():
    m = ev._metrics([1, 2, None, 7], k=6)
    assert m == {"n": 4, "hit@1": 0.25, "hit@6": 0.5, "mrr": round((1 + 0.5 + 1 / 7) / 4, 4)}
    assert ev._metrics([], k=6)["hit@6"] is None


def test_run_eval_ranks_skips_and_tags(qa_project):
    items = ev.load_items(qa_project.root / "eval" / "qa.yaml")
    rep = ev.run_eval(Searcher(qa_project), items, k=3)
    res = {r["id"]: r for r in rep["results"]}
    assert res["orca-maxcore"]["rank"] == 1          # matched via title_contains
    assert res["gaussian-ts"]["rank"] == 1
    assert res["psi4-dconv"]["hit"]
    assert res["miss"]["rank"] is None and res["miss"]["top"]
    skipped = {s["id"]: s["reason"] for s in rep["skipped"]}
    assert "arc" in skipped["needs-arc"] and "TODO" in skipped["todo"]
    o = rep["overall"]
    assert o["n"] == 4 and o["hit@3"] == 0.75
    assert rep["by_tag"]["hpc"]["hit@3"] == 0.0 and rep["by_tag"]["orca"]["hit@1"] == 1.0
    text = ev.format_report(rep, show_failures=True)
    assert "miss" in text and "expected: curated:ess/gaussian/" in text and "Skipped:" in text


def test_cli_exit_codes_and_json(qa_project, capsys):
    cfg = str(qa_project.root / "rag_drg.yaml")
    assert main(["-c", cfg, "eval", "--k", "3", "--min-hit-rate", "0.7"]) == 0
    assert main(["-c", cfg, "eval", "--k", "3", "--min-hit-rate", "0.8"]) == 1
    capsys.readouterr()
    assert main(["-c", cfg, "eval", "--json", "--tags", "orca", "gaussian", "--min-hit-rate", "1"]) == 0
    rep = json.loads(capsys.readouterr().out)
    assert rep["overall"]["n"] == 2 and rep["embeddings"] is None
    # Everything filtered out / skipped -> failure, not a silent pass.
    assert main(["-c", cfg, "eval", "--tags", "arc"]) == 1
    assert main(["-c", cfg, "eval", "--qa", str(qa_project.root / "nope.yaml")]) == 2


def test_lint_reports_broken_qa(project):
    (project.root / "eval").mkdir()
    qa = project.root / "eval" / "qa.yaml"
    assert ev.lint(project) == []  # no file is fine... once it exists it must parse
    qa.write_text("- id: a\n  q: x\n")
    problems = ev.lint(project)
    assert problems and "needs 'expect'" in problems[0]


def test_repository_qa_file_is_valid():
    """The committed eval/qa.yaml parses, has unique ids and a sensible size."""
    from pathlib import Path

    items = ev.load_items(Path(__file__).resolve().parent.parent / "eval" / "qa.yaml")
    assert len(items) >= 30
    assert sum("paraphrase" in it.tags for it in items) >= 5
