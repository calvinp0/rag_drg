"""Tests for rag_drg/tools/diagnose.py (the `diagnose` CLI / `diagnose_output` MCP tool) and errors.yaml.

Fixtures in tests/fixtures/outputs/ are short excerpts of ARC's test outputs (MIT License; the first line of
each file names the source file and the kept line ranges) plus a few clearly marked synthetic files.
"""

import copy
import json
import shutil
from pathlib import Path

import pytest
import yaml

from rag_drg.chunking import chunk_file
from rag_drg.tools import diagnose as dg
from rag_drg.tools.diagnose import diagnose_output, lint_errors_data, load_error_db

ROOT = Path(__file__).resolve().parent.parent
ERRORS_YAML = ROOT / "knowledge" / "ess" / "errors.yaml"
FIX = Path(__file__).resolve().parent / "fixtures" / "outputs"

# fixture -> (software, status, first error id or None, version substring or None)
EXPECTED = {
    "g16_syntax_fail.out": ("gaussian", "failed", "g-qperr-syntax", "Gaussian 16, Revision B.01"),
    "g09_maxsteps_fail.out": ("gaussian", "failed", "g-l9999-max-opt-steps", "Gaussian 09, Revision D.01"),
    "g16_l9999_unconverged_fail.out": ("gaussian", "failed", "g-l9999-unconverged-generic", "Gaussian 16"),
    "g09_l913_multistep_fail.out": ("gaussian", "failed", "g-l913-cc-max-cycles", "Gaussian 09"),
    "g16_l301_basis_fail.out": ("gaussian", "failed", "g-l301-atomic-number-out-of-range", "Gaussian 16"),
    "g09_l401_chk_fail.out": ("gaussian", "failed", "g-chk-data-missing", "Gaussian 09"),
    "g16_irc_deltax_fail.out": ("gaussian", "failed", "g-l123-irc-deltax", "Gaussian 16, Revision A.03"),
    "g09_appended_caldsu_fail.out": ("gaussian", "failed", "g-inaccurate-quadrature-caldsu", "Gaussian 09"),
    "g16_galloc_fail.out": ("gaussian", "failed", "g-memory-allocation-failed", "Gaussian 16, Revision C.01"),
    "g16_slurm_timelimit.out": ("gaussian", "failed", "s-slurm-time-limit", "Gaussian 16"),
    "g16_opt_success.out": ("gaussian", "success", None, "Gaussian 16, Revision B.01"),
    "g16_irc_success.out": ("gaussian", "success", None, "Gaussian 16"),
    "g09_opt_pointgroup_success.out": ("gaussian", "success", None, "Gaussian 09"),
    "g16_opt_incomplete.out": ("gaussian", "incomplete", None, "Gaussian 16"),
    # relaxed scan cut mid-run: 'Number of steps exceeded' of one scan point is only a possible cause
    "g03_scan_incomplete.out": ("gaussian", "incomplete", "g-l9999-max-opt-steps", "Gaussian 03, Revision D.01"),
    "orca4_scf_memory_fail.out": ("orca", "failed", "o-maxcore-insufficient", "Program Version 4.1.2"),
    "orca4_mdci_cores_fail.out": ("orca", "failed", "o-mdci-too-many-processes", "Program Version 4.2.0"),
    "orca4_wfn_not_converged_fail.out": ("orca", "failed", "o-wavefunction-not-converged", "4.2.0"),
    "orca4_multiplicity_fail.out": ("orca", "failed", "o-multiplicity-impossible", "4.1.2"),
    "orca4_sp_success.out": ("orca", "success", None, "Program Version 4.1.2"),
    "orca5_mrci_success.out": ("orca", "success", None, "Program Version 5.0.4"),
    "orca6_incomplete.out": ("orca", "incomplete", None, "Program Version 6.0.0"),
    "molpro_basis_fail.out": ("molpro", "failed", "m-basis-library-exhausted", None),
    "molpro_triples_memory_fail.out": ("molpro", "failed", "m-triples-memory", None),
    "molpro_alloc_memory_fail.out": ("molpro", "failed", "m-insufficient-memory-allocate", None),
    "molpro_ccsd_success.out": ("molpro", "success", None, "Molpro 2022.3"),
    "qchem_opt_success.out": ("qchem", "success", None, "Q-Chem 5.1.0"),
    "qchem_scf_fail.out": ("qchem", "failed", "q-scf-failed", "Q-Chem 5.1.0"),
    "qchem_maxopt_fail.out": ("qchem", "failed", "q-max-opt-cycles", "Q-Chem 5.1.0"),
    "psi4_success.out": ("psi4", "success", None, "Psi4 1.9.1"),
    "psi4_scf_fail.out": ("psi4", "failed", "p-scf-not-converged", "Psi4 1.9.1"),
    "htcondor_memory_exceeded.out": ("scheduler", "failed", "s-memory-exceeded", None),
}
SUCCESS = sorted(f for f, v in EXPECTED.items() if v[1] == "success")


