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
