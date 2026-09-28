import io

import pytest

pypdf = pytest.importorskip("pypdf")

from rag_drg.chunking import chunk_file, classify_section, pdf_sections  # noqa: E402


def _raw_pdf(pages: list[list[str]]) -> bytes:
    """Build a minimal text PDF by hand (one Helvetica text block per page)."""
    objs: list[bytes] = []
    n_pages = len(pages)
    font_id = 3 + 2 * n_pages
    kids = " ".join(f"{3 + 2 * i} 0 R" for i in range(n_pages))
    objs.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objs.append(f"<< /Type /Pages /Kids [{kids}] /Count {n_pages} >>".encode())
    for i, lines in enumerate(pages):
        content_id = 4 + 2 * i
        objs.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {content_id} 0 R "
            f"/Resources << /Font << /F1 {font_id} 0 R >> >> >>".encode()
        )
        ops = ["BT /F1 11 Tf 72 740 Td 14 TL"]
        for line in lines:
            esc = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            ops.append(f"({esc}) Tj T*")
        ops.append("ET")
        stream = "\n".join(ops).encode()
        objs.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(out.tell())
        out.write(f"{i} 0 obj\n".encode() + body + b"\nendobj\n")
    xref = out.tell()
    out.write(f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode())
    for off in offsets:
        out.write(f"{off:010d} 00000 n \n".encode())
    out.write(f"trailer << /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return out.getvalue()


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
