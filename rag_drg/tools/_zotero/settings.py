"""The `zotero:` sub-section of a `type: zotero` source entry.

`config.py` keeps only the generic source fields, so the `zotero:` dict is read back from the
merged YAML (`cfg.extra["sources"]`) by source name.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_API_BASE = "https://api.zotero.org"
DEFAULT_FILE_TYPES = ("application/pdf", "text/html")


class ZoteroError(RuntimeError):
    """A configuration or API problem worth showing to the user as is."""


@dataclass
class ZoteroSettings:
    mode: str = "web"  # "web" (Zotero Web API v3) | "local" (read zotero.sqlite + storage/)
    library_type: str = "group"  # "group" | "user"
    library_id: str | None = None  # group ID, or the numeric userID for a personal library
    api_key_env: str | None = "ZOTERO_API_KEY"  # null -> anonymous (public libraries only)
    api_base: str = DEFAULT_API_BASE
    collections: list[str] | None = None  # names or keys; None = whole library
    collection_tags: bool = True
    include_notes: bool = True
    file_types: list[str] = field(default_factory=lambda: list(DEFAULT_FILE_TYPES))
    max_file_mb: float = 200.0
    page_size: int = 100  # Zotero's maximum `limit`
    timeout: float = 60.0
    data_dir: Path | None = None  # local mode: the Zotero data directory (contains zotero.sqlite)
    linked_base_dir: Path | None = None  # local mode: base dir for "attachments:" relative links
    domain: str = "literature"
    doc_type: str = "paper"

    @property
    def library_prefix(self) -> str:
        kind = "groups" if self.library_type == "group" else "users"
        return f"{kind}/{self.library_id}"

    @property
    def identity(self) -> str:
        """What the cached state belongs to; a change forces a full re-sync."""
        if self.mode == "local":
            return f"local:{self.data_dir}:{self.library_type}:{self.library_id or ''}"
        return f"web:{self.api_base}/{self.library_prefix}"

    def api_key(self) -> str | None:
        """The key is read from the environment on every use and never stored or logged."""
        if not self.api_key_env:
            return None
        key = os.environ.get(self.api_key_env, "").strip()
        if not key:
            raise ZoteroError(
                f"environment variable {self.api_key_env} is not set; export a Zotero API key "
                "with read access to the library (see docs/zotero.md), or set `api_key_env: null` "
                "for a public library"
            )
        return key


def raw_source_entry(cfg, name: str) -> dict:
    for s in (getattr(cfg, "extra", None) or {}).get("sources") or []:
        if isinstance(s, dict) and s.get("name") == name:
            return s
    return {}


def _as_list(v) -> list[str] | None:
    if v is None or v == "" or v == []:
        return None
    if isinstance(v, (list, tuple)):
        return [str(x) for x in v]
    return [str(v)]


def parse_settings(raw: dict, root: Path | None = None) -> tuple[ZoteroSettings, list[str]]:
    """Returns (settings, problems). `raw` is the whole source entry."""
    z = raw.get("zotero") or {}
    problems: list[str] = []
    if not isinstance(z, dict):
        return ZoteroSettings(), ["'zotero' must be a mapping"]

    def path(v):
        if not v:
            return None
        p = Path(os.path.expandvars(os.path.expanduser(str(v))))
        return p if p.is_absolute() or root is None else root / p

    s = ZoteroSettings(
        mode=str(z.get("mode", "web")),
        library_type=str(z.get("library_type", "group")),
        library_id=None if z.get("library_id") in (None, "") else str(z.get("library_id")),
        api_key_env=z.get("api_key_env", "ZOTERO_API_KEY"),
        api_base=str(z.get("api_base") or DEFAULT_API_BASE).rstrip("/"),
        collections=_as_list(z.get("collections")),
        collection_tags=bool(z.get("collection_tags", True)),
        include_notes=bool(z.get("include_notes", True)),
        file_types=_as_list(z.get("file_types")) or list(DEFAULT_FILE_TYPES),
        max_file_mb=float(z.get("max_file_mb", 200)),
        page_size=max(1, min(100, int(z.get("page_size", 100)))),
        timeout=float(z.get("timeout", 60)),
        data_dir=path(z.get("data_dir")),
        linked_base_dir=path(z.get("linked_base_dir")),
        domain=str(raw.get("domain") or "literature"),
        doc_type=str(raw.get("doc_type") or "paper"),
    )
    if s.mode not in ("web", "local"):
        problems.append(f"zotero.mode {s.mode!r} must be 'web' or 'local'")
    if s.library_type not in ("group", "user"):
        problems.append(f"zotero.library_type {s.library_type!r} must be 'group' or 'user'")
    if s.mode == "web":
        if not s.library_id or not s.library_id.isdigit():
            problems.append("zotero.library_id must be the numeric group ID (or userID); quote it in YAML")
        if s.api_key_env is not None and not isinstance(s.api_key_env, str):
            problems.append("zotero.api_key_env must be the NAME of an environment variable (or null)")
        if isinstance(z.get("api_key"), str):
            problems.append("zotero.api_key must not be in the config; use api_key_env")
    else:
        if not s.data_dir:
            problems.append("zotero.data_dir is required for mode: local (e.g. ~/Zotero)")
        if s.library_type == "group" and not s.library_id:
            problems.append("zotero.library_id (group ID) is required for a group library in local mode")
    return s, problems


def settings_for(src, cfg) -> ZoteroSettings:
    raw = raw_source_entry(cfg, src.name)
    s, problems = parse_settings(raw, getattr(cfg, "root", None))
    if not raw:
        problems.append("source entry not found in the configuration")
    if problems:
        raise ZoteroError(f"[{src.name}] " + "; ".join(problems))
    if src.domain:
        s.domain = src.domain
    return s
