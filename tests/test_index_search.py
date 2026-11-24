import numpy as np
import pytest

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


def test_lazy_searcher_answers_before_the_model_is_loaded(project, monkeypatch):
    """The MCP server builds its Searcher with lazy=True: a slow model load must not hold up
    start-up, searches are keyword-only until it finishes, and semantic after."""
    import threading

    from rag_drg import search as search_mod
    from rag_drg.config import EmbeddingConfig

    ingest(project, embed=False, progress=quiet)
    store = Store(project.index_path)
    todo = store.chunks_missing_embeddings()
    store.put_embeddings([i for i, _ in todo], FakeEmbedder().embed([t for _, t in todo]))
    store.set_meta("embedding_model", "fake")
    project.embeddings = EmbeddingConfig(provider="sentence-transformers", model="fake")
    release = threading.Event()

    def slow_make_embedder(_cfg):
        assert release.wait(10)
        return FakeEmbedder()

    monkeypatch.setattr(search_mod, "make_embedder", slow_make_embedder)
    s = Searcher(project, store=store, lazy=True)   # returns while the "model" is still loading
    assert s.embedder is None
    hits = s.search("transition state optimisation", k=3)
    assert hits and not any("semantic" in h.via for h in hits)
    release.set()
    assert s.wait_for_embedder(10)
    assert any("semantic" in h.via for h in s.search("transition state optimisation", k=3))

    # a model that cannot be loaded leaves keyword search working
    monkeypatch.setattr(search_mod, "make_embedder", lambda _cfg: (_ for _ in ()).throw(RuntimeError("no torch")))
    s = Searcher(project, store=store, lazy=True)
    assert s.wait_for_embedder(10) is False and s.search("transition state optimisation")


def test_server_embeds_chunks_indexed_without_vectors(project, monkeypatch, caplog):
    """Chunks added while embeddings were off get their vectors from the server itself, so
    nobody has to know to re-run `rag-drg ingest`; a read-only server says how many are missing."""
    from rag_drg import ingest as ingest_mod
    from rag_drg.config import EmbeddingConfig
    from rag_drg.mcp_server import backfill_embeddings

    ingest(project, embed=False, progress=quiet)
    store = Store(project.index_path)
    todo = store.chunks_missing_embeddings()
    half = todo[: len(todo) // 2]
    store.put_embeddings([i for i, _ in half], FakeEmbedder().embed([t for _, t in half]))
    store.set_meta("embedding_model", "fake")
    store.close()
    project.embeddings = EmbeddingConfig(provider="sentence-transformers", model="fake")
    monkeypatch.setattr(ingest_mod, "make_embedder", lambda _cfg: pytest.fail("must reuse the loaded model"))
    searcher = Searcher(project, embedder=FakeEmbedder())
    n_missing = len(todo) - len(half)

    with caplog.at_level("WARNING"):
        assert backfill_embeddings(project, searcher, readonly=True) == 0
    assert f"{n_missing} chunk(s) have no embedding" in caplog.text
    assert backfill_embeddings(project, searcher) == n_missing
    assert Store(project.index_path, readonly=True).chunks_missing_embeddings() == []
    assert backfill_embeddings(project, searcher) == 0              # nothing left
    assert backfill_embeddings(project, Searcher(project, embedder=False)) == 0   # keyword-only


def test_server_instructions_list_only_the_manuals_in_the_index(tmp_path):
    """The instructions used to name Gaussian 16 and Molpro 2024 whether or not they were indexed."""
    from rag_drg.chunking import Chunk
    from rag_drg.mcp_server import INSTRUCTIONS, coverage_line

    store = Store(tmp_path / "i.sqlite")
    assert coverage_line(store) == ""   # nothing indexed: no claim

    def chunk(n, software, version, doc_type, tags=()):
        return Chunk(source="s", path=f"{n}.md", ordinal=0, title=str(n), text=f"text {n}", domain="ess",
                     software=software, version=version, doc_type=doc_type, tags=list(tags))

    store.sync_source("s", [
        chunk(1, "orca", "6", "reference"), chunk(2, "orca", "5", "theory"),
        chunk(3, "gaussian", "09", "reference"),
        chunk(4, "gaussian", "09|16", "reference", tags=["book"]),   # a book is not the G16 manual
        chunk(5, "molpro", "2024", "card"),                          # a card is not a manual
    ])
    line = coverage_line(store)
    assert "gaussian 09; orca 5, 6." in line and "16" not in line and "molpro" not in line
    assert "{coverage}" in INSTRUCTIONS and "Gaussian 09/16" not in INSTRUCTIONS


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


def test_curated_card_keeps_a_slot_when_manuals_fill_the_results(project):
    """Licensed manuals can fill all k results for a question they only touch on (the ORCA 5 and
    6 manuals are separate files, so the per-file cap doesn't help); the best curated chunk
    then takes the last slot."""
    manual = project.root / "manual"
    for i in range(30):
        (manual / f"corr{i}.rst").write_text(
            f"Static correlation, part {i}\n=========================\n\nPart {i}: static correlation and "
            f"accurate energies (case {i}); strong static correlation needs accurate multireference "
            f"energies for the molecule in example {i}.\n")
    (project.root / "knowledge" / "ess" / "capabilities.md").write_text(
        "---\ntitle: ESS capability matrix\ndomain: ess\ndoc_type: card\nstatus: draft\n---\n"
        "# Which code for which job\n\nMultireference (CASSCF, CASPT2, MRCI): Molpro first; ORCA for "
        "NEVPT2. Use them for a molecule with bond breaking, diradicals and transition-metal "
        "complexes; single-reference coupled cluster is not enough there.\n")
    ingest(project, progress=quiet)
    s = Searcher(project)
    q = "Which program for accurate energies of a molecule with strong static correlation?"

    plain = s.search(q, min_curated=0)
    assert len(plain) == 6 and all(h.chunk.source == "manual" for h in plain)
    hits = s.search(q)
    assert len(hits) == 6
    assert [h.chunk.path for h in hits if h.chunk.source == "curated"] == ["ess/capabilities.md"]
    assert "curated-floor" in hits[-1].via and hits[:5] == plain[:5]
    # a source filter that excludes curated content is respected
    assert all(h.chunk.source == "manual" for h in s.search(q, source="manual"))


def test_identifier_in_a_title_outranks_a_passing_mention(project):
    kb = project.root / "knowledge" / "ess"
    (kb / "levels.md").write_text(
        "---\ntitle: Levels of theory\ndomain: ess\ndoc_type: card\n---\n# Levels of theory\n\n"
        "## ωB97M-V\n\nORCA: `! wB97M-V`. Q-Chem: `METHOD wB97M-V`.\n")
    (kb / "grids.md").write_text(
        "---\ntitle: Grids\ndomain: ess\ndoc_type: gotcha\n---\n# Grids\n\n## NL_GRID\n\n"
        "VV10 functionals such as wB97M-V use NL_GRID; set it as an integer, and wB97M-V "
        "results depend on it.\n")
    ingest(project, progress=quiet)
    hits = Searcher(project).search("How do I request wB97M-V?")
    assert hits[0].chunk.title.endswith("ωB97M-V") and "title" in hits[0].via
