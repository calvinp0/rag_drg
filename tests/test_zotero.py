"""Zotero source: fake Web API v3 server + a tiny fake zotero.sqlite."""

import io
import json
import sqlite3
import textwrap
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
import yaml

from rag_drg.config import load_config
from rag_drg.ingest import FETCHERS, chunks_for_source, fetch_source, ingest, iter_files
from rag_drg.search import Searcher
from rag_drg.tools import zotero as zplugin
from rag_drg.tools._zotero.settings import ZoteroError
from rag_drg.tools._zotero.sync import STATE_FILE, load_state, sync_source

from .pdfutil import raw_pdf

KEY = "sekret-test-key"
GROUP = "4242"


def quiet(*_):
    pass


def snapshot_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("page.html", "<html><title>Blog post</title><body><h1>SMILES grammar</h1>"
                                "<p>Grammar VAEs decode valid SMILES strings.</p></body></html>")
        z.writestr("style.css", "body{}")
    return buf.getvalue()


def item(key, version, **data):
    d = {"key": key, "version": version, **data}
    return {"key": key, "version": version, "library": {"type": "group", "id": int(GROUP)},
            "links": {"alternate": {"href": f"https://www.zotero.org/groups/{GROUP}/items/{key}"}},
            "meta": {}, "data": d}


class FakeZotero:
    """Just enough of api.zotero.org for the sync: items/collections/deleted/file."""

    def __init__(self):
        self.version = 1
        self.items: dict[str, dict] = {}
        self.deleted: dict[str, int] = {}  # key -> library version at which it was deleted
        self.files: dict[str, bytes] = {}
        self.collections = [
            {"key": "C1", "version": 1, "data": {"key": "C1", "name": "VAE ESS NN", "parentCollection": False}},
            {"key": "C2", "version": 1, "data": {"key": "C2", "name": "Datasets", "parentCollection": "C1"}},
            {"key": "C3", "version": 1, "data": {"key": "C3", "name": "Other", "parentCollection": False}},
        ]
        self.log: list[tuple[str, dict, dict]] = []
        self.storage_saw_key = False

    def put(self, obj):
        self.items[obj["key"]] = obj

    def seed(self):
        self.put(item("P1", 1, itemType="journalArticle", title="Automatic chemical design using a VAE",
                      creators=[{"creatorType": "author", "firstName": "Rafael", "lastName": "Gomez-Bombarelli"},
                                {"creatorType": "author", "firstName": "Alan", "lastName": "Aspuru-Guzik"}],
                      date="2018-02-28", DOI="10.1021/acscentsci.7b00572", publicationTitle="ACS Cent. Sci.",
                      abstractNote="A VAE maps molecules to a continuous latent space.",
                      tags=[{"tag": "generative models"}], collections=["C2"]))
        self.items["P1"]["meta"]["parsedDate"] = "2018-02-28"
        self.put(item("A1", 1, itemType="attachment", parentItem="P1", linkMode="imported_file",
                      contentType="application/pdf", filename="gomez 2018.pdf", title="Full Text PDF",
                      md5="md5-a1", tags=[], collections=[]))
        self.files["A1"] = raw_pdf([["Latent space optimization of molecules",
                                     "The encoder maps SMILES to a 196 dimensional latent vector."]])
        self.put(item("N1", 1, itemType="note", parentItem="P1", tags=[], collections=[],
                      note="<div><h1>Takeaways for our pipeline</h1><p>Use a 196-dim latent space; "
                           "property predictor jointly trained.</p></div>"))
        self.put(item("P2", 1, itemType="journalArticle", title="Delta learning of DFT energies",
                      creators=[{"creatorType": "author", "name": "Ramakrishnan"}], date="2015",
                      abstractNote="Delta-ML corrects B3LYP energies to CCSD(T) quality.",
                      tags=[], collections=["C1"]))
        self.put(item("P3", 1, itemType="conferencePaper", title="Unrelated workshop paper",
                      creators=[], tags=[], collections=["C3"], url="https://example.org/p3"))
        self.put(item("A3", 1, itemType="attachment", parentItem="P3", linkMode="linked_file",
                      contentType="application/pdf", path="/home/someone/p3.pdf", tags=[], collections=[]))
        self.put(item("A4", 1, itemType="attachment", parentItem="P3", linkMode="imported_url",
                      contentType="text/html", filename="page.html", title="Snapshot",
                      url="https://example.org/blog", md5="md5-a4", tags=[], collections=[]))
        self.files["A4"] = snapshot_zip()
        self.put(item("T1", 1, itemType="journalArticle", title="Trashed paper", deleted=1,
                      tags=[], collections=["C1"]))
        self.put(item("X1", 1, itemType="annotation", parentItem="A1", annotationText="hi"))

    def serve(self):
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body=b"", headers=None):
                self.send_response(code)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                u = urlsplit(self.path)
                q = {k: v[0] for k, v in parse_qs(u.query).items()}
                fake.log.append((u.path, q, dict(self.headers)))
                if u.path.startswith("/s3/"):
                    fake.storage_saw_key |= "Zotero-API-Key" in self.headers
                    return self._send(200, fake.files[u.path[4:]])
                prefix = f"/groups/{GROUP}"
                if not u.path.startswith(prefix):
                    return self._send(404)
                if self.headers.get("Zotero-API-Key") != KEY:
                    return self._send(403, b"Forbidden")
                path = u.path[len(prefix):]
                hdr = {"Last-Modified-Version": str(fake.version), "Content-Type": "application/json"}
                if path == "/collections":
                    return self._page(fake.collections, q, hdr)
                if path == "/items":
                    since = int(q.get("since", 0))
                    objs = [o for o in fake.items.values() if o["version"] > since]
                    if q.get("includeTrashed") != "1":
                        objs = [o for o in objs if not o["data"].get("deleted")]
                    return self._page(sorted(objs, key=lambda o: o["key"]), q, hdr)
                if path == "/deleted":
                    since = int(q["since"])
                    keys = [k for k, v in fake.deleted.items() if v > since]
                    return self._send(200, json.dumps({"items": keys, "collections": []}).encode(), hdr)
                if path.startswith("/items/") and path.endswith("/file"):
                    k = path.split("/")[2]
                    if k not in fake.files:
                        return self._send(404)
                    # Zotero redirects to its storage host; use a different host name.
                    return self._send(302, headers={"Location": f"http://localhost:{self.server.server_port}/s3/{k}"})
                return self._send(404)

            def _page(self, objs, q, hdr):
                start, limit = int(q.get("start", 0)), int(q.get("limit", 100))
                body = json.dumps(objs[start:start + limit]).encode()
                return self._send(200, body, {**hdr, "Total-Results": str(len(objs))})

        srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return srv


