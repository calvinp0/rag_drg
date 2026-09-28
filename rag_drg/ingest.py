"""Fetching remote sources and ingesting everything into the index."""

from __future__ import annotations

import fnmatch
import hashlib
import json
import logging
import re
import subprocess
import urllib.request
from pathlib import Path
from typing import Callable, Iterator

from .chunking import SUPPORTED_SUFFIXES, Chunk, chunk_file, chunk_html_string
from .config import Config, SourceConfig
from .embeddings import make_embedder
from .store import Store

log = logging.getLogger(__name__)

MAX_TEXT_BYTES = 3_000_000
URL_MAP = "_urls.json"
USER_AGENT = "rag-drg/0.1 (research group documentation indexer)"

# Extension point for plugins (rag_drg/tools/*): fetchers for extra source types, e.g.
# FETCHERS["zotero"] = fn(src, cfg). A plugin source's cache dir (sources_cache/<name>) is then
# indexed like a local folder; bookkeeping files it keeps there go in IGNORED_FILES.
FETCHERS: dict[str, Callable[[SourceConfig, Config], None]] = {}
IGNORED_FILES: set[str] = {URL_MAP}
_plugins_loaded = False


# --------------------------------------------------------------------------- #
# Fetch
# --------------------------------------------------------------------------- #

def _git(*args: str, cwd: Path | None = None):
    log.debug("git %s", " ".join(args))
    subprocess.run(["git", *args], cwd=cwd, check=True)


def _plugin_fetcher(src: SourceConfig) -> Callable[[SourceConfig, Config], None] | None:
    global _plugins_loaded
    if src.type not in FETCHERS and not _plugins_loaded:
        _plugins_loaded = True
        from .plugins import plugin_modules

        plugin_modules()  # importing a plugin registers its fetchers
    return FETCHERS.get(src.type)


def _config_for(src: SourceConfig) -> Config:
    """For callers that don't pass `cfg`: the default config, if it defines this very source."""
    from .config import load_config

    cfg = load_config()
    if any(s.name == src.name and s.path == src.path for s in cfg.sources):
        return cfg
    raise ValueError(f"source '{src.name}' (type '{src.type}') needs the config: call fetch_source(src, cfg)")


def fetch_source(src: SourceConfig, cfg: Config | None = None) -> None:
    if src.type == "local":
        return
    fetcher = _plugin_fetcher(src) if src.type not in ("git", "url") else None
    if fetcher is not None:
        fetcher(src, cfg if cfg is not None else _config_for(src))
        return
    assert src.path is not None
    if src.type == "git":
        if not src.url:
            raise ValueError(f"git source '{src.name}' needs a url")
        if (src.path / ".git").exists():
            _git("fetch", "--depth", "1", "origin", src.ref or "HEAD", cwd=src.path)
            _git("reset", "--hard", "FETCH_HEAD", cwd=src.path)
        else:
            src.path.parent.mkdir(parents=True, exist_ok=True)
            args = ["clone", "--depth", "1"]
            if src.ref:
                args += ["--branch", src.ref]
            if src.sparse:
                args += ["--filter=blob:none", "--no-checkout"]
            _git(*args, src.url, str(src.path))
            if src.sparse:
                # Non-cone mode so single files (e.g. psi4/src/read_options.cc) can be selected.
                _git("sparse-checkout", "set", "--no-cone", *src.sparse, cwd=src.path)
                _git("checkout", cwd=src.path)
        return
    if src.type == "url":
        fetch_urls(src)
        return
    raise ValueError(f"Unknown source type '{src.type}'")


_HREF = re.compile(r"""href\s*=\s*["']([^"'#]+)""", re.IGNORECASE)
_SKIP_EXT = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".css", ".js", ".zip", ".gz", ".tar", ".ico", ".xml")


def _url_file_name(url: str) -> str:
    name = re.sub(r"[^A-Za-z0-9]+", "_", url.split("://", 1)[-1]).strip("_")[:120]
    name += "_" + hashlib.sha1(url.encode()).hexdigest()[:8]
    return name + (".pdf" if url.lower().endswith(".pdf") else ".html")


def _allowed(url: str, src: SourceConfig) -> bool:
    if any(d in url for d in src.deny):
        return False
    if url.lower().endswith(_SKIP_EXT):
        return False
    return any(url.startswith(p) for p in src.allow_prefix) if src.allow_prefix else True


