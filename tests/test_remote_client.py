"""The stdlib-only thin client (integrations/rag-drg-remote) against a real server."""

import importlib.machinery
import importlib.util
import io
import json
import shutil
from pathlib import Path

import pytest

pytest.importorskip("mcp")
from tests.test_auth import live_server  # noqa: E402,F401  (fixture)

ROOT = Path(__file__).resolve().parent.parent
VALID = ROOT / "tests" / "fixtures" / "inputs" / "valid"
OUTPUTS = ROOT / "tests" / "fixtures" / "outputs"


def _client():
    loader = importlib.machinery.SourceFileLoader("rag_drg_remote", str(ROOT / "integrations" / "rag-drg-remote"))
    spec = importlib.util.spec_from_loader("rag_drg_remote", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


@pytest.fixture
def client(live_server, monkeypatch):  # noqa: F811
    url, token, events = live_server
    monkeypatch.setenv("RAG_DRG_URL", url)
    monkeypatch.setenv("RAG_DRG_TOKEN", token)
    return _client(), events


def _hook_json(path: Path) -> str:
    return json.dumps({"tool_name": "Write", "tool_input": {"file_path": str(path)}})


def test_check_valid_and_broken_gaussian(client, tmp_path):
    cli, events = client
    good = tmp_path / "g16_opt.gjf"
    shutil.copy(VALID / "g16_opt.gjf", good)
    n_err, report = cli.check(good)
    assert n_err == 0, report
    bad = tmp_path / "radical.gjf"
    # Doublet multiplicity with an even electron count (CH4) -> parity error.
    bad.write_text("%nprocshared=4\n%mem=8GB\n#P B3LYP/6-31G(d) Opt\n\nmethane\n\n0 2\n"
                   "C 0.0 0.0 0.0\nH 0.63 0.63 0.63\nH -0.63 -0.63 0.63\nH -0.63 0.63 -0.63\nH 0.63 -0.63 -0.63\n\n")
    n_err, report = cli.check(bad)
    assert n_err >= 1 and "error" in report
    assert events[-1]["tool"] == "check_input" and events[-1]["user"] == "alice"


def test_hook_exit_codes(client, tmp_path, capsys, monkeypatch):
    cli, _ = client
    bad = tmp_path / "radical.gjf"
    bad.write_text("#P B3LYP/6-31G(d) SP\n\nt\n\n0 2\nC 0 0 0\nH 0.63 0.63 0.63\nH -0.63 -0.63 0.63\n"
                   "H -0.63 0.63 -0.63\nH 0.63 -0.63 -0.63\n\n")
    assert cli.hook(_hook_json(bad)) == 2
    assert "fix them" in capsys.readouterr().err
    notes = tmp_path / "README.md"
    notes.write_text("# notes\n")
    assert cli.hook(_hook_json(notes)) == 0           # not an ESS file: ignored
    good = tmp_path / "orca_ts.inp"
    shutil.copy(VALID / "orca_ts.inp", good)
    assert cli.hook(_hook_json(good)) == 0
    monkeypatch.setenv("RAG_DRG_URL", "http://127.0.0.1:9")   # server down: never block the agent
    assert cli.hook(_hook_json(bad)) == 0
    assert "skipped" in capsys.readouterr().err


def test_submit_script_found_next_to_input(client, tmp_path):
    cli, _ = client
    shutil.copy(VALID / "orca_ts.inp", tmp_path / "orca_ts.inp")
    n_err, report = cli.check(tmp_path / "orca_ts.inp")
    assert n_err == 0, report
    # The matching script asks for far less memory than %maxcore x nprocs: only visible if the
    # client found the script next to the input and sent it along.
    script = (VALID / "run_orca.sh").read_text().replace("--mem-per-cpu=4G", "--mem-per-cpu=500M")
    (tmp_path / "run_orca.sh").write_text(script)
    n_err, report = cli.check(tmp_path / "orca_ts.inp")
    assert n_err >= 1 and "orca-maxcore-alloc" in report, report


def test_diagnose_search_level_and_bad_token(client, monkeypatch, capsys):
    cli, _ = client
    out = OUTPUTS / "g16_l9999_unconverged_fail.out"
    assert cli.main(["diagnose", str(out)]) == 1
    assert "l9999" in capsys.readouterr().out
    assert cli.main(["search", "maxcore", "--software", "orca"]) == 0
    assert "maxcore" in capsys.readouterr().out.lower()
    assert cli.main(["level", "wb97xd", "--software", "orca"]) == 0
    assert capsys.readouterr().out.strip()  # (the tiny test project has no levels table)
    monkeypatch.setenv("RAG_DRG_TOKEN", "wrong")
    assert cli.main(["search", "maxcore"]) == 3
    assert "401" in capsys.readouterr().err


def test_head_tail_of_big_file(tmp_path):
    cli = _client()
    big = tmp_path / "big.log"
    with open(big, "w") as f:
        f.write(" Entering Gaussian System\n")
        for i in range(200_000):
            f.write(f" SCF cycle {i} energy -100.{i:06d}\n")
        f.write(" Error termination via Lnk1e in /g16/l502.exe\n")
    text = cli._head_tail(big)
    assert text.startswith(" Entering Gaussian") and "l502.exe" in text and "lines omitted" in text
    assert len(text) < 500_000


GPU_SCRIPT = """#!/bin/bash
#SBATCH --job-name=g
#SBATCH --partition=gpu
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=8
#SBATCH --gres=gpu:1
#SBATCH --mem=64G
#SBATCH --time=12:00:00
/opt/g16/g16 < job.gjf > job.log
"""


def test_queue_access_uses_the_requesting_users_identity(client, project, monkeypatch):
    """The shared server judges queue access for the *client*, never its own service account."""
    cli, _ = client
    shutil.copy(ROOT / "servers.example.yaml", project.root / "servers.yaml")  # gpu: alice or @gpuusers
    monkeypatch.setenv("RAG_DRG_SERVER_MODE", "1")
    body = {"filename": "run_gpu.sh", "content": GPU_SCRIPT}

    def access(user, groups):
        res = cli._request("POST", "check_input", body={**body, "client_user": user, "client_groups": groups})
        return [f for f in res["findings"] if f["code"] == "cluster-access"]

    denied = access("bob", ["chem"])
    assert denied and denied[0]["severity"] in ("error", "warning") and "gpu" in denied[0]["message"]
    assert not [f for f in access("alice", ["chem"]) if f["severity"] in ("error", "warning")]
    assert not [f for f in access("carol", ["gpuusers"]) if f["severity"] in ("error", "warning")]
    # No identity sent: the server must not fall back to its own account -> at most "unknown" info.
    assert all(f["severity"] == "info" for f in access(None, None))
