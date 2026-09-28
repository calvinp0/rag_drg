"""Compact output (max_tokens), optional reranker and `tools-schema` for local models."""

import json

from rag_drg.cli import main
from rag_drg.ingest import ingest
from rag_drg.search import (
    CrossEncoderReranker, Hit, Searcher, compact_hits, format_hits, make_reranker, trim_to_relevant,
)
from rag_drg.store import StoredChunk


def _chunk(i, text, doc_type="reference", title=None):
    return StoredChunk(id=i, source="manual", path=f"doc{i}.md", ordinal=0, title=title or f"Doc {i}",
                       text=text, domain="ess", software="orca", version="6", doc_type=doc_type,
                       tags=[], url=None, status=None)


LONG = "\n".join(
    [f"Filler sentence number {n} about something unrelated to the question at hand." for n in range(40)]
    + ["Set %maxcore to the memory per core in MB.", "```", "%maxcore 3000", "```"]
    + [f"More filler {n} that should be dropped from a compact answer." for n in range(40)]
)


def test_trim_keeps_matching_lines_and_code():
    out = trim_to_relevant(LONG, ["%maxcore", "memory"], 300)
    assert len(out) <= 300
    assert "Set %maxcore to the memory per core in MB." in out
    assert "%maxcore 3000" in out and "```" in out  # the code block after the matching line
    assert "Filler sentence number 5 " not in out
    assert out.startswith("…") and out.endswith("…")
    # Short text is returned unchanged; no match -> opening lines.
    assert trim_to_relevant("short", ["x"], 100) == "short"
    assert trim_to_relevant(LONG, ["zzz"], 200).startswith("Filler sentence number 0")


def test_compact_budget_priority_and_citations():
    hits = [Hit(_chunk(1, LONG), 1.0), Hit(_chunk(2, LONG), 0.9), Hit(_chunk(3, "Lesson: %maxcore is per core.", "lesson"), 0.5)]
    for budget in (60, 150, 400):
        out = format_hits(hits, query="maxcore memory", max_tokens=budget)
        assert len(out) <= max(200, budget * 4)
        assert "chunk_id=" in out and "src: manual:" in out
    out = format_hits(hits, query="maxcore memory", max_tokens=400)
    assert out.index("chunk_id=3") < out.index("chunk_id=1")  # the lesson comes first
    picked = compact_hits(hits, "maxcore memory", 400)
    assert [h.chunk.id for h, _ in picked][0] == 3
    # Default output is unchanged by the new keyword arguments.
    assert format_hits(hits) == format_hits(hits, query="maxcore", max_tokens=None)
    assert "Filler sentence number 20" in format_hits(hits)  # full view: plain truncation


class FakeCrossEncoder:
    def __init__(self):
        self.calls = []

    def predict(self, pairs):
        self.calls.append(pairs)
        # Prefer the Gaussian chunk, whatever the fused ranking says.
        return [5.0 if "%mem" in t else -5.0 for _, t in pairs]


def test_reranker_hook(project):
    ingest(project, progress=lambda *_: None)
    s = Searcher(project)
    base = s.search("memory", k=4)
    assert base and "%mem" not in base[0].chunk.text
    fake = FakeCrossEncoder()
    rr = CrossEncoderReranker(fake, top_n=10)
    hits = s.search("memory", k=4, rerank=rr)
    assert "%mem" in hits[0].chunk.text and "rerank" in hits[0].via
    assert fake.calls and all(q == "memory" for q, _ in fake.calls[0])
    # A failing reranker falls back to the fused ranking.
    broken = s.search("memory", k=4, rerank=lambda q, t: 1 / 0)
    assert [h.chunk.id for h in broken] == [h.chunk.id for h in base]


def test_make_reranker_config(project, monkeypatch):
    assert make_reranker(project) is None  # off by default
    import sys
    import types

    fake_mod = types.ModuleType("sentence_transformers")
    fake_mod.CrossEncoder = lambda name: FakeCrossEncoder()
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake_mod)
    project.extra["rerank"] = {"model": "fake/cross-encoder-test", "top_n": 7}
    rr = make_reranker(project)
    assert isinstance(rr, CrossEncoderReranker) and rr.top_n == 7
    assert make_reranker(project) is rr  # cached


def test_tools_schema_cli(project, capsys):
    cfg = str(project.root / "rag_drg.yaml")
    assert main(["-c", cfg, "tools-schema"]) == 0
    tools = json.loads(capsys.readouterr().out)
    names = {t["function"]["name"] for t in tools}
    assert {"search_knowledge", "lookup_level_of_theory", "get_context"} <= names
    search = next(t for t in tools if t["function"]["name"] == "search_knowledge")
    assert search["type"] == "function" and "max_tokens" in search["function"]["parameters"]["properties"]
    assert main(["-c", cfg, "tools-schema", "--format", "ollama"]) == 0
    assert json.loads(capsys.readouterr().out) == tools
    assert main(["-c", cfg, "tools-schema", "--format", "json", "--base-url", "http://rag:8765/"]) == 0
    spec = json.loads(capsys.readouterr().out)
    assert spec["tools"][0]["http"]["url"] == "http://rag:8765/api/search"
