"""Sync a Zotero library into a source cache directory that `rag-drg ingest` indexes.

Layout of `sources_cache/<name>/`:

    _zotero_state.json                  library version, item metadata, downloaded files, report
    items/<ITEMKEY>/<ATTKEY>_<file>.pdf       stored attachment (PDF or HTML snapshot)
    items/<ITEMKEY>/<ATTKEY>_<file>.pdf.meta.yaml   sidecar: title, authors, year, doi, url, tags, ...
    items/<ITEMKEY>/note_<NOTEKEY>.md   child note (front matter links it to the paper)
    items/<ITEMKEY>/abstract.md         only when no file could be stored: title, authors, abstract
    notes/<NOTEKEY>.md                  standalone notes

Each sync updates the metadata in the state (incrementally for the Web API), then rebuilds the
expected set of files from the state: missing/changed files are downloaded, sidecars rewritten
when they change, and anything else under items/ and notes/ is removed (deleted, trashed or
filtered-out items).
"""

from __future__ import annotations

import io
import json
import logging
import re
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import yaml

from .settings import ZoteroError, ZoteroSettings, settings_for

log = logging.getLogger(__name__)

STATE_FILE = "_zotero_state.json"
MANAGED_DIRS = ("items", "notes")
CHILD_TYPES = ("attachment", "note")
SKIP_TYPES = ("annotation",)  # Zotero 7 PDF annotations: separate items, not indexed (yet)
LINKED = ("linked_file", "linked_url")

# Only these parts of an item's data are kept in the state file.
KEEP = (
    "key", "version", "itemType", "title", "creators", "date", "DOI", "url", "publicationTitle",
    "proceedingsTitle", "bookTitle", "websiteTitle", "university", "abstractNote", "extra", "tags",
    "collections", "parentItem", "linkMode", "contentType", "filename", "md5", "path", "note",
    "_parsedDate", "_link",
)


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #

def load_state(dest: Path) -> dict:
    f = dest / STATE_FILE
    if f.is_file():
        try:
            return json.loads(f.read_text())
        except json.JSONDecodeError:
            log.warning("Corrupt %s, doing a full sync", f)
    return {}


def save_state(dest: Path, state: dict) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    tmp = dest / (STATE_FILE + ".tmp")
    tmp.write_text(json.dumps(state, indent=1, sort_keys=True))
    tmp.replace(dest / STATE_FILE)


def _fresh(identity: str) -> dict:
    return {"identity": identity, "version": 0, "items": {}, "collections": {}, "files": {}, "report": {}}


def _keep(obj: dict) -> dict:
    """Web API object ({key, version, data, meta, links}) or local dict -> compact item record."""
    data = dict(obj.get("data") or obj)
    if obj.get("meta", {}).get("parsedDate"):
        data["_parsedDate"] = obj["meta"]["parsedDate"]
    alt = (obj.get("links") or {}).get("alternate") or {}
    if alt.get("href"):
        data["_link"] = alt["href"]
    return {k: data[k] for k in KEEP if data.get(k) not in (None, "", [])}


def apply_items(state: dict, objs: list[dict]) -> int:
    n = 0
    for obj in objs:
        data = obj.get("data") or obj
        key = data.get("key") or obj.get("key")
        if not key or data.get("itemType") in SKIP_TYPES:
            continue
        n += 1
        if data.get("deleted"):  # in the trash
            state["items"].pop(key, None)
        else:
            state["items"][key] = _keep(obj)
    return n


def set_collections(state: dict, objs: list[dict]) -> None:
    state["collections"] = {
        (o.get("data") or o)["key"]: {"name": (o.get("data") or o).get("name", ""),
                                     "parent": (o.get("data") or o).get("parentCollection") or None}
        for o in objs if not (o.get("data") or o).get("deleted")
    }


# --------------------------------------------------------------------------- #
# Metadata helpers
# --------------------------------------------------------------------------- #

_YEAR = re.compile(r"\b(1[6-9]\d\d|20\d\d)\b")
_DOI = re.compile(r"\b(10\.\d{4,9}/\S+)", re.IGNORECASE)


def slug(text: str) -> str:
    """Index tags are space-separated, so 'VAE ESS NN' -> 'vae-ess-nn'."""
    return re.sub(r"[^\w.+-]+", "-", str(text).strip().lower(), flags=re.UNICODE).strip("-")


def _safe_name(name: str, limit: int = 80) -> str:
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._")[:limit] or "file"
    return f"{stem}.{ext.lower()}" if ext else stem


def authors(item: dict) -> list[str]:
    creators = item.get("creators") or []
    main = [c for c in creators if c.get("creatorType") in (None, "author", "inventor", "programmer")]
    out = []
    for c in main or creators:
        if c.get("name"):
            out.append(c["name"])
        else:
            out.append(" ".join(p for p in (c.get("firstName"), c.get("lastName")) if p))
    return [a for a in out if a]


