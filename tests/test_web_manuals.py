"""Manuals that arrive as many saved/mirrored HTML pages, mixed HTML + PDF folders, sidecar
metadata for books, and the scanned-PDF check."""

import textwrap

import pytest

from rag_drg.chunking import chunk_file, parse_html
from rag_drg.config import SourceConfig
from rag_drg.ingest import chunks_for_source

SPHINX_PAGE = textwrap.dedent("""\
    <!DOCTYPE html>
    <!-- saved from url=(0070)https://www.faccts.de/docs/orca/6.0/manual/contents/structurereactivity/optimizations.html -->
    <html><head><title>Geometry Optimizations — ORCA 6.0 Manual</title></head>
    <body>
      <header class="bd-header"><a href="/">ORCA home</a> search the docs</header>
      <div class="bd-sidebar-primary bd-sidebar"><ul><li>Single points</li><li>Frequencies</li></ul></div>
      <main id="main-content">
        <article class="bd-article" role="main">
          <h1>Geometry Optimizations<a class="headerlink" href="#x">¶</a></h1>
          <p>ORCA optimizes in redundant internal coordinates.</p>
          <h2>Transition states</h2>
          <p>Use the OptTS keyword; compute the Hessian first.</p>
          <pre>! B3LYP def2-SVP OptTS
    %geom Calc_Hess true end</pre>
        </article>
      </main>
      <div class="prev-next-area">Previous: Single points</div>
      <footer>© FACCTs</footer>
    </body></html>
    """)


def test_saved_sphinx_page_keeps_only_main_content():
    title, text, url = parse_html(SPHINX_PAGE)
    assert title == "Geometry Optimizations"
    assert url.startswith("https://www.faccts.de/docs/orca/6.0/manual/")
    assert "OptTS" in text and "Calc_Hess true" in text
    for noise in ("ORCA home", "Frequencies", "Previous: Single points", "FACCTs", "¶"):
        assert noise not in text


def test_html_chunks_carry_url_and_kind(tmp_path):
    f = tmp_path / "optimizations.html"
    f.write_text(SPHINX_PAGE)
    meta, chunks = chunk_file(f, "optimizations.html")
    assert meta["url"].endswith("optimizations.html")
    ts = [c for c in chunks if c.title.endswith("Transition states")]
    assert ts and ts[0].kind == "reference" and "Calc_Hess" in ts[0].text


def _page(title, body):
    return f"<html><head><title>{title}</title></head><body><main><h1>{title}</h1><p>{body}</p></main></body></html>"


def test_mirrored_folder_ingest(tmp_path, project):
    root = tmp_path / "orca6"
    site = root / "www.faccts.de" / "docs" / "orca" / "6.0" / "manual"
    (site / "contents").mkdir(parents=True)
    (site / "contents" / "scf.html").write_text(_page("SCF Convergence", "Use TightSCF and SlowConv for difficult cases. " * 5))
    (site / "contents" / "theory.html").write_text(_page("Theoretical background of RIJCOSX", "The Coulomb matrix J is approximated ... " * 5))
    # Clutter that must be ignored:
    (site / "genindex.html").write_text(_page("Index", "A B C"))
    (site / "_static").mkdir()
    (site / "_static" / "embedded.html").write_text(_page("Static", "theme stuff"))
    (site / "_sources").mkdir()
    (site / "_sources" / "scf.rst.txt").write_text("SCF Convergence\n===\nsource copy")
    # A browser "Save page as" copy of the same page, with its asset folder:
    (root / "saved").mkdir()
    (root / "saved" / "scf.html").write_text(_page("SCF Convergence", "Use TightSCF and SlowConv for difficult cases. " * 5))
    (root / "saved" / "scf_files").mkdir()
    (root / "saved" / "scf_files" / "frame.html").write_text(_page("Frame", "ads"))
    (root / "_meta.yaml").write_text("software: orca\nversion: '6'\n")

    src = SourceConfig(name="orca6-web", type="local", path=root, domain="ess")
    chunks = chunks_for_source(project, src)
    paths = {c.path for c in chunks}
    assert paths == {
        "www.faccts.de/docs/orca/6.0/manual/contents/scf.html",
        "www.faccts.de/docs/orca/6.0/manual/contents/theory.html",
    }  # clutter skipped, saved duplicate of scf.html dropped
    scf = next(c for c in chunks if c.path.endswith("scf.html"))
    assert scf.url == "https://www.faccts.de/docs/orca/6.0/manual/contents/scf.html"
    assert scf.software == "orca" and scf.version == "6" and scf.doc_type == "reference"
    theory = next(c for c in chunks if c.path.endswith("theory.html"))
    assert theory.doc_type == "theory"


pypdf = pytest.importorskip("pypdf")
from tests.pdfutil import raw_pdf  # noqa: E402


def test_book_pdf_with_sidecar_metadata(tmp_path, project):
    book_dir = tmp_path / "gaussian" / "book"
    book_dir.mkdir(parents=True)
    (book_dir / "foresman_frisch.pdf").write_bytes(raw_pdf([
        ["Chapter 2 Single point energy calculations",
         "The route section #P HF/6-31G(d) Pop=Reg requests a single point with a population analysis."],
    ]))
    (book_dir / "_meta.yaml").write_text(textwrap.dedent("""\
        title: Exploring Chemistry with Electronic Structure Methods
        version: ["09", "16"]
        tags: [book]
        """))
    src = SourceConfig(name="gaussian-books", type="local", path=tmp_path / "gaussian", domain="ess", software="gaussian")
    chunks = chunks_for_source(project, src)
    assert chunks
    c = chunks[0]
    assert c.version == "09|16" and "book" in c.tags
    assert c.title.startswith("Exploring Chemistry with Electronic Structure Methods")
    assert c.path == "book/foresman_frisch.pdf"


def test_pdf_quality_verdicts(tmp_path):
    from rag_drg.chunking import pdf_quality

    good = tmp_path / "good.pdf"
    good.write_bytes(raw_pdf([["This page has a perfectly normal text layer with many readable words."]] * 3))
    assert pdf_quality(good)["verdict"] == "ok"

    scanned = tmp_path / "scanned.pdf"  # pages with no text layer at all, like an image-only scan
    scanned.write_bytes(raw_pdf([[], [], ["Copyright page"]]))
    q = pdf_quality(scanned)
    assert q["verdict"] == "scanned" and q["pages_without_text"] == 3

    garbled = tmp_path / "garbled.pdf"
    garbled.write_bytes(raw_pdf([["(cid:12)(cid:40)(cid:77)(cid:3)(cid:90) 0x3#@!%^ &*()" * 4]] * 2))
    assert pdf_quality(garbled)["verdict"] == "garbled"
