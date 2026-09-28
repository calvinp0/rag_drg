"""Regression tests for the security review of PR #2."""

import json
import time
import threading
from pathlib import Path

import pytest

pytest.importorskip("mcp")
from tests.test_auth import _get, live_server  # noqa: E402,F401  (fixture)


def test_check_basis_never_reads_server_paths(tmp_path):
    pytest.importorskip("basis_set_exchange")
    from rag_drg.tools.basis import check_basis

    secret = tmp_path / "secret.txt"
    secret.write_text("hunter2-SECRET 1 2 3\n")
    res = check_basis("def2-svp", xyz=str(secret))
    assert "hunter2" not in json.dumps(res, default=str)


def test_close_atom_check_memory_is_bounded():
    from rag_drg.tools._inputcheck.common import check_distances
    from rag_drg.tools.inputcheck import parse_input

    atoms = "\n".join(f"C {i * 1.5:.3f} {(i % 7) * 1.5:.3f} {(i % 11) * 1.5:.3f}" for i in range(4000))
    text = f"%mem=1GB\n#P HF/STO-3G\n\nbig\n\n0 1\n{atoms}\nC 0.0 0.0 0.1\n\n"
    inp, _ = parse_input(text, "big.gjf")
    t = time.time()
    found = check_distances(inp)
    assert time.time() - t < 10
    assert any(f.code == "close-atoms" for f in found)


def test_heavy_rest_request_does_not_block_others(live_server):  # noqa: F811
    url, token, _ = live_server
    atoms = "\n".join(f"C {i * 1.5:.3f} {(i % 7) * 1.5:.3f} {(i % 11) * 1.5:.3f}" for i in range(4000))
    body = json.dumps({"filename": "big.gjf",
                       "content": f"%mem=1GB\n#P HF/STO-3G\n\nbig\n\n0 1\n{atoms}\n\n"}).encode()
    worker = threading.Thread(target=_get, args=(f"{url}/api/check_input", token, "POST", body,
                                                 {"Content-Type": "application/json"}))
    worker.start()
    time.sleep(0.2)
    t = time.time()
    status, _ = _get(f"{url}/api/health")
    assert status == 200 and time.time() - t < 1.0
    worker.join(timeout=60)


def test_oversized_body_rejected_before_reading(live_server):  # noqa: F811
    url, token, _ = live_server
    from rag_drg.tools.rest_api import MAX_BODY

    import http.client
    from urllib.parse import urlparse

    u = urlparse(url)
    conn = http.client.HTTPConnection(u.hostname, u.port, timeout=10)
    conn.putrequest("POST", "/api/check_input")
    conn.putheader("Authorization", f"Bearer {token}")
    conn.putheader("Content-Type", "application/json")
    conn.putheader("Content-Length", str(MAX_BODY + 1))
    conn.endheaders()  # send no body: the server must answer from the header alone
    resp = conn.getresponse()
    assert resp.status == 400 and b"larger than" in resp.read()


def test_api_enforces_allowed_hosts():
    from rag_drg.http_app import _Router, transport_security

    sec = transport_security("0.0.0.0", ["rag.chem.example.ac.il"])
    router = _Router(None, None, sec)

    def scope(host, origin=None):
        h = [(b"host", host.encode())] + ([(b"origin", origin.encode())] if origin else [])
        return {"type": "http", "path": "/api/search", "headers": h}

    assert router._host_problem(scope("rag.chem.example.ac.il:8765")) is None
    assert router._host_problem(scope("localhost:8765")) is None
    assert router._host_problem(scope("evil.example"))[0] == 421
    assert router._host_problem(scope("rag.chem.example.ac.il", "http://evil.example"))[0] == 403
    assert _Router(None, None, transport_security("0.0.0.0", None))._host_problem(scope("any")) is None


def test_cluster_query_not_registered_on_shared_server(project, monkeypatch):
    (project.root / "conf.d").mkdir(exist_ok=True)
    (project.root / "conf.d" / "servers.yaml").write_text("cluster_commands: {enabled: true}\n")
    from rag_drg.config import load_config
    from rag_drg.ingest import ingest

    cfg = load_config(project.root / "rag_drg.yaml")
    ingest(cfg, progress=lambda *_: None)
    from rag_drg.mcp_server import build_server

    def tool_names(mcp):
        import asyncio

        return {t.name for t in asyncio.run(mcp.list_tools())}

    monkeypatch.delenv("RAG_DRG_SERVER_MODE", raising=False)
    assert "cluster_query" in tool_names(build_server(cfg))
    monkeypatch.setenv("RAG_DRG_SERVER_MODE", "1")
    assert "cluster_query" not in tool_names(build_server(cfg))


def test_remote_client_does_not_follow_redirects(tmp_path, monkeypatch):
    import importlib.machinery
    import importlib.util
    from http.server import BaseHTTPRequestHandler, HTTPServer

    seen = []

    class H(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            seen.append((self.path, self.headers.get("Authorization")))
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1:%d/elsewhere" % self.server.server_port)
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    root = Path(__file__).resolve().parent.parent
    loader = importlib.machinery.SourceFileLoader("rdr", str(root / "integrations" / "rag-drg-remote"))
    spec = importlib.util.spec_from_loader("rdr", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    monkeypatch.setenv("RAG_DRG_URL", f"http://127.0.0.1:{srv.server_port}")
    monkeypatch.setenv("RAG_DRG_TOKEN", "tok")
    with pytest.raises(ConnectionError, match="redirected"):
        mod._request("GET", "health")
    srv.shutdown()
    assert [p for p, _ in seen] == ["/api/health"]  # the redirect target was never requested