def year(item: dict) -> int | None:
    m = _YEAR.search(str(item.get("_parsedDate") or item.get("date") or ""))
    return int(m.group(1)) if m else None


def doi(item: dict) -> str | None:
    if item.get("DOI"):
        return str(item["DOI"]).strip().removeprefix("https://doi.org/")
    m = re.search(r"^DOI:\s*(\S+)", str(item.get("extra") or ""), re.IGNORECASE | re.MULTILINE)
    if m:
        return m.group(1)
    return None


def publication(item: dict) -> str | None:
    for k in ("publicationTitle", "proceedingsTitle", "bookTitle", "websiteTitle", "university"):
        if item.get(k):
            return item[k]
    return None


def _item_url(item: dict, s: ZoteroSettings) -> str | None:
    d = doi(item)
    if d:
        return f"https://doi.org/{d}"
    if item.get("url"):
        return item["url"]
    if item.get("_link"):
        return item["_link"]
    if s.mode == "local":
        return f"zotero://select/library/items/{item['key']}"
    return None


def _html_to_md(html: str) -> str:
    from rag_drg.chunking import parse_html

    return parse_html(html or "")[1]


class Collections:
    def __init__(self, colls: dict[str, dict], wanted: list[str] | None):
        self.colls = colls
        self.missing: list[str] = []
        self.include: set[str] | None = None
        if wanted:
            by_name = {c["name"].lower(): k for k, c in colls.items()}
            roots = set()
            for w in wanted:
                k = w if w in colls else by_name.get(w.lower())
                if k:
                    roots.add(k)
                else:
                    self.missing.append(w)
            self.include = set(roots)
            changed = True
            while changed:  # sub-collections belong to their project too
                changed = False
                for k, c in colls.items():
                    if k not in self.include and c.get("parent") in self.include:
                        self.include.add(k)
                        changed = True

    def ancestry(self, key: str) -> list[str]:
        out, seen = [], set()
        while key in self.colls and key not in seen:
            seen.add(key)
            out.append(self.colls[key]["name"])
            key = self.colls[key].get("parent")
        return out

    def names(self, keys: list[str]) -> list[str]:
        out: list[str] = []
        for k in keys or []:
            for n in self.ancestry(k):
                if n not in out:
                    out.append(n)
        return out

    def selected(self, keys: list[str]) -> bool:
        return self.include is None or bool(self.include & set(keys or []))


def paper_meta(item: dict, s: ZoteroSettings, colls: Collections) -> dict:
    names = colls.names(item.get("collections") or [])
    tags = [slug(t["tag"]) for t in item.get("tags") or [] if t.get("tag")]
    if s.collection_tags:
        tags += [slug(n) for n in names]
    tags = list(dict.fromkeys(t for t in tags if t))
    meta = {
        "title": item.get("title") or item.get("filename") or item["key"],
        "authors": authors(item),
        "year": year(item),
        "doi": doi(item),
        "url": _item_url(item, s),
        "publication": publication(item),
        "item_type": item.get("itemType"),
        "collections": names,
        "tags": tags,
        "domain": s.domain,
        "doc_type": s.doc_type,
        "zotero_key": item["key"],
    }
    return {k: v for k, v in meta.items() if v not in (None, "", [])}


def _front_matter(meta: dict, body: str) -> str:
    return "---\n" + yaml.safe_dump(meta, sort_keys=False, allow_unicode=True) + "---\n" + body.rstrip() + "\n"


def _byline(meta: dict) -> str:
    parts = []
    if meta.get("authors"):
        a = meta["authors"]
        parts.append(", ".join(a[:6]) + (" et al." if len(a) > 6 else ""))
    if meta.get("year"):
        parts.append(f"({meta['year']})")
    if meta.get("publication"):
        parts.append(f"*{meta['publication']}*")
    if meta.get("doi"):
        parts.append(f"doi:{meta['doi']}")
    return " ".join(parts)


# --------------------------------------------------------------------------- #
# Render: state -> files
# --------------------------------------------------------------------------- #

def _write(path: Path, data: bytes | str) -> bool:
    raw = data.encode("utf-8") if isinstance(data, str) else data
    if path.is_file() and path.read_bytes() == raw:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    tmp.write_bytes(raw)
    tmp.replace(path)
    return True


def _unzip_snapshot(data: bytes, att: dict) -> bytes:
    """HTML snapshots may come as a zip of the page + assets; keep the main HTML file."""
    if not data.startswith(b"PK\x03\x04"):
        return data
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = z.namelist()
        want = att.get("filename")
        pick = want if want in names else next((n for n in names if n.lower().endswith((".html", ".htm"))), None)
        if not pick:
            raise ZoteroError("zipped snapshot without an HTML file")
        return z.read(pick)