def fetch_urls(src: SourceConfig) -> None:
    """Download `src.urls`; with `crawl: true`, follow links under `allow_prefix` (BFS)."""
    import time
    from collections import deque
    from html import unescape
    from urllib.parse import urljoin, urldefrag

    assert src.path is not None
    src.path.mkdir(parents=True, exist_ok=True)
    mapping: dict[str, str] = {}
    queue = deque((u, 0) for u in src.urls)
    seen = set(src.urls)
    limit = src.max_pages if src.crawl else len(src.urls)
    while queue and len(mapping) < limit:
        url, depth = queue.popleft()
        dest = src.path / _url_file_name(url)
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                ctype = resp.headers.get("Content-Type", "")
                data = resp.read()
        except Exception as e:  # noqa: BLE001 - keep going
            log.warning("Failed to fetch %s: %s", url, e)
            continue
        if "html" not in ctype and "pdf" not in ctype and not url.lower().endswith(".pdf"):
            continue
        dest.write_bytes(data)
        mapping[dest.name] = url
        log.info("  fetched %s", url)
        if src.crawl and "html" in ctype and (src.max_depth is None or depth < src.max_depth):
            html = data.decode("utf-8", errors="replace")
            for href in _HREF.findall(html):
                nxt = urldefrag(urljoin(url, unescape(href)))[0]
                if nxt not in seen and _allowed(nxt, src):
                    seen.add(nxt)
                    queue.append((nxt, depth + 1))
        if src.delay:
            time.sleep(src.delay)
    (src.path / URL_MAP).write_text(json.dumps(mapping, indent=2))


# --------------------------------------------------------------------------- #
# Ingest
# --------------------------------------------------------------------------- #

def _matches(rel: str, patterns: list[str]) -> bool:
    for p in patterns:
        if fnmatch.fnmatch(rel, p):
            return True
        # Let "**/x" also match "x" at the root.
        if p.startswith("**/") and fnmatch.fnmatch(rel, p[3:]):
            return True
    return False


def iter_files(src: SourceConfig) -> Iterator[tuple[Path, str]]:
    root = src.path
    if root is None or not root.exists():
        return
    if root.is_file():
        yield root, root.name
        return
    for p in sorted(root.rglob("*")):
        if not p.is_file() or ".git" in p.parts or p.name in IGNORED_FILES:
            continue
        if p.name.startswith("."):
            continue
        if src.type == "local" and p.parent == root and p.name.lower() == "readme.md":
            # A README at the root of a curated directory describes the folder, not the domain.
            continue
        rel = p.relative_to(root).as_posix()
        if p.suffix.lower() not in SUPPORTED_SUFFIXES or _is_clutter(p.relative_to(root)):
            continue
        if not _matches(rel, src.include) or _matches(rel, src.exclude):
            continue
        if p.suffix.lower() != ".pdf" and p.stat().st_size > MAX_TEXT_BYTES:
            log.info("Skipping large file %s", rel)
            continue
        yield p, rel


# Saved/mirrored web manuals: browser "_files" asset folders, Sphinx build artefacts and
# generated index/search pages carry no documentation and would duplicate real pages.
_CLUTTER_DIRS = ("_static", "_sources", "_images", "_downloads", "_modules", "_sphinx_design_static")
_CLUTTER_FILES = {"genindex.html", "search.html", "py-modindex.html", "searchindex.js", "objects.inv"}
META_SIDECAR = "_meta.yaml"


def _is_clutter(rel: Path) -> bool:
    if rel.name in _CLUTTER_FILES or rel.name == META_SIDECAR or rel.name.endswith(".meta.yaml"):
        return True
    return any(part.endswith("_files") or part in _CLUTTER_DIRS for part in rel.parts[:-1])


def sidecar_meta(root: Path, path: Path) -> dict:
    """Metadata from `_meta.yaml` files in the folders between `root` and `path` (outer first),
    then from `<file>.meta.yaml` next to the file. Lets PDFs/HTML carry version, doc_type, title...
    """
    import yaml

    meta: dict = {}
    if root.is_file():
        root = root.parent
    try:
        rel_parts = path.parent.relative_to(root).parts
    except ValueError:
        rel_parts = ()
    folders = [root] + [root.joinpath(*rel_parts[: i + 1]) for i in range(len(rel_parts))]
    for f in [d / META_SIDECAR for d in folders] + [path.with_name(path.name + ".meta.yaml")]:
        if f.is_file():
            try:
                data = yaml.safe_load(f.read_text()) or {}
            except yaml.YAMLError as e:
                log.warning("Bad sidecar %s: %s", f, e)
                continue
            if isinstance(data, dict):
                meta.update(data)
    return meta