@pytest.fixture
def zot(tmp_path, monkeypatch):
    fake = FakeZotero()
    fake.seed()
    srv = fake.serve()
    monkeypatch.setenv("TEST_ZOTERO_KEY", KEY)

    def make_cfg(**zextra):
        z = {"library_type": "group", "library_id": GROUP, "api_key_env": "TEST_ZOTERO_KEY",
             "api_base": f"http://127.0.0.1:{srv.server_port}", "page_size": 2, **zextra}
        conf = {"index_path": "index/test.sqlite", "lessons_dir": "knowledge/lessons", "chunk_size": 800,
                "sources": [{"name": "zot", "type": "zotero", "domain": "literature", "doc_type": "paper",
                             "zotero": z}]}
        (tmp_path / "rag_drg.yaml").write_text(yaml.safe_dump(conf))
        return load_config(tmp_path / "rag_drg.yaml")

    yield fake, make_cfg
    srv.shutdown()


def read_side(p: Path) -> dict:
    return yaml.safe_load(p.with_name(p.name + ".meta.yaml").read_text())


def test_web_sync_files_sidecars_notes_and_linked(zot):
    fake, make_cfg = zot
    cfg = make_cfg()
    src = cfg.source("zot")
    report = sync_source(src, cfg, progress=quiet)
    root = src.path

    pdf = root / "items/P1/A1_gomez_2018.pdf"
    assert pdf.read_bytes().startswith(b"%PDF")
    side = read_side(pdf)
    assert side["title"] == "Automatic chemical design using a VAE"
    assert side["authors"] == ["Rafael Gomez-Bombarelli", "Alan Aspuru-Guzik"]
    assert side["year"] == 2018 and side["doi"] == "10.1021/acscentsci.7b00572"
    assert side["url"] == "https://doi.org/10.1021/acscentsci.7b00572"
    assert side["domain"] == "literature" and side["doc_type"] == "paper"
    # Zotero tags + collection and its parent collection, as index-friendly tags.
    assert side["tags"] == ["generative-models", "datasets", "vae-ess-nn"]

    note = (root / "items/P1/note_N1.md").read_text()
    fm = yaml.safe_load(note.split("---")[1])
    assert fm["zotero_parent"] == "P1" and "zotero-note" in fm["tags"] and "vae-ess-nn" in fm["tags"]
    assert fm["title"].startswith("Note: Takeaways for our pipeline")
    assert "196-dim latent space" in note

    # No attachment -> abstract card; zipped HTML snapshot unpacked; linked file reported.
    abstract = (root / "items/P2/abstract.md").read_text()
    assert "Delta-ML corrects B3LYP" in abstract and "Ramakrishnan" in abstract
    html = root / "items/P3/A4_page.html"
    assert b"Grammar VAEs" in html.read_bytes()
    assert not (root / "items/P3/abstract.md").exists()
    assert [x["attachment"] for x in report["linked"]] == ["A3"]
    # Trashed items and annotations are skipped.
    assert not (root / "items/T1").exists() and not (root / "items/X1").exists()

    # Pagination: page_size 2 means several /items requests; the key never reaches storage or disk.
    assert sum(1 for p, _, _ in fake.log if p.endswith("/items")) >= 4
    assert all(q.get("includeTrashed") == "1" for p, q, _ in fake.log if p.endswith("/items"))
    assert not fake.storage_saw_key
    for f in root.rglob("*"):
        if f.is_file():
            assert KEY.encode() not in f.read_bytes()
    state = load_state(root)
    assert state["version"] == 1 and report["counts"]["failed"] == 0


