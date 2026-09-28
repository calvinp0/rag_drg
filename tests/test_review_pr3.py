"""Regression tests for the review findings on the ARC / compose PR."""

import json
import os
import time

import pytest

from rag_drg.tools._compose.molecule import parse_xyz_text
from rag_drg.tools._compose.spec import ComposeError, apply_protocol
from rag_drg.tools.arc_input import check_arc_input
from rag_drg.tools.compose_arc import compose_arc_run
from rag_drg.tools.compose_ess import compose_ess_job


def _alias_bomb(levels: int = 8) -> str:
    lines = ["project: bomb", "species:", "  - label: H2O", "    smiles: O",
             "x0: &x0 [" + ", ".join(["lol"] * 10) + "]"]
    for i in range(1, levels):
        lines.append(f"x{i}: &x{i} [" + ", ".join([f"*x{i - 1}"] * 10) + "]")
    return "\n".join(lines) + "\n"


def test_arc_check_refuses_yaml_aliases_quickly():
    t = time.monotonic()
    findings = check_arc_input(_alias_bomb(), "input.yml")
    assert time.monotonic() - t < 5
    assert [f.code for f in findings if f.severity == "error"] == ["arc-yaml-error"]
    assert "alias" in findings[0].message


def test_arc_check_refuses_self_referential_alias():
    findings = check_arc_input("project: p\nspecies: &a [*a]\n", "input.yml")
    assert findings and findings[0].code == "arc-yaml-error"


def test_compose_arc_refuses_yaml_aliases_quickly():
    t = time.monotonic()
    res = compose_arc_run(_alias_bomb(), "nope", servers={})
    assert time.monotonic() - t < 5
    assert res["findings"] and res["findings"][0]["severity"] == "error"


def test_protocol_step_must_be_a_string():
    with pytest.raises(ComposeError, match="step must be a string"):
        apply_protocol({}, {"steps": {"sp": {}}}, ["sp"])
    with pytest.raises(ComposeError, match="step must be a string"):
        apply_protocol({}, None, ["sp"])
    res = compose_ess_job({"program": "orca"}, {"steps": {"sp": {}}}, ["sp"])
    assert not res["ok"] and "step must be a string" in res["errors"][0]


def test_protocol_molecule_is_replaced_not_merged():
    merged, _ = apply_protocol({"molecule": {"xyz": "H 0 0 0\nH 0 0 0.74"}},
                               {"molecule": {"xyz_file": "geom.xyz"}, "basis": "def2-SVP"}, None)
    assert merged["molecule"] == {"xyz": "H 0 0 0\nH 0 0 0.74"}
    assert merged["basis"] == "def2-SVP"
    merged, _ = apply_protocol({"molecule": {"smiles": "O"}}, {"molecule": {"xyz": "H 0 0 0"}}, None)
    assert merged["molecule"] == {"smiles": "O"}


@pytest.mark.parametrize("bad", ["nan", "inf", "-inf"])
def test_xyz_rejects_non_finite_coordinates(bad):
    with pytest.raises(ComposeError, match="finite"):
        parse_xyz_text(f"O {bad} 0 0\nH 0 0 1\nH 0 1 0")


def test_serve_forces_server_mode(monkeypatch):
    from rag_drg import http_app, mcp_server
    from rag_drg.config import load_config

    monkeypatch.setenv("RAG_DRG_SERVER_MODE", "0")  # restored by monkeypatch afterwards
    monkeypatch.setattr(mcp_server, "build_server", lambda *a, **k: object())
    monkeypatch.setattr(http_app, "attach_user", lambda *a, **k: None)
    monkeypatch.setattr(http_app, "run_http", lambda *a, **k: None)
    mcp_server.serve(load_config(None), transport="http", host="127.0.0.1", port=0, auth="none")
    assert os.environ["RAG_DRG_SERVER_MODE"] == "1"
