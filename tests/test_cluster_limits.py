import shutil
from pathlib import Path

import pytest

pytest.importorskip("rag_drg.tools.servers")
from rag_drg.tools.inputcheck import check_input  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
VALID = ROOT / "tests" / "fixtures" / "inputs" / "valid"

SCRIPT = """#!/bin/bash
#SBATCH --job-name=ts
#SBATCH --partition=cpu
#SBATCH --nodes=1
#SBATCH --ntasks=16
#SBATCH --cpus-per-task=1
#SBATCH --mem=64G
#SBATCH --time={time}
{exe} orca_ts.inp > orca_ts.out
"""


@pytest.fixture
def cluster_project(project):
    shutil.copy(ROOT / "servers.example.yaml", project.root / "servers.yaml")
    return project


def _codes(findings):
    return {f.code: f for f in findings}


def test_submit_script_within_limits_passes(cluster_project, monkeypatch):
    monkeypatch.delenv("RAG_DRG_SERVER", raising=False)
    inp = (VALID / "orca_ts.inp").read_text()
    script = SCRIPT.format(time="24:00:00", exe="/opt/orca/orca_6_0_1/orca")
    found = _codes(check_input(content=inp, filename="orca_ts.inp", submit_content=script, cfg=cluster_project))
    assert "cluster-limits" not in found or found["cluster-limits"].severity != "error"
    assert "cluster-executable" not in found


def test_walltime_over_partition_max_and_unregistered_orca(cluster_project, monkeypatch):
    monkeypatch.delenv("RAG_DRG_SERVER", raising=False)
    inp = (VALID / "orca_ts.inp").read_text()
    script = SCRIPT.format(time="30-00:00:00", exe="/home/someone/orca_4_2/orca")
    found = check_input(content=inp, filename="orca_ts.inp", submit_content=script, cfg=cluster_project)
    limits = [f for f in found if f.code == "cluster-limits" and f.severity == "error"]
    assert limits and "walltime" in limits[0].message
    exe = [f for f in found if f.code == "cluster-executable"]
    assert exe and "orca_4_2" in exe[0].message


def test_no_servers_file_means_no_cluster_findings(project):
    inp = (VALID / "orca_ts.inp").read_text()
    script = SCRIPT.format(time="30-00:00:00", exe="/home/someone/orca_4_2/orca")
    found = check_input(content=inp, filename="orca_ts.inp", submit_content=script, cfg=project)
    assert not [f for f in found if f.code.startswith("cluster-")]

