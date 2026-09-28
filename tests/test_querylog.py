import datetime as dt
import json
import threading

import yaml

from rag_drg.cli import main
from rag_drg.plugins import ServerContext
from rag_drg.tools import evaluation as ev
from rag_drg.tools import querylog as ql


def _ctx(cfg, user=None):
    return ServerContext(cfg=cfg, store=None, searcher=None, lock=threading.Lock(), current_user=lambda: user)


def _search_event(query, results, software=None):
    return {
        "tool": "search_knowledge",
        "args": {"query": query, "software": software, "version": None, "domain": None, "doc_type": None, "k": 6},
        "n_results": len(results),
        "results": results,
    }


def _res(source, path, score, title="t", doc_type="card"):
    return {"source": source, "path": path, "title": title, "doc_type": doc_type, "score": score, "extra": "dropped"}


def test_register_mcp_writes_json_lines(project):
    ctx = _ctx(project, user="alice")
    ql.register_mcp(None, ctx)
    assert len(ctx.listeners) == 1
    results = [_res("curated", f"doc{i}.md", 0.03 - i * 0.001) for i in range(8)]
    ctx.emit(_search_event("ORCA maxcore", results, software="orca"))
    ctx.emit({"tool": "record_lesson", "args": {"title": "t", "domain": "ess", "software": "orca"},
              "path": "knowledge/lessons/ess/orca/x.md", "n_results": 1, "user": "bob"})
    path = project.root / "index" / "query_log.jsonl"
    recs = [json.loads(line) for line in path.read_text().splitlines()]
    assert len(recs) == 2
    r = recs[0]
    assert r["tool"] == "search_knowledge" and r["user"] == "alice" and r["n_results"] == 8
    assert r["args"]["query"] == "ORCA maxcore" and r["args"]["software"] == "orca"
    assert len(r["results"]) == 5 and set(r["results"][0]) == set(ql.RESULT_KEYS)
    ts = dt.datetime.fromisoformat(r["ts"])
    assert ts.utcoffset() == dt.timedelta(0)
    assert recs[1]["user"] == "bob" and recs[1]["path"].endswith("x.md")


def test_disabled_and_custom_path(project, tmp_path):
    project.extra["query_log"] = {"enabled": False}
    ctx = _ctx(project)
    ql.register_mcp(None, ctx)
    assert ctx.listeners == []
    project.extra["query_log"] = {"path": str(tmp_path / "logs" / "q.jsonl"), "max_mb": 1}
    ctx = _ctx(project)
    ql.register_mcp(None, ctx)
    ctx.emit(_search_event("x", []))
    assert (tmp_path / "logs" / "q.jsonl").is_file()


def test_rotation_and_reading_order(tmp_path):
    path = tmp_path / "q.jsonl"
    logger = ql.QueryLogger(path, max_bytes=600, keep=2)
    for i in range(30):
        logger.write({"ts": f"2026-09-01T00:00:{i:02d}+00:00", "tool": "search_knowledge", "i": i,
                      "args": {"query": f"q{i}"}})
    assert path.stat().st_size <= 600
    assert (tmp_path / "q.jsonl.1").is_file() and (tmp_path / "q.jsonl.2").is_file()
    assert not (tmp_path / "q.jsonl.3").exists()
    recs = ql.read_records(path)
    idx = [r["i"] for r in recs]
    assert idx == sorted(idx) and idx[-1] == 29 and len(idx) < 30  # oldest dropped, order kept


