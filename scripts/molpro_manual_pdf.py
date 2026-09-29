#!/usr/bin/env python3
"""Download the Molpro online manual (DokuWiki) and turn it into one PDF.

Starts at the table of contents, follows every wiki link depth-first in page order (so the
PDF reads in TOC order), fetches each page's clean ``do=export_xhtml`` view, stitches them
into one HTML file with internal links pointing inside the document, and prints that to PDF
with headless Chromium/Chrome (MathJax renders the formulas first).

Standard library only. Run it on a machine that can reach molpro.net:

    python scripts/molpro_manual_pdf.py                      # -> sources/molpro/web/molpro_manual.pdf
    python scripts/molpro_manual_pdf.py --out sources/molpro/2026/molpro_manual_web.pdf
    python scripts/molpro_manual_pdf.py --out ~/molpro.pdf --delay 1
    python scripts/molpro_manual_pdf.py --html-only          # just the combined HTML
    python scripts/molpro_manual_pdf.py --chrome /path/to/chrome

Pages are cached under ``--cache`` so an interrupted run resumes without re-downloading.
Works for other DokuWiki sites too: pass ``--start <url of the start page>``.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable

DEFAULT_START = "https://www.molpro.net/manual/doku.php?id=table_of_contents"
USER_AGENT = "rag-drg manual fetcher (Dana Research Group, Technion; polite single-threaded crawl)"
MATHJAX = "https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-chtml.js"

_HREF = re.compile(r'''(href|src)\s*=\s*(["'])(.*?)\2''', re.I | re.S)
_BODY = re.compile(r"<body[^>]*>(.*)</body>", re.I | re.S)
_TITLE_H = re.compile(r"<h[1-3][^>]*>(.*?)</h[1-3]>", re.I | re.S)
_TAGS = re.compile(r"<[^>]+>")
_ID_ATTR = re.compile(r'''(?<![-\w])id\s*=\s*(["'])(.*?)\1''', re.I)
# DokuWiki actions / old revisions / media pages are not manual content.
_SKIP_PARAMS = ("do", "rev", "idx", "media", "image", "tab_files", "tab_details")

Fetch = Callable[[str], str]


def page_id(url: str, doku: str) -> str | None:
    """Wiki page id for a link to a page of this wiki, else None."""
    parts = urllib.parse.urlsplit(url)
    base = urllib.parse.urlsplit(doku)
    if (parts.scheme, parts.netloc) != (base.scheme, base.netloc):
        return None
    q = urllib.parse.parse_qs(parts.query)
    if parts.path == base.path:
        if any(k in q for k in _SKIP_PARAMS) or "id" not in q:
            return None
        pid = q["id"][0]
    elif parts.path.startswith(base.path + "/"):  # userewrite=2 style: doku.php/ns:page
        if any(k in q for k in _SKIP_PARAMS):
            return None
        pid = parts.path[len(base.path) + 1:].replace("/", ":")
    else:
        return None
    pid = pid.strip().strip(":").lower()
    return pid or None


def export_url(doku: str, pid: str) -> str:
    return f"{doku}?{urllib.parse.urlencode({'id': pid, 'do': 'export_xhtml'})}"


def http_fetch(delay: float, cache: Path | None) -> Fetch:
    def fetch(url: str) -> str:
        path = cache / (hashlib.sha1(url.encode()).hexdigest() + ".html") if cache else None
        if path and path.exists():
            return path.read_text(encoding="utf-8")
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        last: Exception | None = None
        for attempt in range(4):
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    text = resp.read().decode("utf-8", errors="replace")
                break
            except Exception as e:  # noqa: BLE001 - retry, then give up on this page
                last = e
                time.sleep(2 ** attempt)
        else:
            raise RuntimeError(f"failed to fetch {url}: {last}")
        if path:
            path.write_text(text, encoding="utf-8")
        if delay:
            time.sleep(delay)
        return text
    return fetch


def body_of(page: str) -> str:
    m = _BODY.search(page)
    return m.group(1) if m else page


def title_of(body: str, pid: str) -> str:
    m = _TITLE_H.search(body)
    return html.unescape(_TAGS.sub("", m.group(1))).strip() if m else pid


def anchor(pid: str, frag: str = "") -> str:
    a = "p-" + re.sub(r"[^a-z0-9_.-]+", "-", pid.lower())
    return f"{a}--{frag}" if frag else a


def crawl(start: str, fetch: Fetch, max_pages: int = 2000, log=print) -> list[tuple[str, str]]:
    """Depth-first, in link order, from `start`. Returns [(page id, body html)]."""
    doku = urllib.parse.urlsplit(start)._replace(query="", fragment="").geturl()
    first = page_id(start, doku)
    if not first:
        raise SystemExit(f"--start must be a DokuWiki page URL (doku.php?id=...): {start}")
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    stack = [first]
    while stack and len(out) < max_pages:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        try:
            body = body_of(fetch(export_url(doku, pid)))
        except RuntimeError as e:
            log(f"  ! {e}")
            continue
        out.append((pid, body))
        log(f"  [{len(out)}] {pid}")
        children = []
        for _, _, ref in _HREF.findall(body):
            child = page_id(urllib.parse.urljoin(doku, html.unescape(ref)), doku)
            if child and child not in seen and child not in children:
                children.append(child)
        stack.extend(reversed(children))  # visit in the order the page lists them
    if stack:
        log(f"  stopped at --max-pages {max_pages}; {len(set(stack) - seen)} page(s) not fetched")
    return out


def combine(pages: list[tuple[str, str]], start: str, mathjax: bool = True) -> str:
    """One HTML document; wiki links become in-document anchors, assets absolute URLs."""
    doku = urllib.parse.urlsplit(start)._replace(query="", fragment="").geturl()
    included = {pid for pid, _ in pages}

    def rewrite(pid: str, body: str) -> str:
        def ref(m: re.Match) -> str:
            attr, quote, raw = m.groups()
            target = urllib.parse.urljoin(doku, html.unescape(raw))
            if raw.startswith("#"):
                new = "#" + anchor(pid, raw[1:])
            else:
                other = page_id(target, doku)
                if attr.lower() == "href" and other in included:
                    frag = urllib.parse.urlsplit(target).fragment
                    new = "#" + anchor(other, frag)
                else:
                    new = target
            return f"{attr}={quote}{html.escape(new, quote=True)}{quote}"

        body = _HREF.sub(ref, body)
        # Section ids repeat across pages ("options", "examples"); namespace them per page.
        return _ID_ATTR.sub(lambda m: f'id={m.group(1)}{anchor(pid, m.group(2))}{m.group(1)}', body)

    toc = "\n".join(
        f'<li><a href="#{anchor(pid)}">{html.escape(title_of(body, pid))}</a></li>'
        for pid, body in pages
    )
    sections = "\n".join(
        f'<section class="page" id="{anchor(pid)}">\n{rewrite(pid, body)}\n</section>'
        for pid, body in pages
    )
    mj = (
        "<script>window.MathJax={tex:{inlineMath:[['$','$'],['\\\\(','\\\\)']],"
        "displayMath:[['$$','$$'],['\\\\[','\\\\]']]},startup:{pageReady:()=>MathJax.startup"
        ".defaultPageReady().then(()=>{document.body.dataset.mathjax='done'})}};</script>\n"
        f'<script src="{MATHJAX}" async></script>'
        if mathjax else ""
    )
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Molpro manual</title>
<style>
  body {{ font: 10.5pt/1.4 "DejaVu Serif", Georgia, serif; margin: 0 1.2cm; }}
  pre, code {{ font: 9pt "DejaVu Sans Mono", monospace; white-space: pre-wrap; }}
  pre {{ background: #f5f5f5; padding: .4em; border: 1px solid #ddd; }}
  table {{ border-collapse: collapse; }} td, th {{ border: 1px solid #ccc; padding: 2px 5px; }}
  img {{ max-width: 100%; }}
  section.page {{ break-before: page; }}
  .source {{ color: #666; font-size: 9pt; }}
</style>
{mj}
</head><body>
<h1>Molpro manual</h1>
<p class="source">Downloaded {time.strftime('%Y-%m-%d')} from {html.escape(start)} ({len(pages)} pages).</p>
<ol>{toc}</ol>
{sections}
</body></html>
"""