def test_every_fixture_is_covered():
    assert {p.name for p in FIX.iterdir()} == set(EXPECTED)
    per_software: dict[str, set] = {}
    for sw, status, _, _ in EXPECTED.values():
        per_software.setdefault(sw, set()).add(status)
    for sw in ("gaussian", "orca", "qchem", "molpro", "psi4"):
        assert {"failed", "success"} <= per_software[sw], sw


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_fixture_diagnosis(name):
    software, status, first, version = EXPECTED[name]
    d = diagnose_output(path=FIX / name)
    assert d.software == software
    assert d.status == status, d.reason
    if first:
        assert d.errors and d.errors[0]["id"] == first, [e["id"] for e in d.errors]
        assert d.errors[0]["fixes"] and d.errors[0]["meaning"] and d.errors[0]["sources"]
        assert ">" in d.excerpt
    else:
        assert d.errors == []
    if version:
        assert version in (d.version or "")
    text = d.format_text()
    assert status.upper() in text
    assert "knowledge/ess/capabilities.md" in " ".join(d.cards)


def test_repo_errors_yaml_lints_clean_and_has_every_program():
    data = yaml.safe_load(ERRORS_YAML.read_text())
    assert lint_errors_data(data, "errors.yaml") == []
    meta = data["meta"]
    assert meta["status"] == "draft" and meta["domain"] == "ess" and meta["doc_type"] == "error"
    softwares = {e["software"] for e in data["errors"]}
    assert {"gaussian", "orca", "qchem", "molpro", "psi4", "scheduler"} <= softwares
    fallbacks = [e["id"] for e in data["errors"] if e.get("priority", 50) <= 0]
    assert fallbacks == ["q-generic-error-line"]


def test_no_pattern_matches_a_successful_output():
    """Run EVERY pattern (all programs, fallback included) over every successful fixture."""
    db = load_error_db(paths=[ERRORS_YAML])
    for name in SUCCESS:
        text = (FIX / name).read_text()
        hits = [(e.id, e.regex.search(text).group(0)) for e in db if e.regex.search(text)]
        assert hits == [], (name, hits)


def test_benign_lookalikes_in_successful_jobs_are_not_errors():
    # A successful IRC prints "Delta-x Convergence NOT Met" and a successful opt prints
    # "Change in point group or standard orientation"; neither may be reported.
    irc = (FIX / "g16_irc_success.out").read_text()
    opt = (FIX / "g09_opt_pointgroup_success.out").read_text()
    assert "Delta-x Convergence NOT Met" in irc
    assert "Change in point group or standard orientation" in opt
    # ... even if the job had not terminated normally.
    for text in (irc, opt):
        cut = text[: text.rindex(" Normal termination")]
        d = diagnose_output(content=cut)
        assert d.status == "incomplete"
        assert not {"g-l123-irc-deltax", "g-l202-point-group-change"} & {e["id"] for e in d.errors}


def _db(entries, meta=True):
    data = {"errors": entries}
    if meta:
        data["meta"] = {"title": "t", "domain": "ess", "doc_type": "gotcha", "status": "draft"}
    return data