def test_incremental_sync_and_deletion(zot):
    fake, make_cfg = zot
    cfg = make_cfg()
    src = cfg.source("zot")
    sync_source(src, cfg, progress=quiet)
    downloads_before = sum(1 for p, _, _ in fake.log if p.startswith("/s3/"))

    # Library changes: P2 edited + a new note, N1 and P3 (with its children) deleted, P1 trashed? no.
    fake.version = 3
    fake.items["P2"]["version"] = 2
    fake.items["P2"]["data"]["abstractNote"] = "Updated abstract about Delta-ML."
    fake.put(item("N2", 3, itemType="note", parentItem="P2", tags=[{"tag": "todo"}], collections=[],
                  note="<p>Reproduce with ORCA wB97X-D.</p>"))
    for k in ("N1", "P3", "A3", "A4"):
        fake.items.pop(k)
        fake.deleted[k] = 3
    fake.log.clear()
    report = sync_source(src, cfg, progress=quiet)

    item_reqs = [q for p, q, _ in fake.log if p.endswith("/items")]
    assert item_reqs and all(q["since"] == "1" for q in item_reqs)
    assert any(p.endswith("/deleted") and q["since"] == "1" for p, q, _ in fake.log)
    assert report["counts"]["changed"] == 2 and report["counts"]["deleted"] == 4
    root = src.path
    assert not (root / "items/P1/note_N1.md").exists()
    assert not (root / "items/P3").exists()
    assert "Updated abstract" in (root / "items/P2/abstract.md").read_text()
    assert "wB97X-D" in (root / "items/P2/note_N2.md").read_text()
    # The unchanged PDF is not downloaded again.
    assert (root / "items/P1/A1_gomez_2018.pdf").exists()
    assert not [p for p, _, _ in fake.log if p.startswith("/s3/")] and downloads_before == 2
    assert load_state(root)["version"] == 3

    # Moving an item to the trash removes it too.
    fake.version = 4
    fake.items["P2"]["version"] = 4
    fake.items["P2"]["data"]["deleted"] = 1
    sync_source(src, cfg, progress=quiet)
    assert not (root / "items/P2").exists()


