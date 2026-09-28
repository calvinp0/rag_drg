"""Pluggable embedding providers.

* ``none`` - keyword search only (default; zero extra dependencies).
* ``sentence-transformers`` - runs a local model on CPU/GPU.
* ``openai`` - any OpenAI-compatible ``/v1/embeddings`` endpoint, e.g. Ollama
  (``http://localhost:11434/v1`` with ``nomic-embed-text``), vLLM, LM Studio,
  or OpenAI itself. Uses only the standard library for HTTP.
"""

from __future__ import annotations

import json
import os
import urllib.request
from typing import Protocol, Sequence

import numpy as np

from .config import EmbeddingConfig


class Embedder(Protocol):
    name: str

    def embed(self, texts: Sequence[str], is_query: bool = False) -> np.ndarray: ...


class SentenceTransformerEmbedder:
    def __init__(self, cfg: EmbeddingConfig):
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as e:  # pragma: no cover - optional dep
            raise RuntimeError("Install `rag-drg[st]` to use sentence-transformers embeddings") from e
        self.model = SentenceTransformer(cfg.model)
        self.name = f"st:{cfg.model}"
        self.batch_size = cfg.batch_size
        # BGE / E5 style models expect an instruction prefix on queries.
        lower = cfg.model.lower()
        self.query_prefix = (
            "Represent this sentence for searching relevant passages: " if "bge" in lower
            else "query: " if "e5" in lower else ""
        )

    def embed(self, texts, is_query=False):
        if is_query and self.query_prefix:
            texts = [self.query_prefix + t for t in texts]
        return np.asarray(
            self.model.encode(list(texts), batch_size=self.batch_size, normalize_embeddings=True),
            dtype=np.float32,
        )


class OpenAICompatibleEmbedder:
    def __init__(self, cfg: EmbeddingConfig):
        self.base_url = (cfg.base_url or "https://api.openai.com/v1").rstrip("/")
        self.model = cfg.model
        self.api_key = os.environ.get(cfg.api_key_env) if cfg.api_key_env else None
        self.batch_size = cfg.batch_size
        self.name = f"openai:{self.base_url}:{cfg.model}"
        lower = cfg.model.lower()
        self.query_prefix = "search_query: " if "nomic" in lower else ""
        self.doc_prefix = "search_document: " if "nomic" in lower else ""

    def _post(self, texts: list[str]) -> list[list[float]]:
        body = json.dumps({"model": self.model, "input": texts}).encode()
        req = urllib.request.Request(f"{self.base_url}/embeddings", data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        if self.api_key:
            req.add_header("Authorization", f"Bearer {self.api_key}")
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read())
        return [d["embedding"] for d in sorted(data["data"], key=lambda d: d["index"])]

    def embed(self, texts, is_query=False):
        prefix = self.query_prefix if is_query else self.doc_prefix
        texts = [prefix + t for t in texts]
        out: list[list[float]] = []
        for i in range(0, len(texts), self.batch_size):
            out.extend(self._post(texts[i:i + self.batch_size]))
        return np.asarray(out, dtype=np.float32)


def make_embedder(cfg: EmbeddingConfig) -> Embedder | None:
    provider = cfg.provider.lower()
    if provider in ("none", "", "off", "bm25"):
        return None
    if provider in ("sentence-transformers", "st", "local"):
        return SentenceTransformerEmbedder(cfg)
    if provider in ("openai", "ollama", "openai-compatible"):
        return OpenAICompatibleEmbedder(cfg)
    raise ValueError(f"Unknown embeddings provider '{cfg.provider}'")