def test_generic_fallback_only_when_nothing_else_matched_and_not_successful():
    db = load_error_db(paths=[ERRORS_YAML])
    ok = (FIX / "qchem_opt_success.out").read_text()
    # Unknown Q-Chem failure: only the generic 'error' line matches -> used as a fallback. Without a
    # termination marker a fallback does not prove failure: reported as a possible cause.
    broken = ok[: ok.rindex("Thank you")].rsplit("\n", 3)[0] + "\n some module error: bad things happened\n"
    d = diagnose_output(content=broken, db=db)
    assert d.status == "incomplete" and "possible cause" in d.reason
    assert [e["id"] for e in d.errors] == ["q-generic-error-line"]
    d = diagnose_output(content=broken + " Q-Chem fatal error occurred in module x\n", db=db)
    assert d.status == "failed" and d.errors[0]["id"] == "q-generic-error-line"
    # The same line in a normally terminated job is ignored.
    d = diagnose_output(content=ok.replace("Archival summary:", " some module error: harmless\nArchival summary:"),
                        db=db)
    assert d.status == "success" and d.errors == []
    # A specific match suppresses the fallback.
    d = diagnose_output(path=FIX / "qchem_scf_fail.out", db=db)
    assert "q-generic-error-line" not in [e["id"] for e in d.errors]


def test_priority_orders_specific_before_generic():
    d = diagnose_output(path=FIX / "g16_l301_basis_fail.out")
    ids = [e["id"] for e in d.errors]
    assert ids.index("g-l301-atomic-number-out-of-range") < ids.index("g-l301-generic")
    d = diagnose_output(path=FIX / "orca4_scf_memory_fail.out")
    ids = [e["id"] for e in d.errors]
    assert ids[0] == "o-maxcore-insufficient" and "o-scf-error-termination" in ids


def test_gaussian_failing_link_disambiguates():
    head = (FIX / "g16_opt_success.out").read_text().split(" [...")[0]
    body = ("\n Inaccurate quadrature in CalDSu.\n"
            "    -- Number of steps exceeded,  NStep=  27\n")
    l9999 = head + body + (" Error termination request processed by link 9999.\n"
                           " Error termination via Lnk1e in /opt/g16/l9999.exe at Sat Sep  7 01:58:23 2019.\n")
    l502 = head + body + " Error termination via Lnk1e in /opt/g16/l502.exe at Sat Sep  7 01:58:23 2019.\n"
    d = diagnose_output(content=l9999)
    assert d.failing_link == "l9999"
    assert d.errors[0]["id"] == "g-l9999-max-opt-steps"
    d = diagnose_output(content=l502)
    assert d.failing_link == "l502"
    assert d.errors[0]["id"] == "g-inaccurate-quadrature-caldsu"
    # A CC cycle limit in l913 is not an optimisation problem (ARC labels it MaxOptCycles).
    d = diagnose_output(path=FIX / "g09_l913_multistep_fail.out")
    assert d.failing_link == "l913"
    assert not [e for e in d.errors if e["id"].startswith("g-l9999")]


def test_gaussian_multistep_and_appended_outputs_use_the_final_step():
    d = diagnose_output(path=FIX / "g09_l913_multistep_fail.out")
    assert "after 1 normally terminated step" in d.reason
    assert d.route.startswith("#P Geom=AllCheck") and "CCSD(T)" in d.route
    # Three jobs appended to one file (l502, l9999, l502): only the last one counts.
    d = diagnose_output(path=FIX / "g09_appended_caldsu_fail.out")
    assert [e["id"] for e in d.errors] == ["g-inaccurate-quadrature-caldsu"]
    assert "scf=(NDamp=30,NoDIIS,xqc)" in d.route
    # A next step that started after a normal termination and never finished is not a success.
    text = (FIX / "g16_opt_success.out").read_text()
    text += " Link1:  Proceeding to internal job step number  2.\n ------\n #P freq\n ------\n"
    assert diagnose_output(content=text).status == "incomplete"


def test_incomplete_job_points_to_the_scheduler():
    for name in ("g16_opt_incomplete.out", "orca6_incomplete.out"):
        d = diagnose_output(path=FIX / name)
        assert d.status == "incomplete"
        assert any("sacct" in n for n in d.notes)
    d = diagnose_output(content="")
    assert d.status == "unknown"


