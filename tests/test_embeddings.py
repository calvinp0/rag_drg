import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import numpy as np

from rag_drg.config import EmbeddingConfig
from rag_drg.embeddings import OpenAICompatibleEmbedder, make_embedder
from rag_drg.ingest import ingest
from rag_drg.search import Searcher


class _Handler(BaseHTTPRequestHandler):
    requests: list = []

    def do_POST(self):  # noqa: N802 - http.server API
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        _Handler.requests.append((self.path, body, self.headers.get("Authorization")))
        data = [
            {"index": i, "embedding": [float(len(t)), float(t.count("a")), 1.0]}
            for i, t in enumerate(body["input"])
        ]
        payload = json.dumps({"data": list(reversed(data))}).encode()  # out of order on purpose
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


def _server():
    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def test_openai_compatible_embedder(monkeypatch):
    srv = _server()
    monkeypatch.setenv("MY_KEY", "secret")
    cfg = EmbeddingConfig(provider="openai", model="nomic-embed-text", base_url=f"http://127.0.0.1:{srv.server_port}/v1",
                          api_key_env="MY_KEY", batch_size=2)
    emb = make_embedder(cfg)
    assert isinstance(emb, OpenAICompatibleEmbedder)
    vecs = emb.embed(["a", "bb", "aaa"])
    assert vecs.shape == (3, 3)
    # Results are re-ordered by index; nomic models get task prefixes.
    assert np.allclose(vecs[:, 0], [len("search_document: a"), len("search_document: bb"), len("search_document: aaa")])
    path, body, auth = _Handler.requests[0]
    assert path == "/v1/embeddings" and auth == "Bearer secret" and len(body["input"]) == 2
    q = emb.embed(["x"], is_query=True)
    assert q[0, 0] == len("search_query: x")
    srv.shutdown()


def test_ingest_with_embeddings_enables_semantic_search(project):
    srv = _server()
    project.embeddings = EmbeddingConfig(provider="openai", model="m", base_url=f"http://127.0.0.1:{srv.server_port}/v1")
    report = ingest(project, progress=lambda *_: None)
    assert report["embedded"] > 0
    s = Searcher(project)
    assert s.embedder is not None
    hits = s.search("transition state")
    assert any("semantic" in h.via for h in hits)
    # A different configured model than the one in the index disables the dense path.
    project.embeddings = EmbeddingConfig(provider="openai", model="other", base_url=project.embeddings.base_url)
    assert Searcher(project).embedder is None
    srv.shutdown()
