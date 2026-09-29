import numpy as np

from rag_drg.ingest import ingest
from rag_drg.lessons import index_lesson, write_lesson
from rag_drg.search import Searcher, fts_query, identifiers, format_hits
from rag_drg.store import Store


def quiet(*_):
    pass


def test_ingest_metadata_and_incremental(project):
    report = ingest(project, progress=quiet)
    assert report["curated"]["added"] > 0 and report["manual"]["added"] > 0

    store = Store(project.index_path, readonly=True)
    stats = store.stats()
    assert stats["by_software"]["orca"] >= 2
    assert stats["by_doc_type"]["template"] == 1
    orca = store.list_files(software="orca")
    assert orca[0]["version"] == "5|6" and orca[0]["status"] == "verified"
    store.close()

    # Re-running is a no-op.
    report2 = ingest(project, progress=quiet)
    assert report2["curated"]["added"] == 0 and report2["curated"]["updated"] == 0

    # Deleting a file removes its chunks.
    (project.root / "knowledge" / "ess" / "gaussian" / "g16.md").unlink()
    report3 = ingest(project, progress=quiet)
    assert report3["curated"]["removed"] > 0


def test_search_filters_and_boosts(project):
    ingest(project, progress=quiet)
    s = Searcher(project)

    hits = s.search("memory per core maxcore")
    assert hits and hits[0].chunk.software == "orca"
    assert "exact" in hits[0].via or "keyword" in hits[0].via

    g = s.search("TS optimisation", software="gaussian")
    assert g and all(h.chunk.software == "gaussian" for h in g)

    # Version filter: ORCA card is tagged 5|6; version 6 matches, version 4 does not.
    assert s.search("maxcore", software="orca", version="6")
    assert not s.search("maxcore", software="orca", version="4")

    # Unversioned chunks match any version filter.
    assert s.search("d_convergence", version="1.9")

    text = format_hits(hits)
    assert "chunk_id=" in text and "curated:ess/orca/orca.md" in text


def test_context_neighbours(project):
    ingest(project, progress=quiet)
    s = Searcher(project)
    hit = s.search("Calc_Hess")[0]
    ctx = s.context(hit.chunk.id, neighbors=5)
    assert len(ctx) >= 2 and all(c.path == hit.chunk.path for c in ctx)


def test_lessons_are_indexed_immediately_and_ranked(project):
    ingest(project, progress=quiet)
    path = write_lesson(
        project, title="ORCA maxcore is per core", mistake="set %maxcore 64000 for 16 cores",
        correction="%maxcore 3000 for 16 cores with 4 GB each", domain="ess", software="orca",
        version="6", tags=["maxcore"],
    )
    assert path.parent == project.lessons_dir / "ess" / "orca"
    store = Store(project.index_path)
    index_lesson(project, store, path)
    s = Searcher(project, store=store)
    hits = s.search("maxcore per core", software="orca")
    # A verified gotcha card may outrank an unreviewed lesson, but the lesson must surface.
    lesson = [h for h in hits[:2] if h.chunk.doc_type == "lesson"]
    assert lesson and lesson[0].chunk.status == "unreviewed"
    # A full re-ingest keeps it (lessons are their own source) and does not duplicate it.
    store.close()
    report = ingest(project, progress=quiet)
    assert report["lessons"]["added"] == 0
    assert all(k != "curated" or v["removed"] == 0 for k, v in report.items() if isinstance(v, dict))


class FakeEmbedder:
    name = "fake"

    def embed(self, texts, is_query=False):
        # Bag-of-letters vectors: enough to exercise the dense path deterministically.
        out = np.zeros((len(texts), 26), dtype=np.float32)
        for i, t in enumerate(texts):
            for ch in t.lower():
                if "a" <= ch <= "z":
                    out[i, ord(ch) - 97] += 1
        return out


def test_dense_path_fuses_with_keyword(project):
    ingest(project, embed=False, progress=quiet)
    store = Store(project.index_path)
    todo = store.chunks_missing_embeddings()
    emb = FakeEmbedder()
    store.put_embeddings([i for i, _ in todo], emb.embed([t for _, t in todo]))
    s = Searcher(project, store=store, embedder=emb)
    hits = s.search("transition state optimisation", k=3)
    assert hits and any("semantic" in h.via for h in hits)
    filtered = s.search("transition state optimisation", software="gaussian")
    assert all(h.chunk.software == "gaussian" for h in filtered)


def test_query_helpers():
    assert fts_query("How do I set the %maxcore?") == '"set" OR "maxcore"'
    ids = identifiers("use opt=(ts,calcfc) with def2-TZVP and ts_guess_level, not B3LYP")
    assert "def2-tzvp" in ids and "ts_guess_level" in ids and "b3lyp" in ids
    assert any(i.startswith("opt=") for i in ids)


def test_named_software_in_query_is_preferred(project, monkeypatch):
    """"Gaussian TS optimisation" prefers Gaussian chunks even without software=, which the
    curated-type boosts alone (a verified ORCA gotcha) would otherwise override."""
    from rag_drg import search as search_mod

    assert search_mod.named_software("PySCF example for SMD solvation") == {"pyscf"}
    assert search_mod.named_software("translate wB97X-D from G16 to ORCA") == {"gaussian", "orca"}
    assert search_mod.named_software("Q-Chem memory") == {"qchem"}
    assert search_mod.named_software("how do I restart a crashed optimisation") == set()

    ingest(project, progress=quiet)
    s = Searcher(project)
    hits = s.search("Gaussian TS optimisation")
    assert hits[0].chunk.software == "gaussian" and "named-software" in hits[0].via
    # an explicit software filter is unchanged (no double boost, no effect on the filter)
    assert all("named-software" not in h.via for h in s.search("Gaussian TS optimisation", software="gaussian"))
    monkeypatch.setattr(search_mod, "NAMED_SOFTWARE_BOOST", 1.0)
    assert Searcher(project).search("Gaussian TS optimisation")[0].chunk.software == "orca"  # the old behaviour