def test_scheduler_line_in_merged_output_overrides_incomplete():
    d = diagnose_output(path=FIX / "g16_slurm_timelimit.out")
    assert d.software == "gaussian" and d.status == "failed"
    assert d.errors[0]["id"] == "s-slurm-time-limit"
    assert any("sacct" in f for f in d.errors[0]["fixes"])


def _big_output(tmp_path, name="g09_maxsteps_fail.out", filler_lines=30000):
    lines = (FIX / name).read_text().splitlines()
    head, tail = lines[:40], lines[40:]
    filler = [f" Iteration {i:6d} of a long boring optimisation, nothing to see here at all." for i in range(filler_lines)]
    path = tmp_path / "big.out"
    path.write_text("\n".join(head + filler + tail) + "\n")
    return path, head + filler + tail


def test_big_file_is_read_as_head_plus_tail(tmp_path, monkeypatch):
    path, _ = _big_output(tmp_path)
    monkeypatch.setattr(dg, "FULL_READ_BYTES", 100_000)
    text, complete = dg.read_head_tail(path, tail_lines=500)
    assert not complete and "not read" in text
    assert len(text) < 200_000
    d = diagnose_output(path=path)
    assert d.status == "failed" and d.errors[0]["id"] == "g-l9999-max-opt-steps"
    assert d.version == "Gaussian 09, Revision D.01" and d.route.startswith("#P opt=(calcfc,maxstep=5,tight)")
    assert not d.complete_read


def test_full_scan_when_the_tail_explains_nothing(tmp_path, monkeypatch):
    lines = (FIX / "g16_syntax_fail.out").read_text().splitlines()
    cut = next(i for i, l in enumerate(lines) if "QPErr" in l)
    filler = [f" padding line {i} " + "x" * 60 for i in range(3000)]
    path = tmp_path / "late.out"
    # The message is neither in the head (first 300 lines) nor in the 20-line tail.
    path.write_text("\n".join(filler[:400] + lines[:cut + 1] + filler + lines[cut + 1:]) + "\n")
    monkeypatch.setattr(dg, "FULL_READ_BYTES", 10_000)
    d = diagnose_output(path=path, tail_lines=20)
    assert d.errors and d.errors[0]["id"] == "g-qperr-syntax"
    assert any("whole file was scanned" in n for n in d.notes)


def test_content_mode_with_only_head_and_tail(tmp_path):
    _, lines = _big_output(tmp_path)
    content = "\n".join(lines[:100] + ["[... 29000 lines omitted ...]"] + lines[-300:])
    d = diagnose_output(content=content, filename="job.out")
    assert d.file == "job.out"
    assert d.status == "failed" and d.errors[0]["id"] == "g-l9999-max-opt-steps"
    assert d.version.startswith("Gaussian 09") and d.route
    assert not d.complete_read
    # Tail only (no header): still finds the program and the error.
    d = diagnose_output(content="\n".join(lines[-300:]))
    assert d.software == "gaussian" and d.errors[0]["id"] == "g-l9999-max-opt-steps"
    # Explicit software for a tail that does not identify the program.
    d = diagnose_output(content=" ? Error\n ? Basis library exhausted\n ? The problem occurs in Binput\n",
                        software="molpro")
    assert d.status == "failed" and d.errors[0]["id"] == "m-basis-library-exhausted"


def test_huge_content_is_trimmed():
    lines = (FIX / "orca4_scf_memory_fail.out").read_text().splitlines()
    content = "\n".join(lines[:30] + ["x" * 100] * 30000 + lines[30:])
    d = diagnose_output(content=content)
    assert d.errors[0]["id"] == "o-maxcore-insufficient"
    assert not d.complete_read


def test_versions_filter():
    entry = {"software": "gaussian", "id": "g-only-in-09", "pattern": "Convergence failure", "meaning": "m",
             "fixes": ["f"], "sources": ["s"], "versions": ["09"], "priority": 99}
    db = dg._entries_from_data(_db([entry]))
    text = (FIX / "g09_appended_caldsu_fail.out").read_text().replace("Inaccurate quadrature in CalDSu.",
                                                                      "Convergence failure -- run terminated.")
    assert diagnose_output(content=text, db=db).errors[0]["id"] == "g-only-in-09"
    assert diagnose_output(content=text.replace("Gaussian 09, Revision", "Gaussian 16, Revision"), db=db).errors == []