def test_renamed_attachment_is_moved_not_downloaded_again(zot):
    fake, make_cfg = zot
    cfg = make_cfg()
    src = cfg.source("zot")
    sync_source(src, cfg, progress=quiet)
    root = src.path
    assert (root / "items/P1/A1_gomez_2018.pdf").is_file()

    # Renamed in Zotero (same file, same md5): only the metadata version changes.
    fake.version = 5
    fake.items["A1"]["version"] = 5
    fake.items["A1"]["data"]["filename"] = "gomez-bombarelli 2018.pdf"
    fake.log.clear()
    sync_source(src, cfg, progress=quiet)
    assert (root / "items/P1/A1_gomez-bombarelli_2018.pdf").is_file()
    assert not (root / "items/P1/A1_gomez_2018.pdf").exists()
    assert not [p for p, _, _ in fake.log if p.startswith("/s3/")]
    assert load_state(root)["files"]["A1"]["path"] == "items/P1/A1_gomez-bombarelli_2018.pdf"


def test_collection_filter(zot):
    _, make_cfg = zot
    cfg = make_cfg(collections=["vae ess nn"], collection_tags=False)
    src = cfg.source("zot")
    report = sync_source(src, cfg, progress=quiet)
    root = src.path
    assert (root / "items/P1").exists() and (root / "items/P2").exists()  # C2 is inside C1
    assert not (root / "items/P3").exists()
    assert report["counts"]["filtered_out"] == 1
    assert read_side(root / "items/P1/A1_gomez_2018.pdf")["tags"] == ["generative-models"]


def test_missing_api_key(zot, monkeypatch):
    _, make_cfg = zot
    cfg = make_cfg()
    monkeypatch.delenv("TEST_ZOTERO_KEY")
    with pytest.raises(ZoteroError, match="TEST_ZOTERO_KEY is not set"):
        sync_source(cfg.source("zot"), cfg, progress=quiet)
    assert not (cfg.source("zot").path / STATE_FILE).exists()


def test_wrong_key_is_a_clear_error(zot, monkeypatch):
    _, make_cfg = zot
    cfg = make_cfg()
    monkeypatch.setenv("TEST_ZOTERO_KEY", "bad-key-XYZ123")
    with pytest.raises(ZoteroError, match="403") as e:
        sync_source(cfg.source("zot"), cfg, progress=quiet)
    assert "XYZ123" not in str(e.value)


def test_fetch_hook_and_ingest_metadata(zot):
    pytest.importorskip("pypdf")  # the synced PDF is indexed through the optional `pdf` extra
    _, make_cfg = zot
    cfg = make_cfg()
    assert FETCHERS["zotero"] is zplugin._fetch
    src = cfg.source("zot")
    fetch_source(src, cfg)
    rels = [rel for _, rel in iter_files(src)]
    assert STATE_FILE not in rels and not any(r.endswith(".meta.yaml") for r in rels)

    chunks = chunks_for_source(cfg, src)
    pdf = [c for c in chunks if "Latent space optimization" in c.text]
    assert pdf and pdf[0].title.startswith("Automatic chemical design using a VAE")
    assert pdf[0].url == "https://doi.org/10.1021/acscentsci.7b00572"
    assert pdf[0].domain == "literature" and pdf[0].doc_type == "paper"
    assert "vae-ess-nn" in pdf[0].tags
    note = [c for c in chunks if "property predictor jointly trained" in c.text]
    assert note and "zotero-note" in note[0].tags and note[0].url == pdf[0].url

    report = ingest(cfg, fetch=True, embed=False, progress=quiet)
    assert report["zot"]["added"] > 0
    hits = Searcher(cfg).search("196 dimensional latent vector", domain="literature")
    assert hits and hits[0].chunk.source == "zot" and hits[0].chunk.doc_type == "paper"


def test_fetch_source_without_cfg_uses_default_config(zot, monkeypatch):
    # `rag-drg fetch` currently calls fetch_source(src) without the config.
    _, make_cfg = zot
    cfg = make_cfg()
    monkeypatch.setenv("RAG_DRG_CONFIG", str(cfg.root / "rag_drg.yaml"))
    fetch_source(cfg.source("zot"))
    assert (cfg.source("zot").path / STATE_FILE).exists()


