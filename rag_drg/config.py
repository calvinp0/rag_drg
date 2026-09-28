"""Configuration loading.

The config file (``rag_drg.yaml``) is looked up in this order:
``--config`` argument, ``$RAG_DRG_CONFIG``, ``./rag_drg.yaml``, then the
repository root next to this package. Relative paths inside the file are
resolved against the directory that contains the config file, so the same
config works for everyone who clones the repo.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG_NAME = "rag_drg.yaml"


@dataclass
class EmbeddingConfig:
    # "none" -> keyword (BM25) search only; works everywhere with no extra deps.
    # "sentence-transformers" -> local model, e.g. BAAI/bge-small-en-v1.5.
    # "openai" -> any OpenAI-compatible /v1/embeddings endpoint (Ollama, vLLM, LM Studio, OpenAI).
    provider: str = "none"
    model: str = "BAAI/bge-small-en-v1.5"
    base_url: str | None = None
    api_key_env: str | None = None
    batch_size: int = 32


@dataclass
class SourceConfig:
    name: str
    type: str  # "local" | "git" | "url"
    path: Path | None = None  # local: directory/file; git/url: filled in as cache dir
    url: str | None = None
    urls: list[str] = field(default_factory=list)
    ref: str | None = None
    sparse: list[str] = field(default_factory=list)
    include: list[str] = field(default_factory=lambda: ["**/*"])
    exclude: list[str] = field(default_factory=list)
    domain: str | None = None
    software: str | None = None
    version: str | None = None
    doc_type: str = "reference"
    # Per-file overrides: [{"glob": "arc/schemas/*.json", "doc_type": "schema"}, ...]; first match wins.
    doc_type_rules: list[dict] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    enabled: bool = True
    base_url: str | None = None  # used to build citation links for files in the source
    # url sources only: follow links from the start `urls`, staying under `allow_prefix`.
    crawl: bool = False
    allow_prefix: list[str] = field(default_factory=list)
    deny: list[str] = field(default_factory=list)  # substrings; matching URLs are skipped
    max_pages: int = 200
    delay: float = 0.5


@dataclass
class Config:
    root: Path
    index_path: Path
    cache_dir: Path
    lessons_dir: Path
    embeddings: EmbeddingConfig
    sources: list[SourceConfig]
    chunk_size: int = 1500
    chunk_overlap: int = 200

    def source(self, name: str) -> SourceConfig:
        for s in self.sources:
            if s.name == name:
                return s
        raise KeyError(f"Unknown source '{name}'. Known: {[s.name for s in self.sources]}")


def _find_config(explicit: str | os.PathLike | None) -> Path:
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    if os.environ.get("RAG_DRG_CONFIG"):
        candidates.append(Path(os.environ["RAG_DRG_CONFIG"]))
    candidates.append(Path.cwd() / DEFAULT_CONFIG_NAME)
    candidates.append(Path(__file__).resolve().parent.parent / DEFAULT_CONFIG_NAME)
    for c in candidates:
        if c.is_file():
            return c.resolve()
    raise FileNotFoundError(
        f"No {DEFAULT_CONFIG_NAME} found (tried: {', '.join(str(c) for c in candidates)})"
    )


def _as_list(value: Any) -> list:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def load_config(path: str | os.PathLike | None = None) -> Config:
    cfg_path = _find_config(path)
    root = cfg_path.parent
    raw = yaml.safe_load(cfg_path.read_text()) or {}

    def resolve(p: str | None, default: str) -> Path:
        p = os.path.expandvars(os.path.expanduser(p or default))
        q = Path(p)
        return q if q.is_absolute() else (root / q)

    cache_dir = resolve(raw.get("cache_dir"), "sources_cache")
    emb_raw = raw.get("embeddings") or {}
    embeddings = EmbeddingConfig(
        provider=str(emb_raw.get("provider", "none")),
        model=str(emb_raw.get("model", EmbeddingConfig.model)),
        base_url=emb_raw.get("base_url"),
        api_key_env=emb_raw.get("api_key_env"),
        batch_size=int(emb_raw.get("batch_size", 32)),
    )

    sources: list[SourceConfig] = []
    for s in raw.get("sources") or []:
        stype = s.get("type", "local")
        if stype == "local":
            spath = resolve(s.get("path"), s["name"])
        else:
            spath = cache_dir / s["name"]
        sources.append(
            SourceConfig(
                name=s["name"],
                type=stype,
                path=spath,
                url=s.get("url"),
                urls=_as_list(s.get("urls")),
                ref=s.get("ref"),
                sparse=_as_list(s.get("sparse")),
                include=_as_list(s.get("include")) or ["**/*"],
                exclude=_as_list(s.get("exclude")),
                domain=s.get("domain"),
                software=s.get("software"),
                version=None if s.get("version") is None else str(s.get("version")),
                doc_type=s.get("doc_type", "reference"),
                doc_type_rules=_as_list(s.get("doc_type_rules")),
                tags=_as_list(s.get("tags")),
                enabled=bool(s.get("enabled", True)),
                base_url=s.get("base_url"),
                crawl=bool(s.get("crawl", False)),
                allow_prefix=_as_list(s.get("allow_prefix")),
                deny=_as_list(s.get("deny")),
                max_pages=int(s.get("max_pages", 200)),
                delay=float(s.get("delay", 0.5)),
            )
        )

    return Config(
        root=root,
        index_path=resolve(raw.get("index_path"), "index/rag_drg.sqlite"),
        cache_dir=cache_dir,
        lessons_dir=resolve(raw.get("lessons_dir"), "knowledge/lessons"),
        embeddings=embeddings,
        sources=sources,
        chunk_size=int(raw.get("chunk_size", 1500)),
        chunk_overlap=int(raw.get("chunk_overlap", 200)),
    )
