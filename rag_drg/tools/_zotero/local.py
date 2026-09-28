"""Read a Zotero data directory (`zotero.sqlite` + `storage/`) for personal libraries without
Web API access. Produces the same item dicts as the Web API's `data` field, so the rest of the
sync is shared.

Zotero keeps the database locked while it runs, so it is copied to a temp file first (with
its `-wal`/`-journal` file if present). Only these tables/columns are read:

  libraries(libraryID, type)                         type 'user' = My Library
  groups(groupID, libraryID)                         group libraries (library_type: group)
  items(itemID, itemTypeID, libraryID, key, version)
  itemTypes(itemTypeID, typeName)                    or the view itemTypesCombined (Zotero 7)
  fields(fieldID, fieldName)                         or the view fieldsCombined (Zotero 7)
  itemData(itemID, fieldID, valueID), itemDataValues(valueID, value)
  creators(creatorID, firstName, lastName, fieldMode)
  creatorTypes(creatorTypeID, creatorType)
  itemCreators(itemID, creatorID, creatorTypeID, orderIndex)
  tags(tagID, name), itemTags(itemID, tagID, type)
  collections(collectionID, collectionName, parentCollectionID, libraryID, key)
  collectionItems(collectionID, itemID)
  itemAttachments(itemID, parentItemID, linkMode, contentType, path)
  itemNotes(itemID, parentItemID, note)
  deletedItems(itemID)                               the trash
"""

from __future__ import annotations

import shutil
import sqlite3
import tempfile
from pathlib import Path

from .settings import ZoteroError, ZoteroSettings
from .webapi import FileNotAvailable

LINK_MODES = {0: "imported_file", 1: "imported_url", 2: "linked_file", 3: "linked_url", 4: "embedded_image"}


def _table(conn: sqlite3.Connection, *names: str) -> str:
    for n in names:
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name=?", (n,)).fetchone():
            return n
    raise ZoteroError(f"zotero.sqlite has none of the tables {names}; unsupported Zotero version?")


def read_library(s: ZoteroSettings) -> tuple[list[dict], list[dict]]:
    """Returns (items as Web-API-style `data` dicts, collections as Web-API-style objects)."""
    assert s.data_dir is not None
    db = s.data_dir / "zotero.sqlite"
    if not db.is_file():
        raise ZoteroError(f"{db} not found; set zotero.data_dir to your Zotero data directory")
    with tempfile.TemporaryDirectory(prefix="rag-drg-zotero-") as tmp:
        copy = Path(tmp) / "zotero.sqlite"
        shutil.copy2(db, copy)
        for suffix in ("-wal", "-journal"):
            if db.with_name(db.name + suffix).exists():
                shutil.copy2(db.with_name(db.name + suffix), copy.with_name(copy.name + suffix))
        conn = sqlite3.connect(copy)
        try:
            return _read(conn, s)
        finally:
            conn.close()