def test_cli_sync_and_status(zot, capsys):
    from rag_drg.cli import main

    _, make_cfg = zot
    cfg = make_cfg()
    conf = str(cfg.root / "rag_drg.yaml")
    assert main(["--config", conf, "zotero", "sync", "--source", "zot"]) == 0
    capsys.readouterr()
    assert main(["--config", conf, "zotero", "status", "--json"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status[0]["library_version"] == 1 and status[0]["counts"]["files"] == 2
    assert status[0]["linked"][0]["attachment"] == "A3"


def test_lint_flags_bad_config(tmp_path):
    (tmp_path / "rag_drg.yaml").write_text(textwrap.dedent("""\
        sources:
          - name: z
            type: zotero
            zotero: {library_type: group, library_id: "abc", api_key: "oops"}
          - name: zoff
            type: zotero
            enabled: false
            zotero: {library_id: null}
        """))
    problems = zplugin.lint(load_config(tmp_path / "rag_drg.yaml"))
    assert any("library_id" in p for p in problems)
    assert any("api_key must not" in p for p in problems)
    assert not any(p.startswith("source zoff") for p in problems)


# --------------------------------------------------------------------------- #
# Local mode
# --------------------------------------------------------------------------- #

LOCAL_SCHEMA = """
CREATE TABLE libraries (libraryID INTEGER PRIMARY KEY, type TEXT);
CREATE TABLE groups (groupID INTEGER PRIMARY KEY, libraryID INTEGER);
CREATE TABLE itemTypes (itemTypeID INTEGER PRIMARY KEY, typeName TEXT);
CREATE TABLE items (itemID INTEGER PRIMARY KEY, itemTypeID INT, libraryID INT, key TEXT, version INT);
CREATE TABLE fields (fieldID INTEGER PRIMARY KEY, fieldName TEXT);
CREATE TABLE itemDataValues (valueID INTEGER PRIMARY KEY, value);
CREATE TABLE itemData (itemID INT, fieldID INT, valueID INT);
CREATE TABLE creators (creatorID INTEGER PRIMARY KEY, firstName TEXT, lastName TEXT, fieldMode INT);
CREATE TABLE creatorTypes (creatorTypeID INTEGER PRIMARY KEY, creatorType TEXT);
CREATE TABLE itemCreators (itemID INT, creatorID INT, creatorTypeID INT, orderIndex INT);
CREATE TABLE tags (tagID INTEGER PRIMARY KEY, name TEXT);
CREATE TABLE itemTags (itemID INT, tagID INT, type INT);
CREATE TABLE collections (collectionID INTEGER PRIMARY KEY, collectionName TEXT, parentCollectionID INT,
                          libraryID INT, key TEXT);
CREATE TABLE collectionItems (collectionID INT, itemID INT);
CREATE TABLE itemAttachments (itemID INTEGER PRIMARY KEY, parentItemID INT, linkMode INT, contentType TEXT, path TEXT);
CREATE TABLE itemNotes (itemID INTEGER PRIMARY KEY, parentItemID INT, note TEXT, title TEXT);
CREATE TABLE deletedItems (itemID INTEGER PRIMARY KEY);
"""


def make_local_library(data_dir: Path) -> None:
    data_dir.mkdir(parents=True)
    conn = sqlite3.connect(data_dir / "zotero.sqlite")
    conn.executescript(LOCAL_SCHEMA)
    x = conn.execute
    x("INSERT INTO libraries VALUES (1, 'user')")
    for i, t in enumerate(["journalArticle", "attachment", "note"], 1):
        x("INSERT INTO itemTypes VALUES (?, ?)", (i, t))
    for i, f in enumerate(["title", "DOI", "date", "abstractNote", "publicationTitle"], 1):
        x("INSERT INTO fields VALUES (?, ?)", (i, f))
    x("INSERT INTO items VALUES (1, 1, 1, 'PAPERKEY', 5)")
    x("INSERT INTO items VALUES (2, 2, 1, 'PDFKEY01', 5)")
    x("INSERT INTO items VALUES (3, 3, 1, 'NOTEKEY1', 5)")
    x("INSERT INTO items VALUES (4, 1, 1, 'TRASHED1', 5)")
    x("INSERT INTO items VALUES (5, 2, 1, 'LINKED01', 5)")
    vals = [(1, 1, "SchNet: a continuous-filter network"), (1, 2, "10.5555/schnet"),
            (1, 3, "2017-06-01 2017-06-01"), (1, 4, "Message passing for energies."),
            (4, 1, "Old trashed paper")]
    for n, (item_id, field_id, value) in enumerate(vals, 1):
        x("INSERT INTO itemDataValues VALUES (?, ?)", (n, value))
        x("INSERT INTO itemData VALUES (?, ?, ?)", (item_id, field_id, n))
    x("INSERT INTO creatorTypes VALUES (1, 'author')")
    x("INSERT INTO creators VALUES (1, 'Kristof', 'Schutt', 0)")
    x("INSERT INTO itemCreators VALUES (1, 1, 1, 0)")
    x("INSERT INTO tags VALUES (1, 'neural networks')")
    x("INSERT INTO itemTags VALUES (1, 1, 0)")
    x("INSERT INTO collections VALUES (1, 'VAE ESS NN', NULL, 1, 'COLL0001')")
    x("INSERT INTO collectionItems VALUES (1, 1)")
    x("INSERT INTO itemAttachments VALUES (2, 1, 0, 'application/pdf', 'storage:schnet.pdf')")
    x("INSERT INTO itemAttachments VALUES (5, 1, 2, 'application/pdf', '/nonexistent/other.pdf')")
    x("INSERT INTO itemNotes VALUES (3, 1, '<p>SchNet cutoff 5 A works for our set.</p>', 'SchNet cutoff')")
    x("INSERT INTO deletedItems VALUES (4)")
    conn.commit()
    conn.close()
    (data_dir / "storage" / "PDFKEY01").mkdir(parents=True)
    (data_dir / "storage" / "PDFKEY01" / "schnet.pdf").write_bytes(
        raw_pdf([["Continuous-filter convolutional layers", "Interaction blocks update atom features."]]))


def test_local_mode(tmp_path):
    data_dir = tmp_path / "Zotero"
    make_local_library(data_dir)
    (tmp_path / "rag_drg.yaml").write_text(yaml.safe_dump({
        "index_path": "index/t.sqlite", "lessons_dir": "knowledge/lessons",
        "sources": [{"name": "mine", "type": "zotero", "zotero": {"mode": "local", "library_type": "user",
                                                                   "data_dir": str(data_dir)}}]}))
    cfg = load_config(tmp_path / "rag_drg.yaml")
    src = cfg.source("mine")
    report = sync_source(src, cfg, progress=quiet)
    root = src.path
    pdf = root / "items/PAPERKEY/PDFKEY01_schnet.pdf"
    side = read_side(pdf)
    assert side["title"] == "SchNet: a continuous-filter network" and side["year"] == 2017
    assert side["authors"] == ["Kristof Schutt"] and side["url"] == "https://doi.org/10.5555/schnet"
    assert side["tags"] == ["neural-networks", "vae-ess-nn"] and side["doc_type"] == "paper"
    assert "SchNet cutoff 5 A" in (root / "items/PAPERKEY/note_NOTEKEY1.md").read_text()
    assert not (root / "items/TRASHED1").exists()
    assert [x["attachment"] for x in report["linked"]] == ["LINKED01"]

    # Second run copies nothing. A file that vanished from storage keeps its cached copy and is
    # reported; deleting the attachment item itself removes the copy.
    report = sync_source(src, cfg, progress=quiet)
    assert report["counts"]["downloaded"] == 0
    (data_dir / "storage" / "PDFKEY01" / "schnet.pdf").unlink()
    report = sync_source(src, cfg, progress=quiet)
    assert pdf.exists() and "PDFKEY01" in report["failed"]
    conn = sqlite3.connect(data_dir / "zotero.sqlite")
    conn.execute("DELETE FROM items WHERE itemID = 2")
    conn.commit()
    conn.close()
    sync_source(src, cfg, progress=quiet)
    assert not pdf.exists() and (root / "items/PAPERKEY/abstract.md").exists()