def _wanted_file(att: dict, s: ZoteroSettings) -> bool:
    ctype = (att.get("contentType") or "").lower()
    if ctype in s.file_types:
        return True
    fname = (att.get("filename") or "").lower()
    return not ctype and (fname.endswith(".pdf") and "application/pdf" in s.file_types)


def _att_rel(item_key: str, att: dict) -> str:
    ctype = (att.get("contentType") or "").lower()
    name = _safe_name(att.get("filename") or att.get("title") or "file")
    ext = ".pdf" if "pdf" in ctype else ".html" if "html" in ctype else ""
    if ext and not name.lower().endswith(ext) and not (ext == ".html" and name.lower().endswith(".htm")):
        name += ext
    return f"items/{item_key}/{att['key']}_{name}"


def render(dest: Path, state: dict, s: ZoteroSettings,
           fetch: Callable[[dict], bytes], signature: Callable[[dict], str | None],
           progress: Callable[[str], None] = log.info) -> dict:
    items: dict = state["items"]
    colls = Collections(state.get("collections") or {}, s.collections)
    files: dict = state.setdefault("files", {})
    expected: set[str] = set()
    report = {"linked": [], "failed": {}, "counts": {}, "unknown_collections": colls.missing}
    counts = dict(items=0, files=0, downloaded=0, notes=0, abstracts=0, filtered_out=0, other_attachments=0)
    children: dict[str, list[dict]] = {}
    for it in items.values():
        if it.get("itemType") in CHILD_TYPES and it.get("parentItem"):
            children.setdefault(it["parentItem"], []).append(it)

    def note_md(note: dict, parent_meta: dict | None, rel: str) -> None:
        body = _html_to_md(note.get("note", ""))
        first = next((ln.strip("# ").strip() for ln in body.splitlines() if ln.strip()), "") or "Note"
        meta = {"title": f"Note: {first[:100]}" + (f" (on: {parent_meta['title']})" if parent_meta else ""),
                "domain": s.domain, "doc_type": s.doc_type, "zotero_key": note["key"]}
        tags = [slug(t["tag"]) for t in note.get("tags") or [] if t.get("tag")]
        if parent_meta:
            meta.update(zotero_parent=parent_meta["zotero_key"], parent_title=parent_meta["title"])
            for k in ("authors", "year", "doi", "url", "collections"):
                if parent_meta.get(k):
                    meta[k] = parent_meta[k]
            tags = list(parent_meta.get("tags") or []) + tags
            header = f"Note on **{parent_meta['title']}** {_byline(parent_meta)}\n\n"
        else:
            names = colls.names(note.get("collections") or [])
            if s.collection_tags:
                tags += [slug(n) for n in names]
            header = ""
        meta["tags"] = list(dict.fromkeys(tags + ["zotero-note"]))
        _write(dest / rel, _front_matter(meta, header + body))
        expected.add(rel)
        counts["notes"] += 1

    for key in sorted(items):
        it = items[key]
        itype = it.get("itemType")
        if itype in CHILD_TYPES and it.get("parentItem"):
            continue  # handled with its parent
        if not colls.selected(it.get("collections") or []):
            counts["filtered_out"] += 1
            continue
        if itype == "note":
            if s.include_notes:
                note_md(it, None, f"notes/{key}.md")
            continue
        counts["items"] += 1
        meta = paper_meta(it, s, colls)
        kids = sorted(children.get(key, []), key=lambda c: c["key"])
        atts = [c for c in kids if c.get("itemType") == "attachment"]
        if itype == "attachment":  # a standalone file is its own paper
            atts = [it]
        have_file = False
        for att in atts:
            akey = att["key"]
            lm = att.get("linkMode") or ""
            if not _wanted_file(att, s) and lm != "linked_url":
                counts["other_attachments"] += 1
                continue
            if lm == "linked_url" or (lm == "linked_file" and s.mode == "web"):
                report["linked"].append({"item": key, "title": meta["title"], "attachment": akey,
                                         "link_mode": lm, "target": att.get("url") or att.get("path")
                                         or att.get("filename") or ""})
                continue
            rel = _att_rel(key, att)
            sig = signature(att)
            prev = files.get(akey) or {}
            path = dest / rel
            if prev.get("path") and prev["path"] != rel and (dest / prev["path"]).is_file() \
                    and prev.get("sig") == sig and not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                (dest / prev["path"]).replace(path)  # renamed in Zotero; no need to download again
            if not path.is_file() or prev.get("sig") != sig or prev.get("path") != rel:
                try:
                    data = fetch(att)
                    if "html" in (att.get("contentType") or ""):
                        data = _unzip_snapshot(data, att)
                    _write(path, data)
                    files[akey] = {"path": rel, "sig": sig}
                    counts["downloaded"] += 1
                    progress(f"  downloaded {rel}")
                except Exception as e:  # noqa: BLE001 - one file must not stop the sync
                    report["failed"][akey] = {"item": key, "title": meta["title"], "error": str(e)}
                    if lm == "linked_file":
                        report["linked"].append({"item": key, "title": meta["title"], "attachment": akey,
                                                 "link_mode": lm, "target": att.get("path") or ""})
                    if not path.is_file():
                        continue
            side_meta = dict(meta)
            side_meta["zotero_attachment"] = akey
            if att is not it and att.get("title") and att["title"] not in ("Full Text PDF", "PDF", "Snapshot"):
                side_meta["attachment_title"] = att["title"]
            _write(dest / (rel + ".meta.yaml"), yaml.safe_dump(side_meta, sort_keys=False, allow_unicode=True))
            expected.update({rel, rel + ".meta.yaml"})
            counts["files"] += 1
            have_file = True
        if s.include_notes:
            for note in (c for c in kids if c.get("itemType") == "note"):
                note_md(note, meta, f"items/{key}/note_{note['key']}.md")
        if not have_file and itype != "attachment":
            body = f"# {meta['title']}\n\n{_byline(meta)}\n"
            if it.get("abstractNote"):
                body += f"\n## Abstract\n\n{it['abstractNote'].strip()}\n"
            if meta.get("url"):
                body += f"\n<{meta['url']}>\n"
            _write(dest / f"items/{key}/abstract.md", _front_matter(meta, body))
            expected.add(f"items/{key}/abstract.md")
            counts["abstracts"] += 1

    # Remove everything that is no longer expected (deleted/trashed/filtered items, old names).
    removed = 0
    for d in MANAGED_DIRS:
        root = dest / d
        if not root.exists():
            continue
        for p in sorted(root.rglob("*"), reverse=True):
            rel = p.relative_to(dest).as_posix()
            if p.is_file() and rel not in expected:
                p.unlink()
                removed += 1
            elif p.is_dir() and not any(p.iterdir()):
                p.rmdir()
    for akey in [k for k, v in files.items() if v.get("path") not in expected]:
        files.pop(akey)
    counts["removed"] = removed
    counts["linked"] = len(report["linked"])
    counts["failed"] = len(report["failed"])
    report["counts"] = counts
    return report


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

