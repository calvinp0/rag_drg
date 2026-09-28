"""Token auth: token file, CLI, refusals, and a real HTTP server (MCP + REST) on 127.0.0.1."""

import asyncio
import json
import socket
import threading
import time
import urllib.error
import urllib.request

import pytest

from rag_drg.auth import TokenStore, hash_token, is_loopback, tokens_path
from rag_drg.cli import main
from rag_drg.ingest import ingest

mcp_sdk = pytest.importorskip("mcp")


def test_token_store_hashes_only(project):
    store = TokenStore(tokens_path(project))
    assert store.path == project.index_path.parent / "tokens.yaml"
    tok = store.add("alice")
    text = store.path.read_text()
    assert tok not in text and hash_token(tok) in text
    assert store.verify(tok) == "alice"
    assert store.verify(tok + "x") is None and store.verify("") is None
    with pytest.raises(ValueError):
        store.add("alice")
    # A second store (the running server) sees CLI changes without restart.
    server_view = TokenStore(store.path)
    assert server_view.verify(tok) == "alice"
    server_view.flush()
    assert "last_used" in store.path.read_text()
    assert store.revoke("alice") and not store.revoke("alice")
    time.sleep(0.01)
    assert server_view.verify(tok) is None


def test_tokens_file_from_config(project):
    project.extra["auth"] = {"tokens_file": "secrets/t.yaml"}
    assert tokens_path(project) == project.root / "secrets" / "t.yaml"


def test_tokens_cli(project, capsys):
    cfg = str(project.root / "rag_drg.yaml")
    assert main(["-c", cfg, "tokens", "add", "bob"]) == 0
    tok = capsys.readouterr().out.strip()
    assert tok.startswith("rdg_")
    assert main(["-c", cfg, "tokens", "list"]) == 0
    out = capsys.readouterr().out
    assert "bob" in out and tok not in out
    assert main(["-c", cfg, "tokens", "revoke", "bob"]) == 0
    assert main(["-c", cfg, "tokens", "revoke", "bob"]) == 1


def test_serve_refusals(project):
    from rag_drg.mcp_server import serve

    with pytest.raises(SystemExit, match="without authentication"):
        serve(project, transport="http", host="0.0.0.0", auth="none")
    with pytest.raises(SystemExit, match="No API tokens"):
        serve(project, transport="http", host="127.0.0.1", auth="token")
    assert is_loopback("127.0.0.1") and is_loopback("::1") and is_loopback("localhost")
    assert not is_loopback("0.0.0.0") and not is_loopback("rag.chem.example.ac.il")


def test_transport_security_allowed_hosts():
    from rag_drg.http_app import transport_security

    ts = transport_security("0.0.0.0", ["rag.chem.example.ac.il"])
    assert ts.enable_dns_rebinding_protection
    assert "rag.chem.example.ac.il" in ts.allowed_hosts and "rag.chem.example.ac.il:*" in ts.allowed_hosts
    assert "http://rag.chem.example.ac.il:*" in ts.allowed_origins
    assert not transport_security("0.0.0.0", None).enable_dns_rebinding_protection
    assert not transport_security("0.0.0.0", ["*"]).enable_dns_rebinding_protection


# --- live server ---------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def live_server(project):
    import uvicorn

    from rag_drg.http_app import attach_user, build_http_app
    from rag_drg.mcp_server import build_server

    ingest(project, progress=lambda *_: None)
    store = TokenStore(tokens_path(project))
    token = store.add("alice")
    mcp = build_server(project)
    attach_user(mcp)
    events: list[dict] = []
    mcp._rag_drg_ctx.subscribe(events.append)
    port = _free_port()
    app = build_http_app(mcp, "http", "127.0.0.1", None, TokenStore(store.path))
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.02)
    assert server.started
    yield f"http://127.0.0.1:{port}", token, events
    server.should_exit = True
    th.join(timeout=10)


def _get(url, token=None, method="GET", data=None, headers=None):
    req = urllib.request.Request(url, method=method, data=data, headers=dict(headers or {}))
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def test_rest_api_auth_and_search(live_server):
    base, token, events = live_server
    code, body = _get(f"{base}/api/health")
    assert code == 200 and json.loads(body)["status"] == "ok"

    code, body = _get(f"{base}/api/search?q=maxcore")
    assert code == 401 and json.loads(body)["error"] == "unauthorized"
    assert _get(f"{base}/api/search?q=maxcore", token="rdg_wrong")[0] == 401

    code, body = _get(f"{base}/api/search?q=maxcore%20memory&software=orca&k=3", token=token)
    assert code == 200
    data = json.loads(body)
    assert data["results"] and data["results"][0]["software"] == "orca"
    assert events[-1]["user"] == "alice" and events[-1]["transport"] == "rest"

    code, body = _get(f"{base}/api/search?q=maxcore%20memory&max_tokens=60", token=token)
    data = json.loads(body)
    assert code == 200 and len(data["text"]) <= 60 * 4 + 1
    assert "chunk_id=" in data["text"]

    code, body = _get(f"{base}/api/level?name=b3lyp", token=token)
    assert code == 200 and "text" in json.loads(body)
    assert _get(f"{base}/api/search", token=token)[0] == 400

    # The MCP endpoint is behind the same check.
    init = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}).encode()
    hdrs = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    assert _get(f"{base}/mcp", method="POST", data=init, headers=hdrs)[0] == 401
    assert _get(f"{base}/mcp", token="nope", method="POST", data=init, headers=hdrs)[0] == 401


async def _mcp_session(url: str, token: str):
    headers = {"Authorization": f"Bearer {token}"}
    from mcp import ClientSession

    try:  # SDK 2.x: headers go on the httpx client
        import httpx2
        from mcp.client.streamable_http import streamable_http_client

        async with httpx2.AsyncClient(headers=headers, timeout=30) as hc:
            async with streamable_http_client(url, http_client=hc) as (r, w):
                async with ClientSession(r, w) as s:
                    return await _exercise(s)
    except ImportError:  # SDK 1.x
        from mcp.client.streamable_http import streamablehttp_client

        async with streamablehttp_client(url, headers=headers) as (r, w, _):
            async with ClientSession(r, w) as s:
                return await _exercise(s)


async def _exercise(s):
    await s.initialize()
    tools = await s.list_tools()
    res = await s.call_tool("search_knowledge", {"query": "maxcore memory", "max_tokens": 80})
    text = "".join(getattr(c, "text", "") for c in res.content)
    return [t.name for t in tools.tools], text


def test_mcp_handshake_with_token_and_user_attribution(live_server):
    base, token, events = live_server
    names, text = asyncio.run(_mcp_session(f"{base}/mcp", token))
    assert "search_knowledge" in names and "record_lesson" in names
    assert "maxcore" in text.lower() and len(text) <= 80 * 4 + 1
    ev = [e for e in events if e.get("tool") == "search_knowledge" and "transport" not in e]
    assert ev and ev[-1]["user"] == "alice" and ev[-1]["args"]["max_tokens"] == 80