def test_lint_catches_bad_entries():
    good = {"software": "gaussian", "id": "g-x", "pattern": "abc", "meaning": "m", "fixes": ["f"], "sources": ["s"]}
    bad = [
        good,
        dict(good),                                              # duplicate id
        {**good, "id": "g-y", "pattern": "a(b"},                 # does not compile
        {**good, "id": "g-z", "software": "nwchem"},             # unknown software
        {**good, "id": "g-w", "fixes": []},                      # empty fixes
        {**good, "id": "o-v", "software": "orca", "links": ["l502"]},
        {**good, "id": "g-u", "severity": "catastrophic"},
        {**good, "id": "g-t", "pattern": "x*"},                  # matches everything
        {k: v for k, v in good.items() if k != "meaning"} | {"id": "g-s"},
    ]
    problems = "\n".join(lint_errors_data(_db(bad), "e.yaml"))
    for needle in ("duplicate id", "does not compile", "nwchem", "g-w: missing 'fixes'", "only meaningful for gaussian",
                   "severity", "matches the empty string", "g-s: missing 'meaning'"):
        assert needle in problems, needle
    assert "meta" in "\n".join(lint_errors_data(_db([good], meta=False), "e.yaml"))


def test_lint_hook_runs_for_the_project(project):
    dest = project.root / "knowledge" / "ess" / "errors.yaml"
    shutil.copy(ERRORS_YAML, dest)
    assert dg.lint(project) == []
    data = yaml.safe_load(dest.read_text())
    broken = copy.deepcopy(data)
    broken["errors"][1]["id"] = broken["errors"][0]["id"]
    dest.write_text(yaml.safe_dump(broken))
    assert any("duplicate id" in p for p in dg.lint(project))
    from rag_drg.lint import lint as full_lint

    assert any("duplicate id" in p for p in full_lint(project))


def test_errors_yaml_is_chunked_per_entry_and_searchable(project):
    dest = project.root / "knowledge" / "ess" / "errors.yaml"
    shutil.copy(ERRORS_YAML, dest)
    meta, chunks = chunk_file(dest, "ess/errors.yaml")
    assert meta["title"] == "ESS error messages and fixes" and meta["status"] == "draft"
    n = len(yaml.safe_load(ERRORS_YAML.read_text())["errors"])
    assert len(chunks) == n
    titles = {c.title for c in chunks}
    assert "ESS errors > gaussian > g-l9999-max-opt-steps" in titles
    one = next(c for c in chunks if c.title.endswith("g-l9999-max-opt-steps"))
    assert "Number of steps exceeded" in one.text and "Fixes, in order" in one.text

    from rag_drg.ingest import ingest
    from rag_drg.search import Searcher

    ingest(project, progress=lambda *_: None)
    hits = Searcher(project).search("l9999 error gaussian", k=5)
    assert hits and hits[0].chunk.title.startswith("ESS errors > gaussian > g-l9999")
    hits = Searcher(project).search("Atomic number out of range for basis", k=3)
    assert hits[0].chunk.title == "ESS errors > gaussian > g-l301-atomic-number-out-of-range"


def test_cli_exit_codes_and_json(project, capsys):
    from rag_drg.cli import main

    cfg = str(project.root / "rag_drg.yaml")
    assert main(["-c", cfg, "diagnose", str(FIX / "g16_opt_success.out")]) == 0
    assert main(["-c", cfg, "diagnose", str(FIX / "g16_opt_incomplete.out")]) == 2
    capsys.readouterr()
    assert main(["-c", cfg, "diagnose", "--json", str(FIX / "g16_syntax_fail.out")]) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "failed" and out["errors"][0]["id"] == "g-qperr-syntax"
    assert main(["-c", cfg, "diagnose", str(FIX / "g16_opt_success.out"), str(FIX / "orca6_incomplete.out")]) == 2
    assert main(["-c", cfg, "diagnose", str(FIX / "g16_opt_success.out"), str(FIX / "psi4_scf_fail.out"),
                 str(FIX / "orca6_incomplete.out")]) == 1
    assert main(["-c", cfg, "diagnose", "--software", "orca", str(FIX / "orca4_sp_success.out")]) == 0
    assert main(["-c", cfg, "diagnose", str(project.root / "missing.out")]) == 2


