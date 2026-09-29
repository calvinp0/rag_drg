"""scripts/molpro_manual_pdf.py: DokuWiki crawl order, link rewriting (offline, fake wiki)."""
import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "molpro_manual_pdf", Path(__file__).resolve().parents[1] / "scripts" / "molpro_manual_pdf.py")
mm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mm)

DOKU = "https://www.molpro.net/manual/doku.php"
START = DOKU + "?id=table_of_contents"


def _page(title, *links):
    items = "".join(f'<a href="{h}">x</a>' for h in links)
    return f"<html><body><div class='dokuwiki export'><h1 id='top'>{title}</h1>{items}</div></body></html>"


WIKI = {
    "table_of_contents": _page(
        "Contents", "/manual/doku.php?id=intro", "/manual/doku.php?id=scf#options",
        "/manual/doku.php?id=intro&amp;do=edit", "https://other.org/doku.php?id=x",
        "/manual/lib/exe/fetch.php?media=fig.png"),
    "intro": _page("Introduction", "/manual/doku.php?id=intro:running", "#top"),
    "intro:running": _page("Running Molpro", "/manual/doku.php?id=table_of_contents"),
    "scf": _page("SCF", "/manual/doku.php?id=missing"),
}


def fake_fetch(url):
    from urllib.parse import parse_qs, urlsplit
    q = parse_qs(urlsplit(url).query)
    assert q["do"] == ["export_xhtml"]
    pid = q["id"][0]
    if pid not in WIKI:
        raise RuntimeError(f"404 {url}")
    return WIKI[pid]


def test_page_id_filters_actions_and_other_hosts():
    assert mm.page_id(DOKU + "?id=Intro:Running", DOKU) == "intro:running"
    assert mm.page_id(DOKU + "?id=intro&do=edit", DOKU) is None
    assert mm.page_id(DOKU + "?id=intro&rev=123", DOKU) is None
    assert mm.page_id("https://other.org/manual/doku.php?id=x", DOKU) is None
    assert mm.page_id("https://www.molpro.net/manual/lib/exe/fetch.php?media=a.png", DOKU) is None
    assert mm.page_id(DOKU + "/intro/running", DOKU) == "intro:running"


def test_crawl_is_depth_first_in_link_order_and_skips_failures():
    pages = mm.crawl(START, fake_fetch, log=lambda *_: None)
    assert [p for p, _ in pages] == ["table_of_contents", "intro", "intro:running", "scf"]


def test_combine_rewrites_links_to_anchors_and_assets_to_absolute():
    pages = mm.crawl(START, fake_fetch, log=lambda *_: None)
    doc = mm.combine(pages, START, mathjax=False)
    assert 'href="#p-intro"' in doc
    assert 'href="#p-scf--options"' in doc  # cross-page fragment
    assert 'href="#p-intro--top"' in doc  # in-page fragment
    norm = doc.replace("'", '"')
    assert 'id="p-intro--top"' in norm and 'id="p-scf--top"' in norm  # ids namespaced per page
    assert 'href="https://www.molpro.net/manual/lib/exe/fetch.php?media=fig.png"' in doc
    assert 'href="https://other.org/doku.php?id=x"' in doc
    assert "<li><a href=\"#p-intro-running\">Running Molpro</a></li>" in doc
    assert "mathjax" not in doc.lower()