_HOST_DIR = re.compile(r"^[a-z0-9-]+(\.[a-z0-9-]+)+(:\d+)?$", re.IGNORECASE)


def _url_from_mirror_path(rel: str) -> str | None:
    """`wget --mirror` stores https://host/a/b.html as <folder>/host/a/b.html; rebuild the URL."""
    parts = rel.split("/")
    for i, part in enumerate(parts[:-1]):
        if _HOST_DIR.match(part) and not part.lower().endswith((".html", ".htm", ".pdf")):
            return "https://" + "/".join(parts[i:])
    return None


def _versions(value) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, (list, tuple)):
        return "|".join(str(v) for v in value)
    return str(value)


def _infer_from_path(rel: str) -> tuple[str | None, str | None]:
    parts = rel.split("/")
    domain = parts[0] if len(parts) > 1 else None
    software = parts[1] if len(parts) > 2 else None
    return domain, software


def chunks_for_source(cfg: Config, src: SourceConfig) -> list[Chunk]:
    url_map: dict[str, str] = {}
    if src.path and (src.path / URL_MAP).exists():
        url_map = json.loads((src.path / URL_MAP).read_text())

    from .lessons import LESSONS_SOURCE

    all_chunks: list[Chunk] = []
    seen_content: dict[tuple, tuple] = {}
    for path, rel in iter_files(src):
        if src.name != LESSONS_SOURCE and _is_within(path, cfg.lessons_dir):
            continue  # lessons are indexed by their own source
        try:
            if path.suffix.lower() in (".html", ".htm") and path.name in url_map:
                meta, chunks = {}, chunk_html_string(
                    path.read_text(errors="replace"), rel, cfg.chunk_size, cfg.chunk_overlap
                )
            else:
                meta, chunks = chunk_file(path, rel, cfg.chunk_size, cfg.chunk_overlap)
        except Exception as e:  # noqa: BLE001 - one bad file must not stop the ingest
            log.warning("Failed to chunk %s/%s: %s", src.name, rel, e)
            continue
        if not chunks:
            if path.suffix.lower() == ".pdf":
                log.warning("[%s] %s: no extractable text - scanned PDF? Run `rag-drg check-pdf` on it.", src.name, rel)
            continue
        digest = hashlib.sha1("\n".join(c.text for c in chunks).encode()).hexdigest()
        if path.suffix.lower() == ".pdf":
            _warn_if_needs_ocr(src.name, rel, chunks)
        if src.path is not None:
            side = sidecar_meta(src.path, path)
            meta = {**side, **meta}
            if side.get("title") and path.suffix.lower() == ".pdf":
                # "gaussian_book_scan > Chapter 3 (p. 40)" -> "Exploring Chemistry ... > Chapter 3 (p. 40)"
                for c in chunks:
                    if c.title.startswith(path.stem):
                        c.title = str(side["title"]) + c.title[len(path.stem):]

        p_domain, p_software = _infer_from_path(rel) if src.type == "local" else (None, None)
        domain = meta.get("domain") or src.domain or p_domain
        software = meta.get("software") or src.software or (p_software if domain == "ess" else None)
        version = _versions(meta.get("version", src.version))
        rule_type = next(
            (r.get("doc_type") for r in src.doc_type_rules if _matches(rel, [r.get("glob", "")])), None
        )
        doc_type = meta.get("doc_type") or meta.get("type") or rule_type or src.doc_type
        if doc_type == "reference" and path.suffix.lower() in (".py", ".cc"):
            doc_type = "code"
        tags = [str(t) for t in (meta.get("tags") or [])] + list(src.tags)
        if src.type == "local" and src.domain and p_domain:
            tags.append(p_domain)  # e.g. papers/<project>/x.pdf -> tag <project>
        if software and isinstance(software, list):
            tags += [str(s) for s in software[1:]]
            software = software[0]
        url = meta.get("url") or url_map.get(path.name) or _url_from_mirror_path(rel)
        if not url and src.base_url:
            url = src.base_url.rstrip("/") + "/" + rel
        for c in chunks:
            c.source = src.name
            c.domain = domain.lower() if isinstance(domain, str) else domain
            # A chunker may already know the program per chunk (e.g. one errors.yaml entry each).
            c.software = c.software or (software.lower() if isinstance(software, str) else software)
            c.version = version
            # Manual sections the chunker recognised as method background become "theory",
            # so keyword questions and theory questions can be filtered apart.
            c.doc_type = "theory" if (c.kind == "theory" and doc_type == "reference") else doc_type
            c.tags = tags
            c.url = url
            c.status = meta.get("status")
        # The same page saved twice (browser copy + wget mirror, index.html?x, two folders):
        # keep one copy, preferring the one whose original URL is known, then the shorter path.
        # Identical text with different metadata (e.g. sidecars for two program versions) is not
        # a duplicate: both are kept so each version filter finds it.
        dkey = (digest, chunks[0].domain, chunks[0].software, version, chunks[0].doc_type)
        rank = (url is None, len(rel), rel)
        if dkey in seen_content and seen_content[dkey][0] <= rank:
            log.info("[%s] %s duplicates %s, skipped", src.name, rel, seen_content[dkey][1])
            continue
        if dkey in seen_content:
            log.info("[%s] %s duplicates %s, skipped", src.name, seen_content[dkey][1], rel)
        seen_content[dkey] = (rank, rel, chunks)
    for _, _, chunks in seen_content.values():
        all_chunks.extend(chunks)
    return all_chunks


