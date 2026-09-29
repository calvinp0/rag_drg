import io

import pytest

pypdf = pytest.importorskip("pypdf")

from rag_drg.chunking import chunk_file, classify_section, pdf_sections  # noqa: E402
from tests.pdfutil import raw_pdf as _raw_pdf  # noqa: E402


@pytest.fixture
def manual_pdf(tmp_path):
    pages = [
        ["Contents", "1 Theory ........ 2", "2 Geometry Optimization ........ 3"],
        ["1 Theory of the SCF method",
         "The energy E = <Psi|H|Psi> is minimised with respect to the orbitals.",
         "The Fock operator F = h + J - K defines the Roothaan equations FC = SCe."],
        ["2 Geometry Optimization", "Use the OptTS keyword for saddle points.",
         "2.1 Keywords", "! OptTS Freq", "%geom", "Calc_Hess true", "Recalc_Hess 5", "end",
         "2.2 Transition states", "Always verify one imaginary frequency."],
    ]
    writer = pypdf.PdfWriter(clone_from=pypdf.PdfReader(io.BytesIO(_raw_pdf(pages))))
    writer.add_outline_item("Contents", 0)
    theory = writer.add_outline_item("1 Theory of the SCF method", 1)
    opt = writer.add_outline_item("2 Geometry Optimization", 2)
    writer.add_outline_item("2.1 Keywords", 2, parent=opt)
    writer.add_outline_item("2.2 Transition states", 2, parent=opt)
    assert theory is not None
    path = tmp_path / "orca_manual.pdf"
    with open(path, "wb") as f:
        writer.write(f)
    return path


def test_pdf_sections_follow_bookmarks(manual_pdf):
    secs = {" > ".join(c): (body, a, b) for c, body, a, b in pdf_sections(manual_pdf)}
    assert "orca_manual > Contents" not in secs  # table of contents is skipped
    kw = secs["orca_manual > 2 Geometry Optimization > 2.1 Keywords"]
    assert "Recalc_Hess 5" in kw[0] and "imaginary" not in kw[0]
    assert kw[1] == kw[2] == 3
    ts = secs["orca_manual > 2 Geometry Optimization > 2.2 Transition states"]
    assert "imaginary frequency" in ts[0]
    parent = secs["orca_manual > 2 Geometry Optimization"]
    assert "OptTS keyword" in parent[0] and "Recalc_Hess" not in parent[0]


def test_pdf_chunks_are_classified(manual_pdf):
    _, chunks = chunk_file(manual_pdf, "orca/6/orca_manual.pdf")
    by_title = {c.title: c for c in chunks}
    theory = next(c for t, c in by_title.items() if "Theory of the SCF" in t)
    assert theory.kind == "theory" and theory.title.endswith("(p. 2)")
    keywords = next(c for t, c in by_title.items() if t.startswith("orca_manual > 2 Geometry Optimization > 2.1"))
    assert keywords.kind == "reference"


def test_pdf_without_outline_falls_back_to_pages(tmp_path):
    path = tmp_path / "plain.pdf"
    path.write_bytes(_raw_pdf([["page one text"], ["page two text"]]))
    secs = pdf_sections(path)
    assert [(a, b) for _, _, a, b in secs] == [(1, 1), (2, 2)]


def test_classify_section_heuristics():
    assert classify_section(["Manual", "Theoretical background"], "prose") == "theory"
    assert classify_section(["Manual", "The $rem keywords"], "prose") == "reference"
    code = "\n".join(["! B3LYP def2-SVP", "%pal nprocs 4 end", "* xyz 0 1", "H 0 0 0", "*", "Some text"])
    assert classify_section(["Manual", "Running jobs"], code) == "reference"
    maths = "The energy is E = Σ ⟨φ|h|φ⟩ + ½ Σ (J − K) where α, β, γ, δ ≤ ε. " * 5
    assert classify_section(["Manual", "Correlation energy"], maths) == "theory"


def test_fonttools_warning_is_shown_once(caplog):
    """pypdf's per-font "fontTools is required" warning is collapsed to one actionable line."""
    import logging

    from rag_drg.chunking import _OnceFontToolsWarning

    _OnceFontToolsWarning.seen = False
    logger = logging.getLogger("pypdf._cmap")
    flt = _OnceFontToolsWarning()
    logger.addFilter(flt)
    try:
        with caplog.at_level(logging.WARNING, logger="pypdf._cmap"):
            for font in ("CMEX10", "CMMI5", "Courier"):
                logger.warning("fontTools is required to fully parse the encoding of a CFF Type1 font %s", font)
            logger.warning("some other pypdf warning")
    finally:
        logger.removeFilter(flt)
    msgs = [r.getMessage() for r in caplog.records]
    assert sum("fontTools" in m for m in msgs) == 1
    assert "pip install fonttools" in msgs[0]
    assert "some other pypdf warning" in msgs