def find_chrome(explicit: str | None) -> str | None:
    if explicit:
        return explicit
    names = ["chromium", "chromium-browser", "google-chrome", "google-chrome-stable", "chrome",
             "msedge"]
    for n in names:
        if p := shutil.which(n):
            return p
    for p in [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        "/opt/pw-browsers/chromium",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    ]:
        if os.path.exists(p):
            if os.path.isdir(p):  # Playwright layout: chromium-*/chrome-linux/chrome
                hits = sorted(Path(p).glob("*/chrome")) + sorted(Path(p).glob("chrome-*/chrome"))
                if hits:
                    return str(hits[0])
                continue
            return p
    return None


def print_pdf(chrome: str, html_path: Path, pdf_path: Path, wait_ms: int) -> None:
    cmd = [
        chrome, "--headless=new", "--disable-gpu", "--no-sandbox", "--no-pdf-header-footer",
        f"--virtual-time-budget={wait_ms}", "--run-all-compositor-stages-before-draw",
        f"--print-to-pdf={pdf_path}", html_path.resolve().as_uri(),
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if not pdf_path.exists() or pdf_path.stat().st_size == 0:
        raise RuntimeError("Chrome produced no PDF")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--start", default=DEFAULT_START, help="start page (default: Molpro TOC)")
    ap.add_argument("--out", type=Path, default=Path("sources/molpro/web/molpro_manual.pdf"))
    ap.add_argument("--cache", type=Path, default=None,
                    help="page cache dir (default: ~/.cache/rag-drg/<site>); kept out of --out's "
                         "folder so rag-drg does not ingest raw pages next to the PDF")
    ap.add_argument("--delay", type=float, default=0.5, help="seconds between requests")
    ap.add_argument("--max-pages", type=int, default=2000)
    ap.add_argument("--html-only", action="store_true", help="write the combined HTML, no PDF")
    ap.add_argument("--no-mathjax", action="store_true", help="leave formulas as LaTeX source")
    ap.add_argument("--chrome", help="path to chrome/chromium (default: search PATH etc.)")
    ap.add_argument("--wait-ms", type=int, default=60000,
                    help="time Chrome gets to load images and typeset math before printing")
    a = ap.parse_args(argv)

    a.out.parent.mkdir(parents=True, exist_ok=True)
    site = urllib.parse.urlsplit(a.start).netloc.replace(":", "_") or "site"
    cache = a.cache or Path.home() / ".cache" / "rag-drg" / site
    cache.mkdir(parents=True, exist_ok=True)
    print(f"Crawling {a.start} (cache: {cache})")
    pages = crawl(a.start, http_fetch(a.delay, cache), a.max_pages)
    if not pages:
        print("No pages fetched.", file=sys.stderr)
        return 1
    # The combined HTML sits in the cache unless it is the deliverable, so an ingested
    # sources/ folder holds only the PDF (not the same manual twice).
    html_path = a.out.with_suffix(".html") if a.html_only else cache / (a.out.stem + ".html")
    html_path.write_text(combine(pages, a.start, mathjax=not a.no_mathjax), encoding="utf-8")
    print(f"Wrote {html_path} ({len(pages)} pages)")
    if a.html_only:
        return 0
    chrome = find_chrome(a.chrome)
    if not chrome:
        print("No Chrome/Chromium found: open the HTML in a browser and Print -> Save as PDF, "
              "or pass --chrome /path/to/chrome.", file=sys.stderr)
        return 2
    print(f"Printing to PDF with {chrome} ...")
    print_pdf(chrome, html_path, a.out, a.wait_ms)
    print(f"Wrote {a.out} ({a.out.stat().st_size / 1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
