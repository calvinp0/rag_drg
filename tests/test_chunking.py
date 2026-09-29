import json
import textwrap

from rag_drg.chunking import (
    chunk_file,
    html_to_markdown,
    json_sections,
    markdown_sections,
    psi4_options_sections,
    python_sections,
    rst_sections,
    split_front_matter,
    split_long,
)


def test_front_matter():
    meta, body = split_front_matter("---\ntitle: X\nversion: [5, 6]\n---\n# Hi\n")
    assert meta == {"title": "X", "version": [5, 6]}
    assert body.strip() == "# Hi"
    assert split_front_matter("no front matter") == ({}, "no front matter")


def test_markdown_sections_breadcrumbs_and_code_fences():
    md = textwrap.dedent("""\
        # A
        intro
        ## B
        text b
        ```
        # not a heading
        ```
        ### C
        text c
        ## D
        text d
        """)
    secs = markdown_sections(md)
    crumbs = [tuple(c) for c, _ in secs]
    assert crumbs == [("A",), ("A", "B"), ("A", "B", "C"), ("A", "D")]
    assert "# not a heading" in secs[1][1]


def test_rst_sections():
    rst = "Title\n=====\n\nintro\n\nSub\n---\n\nbody\n\nOther\n=====\n\nmore\n"
    secs = rst_sections(rst)
    assert [tuple(c) for c, _ in secs] == [("Title",), ("Title", "Sub"), ("Other",)]


def test_split_long_respects_size_and_overlap():
    text = "\n\n".join(f"paragraph {i} " + "word " * 40 for i in range(20))
    pieces = split_long(text, 500, 80)
    assert len(pieces) > 5
    assert all(len(p) <= 600 for p in pieces)
    one_huge = "x" * 2000
    assert all(len(p) <= 500 for p in split_long(one_huge, 500, 50))


def test_python_sections():
    src = textwrap.dedent('''\
        """Module doc."""
        SETTING = {"a": 1}

        def public(x):
            """Doc."""
            return x

        def _private():
            pass

        class Foo:
            """Foo doc."""
            def method(self):
                return 1
        ''')
    secs = python_sections(src, "pkg.mod")
    titles = [" > ".join(c) for c, _ in secs]
    assert titles[0] == "pkg.mod" and "SETTING" in secs[0][1]
    assert "pkg.mod > def public" in titles
    assert "pkg.mod > class Foo" in titles
    assert "pkg.mod > class Foo > def method" in titles
    assert not any("_private" in t for t in titles)


def test_html_to_markdown_skips_nav_and_keeps_pre():
    html = """<html><head><title>Opt</title><style>x{}</style></head><body>
      <nav>menu</nav><h1>Opt</h1><p>Requests a geometry optimization.</p>
      <h2>Options</h2><pre>Opt=(TS,CalcFC)</pre></body></html>"""
    title, md = html_to_markdown(html)
    assert title == "Opt"
    assert "menu" not in md
    assert "# Opt" in md and "Opt=(TS,CalcFC)" in md


def test_json_schema_sections():
    schema = {
        "title": "S", "required": ["a"],
        "properties": {"a": {"type": "string"}, "b": {"type": "number"}},
        "$defs": {"thing": {"type": "object"}},
    }
    secs = json_sections(json.dumps(schema), "s.json")
    titles = [" > ".join(c) for c, _ in secs]
    assert "s.json > property a (required)" in titles
    assert "s.json > property b" in titles
    assert "s.json > $defs/thing" in titles


def test_psi4_options_parser():
    src = textwrap.dedent('''\
        if (name == "SCF" || options.read_globals()) {
            /*- SUBSECTION General -*/

            /*- Reference wavefunction type -*/
            options.add_str("REFERENCE", "RHF", "RHF ROHF UHF");
            /*- Max iterations !expert -*/
            options.add_int("MAXITER", 100);
        }
        if (name == "OPTKING" || options.read_globals()) {
            /*- Optimization type -*/
            options.add_str("OPT_TYPE", "MIN", "MIN TS IRC");
        }
        ''')
    secs = dict((c[1], body) for c, body in psi4_options_sections(src))
    assert set(secs) == {"SCF", "OPTKING"}
    assert 'REFERENCE [str] default="RHF" allowed: RHF ROHF UHF -- Reference wavefunction type' in secs["SCF"]
    assert "MAXITER [int] default=100 (expert) -- Max iterations" in secs["SCF"]
    assert "SUBSECTION" not in secs["SCF"]
    assert "OPT_TYPE" in secs["OPTKING"]


def test_chunk_file_markdown_uses_title(tmp_path):
    p = tmp_path / "card.md"
    p.write_text("---\ntitle: Card\n---\n## Sec\nbody text\n")
    meta, chunks = chunk_file(p, "card.md")
    assert meta["title"] == "Card"
    assert chunks[0].title == "Card > Sec"
    assert [c.ordinal for c in chunks] == list(range(len(chunks)))


def test_old_htm_pages_decode_windows_1252(tmp_path):
    """Gaussian 09 .htm pages are Windows-1252; they must not turn into replacement characters."""
    from rag_drg.chunking import chunk_file, read_text_file

    page = "<html><head><title>Opt</title></head><body><h1>Opt</h1><p>Distances in Å – see Freq.</p></body></html>"
    plain = tmp_path / "k_opt.htm"
    plain.write_bytes(page.encode("cp1252"))
    assert "Å –" in read_text_file(plain)
    _, chunks = chunk_file(plain, "k_opt.htm")
    assert "Å" in " ".join(c.text for c in chunks) and "�" not in " ".join(c.text for c in chunks)

    declared = tmp_path / "declared.htm"
    declared.write_bytes(page.replace("<head>", '<head><meta charset="iso-8859-1">').replace("–", "-").encode("latin-1"))
    assert "Å" in read_text_file(declared)
    utf8 = tmp_path / "utf8.html"
    utf8.write_text(page, encoding="utf-8")
    assert "Å –" in read_text_file(utf8)