def test_mcp_tool_registration(project):
    """The plugin registers a content-mode tool; exercise it through a stand-in server object."""

    class FakeMCP:
        def __init__(self):
            self.tools = {}

        def tool(self):
            def deco(fn):
                self.tools[fn.__name__] = fn
                return fn
            return deco

    events = []

    class Ctx:
        cfg = project

        def emit(self, event):
            events.append(event)

    mcp = FakeMCP()
    dg.register_mcp(mcp, Ctx())
    tool = mcp.tools["diagnose_output"]
    out = tool(content=(FIX / "orca4_mdci_cores_fail.out").read_text(), filename="x.out")
    assert "FAILED" in out and "o-mdci-too-many-processes" in out
    assert events and events[-1]["tool"] == "diagnose_output" and events[-1]["status"] == "failed"
    assert "UNKNOWN" in tool(content="hello world")


# ----------------------------------------------------------------- review fixes

def test_incomplete_is_failed_only_for_messages_that_prove_failure():
    scan = (FIX / "g03_scan_incomplete.out").read_text()
    d = diagnose_output(content=scan)
    assert d.status == "incomplete" and "possible cause" in d.reason
    assert "Possible cause" in d.format_text() and any("relaxed scan" in n for n in d.notes)
    # a message that is fatal on its own (no `links`) still turns a marker-less output into failed
    d = diagnose_output(content=scan + " galloc:  could not allocate memory.\n")
    assert d.status == "failed" and d.errors[0]["id"] == "g-memory-allocation-failed"
    # ... and so does a scheduler kill line
    d = diagnose_output(content=scan + "slurmstepd: error: *** JOB 1 ON n1 CANCELLED AT 2026-01-01T00:00:00 "
                                        "DUE TO TIME LIMIT ***\n")
    assert d.status == "failed" and d.errors[0]["id"] == "s-slurm-time-limit"
    # with the error termination the same scan message is the diagnosis
    d = diagnose_output(content=scan + " Error termination via Lnk1e in /opt/g03/l9999.exe at Thu Feb  7 2019.\n")
    assert d.status == "failed" and d.errors[0]["id"] == "g-l9999-max-opt-steps"


def test_standalone_field_overrides_default_and_is_linted():
    base = {"software": "orca", "id": "o-x", "pattern": "SOMETHING ODD", "meaning": "m", "fixes": ["f"],
            "sources": ["s"]}
    text = "* O   R   C   A *\n Program Version 6.0.0\n SOMETHING ODD\n"
    entries = dg._entries_from_data(_db([base]))
    assert entries[0].proves_failure
    assert diagnose_output(content=text, db=entries).status == "failed"
    entries = dg._entries_from_data(_db([{**base, "standalone": False}]))
    assert not entries[0].proves_failure
    assert diagnose_output(content=text, db=entries).status == "incomplete"
    assert any("standalone" in p for p in lint_errors_data(_db([{**base, "standalone": "no"}]), "x"))


def test_fallback_scan_is_bounded(tmp_path, monkeypatch):
    lines = (FIX / "g16_syntax_fail.out").read_text().splitlines()
    cut = next(i for i, l in enumerate(lines) if "QPErr" in l)
    filler = [f" padding line {i} " + "x" * 60 for i in range(3000)]
    path = tmp_path / "late.out"
    path.write_text("\n".join(filler[:400] + lines[:cut + 1] + filler + lines[cut + 1:]) + "\n")
    monkeypatch.setattr(dg, "FULL_READ_BYTES", 10_000)
    monkeypatch.setattr(dg, "FALLBACK_SCAN_BYTES", 50_000)  # the message is ~230 kB before the end
    d = diagnose_output(path=path, tail_lines=20)
    assert not d.errors and not d.complete_read
    assert any("MB of the file were scanned" in n for n in d.notes)
    monkeypatch.setattr(dg, "FALLBACK_SCAN_BYTES", 400_000)
    d = diagnose_output(path=path, tail_lines=20)
    assert d.errors and d.errors[0]["id"] == "g-qperr-syntax"