def _warn_if_needs_ocr(source: str, rel: str, chunks: list[Chunk]) -> None:
    text = " ".join(c.text for c in chunks)
    letters = sum(ch.isalpha() for ch in text)
    visible = sum(not ch.isspace() for ch in text) or 1
    if "(cid:" in text or letters / visible < 0.6:
        log.warning("[%s] %s: extracted text looks garbled - check with `rag-drg check-pdf`; "
                    "consider `ocrmypdf --force-ocr`.", source, rel)


def embed_missing(cfg: Config, store: Store, progress: Callable[[str], None] = print) -> int:
    embedder = make_embedder(cfg.embeddings)
    if embedder is None:
        return 0
    if store.get_meta("embedding_model") != embedder.name:
        if store.get_meta("embedding_model"):
            progress(f"Embedding model changed -> re-embedding everything with {embedder.name}")
        store.clear_embeddings()
        store.set_meta("embedding_model", embedder.name)
    todo = store.chunks_missing_embeddings()
    if not todo:
        return 0
    progress(f"Embedding {len(todo)} chunks with {embedder.name} ...")
    step = 256
    for i in range(0, len(todo), step):
        batch = todo[i:i + step]
        vecs = embedder.embed([t for _, t in batch])
        store.put_embeddings([cid for cid, _ in batch], vecs)
        progress(f"  {min(i + step, len(todo))}/{len(todo)}")
    return len(todo)


def ingest(
    cfg: Config,
    only: list[str] | None = None,
    fetch: bool = False,
    embed: bool = True,
    progress: Callable[[str], None] = print,
) -> dict:
    store = Store(cfg.index_path)
    report: dict = {}
    try:
        for src in cfg.sources:
            if only and src.name not in only:
                continue
            if not src.enabled and not only:
                continue
            if fetch:
                progress(f"[{src.name}] fetching ...")
                try:
                    fetch_source(src, cfg)
                except Exception as e:  # noqa: BLE001
                    progress(f"[{src.name}] fetch failed: {e}")
            if src.path is None or not src.path.exists():
                progress(f"[{src.name}] skipped: {src.path} does not exist"
                         + (" (run `rag-drg fetch`)" if src.type != "local" else ""))
                continue
            chunks = chunks_for_source(cfg, src)
            res = store.sync_source(src.name, chunks)
            report[src.name] = res
            progress(f"[{src.name}] {len(chunks)} chunks: {res}")
        # Lessons learned are always indexed, as their own source.
        from .lessons import LESSONS_SOURCE, lessons_source

        if cfg.lessons_dir.exists() and (not only or LESSONS_SOURCE in only):
            chunks = chunks_for_source(cfg, lessons_source(cfg))
            report[LESSONS_SOURCE] = store.sync_source(LESSONS_SOURCE, chunks)
            progress(f"[{LESSONS_SOURCE}] {len(chunks)} chunks: {report[LESSONS_SOURCE]}")
        if embed:
            n = embed_missing(cfg, store, progress)
            if n:
                report["embedded"] = n
    finally:
        store.close()
    return report


def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False