def test_concurrent_writes_do_not_interleave(tmp_path):
    logger = ql.QueryLogger(tmp_path / "q.jsonl", max_bytes=10**9)

    def work(n):
        for i in range(50):
            logger(_search_event(f"thread {n} query {i} " + "x" * 200, [_res("curated", "a.md", 0.03)]))

    threads = [threading.Thread(target=work, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    lines = (tmp_path / "q.jsonl").read_text().splitlines()
    assert len(lines) == 400 and all(json.loads(line)["tool"] == "search_knowledge" for line in lines)


def test_parse_since():
    now = dt.datetime(2026, 9, 28, tzinfo=dt.timezone.utc)
    assert ql.parse_since("7d", now) == now - dt.timedelta(days=7)
    assert ql.parse_since("12h", now) == now - dt.timedelta(hours=12)
    assert ql.parse_since("2026-09-01", now) == dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc)
    assert ql.parse_since("all") is None
    try:
        ql.parse_since("yesterday")
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError")


def _records():
    def rec(day, event, user=None):
        r = ql.make_record(event)
        r["ts"] = f"2026-09-{day:02d}T10:00:00+00:00"
        r["user"] = user
        return r

    good = [_res("curated", "ess/orca/orca-essentials.md", 0.029)]
    return [
        rec(20, _search_event("ORCA maxcore", good, "orca"), "alice"),
        rec(21, _search_event("orca  MAXCORE", good, "orca"), "bob"),
        rec(21, _search_event("gaussian counterpoise", [_res("psi4", "doc/tutorial.rst", 0.0164, doc_type="reference")])),
        rec(22, _search_event("ARC kinetics tunneling", [_res("arc", "arc/schemas/x.json", 0.05, doc_type="schema")])),
        rec(22, _search_event("xyzzy", [])),
        rec(22, _search_event("xyzzy", []), "alice"),
        rec(22, {"tool": "lookup_level_of_theory", "args": {"name": "foo", "software": None}, "n_results": 0}),
        rec(23, {"tool": "record_lesson", "args": {"title": "Molpro memory", "domain": "ess", "software": "molpro"},
                 "path": "knowledge/lessons/ess/molpro/m.md", "n_results": 1}, "carol"),
    ]


def test_build_report_aggregates():
    rep = ql.build_report(_records(), low_score=0.02)
    assert rep["n_events"] == 8
    assert rep["by_tool"] == {"search_knowledge": 6, "lookup_level_of_theory": 1, "record_lesson": 1}
    assert rep["by_day"]["2026-09-22"] == 4
    assert rep["by_user"]["alice"] == 2 and rep["by_user"]["(anonymous)"] == 4
    assert rep["by_software_filter"] == {"(no filter)": 4, "orca": 2}
    top = rep["top_queries"][0]
    assert top["count"] == 2 and top["users"] == ["alice", "bob"]  # normalised "orca maxcore"
    empty = {(e["tool"], e["query"]): e["count"] for e in rep["empty_queries"]}
    assert empty == {("search_knowledge", "xyzzy"): 2, ("lookup_level_of_theory", "foo"): 1}
    low = {e["query"]: e["reasons"] for e in rep["low_confidence"]}
    assert set(low) == {"gaussian counterpoise", "ARC kinetics tunneling"}
    assert len(low["gaussian counterpoise"]) == 2 and low["ARC kinetics tunneling"] == ["top result not curated"]
    assert rep["top_documents"][0] == {"doc": "curated:ess/orca/orca-essentials.md", "count": 2}
    assert rep["lessons"][0]["title"] == "Molpro memory" and rep["lessons"][0]["user"] == "carol"
    text = ql.format_report(rep)
    assert "EMPTY results" in text and "xyzzy" in text and "Molpro memory" in text


def test_to_qa_output_is_valid_yaml_and_skips_known():
    out = ql.to_qa(_records(), existing=["ORCA maxcore"], existing_ids=["xyzzy"])
    items = yaml.safe_load(out)
    qs = [it["q"] for it in items]
    assert "ORCA maxcore" not in qs and "orca  MAXCORE" not in qs      # already in qa.yaml
    assert qs[0] == "xyzzy"                                            # empty results first
    assert items[0]["id"] == "xyzzy-2" and items[0]["expect"] == ["TODO"]
    assert "empty" in items[0]["tags"]
    parsed = ev.parse_items(items)                                     # loads as eval items ...
    assert all(it.todo for it in parsed)                               # ... that eval skips
    only_empty = yaml.safe_load(ql.to_qa(_records(), empty_only=True))
    assert [it["q"] for it in only_empty] == ["xyzzy"]
    assert yaml.safe_load(ql.to_qa([])) == []


def test_cli_report_and_to_qa(project, capsys):
    path = project.root / "index" / "query_log.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    now = dt.datetime.now(dt.timezone.utc)
    lines = []
    for i, r in enumerate(_records()):
        r["ts"] = (now - dt.timedelta(days=10 - i, hours=-1)).isoformat()  # first 3 older than 7d
        lines.append(json.dumps(r))
    path.write_text("\n".join(lines) + "\n{truncated line\n")
    cfg = str(project.root / "rag_drg.yaml")
    assert main(["-c", cfg, "queries", "report", "--json"]) == 0
    rep = json.loads(capsys.readouterr().out)
    assert rep["n_events"] == 5 and rep["lessons"]
    assert main(["-c", cfg, "queries", "report", "--since", "all"]) == 0
    assert "8 event(s)" in capsys.readouterr().out
    assert main(["-c", cfg, "queries", "to-qa", "--since", "30d"]) == 0
    assert len(yaml.safe_load(capsys.readouterr().out)) == 4
    assert main(["-c", cfg, "queries", "report", "--since", "soon"]) == 2
