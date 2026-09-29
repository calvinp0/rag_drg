"""bin/rag-drg: finds the real rag-drg in whatever environment it was installed into."""

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

LAUNCHER = Path(__file__).resolve().parents[1] / "bin" / "rag-drg"
pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


def _fake(path: Path, label: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'#!/bin/sh\necho "{label} $* cfg=${{RAG_DRG_CONFIG:-}}"\n')
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    (r / "bin").mkdir(parents=True)
    shutil.copy(LAUNCHER, r / "bin" / "rag-drg")
    (r / "rag_drg.yaml").write_text("sources: []\n")
    return r


def _run(repo, env=None, args=("lint",)):
    base = {"PATH": "/usr/bin:/bin", "HOME": str(repo)}
    base.update(env or {})
    return subprocess.run([str(repo / "bin" / "rag-drg"), *args], text=True, capture_output=True, env=base)


def test_uses_the_repo_venv_and_sets_the_config(repo):
    _fake(repo / ".venv" / "bin" / "rag-drg", "venv")
    r = _run(repo)
    assert r.returncode == 0 and r.stdout.startswith("venv lint")
    assert f"cfg={repo / 'rag_drg.yaml'}" in r.stdout
    # an explicitly set config is left alone
    r = _run(repo, {"RAG_DRG_CONFIG": "/elsewhere.yaml"})
    assert "cfg=/elsewhere.yaml" in r.stdout


def test_explicit_bin_wins_and_must_exist(repo, tmp_path):
    _fake(repo / ".venv" / "bin" / "rag-drg", "venv")
    conda = _fake(tmp_path / "conda" / "envs" / "rag" / "bin" / "rag-drg", "conda-env")
    r = _run(repo, {"RAG_DRG_BIN": str(conda)}, args=("serve",))
    assert r.returncode == 0 and r.stdout.startswith("conda-env serve")
    r = _run(repo, {"RAG_DRG_BIN": str(tmp_path / "typo")})
    assert r.returncode == 127 and "is not an executable file" in r.stderr and not r.stdout


def test_active_conda_env_then_path(repo, tmp_path):
    prefix = tmp_path / "miniforge3" / "envs" / "rag"
    _fake(prefix / "bin" / "rag-drg", "conda")
    assert _run(repo, {"CONDA_PREFIX": str(prefix)}).stdout.startswith("conda lint")
    other = _fake(tmp_path / "pathbin" / "rag-drg", "onpath")
    # the launcher's own directory on PATH is skipped (no loop), the real one is used
    r = _run(repo, {"PATH": f"{repo / 'bin'}:{other.parent}:/usr/bin:/bin"})
    assert r.returncode == 0 and r.stdout.startswith("onpath lint")


def test_named_conda_env_found_from_another_active_env(repo, tmp_path):
    """Claude Code started from e.g. arc_env still finds an env called rag-drg (no RAG_DRG_BIN)."""
    base = tmp_path / "miniforge3"
    _fake(base / "envs" / "rag-drg" / "bin" / "rag-drg", "named")
    (base / "envs" / "arc_env" / "bin").mkdir(parents=True)  # active env without rag-drg
    env = {"CONDA_EXE": str(base / "bin" / "conda"), "CONDA_PREFIX": str(base / "envs" / "arc_env")}
    r = _run(repo, env, args=("serve",))
    assert r.returncode == 0 and r.stdout.startswith("named serve")
    # the active env still wins when it has rag-drg itself
    _fake(base / "envs" / "arc_env" / "bin" / "rag-drg", "active")
    assert _run(repo, env).stdout.startswith("active lint")


def test_named_conda_env_in_default_install_dir_and_custom_name(repo, tmp_path):
    home = tmp_path / "home"
    _fake(home / "miniconda3" / "envs" / "rag_drg" / "bin" / "rag-drg", "underscore")
    r = _run(repo, {"HOME": str(home)})  # no CONDA_EXE: e.g. a non-login shell
    assert r.returncode == 0 and r.stdout.startswith("underscore lint")
    _fake(home / ".conda" / "envs" / "kb" / "bin" / "rag-drg", "custom")
    r = _run(repo, {"HOME": str(home), "RAG_DRG_CONDA_ENV": "kb"})
    assert r.stdout.startswith("custom lint")


def test_nothing_installed(repo):
    r = _run(repo, {"PATH": f"{repo / 'bin'}:/usr/bin:/bin"})
    assert r.returncode == 127 and "no installation found" in r.stderr


def test_launcher_is_executable_in_git():
    assert os.access(LAUNCHER, os.X_OK)
