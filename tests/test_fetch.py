import functools
import threading
from http.server import HTTPServer, SimpleHTTPRequestHandler

from rag_drg.config import SourceConfig
from rag_drg.ingest import chunks_for_source, fetch_urls


class _Quiet(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def test_crawl_stays_under_prefix_and_maps_urls(tmp_path, project):
    site = tmp_path / "site"
    (site / "manual").mkdir(parents=True)
    (site / "manual" / "index.html").write_text(
        '<html><title>Manual</title><body><h1>Manual</h1><a href="opt.html">opt</a>'
        '<a href="/other/x.html">outside</a><a href="opt.html#frag">dup</a></body></html>'
    )
    (site / "manual" / "opt.html").write_text(
        "<html><title>Opt</title><body><h1>Opt keyword</h1><p>Opt=(TS,CalcFC) finds a TS.</p></body></html>"
    )
    (site / "other").mkdir()
    (site / "other" / "x.html").write_text("<html><body>should not be fetched</body></html>")

    srv = HTTPServer(("127.0.0.1", 0), functools.partial(_Quiet, directory=str(site)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}"
    try:
        src = SourceConfig(
            name="web", type="url", path=tmp_path / "cache", urls=[f"{base}/manual/index.html"],
            crawl=True, allow_prefix=[f"{base}/manual/"], delay=0, domain="ess", software="gaussian",
        )
        fetch_urls(src)
    finally:
        srv.shutdown()

    fetched = sorted(p.name for p in (tmp_path / "cache").glob("*.html"))
    assert len(fetched) == 2  # index + opt, not /other/, no duplicate for #frag
    chunks = chunks_for_source(project, src)
    opt = [c for c in chunks if "CalcFC" in c.text]
    assert opt and opt[0].url == f"{base}/manual/opt.html"
    assert opt[0].title.startswith("Opt")
    assert opt[0].software == "gaussian"


def test_crawl_max_depth(tmp_path):
    site = tmp_path / "site"
    site.mkdir()
    (site / "keywords.html").write_text('<a href="opt.html">opt</a>')
    (site / "opt.html").write_text('<h1>Opt</h1><a href="deeper.html">more</a>')
    (site / "deeper.html").write_text("<h1>Deeper</h1>")
    srv = HTTPServer(("127.0.0.1", 0), functools.partial(_Quiet, directory=str(site)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}"
    try:
        src = SourceConfig(name="g", type="url", path=tmp_path / "cache", urls=[f"{base}/keywords.html"],
                           crawl=True, allow_prefix=[base], max_depth=1, delay=0)
        fetch_urls(src)
    finally:
        srv.shutdown()
    import json
    fetched = set(json.loads((tmp_path / "cache" / "_urls.json").read_text()).values())
    assert fetched == {f"{base}/keywords.html", f"{base}/opt.html"}


def test_fetch_manuals_downloads_vendor_files_into_the_source_folder(tmp_path):
    """`download:` on a local source: a direct PDF, a link looked up on a page, and an archive.
    Existing files are kept, and an archive member that points outside the folder is refused."""
    import io
    import tarfile

    from rag_drg.cli import main

    site = tmp_path / "site"
    (site / "manual" / "_static").mkdir(parents=True)
    (site / "manual" / "qchem_manual.pdf").write_bytes(b"%PDF-1.4 qchem")
    (site / "manual" / "_static" / "orca.pdf").write_bytes(b"%PDF-1.4 orca")
    (site / "manual" / "index.html").write_text('<a href="_static/orca.pdf?v=1">Download PDF</a>')

    def tgz(name, members):
        with tarfile.open(site / name, "w:gz") as tar:
            for member, data in members:
                info = tarfile.TarInfo(member)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))

    tgz("g09ur.tgz", [("g09ur/k_opt.htm", b"<html><body><h1>Opt</h1><p>Opt keyword</p></body></html>")])
    tgz("evil.tgz", [("../escaped.htm", b"<html></html>")])
    srv = HTTPServer(("127.0.0.1", 0), functools.partial(_Quiet, directory=str(site)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}"

    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "rag_drg.yaml").write_text(f"""
index_path: index/test.sqlite
sources:
  - {{name: qchem, type: local, path: sources/qchem, download: ["{base}/manual/qchem_manual.pdf"]}}
  - name: orca
    type: local
    path: sources/orca
    download: [{{page: "{base}/manual/index.html", link: orca.pdf}}]
  - name: g09
    type: local
    path: sources/g09
    download: [{{url: "{base}/g09ur.tgz", creates: g09ur}}, "{base}/evil.tgz", "{base}/missing.pdf"]
""")
    cfg = str(proj / "rag_drg.yaml")
    try:
        assert main(["--config", cfg, "fetch"]) == 0            # a plain fetch downloads no manuals
        assert not (proj / "sources").exists()
        assert main(["--config", cfg, "fetch", "--manuals"]) == 0
        assert (proj / "sources/qchem/qchem_manual.pdf").read_bytes() == b"%PDF-1.4 qchem"
        assert (proj / "sources/orca/orca.pdf").read_bytes() == b"%PDF-1.4 orca"
        assert (proj / "sources/g09/g09ur/k_opt.htm").is_file()
        assert not (proj / "sources/escaped.htm").exists()        # refused, and it did not stop the rest
        assert not list((proj / "sources").rglob(".download-*"))  # no partial files left behind
        # a second run keeps what is there (a hand-placed or edited copy is not overwritten)
        (proj / "sources/qchem/qchem_manual.pdf").write_bytes(b"mine")
        assert main(["--config", cfg, "fetch", "--source", "qchem"]) == 0
        assert (proj / "sources/qchem/qchem_manual.pdf").read_bytes() == b"mine"
    finally:
        srv.shutdown()


def test_fetch_manuals_crawls_manual_sources_and_builds_pdfs(tmp_path, monkeypatch):
    """--manuals also covers manuals that are many pages: a url source marked `manual: true` is
    crawled (and kept on later runs), and a `build: dokuwiki-pdf` entry is built. Neither happens
    on a plain fetch or during `ingest --fetch`, the nightly job."""
    from rag_drg import dokuwiki_pdf
    from rag_drg.cli import main
    from rag_drg.config import load_config
    from rag_drg.ingest import ingest

    site = tmp_path / "site"
    (site / "kw").mkdir(parents=True)
    (site / "kw" / "index.html").write_text('<html><body><h1>Keywords</h1><a href="opt.html">Opt</a></body></html>')
    (site / "kw" / "opt.html").write_text("<html><body><h1>Opt</h1><p>Opt=CalcFC computes force constants.</p></body></html>")
    srv = HTTPServer(("127.0.0.1", 0), functools.partial(_Quiet, directory=str(site)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}"
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "rag_drg.yaml").write_text(f"""
index_path: index/test.sqlite
sources:
  - name: g16-keywords
    type: url
    manual: true
    crawl: true
    delay: 0
    urls: ["{base}/kw/index.html"]
    allow_prefix: ["{base}/kw/"]
    software: gaussian
    version: "16"
  - name: molpro
    type: local
    path: sources/molpro/2026
    version: "2026"
    download: [{{build: dokuwiki-pdf, start: "{base}/doku.php?id=toc", creates: manual.pdf}}]
""")
    cfg_path = str(proj / "rag_drg.yaml")
    builds = []

    def fake_build(start, out, cache=None, expect_version=None, log=print, **_):
        assert cache is not None and cache.is_dir()   # a fresh page cache, not ~/.cache
        builds.append((start, expect_version))
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"%PDF-1.4 molpro")
        return out

    monkeypatch.setattr(dokuwiki_pdf, "build_pdf", fake_build)
    cache = proj / "sources_cache" / "g16-keywords"
    try:
        assert main(["--config", cfg_path, "fetch"]) == 0
        ingest(load_config(cfg_path), fetch=True, embed=False, progress=lambda *_: None)
        assert not cache.exists() and not builds                      # opt-in only

        assert main(["--config", cfg_path, "fetch", "--manuals"]) == 0
        assert len(list(cache.glob("*.html"))) == 2 and (cache / "_urls.json").exists()
        assert (proj / "sources/molpro/2026/manual.pdf").exists()
        assert builds == [(f"{base}/doku.php?id=toc", "2026")]

        (site / "kw" / "index.html").unlink()                          # a second run must not need the site
        assert main(["--config", cfg_path, "fetch", "--manuals"]) == 0
        assert len(builds) == 1 and len(list(cache.glob("*.html"))) == 2

        report = ingest(load_config(cfg_path), embed=False, progress=lambda *_: None)
        assert report["g16-keywords"]["added"] >= 1                    # indexed without naming it
    finally:
        srv.shutdown()
