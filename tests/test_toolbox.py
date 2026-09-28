"""Tool profiles (full / minimal), tool discovery and search hints."""

import asyncio
import json

import pytest

pytest.importorskip("mcp")

from rag_drg.ingest import ingest  # noqa: E402
from rag_drg.mcp_server import build_server  # noqa: E402
from rag_drg.toolbox import ToolRegistrar, tool_hints  # noqa: E402


def _tools(mcp):
    return {t.name: t for t in asyncio.run(mcp.list_tools())}


def _size(tools):
    return sum(len(json.dumps(t.model_dump(exclude_none=True), default=str)) for t in tools.values()) // 4


@pytest.fixture
def servers(project):
    ingest(project, progress=lambda *_: None)
    return build_server(project, profile="full"), build_server(project, profile="minimal")


def test_minimal_profile_exposes_three_small_tools(servers):
    full, minimal = servers
    ft, mt = _tools(full), _tools(minimal)
    assert set(mt) == {"search_knowledge", "find_tool", "run_tool"}
    assert "check_input" in ft and "find_tool" not in ft
    assert _size(mt) < 1000 < _size(ft)
    # every full-profile tool is still reachable in minimal
    assert set(ft) <= set(minimal._rag_drg_tools.specs)


@pytest.mark.parametrize("task, tool", [
    ("why did my gaussian job fail", "diagnose_output"),
    ("check my ORCA input before submitting", "check_input"),
    ("does def2-TZVP cover iodine", "check_basis"),
    ("is wB97X-D available in ORCA", "lookup_level_of_theory"),
    ("which queue can I use", "queue_access"),
    ("I got corrected: Molpro memory is in words", "record_lesson"),
    ("make a submit script for ORCA on zeus", "render_submit_script"),
])
def test_find_tool_ranks_the_right_tool_first(servers, task, tool):
    _, minimal = servers
    hits = minimal._rag_drg_tools.find(task, k=2)
    assert hits and hits[0][1].name == tool


def test_run_tool_calls_and_validates(servers):
    _, minimal = servers
    reg = minimal._rag_drg_tools
    out = reg.run("check_input", {"content": "%mem=1GB\n#P HF/STO-3G\n\nt\n\n0 1\nH 0 0 0\nH 0 0 0.74\n\n",
                                  "filename": "h2.gjf"})
    assert "h2.gjf" in out
    assert reg.run("check_input", {"contnt": "x"}).startswith("Bad arguments for check_input")
    assert "Did you mean" in reg.run("chek_input", {})
    assert "No tool named" in reg.run("run_tool", {})  # no recursion through the dispatcher


def test_mcp_round_trip_in_minimal_profile(servers):
    _, minimal = servers
    res = asyncio.run(minimal.call_tool("find_tool", {"task": "diagnose a failed ORCA job"}))
    text = json.dumps(res, default=lambda o: getattr(o, "text", str(o)))
    assert "diagnose_output" in text and "run_tool" in text
    res = asyncio.run(minimal.call_tool("run_tool", {"name": "lookup_level_of_theory", "args": {"name": "hf"}}))
    # The tool ran through the dispatcher (the tiny test project has no levels table).
    assert "levels" in json.dumps(res, default=lambda o: getattr(o, "text", str(o))).lower()


def test_search_hints_point_to_tools(servers):
    full, minimal = servers
    available = set(full._rag_drg_tools.specs)
    assert any("diagnose_output" in h for h in tool_hints("my gaussian job failed with l9999", available))
    assert any("check_input" in h for h in tool_hints("check my orca input", available))
    assert tool_hints("what is maxcore", available) == []
    assert tool_hints("my job failed", {"search_knowledge"}) == []  # only tools that exist


def test_unknown_profile_rejected():
    with pytest.raises(ValueError):
        ToolRegistrar(object(), "tiny")