def sync_source(src, cfg, full: bool = False, progress: Callable[[str], None] = log.info) -> dict:
    """Sync one `type: zotero` source into `src.path`. Returns the report."""
    if src.path is None:
        raise ZoteroError(f"[{src.name}] has no cache path")
    s = settings_for(src, cfg)
    dest = Path(src.path)
    old = load_state(dest)
    same_library = old.get("identity") == s.identity
    state = old if (same_library and not full) else _fresh(s.identity)
    if full and same_library:
        # A full re-sync re-reads all metadata; files whose checksum is unchanged are kept.
        state["files"] = old.get("files", {})

    if s.mode == "local":
        from . import local

        objs, collections = local.read_library(s)
        state["items"] = {}
        apply_items(state, objs)
        set_collections(state, collections)
        max_bytes = int(s.max_file_mb * 1_000_000)
        n_changed = len(state["items"])
        progress(f"[{src.name}] read {n_changed} items from {s.data_dir / 'zotero.sqlite'}")
        report = render(dest, state, s, fetch=lambda a: local.read_file(s, a, max_bytes),
                        signature=lambda a: local.file_signature(s, a), progress=progress)
    else:
        from .webapi import ZoteroWebAPI

        api = ZoteroWebAPI(s.api_base, s.library_prefix, s.api_key(), timeout=s.timeout, page_size=s.page_size)
        since = int(state.get("version") or 0)
        set_collections(state, api.collections())
        version, objs = api.items_since(since)
        n_changed = apply_items(state, objs)
        n_deleted = 0
        if since:
            deleted = api.deleted_since(since)
            for key in deleted.get("items") or []:
                n_deleted += state["items"].pop(key, None) is not None
        progress(f"[{src.name}] library version {since} -> {version}: {n_changed} changed, {n_deleted} deleted")
        max_bytes = int(s.max_file_mb * 1_000_000)
        report = render(dest, state, s, fetch=lambda a: api.download(a["key"], max_bytes),
                        signature=lambda a: a.get("md5") or f"v{a.get('version', 0)}", progress=progress)
        state["version"] = version
        report["counts"]["changed"] = n_changed
        report["counts"]["deleted"] = n_deleted
    state["last_sync"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    state["report"] = report
    save_state(dest, state)
    c = report["counts"]
    progress(f"[{src.name}] {c['items']} items: {c['files']} files ({c['downloaded']} new), "
             f"{c['notes']} notes, {c['abstracts']} abstract-only, {c['linked']} linked (not downloadable), "
             f"{c['failed']} failed, {c['removed']} removed")
    return report