def _read(conn: sqlite3.Connection, s: ZoteroSettings) -> tuple[list[dict], list[dict]]:
    if s.library_type == "group":
        row = conn.execute("SELECT libraryID FROM groups WHERE groupID=?", (int(s.library_id or 0),)).fetchone()
    else:
        row = conn.execute("SELECT libraryID FROM libraries WHERE type='user' ORDER BY libraryID").fetchone()
    if not row:
        raise ZoteroError(f"library {s.library_type} {s.library_id or ''} not found in zotero.sqlite")
    lib = row[0]
    item_types, fields = _table(conn, "itemTypesCombined", "itemTypes"), _table(conn, "fieldsCombined", "fields")

    items: dict[int, dict] = {}
    for item_id, key, version, type_name in conn.execute(
        f"SELECT i.itemID, i.key, i.version, t.typeName FROM items i JOIN {item_types} t "
        "ON t.itemTypeID = i.itemTypeID WHERE i.libraryID=?", (lib,)
    ):
        items[item_id] = {"key": key, "version": version or 0, "itemType": type_name,
                          "creators": [], "tags": [], "collections": []}
    for item_id, name, value in conn.execute(
        f"SELECT d.itemID, f.fieldName, v.value FROM itemData d JOIN {fields} f ON f.fieldID = d.fieldID "
        "JOIN itemDataValues v ON v.valueID = d.valueID"
    ):
        if item_id in items:
            items[item_id][name] = value
    for item_id, first, last, mode, ctype in conn.execute(
        "SELECT ic.itemID, c.firstName, c.lastName, c.fieldMode, ct.creatorType FROM itemCreators ic "
        "JOIN creators c ON c.creatorID = ic.creatorID JOIN creatorTypes ct ON ct.creatorTypeID = ic.creatorTypeID "
        "ORDER BY ic.itemID, ic.orderIndex"
    ):
        if item_id in items:
            cr = {"creatorType": ctype, "name": last} if mode == 1 else \
                {"creatorType": ctype, "firstName": first or "", "lastName": last or ""}
            items[item_id]["creators"].append(cr)
    for item_id, name, ttype in conn.execute(
        "SELECT it.itemID, t.name, it.type FROM itemTags it JOIN tags t ON t.tagID = it.tagID"
    ):
        if item_id in items:
            items[item_id]["tags"].append({"tag": name, "type": ttype or 0})

    coll_rows = conn.execute(
        "SELECT collectionID, key, collectionName, parentCollectionID FROM collections WHERE libraryID=?", (lib,)
    ).fetchall()
    coll_key = {cid: key for cid, key, _, _ in coll_rows}
    collections = [{"key": key, "data": {"key": key, "name": name, "parentCollection": coll_key.get(parent) or False}}
                   for _, key, name, parent in coll_rows]
    for cid, item_id in conn.execute("SELECT collectionID, itemID FROM collectionItems"):
        if item_id in items and cid in coll_key:
            items[item_id]["collections"].append(coll_key[cid])

    key_of = {i: d["key"] for i, d in items.items()}
    for item_id, parent, link_mode, ctype, path in conn.execute(
        "SELECT itemID, parentItemID, linkMode, contentType, path FROM itemAttachments"
    ):
        if item_id in items:
            d = items[item_id]
            d.update(parentItem=key_of.get(parent) or False, linkMode=LINK_MODES.get(link_mode, str(link_mode)),
                     contentType=ctype or "", path=path or "")
            if path and path.startswith("storage:"):
                d["filename"] = path[len("storage:"):]
            elif path:
                d["filename"] = Path(path.split(":", 1)[-1]).name
    for item_id, parent, note in conn.execute("SELECT itemID, parentItemID, note FROM itemNotes"):
        if item_id in items:
            items[item_id].update(parentItem=key_of.get(parent) or False, note=note or "")
    for (item_id,) in conn.execute("SELECT itemID FROM deletedItems"):
        if item_id in items:
            items[item_id]["deleted"] = 1
    return list(items.values()), collections


def file_path(s: ZoteroSettings, att: dict) -> Path | None:
    """Where the attachment's file is on disk (stored or linked), if it can be found."""
    assert s.data_dir is not None
    path = att.get("path") or ""
    if path.startswith("storage:"):
        return s.data_dir / "storage" / att["key"] / path[len("storage:"):]
    if path.startswith("attachments:"):
        return s.linked_base_dir / path[len("attachments:"):] if s.linked_base_dir else None
    return Path(path) if path else None


def file_signature(s: ZoteroSettings, att: dict) -> str | None:
    p = file_path(s, att)
    if p is None or not p.is_file():
        return None
    st = p.stat()
    return f"{st.st_size}:{int(st.st_mtime)}"


def read_file(s: ZoteroSettings, att: dict, max_bytes: int) -> bytes:
    p = file_path(s, att)
    if p is None or not p.is_file():
        raise FileNotAvailable(f"file not found: {p}")
    if p.stat().st_size > max_bytes:
        raise ZoteroError(f"file larger than {max_bytes // 1_000_000} MB, skipped")
    return p.read_bytes()
