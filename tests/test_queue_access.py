"""Queue access rules (servers.yaml `access:`), client identity, live qstat/scontrol parsing,
and the discover-pbs draft."""

import copy
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from rag_drg.tools import servers as srv
from rag_drg.tools._servers import access as acc_mod
from rag_drg.tools._servers import cluster
from rag_drg.tools._servers.live_access import (
    discover_pbs,
    parse_pbsnodes,
    parse_qstat_qf,
    parse_scontrol_partitions,
    pbs_size_gb,
)
from rag_drg.tools._servers.model import validate_data

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "servers.example.yaml"

# PBS Pro 2022 `qstat -Qf` (trimmed); acl_users wraps onto a tab-indented continuation line.
QSTAT_QF = """Queue: workq
    queue_type = Execution
    total_jobs = 12
    state_count = Transit:0 Queued:2 Held:0 Waiting:0 Running:10 Exiting:0 Begun:0
    resources_max.walltime = 72:00:00
    resources_max.ncpus = 48
    resources_max.mem = 250gb
    resources_default.walltime = 01:00:00
    max_user_run = 20
    enabled = True
    started = True

Queue: gpu
    queue_type = Execution
    total_jobs = 3
    acl_group_enable = True
    acl_groups = chem,danagrp
    resources_max.walltime = 48:00:00
    resources_max.ngpus = 4
    resources_max.ncpus = 32
    max_run = [u:PBS_GENERIC=4]
    enabled = True
    started = True

Queue: long
    queue_type = Execution
    acl_user_enable = True
    acl_users = alice,bob,charlie,dave,erin,frank,grace,heidi,ivan,judy,mallory,oscar,pe
\tggy,trent
    resources_max.walltime = 336:00:00
    enabled = True
    started = True

Queue: old
    queue_type = Execution
    resources_max.walltime = 24:00:00
    enabled = False
    started = True

Queue: route_all
    queue_type = Route
    route_destinations = workq,gpu
    enabled = True
    started = True
"""

PBSNODES_A = """n001
     Mom = n001.zeus
     ntype = PBS
     state = free
     pcpus = 48
     resources_available.arch = linux
     resources_available.host = n001
     resources_available.mem = 263842692kb
     resources_available.ncpus = 48
     resources_available.Qlist = workq,long
     resources_assigned.mem = 0kb

n002
     Mom = n002.zeus
     state = job-busy
     resources_available.mem = 527685384kb
     resources_available.ncpus = 32
     resources_available.ngpus = 4
     resources_available.Qlist = gpu
"""

PBSNODES_ASJ = """                                                        mem       ncpus   nmics   ngpus
vnode           state           njobs   run   susp      f/t        f/t     f/t     f/t   jobs
--------------- --------------- ------ ----- ------ ------------ ------- ------- ------- -------
n001            free                 0     0      0  252gb/252gb   48/48     0/0     0/0 --
n002            job-busy             1     1      0  100gb/503gb    0/32     0/0     0/4 1234.zeus
"""

SCONTROL = """PartitionName=cpu
   AllowGroups=ALL AllowAccounts=ALL AllowQos=ALL
   AllocNodes=ALL Default=YES QoS=N/A
   DefaultTime=01:00:00 DisableRootJobs=NO ExclusiveUser=NO GraceTime=0 Hidden=NO
   MaxNodes=UNLIMITED MaxTime=3-00:00:00 MinNodes=0 LLN=NO MaxCPUsPerNode=UNLIMITED
   State=UP TotalCPUs=960 TotalNodes=20 SelectTypeParameters=NONE

PartitionName=gpu
   AllowGroups=gpuusers,admins AllowAccounts=ALL AllowQos=ALL
   MaxTime=2-00:00:00 State=UP

PartitionName=paid
   AllowGroups=ALL AllowAccounts=projx DenyAccounts=(null)
   MaxTime=7-00:00:00 State=UP

PartitionName=maint
   AllowGroups=ALL AllowAccounts=ALL
   MaxTime=1:00:00 State=DOWN
"""


@pytest.fixture
def example(project):
    shutil.copy(EXAMPLE, project.root / "servers.yaml")
    return srv.load_servers(project)["example"]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for v in ("RAG_DRG_CLIENT_USER", "RAG_DRG_CLIENT_GROUPS", "RAG_DRG_SERVER_MODE", "RAG_DRG_SERVER"):
        monkeypatch.delenv(v, raising=False)


# ----------------------------------------------------------------- static rules

def test_example_access_rule_parsed(example):
    gpu = example.partitions["gpu"]
    assert gpu.access.users == ["alice"] and gpu.access.groups == ["gpuusers"]
    assert example.partitions["cpu"].access is None


@pytest.mark.parametrize("access, expected", [
    ("everyone", "must be a mapping"),
    ({"users": "alice"}, "must be a list"),
    ({"users": ["al ice"]}, "not a valid Unix user"),
    ({"groups": ["chem;rm"]}, "not a valid Unix group"),
    ({"users": [], "groups": []}, "lists no users or groups"),
    ({"users": ["a"], "members": ["b"]}, "unknown key"),
])
def test_access_validation(access, expected):
    raw = yaml.safe_load(EXAMPLE.read_text())
    raw["servers"]["example"]["partitions"]["gpu"]["access"] = access
    assert any(expected in p for p in validate_data(raw)), validate_data(raw)


@pytest.mark.parametrize("user, groups, allowed", [
    ("alice", None, True),              # listed user
    ("bob", ["gpuusers"], True),        # member of a listed group
    ("bob", ["chem", "users"], False),  # neither
    ("bob", None, None),                # groups unknown -> could still be a member
    (None, ["chem"], None),             # user unknown -> could still be listed
    (None, ["gpuusers"], True),
    (None, None, None),
])
def test_queue_access(example, user, groups, allowed):
    r = srv.queue_access(example, "gpu", user, groups)
    assert r["allowed"] is allowed, r
    assert r["partition"] == "gpu"
    if allowed is False:
        assert "ask the PI" in r["reason"]


def test_queue_access_open_unknown_and_local(example, monkeypatch):
    assert srv.queue_access(example, "cpu")["allowed"] is True
    assert srv.queue_access(example, None)["allowed"] is True  # default partition
    assert srv.queue_access(example, "bigmem")["allowed"] is False
    monkeypatch.setattr(acc_mod, "local_identity", lambda: ("carol", ["gpuusers"]))
    assert srv.queue_access(example, "gpu")["allowed"] is None  # never implicit
    assert srv.queue_access(example, "gpu", use_local_identity=True)["allowed"] is True


def test_group_only_rule_denies_when_groups_known(example):
    example.partitions["gpu"].access.users = []
    assert srv.queue_access(example, "gpu", None, ["chem"])["allowed"] is False


def test_check_resources_and_submit_access(example):
    errs = srv.check_resources(example, "gpu", 8, 64, "1:00:00", gpus=1, user="bob", groups=["chem"])
    assert [e["severity"] for e in errs] == ["error"] and "restricted" in errs[0]["message"]
    assert srv.check_resources(example, "gpu", 8, 64, "1:00:00", gpus=1, user="bob", groups=["chem"],
                               check_access=False) == []
    with pytest.raises(srv.SubmitError) as e:
        srv.render_submit_script(example, "gaussian-16-gpu", "a.gjf", gpus=1, partition="gpu",
                                 user="bob", groups=["chem"])
    assert "restricted" in str(e.value)
    # default partition: the only allowed one (gpu) is denied -> clear error
    with pytest.raises(srv.SubmitError):
        srv.render_submit_script(example, "gaussian-16-gpu", "a.gjf", gpus=1, user="bob", groups=["chem"])
    script, notes = srv.render_submit_script(example, "gaussian-16-gpu", "a.gjf", gpus=1, user="alice")
    assert "#SBATCH --partition=gpu" in script


def test_submit_default_skips_denied_partition(example):
    ex = copy.deepcopy(example)
    ex.partitions["cpu"].access = srv.Access(users=["alice"])
    ex.software["psi4"].partitions = []
    script, notes = srv.render_submit_script(ex, "psi4", "a.in", user="bob", groups=["gpuusers"], gpus=1)
    assert "#SBATCH --partition=gpu" in script and "skipped cpu" in notes[1]
    script, _ = srv.render_submit_script(ex, "psi4", "a.in", cores=8, mem_gb=16)  # no identity: plain default
    assert "#SBATCH --partition=cpu" in script


def test_list_and_card_show_access(example):
    text = srv._list_text({"example": example})
    assert "restricted: gpu -> only users alice; groups gpuusers (ask the PI" in text
    card = srv.render_card(example)
    assert "| users alice; groups gpuusers |" in card and "| everyone |" in card
    assert srv._summary({"example": example})[0]["partitions"]["gpu"]["access"]["groups"] == ["gpuusers"]


# ----------------------------------------------------------------- identity contract

def test_client_identity_order(monkeypatch):
    monkeypatch.setattr(acc_mod, "local_identity", lambda: ("svc", ["svc"]))
    assert srv.client_identity(with_source=True) == ("svc", ["svc"], "local")
    monkeypatch.setenv("RAG_DRG_SERVER_MODE", "1")
    assert srv.local_identity_allowed() is False
    assert srv.client_identity(with_source=True) == (None, None, None)
    monkeypatch.setenv("RAG_DRG_CLIENT_USER", "alice")
    monkeypatch.setenv("RAG_DRG_CLIENT_GROUPS", "chem, danagrp,,bad;name")
    assert srv.client_identity(with_source=True) == ("alice", ["chem", "danagrp"], "env")
    with srv.client_identity_scope("bob", "gpuusers"):
        assert srv.client_identity(with_source=True) == ("bob", ["gpuusers"], "context")
        assert srv.client_identity(("carol", None)) == ("carol", None)
    assert srv.client_identity() == ("alice", ["chem", "danagrp"])
    monkeypatch.setenv("RAG_DRG_SERVER_MODE", "false")
    assert srv.local_identity_allowed() is True


def test_access_problem_severity(example, monkeypatch):
    monkeypatch.setattr(acc_mod, "local_identity", lambda: ("bob", ["users"]))
    # local identity, cluster elsewhere -> warning, not a blocking error
    assert srv.access_problem(example, "gpu")["severity"] == "warning"
    monkeypatch.setattr(cluster.socket, "gethostname", lambda: "login.example.org")
    assert srv.access_problem(example, "gpu")["severity"] == "error"  # we are on the cluster
    monkeypatch.setenv("RAG_DRG_SERVER_MODE", "1")
    assert srv.access_problem(example, "gpu")["severity"] == "info"
    with srv.client_identity_scope("alice", None):
        assert srv.access_problem(example, "gpu") is None
    assert srv.access_problem(example, "cpu") is None


def test_cluster_limits_bridge_uses_identity(project, monkeypatch):
    from rag_drg.tools.inputcheck import check_input

    shutil.copy(EXAMPLE, project.root / "servers.yaml")
    script = ("#!/bin/bash\n#SBATCH --partition=gpu\n#SBATCH --ntasks=1\n#SBATCH --cpus-per-task=8\n"
              "#SBATCH --mem=64G\n#SBATCH --gres=gpu:1\n#SBATCH --time=10:00:00\n"
              "/opt/gaussian/g16-C.02-gpu/g16/g16 < a.gjf > a.log\n")
    inp = "%nprocshared=8\n%mem=56GB\n#p b3lyp/6-31g(d) opt\n\ntitle\n\n0 1\nH 0 0 0\nH 0 0 0.74\n\n"
    monkeypatch.setenv("RAG_DRG_SERVER_MODE", "1")

    def access_findings():
        found = check_input(content=inp, filename="a.gjf", submit_content=script, cfg=project)
        return [f for f in found if f.code == "cluster-access"]

    assert [f.severity for f in access_findings()] == ["info"]
    with srv.client_identity_scope("bob", ["chem"]):
        f = access_findings()
        assert [x.severity for x in f] == ["error"] and "gpuusers" in f[0].message
    monkeypatch.setenv("RAG_DRG_CLIENT_USER", "alice")
    assert access_findings() == []


def test_cli_access_and_check(project, example, monkeypatch, capsys):
    from rag_drg.cli import main

    cfg = str(project.root / "rag_drg.yaml")
    monkeypatch.setenv("RAG_DRG_SERVER_MODE", "1")
    assert main(["-c", cfg, "servers", "access", "example", "--user", "bob", "--groups", "chem"]) == 0
    out = capsys.readouterr().out
    assert "cpu: yes" in out and "gpu: NO" in out and "identity (given): user bob, groups chem" in out
    assert main(["-c", cfg, "servers", "access", "example"]) == 0
    assert "gpu: unknown" in capsys.readouterr().out
    assert main(["-c", cfg, "servers", "check", "example", "gpu", "--cores", "8", "--mem", "64", "--time",
                 "1:00:00", "--gpus", "1", "--user", "bob", "--groups", "chem"]) == 1
    assert main(["-c", cfg, "servers", "access", "example", "--live"]) == 1  # live commands disabled


def test_mcp_queue_access(project, example, monkeypatch):
    import json

    from tests.test_servers import FakeMCP, _ctx

    monkeypatch.setenv("RAG_DRG_SERVER_MODE", "1")
    mcp = FakeMCP()
    srv.register_mcp(mcp, _ctx(project))
    rows = json.loads(mcp.tools["queue_access"]("example", None, "bob", ["gpuusers"]))
    assert [(r["partition"], r["allowed"]) for r in rows] == [("cpu", True), ("gpu", True)]
    rows = json.loads(mcp.tools["queue_access"]("example", "gpu"))
    assert rows[0]["allowed"] is None
    res = json.loads(mcp.tools["check_resources"]("example", "gpu", 8, 64, "1:00:00", 1, None, "bob", ["x"]))
    assert res[0]["severity"] == "error"
    assert "Cannot render" in mcp.tools["render_submit_script"]("example", "gaussian-16-gpu", "a.gjf", gpus=1,
                                                                user="bob", groups=["x"])


# ----------------------------------------------------------------- live: PBS

def test_parse_qstat_qf_continuation_and_types():
    q = parse_qstat_qf(QSTAT_QF)
    assert list(q) == ["workq", "gpu", "long", "old", "route_all"]
    assert q["gpu"]["acl_groups"] == "chem,danagrp"
    assert q["long"]["acl_users"].endswith(",peggy,trent")
    assert q["workq"]["resources_max.mem"] == "250gb"
    assert pbs_size_gb("263842692kb") == pytest.approx(251.6, abs=0.1)
    assert pbs_size_gb("4tb") == 4096 and pbs_size_gb("1073741824") == 1


class FakeRun:
    def __init__(self, outputs):
        self.outputs, self.calls = outputs, []

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        cmd = argv[-1] if argv[0] == "ssh" else " ".join(argv)
        for key, out in self.outputs.items():
            if cmd.startswith(key):
                return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="command not found")


def _pbs(example):
    s = copy.deepcopy(example)
    s.scheduler, s.commands = "pbspro", {}
    return s


def test_live_queue_access_pbs(example, monkeypatch):
    fake = FakeRun({"id -un": "peggy\n", "id -Gn": "users danagrp\n", "qstat -Qf": QSTAT_QF})
    monkeypatch.setattr(subprocess, "run", fake)
    res = srv.live_queue_access(_pbs(example))
    assert res["user"] == "peggy" and res["groups"] == ["users", "danagrp"]
    by = {q["queue"]: q for q in res["queues"]}
    assert by["workq"]["usable"] is True and by["workq"]["limits"]["max_walltime"] == "72:00:00"
    assert by["workq"]["limits"]["max_user_run"] == "20"
    assert by["gpu"]["usable"] is True and "group_list=danagrp" in by["gpu"]["why"]
    assert by["gpu"]["limits"]["ngpus"] == "4" and by["gpu"]["limits"]["max_run"] == "[u:PBS_GENERIC=4]"
    assert by["long"]["usable"] is True  # 'peggy' was split across the continuation line
    assert by["old"]["usable"] is False and "disabled" in by["old"]["why"]
    assert [c[-1] for c in fake.calls] == ["id -un", "id -Gn", "qstat -Qf"]
    assert all(c[0] == "ssh" and "BatchMode=yes" in c for c in fake.calls)

    fake2 = FakeRun({"id -un": "zed\n", "id -Gn": "users\n", "qstat -Qf": QSTAT_QF})
    monkeypatch.setattr(subprocess, "run", fake2)
    report = srv.cluster_query(_pbs(example), "queue_access")
    assert "gpu: usable=NO" in report and "long: usable=NO" in report and "workq: usable=yes" in report
    assert "workq: usable=yes" in report and "servers.yaml: not in servers.yaml" in report


def test_live_queue_access_unknown_groups_and_torque(example, monkeypatch):
    monkeypatch.setattr(subprocess, "run", FakeRun({"id -un": "zed\n", "qstat -Qf": QSTAT_QF}))
    res = srv.live_queue_access(_pbs(example))
    by = {q["queue"]: q for q in res["queues"]}
    assert res["groups"] is None and by["gpu"]["usable"] is None and res["errors"]
    torque = _pbs(example)
    torque.scheduler = "torque"
    assert srv.live_queue_access(torque)["supported"] is False


# ----------------------------------------------------------------- live: Slurm

def test_live_queue_access_slurm(example, monkeypatch):
    parts = parse_scontrol_partitions(SCONTROL)
    assert parts["gpu"]["AllowGroups"] == "gpuusers,admins" and parts["cpu"]["MaxTime"] == "3-00:00:00"
    fake = FakeRun({"id -un": "bob\n", "id -Gn": "users gpuusers\n", "scontrol show partition": SCONTROL,
                    "sacctmgr show assoc": "chemgrp|\n"})
    monkeypatch.setattr(subprocess, "run", fake)
    res = srv.live_queue_access(example)
    by = {q["queue"]: q for q in res["queues"]}
    assert res["accounts"] == ["chemgrp"]
    assert by["cpu"]["usable"] is True and by["cpu"]["limits"]["max_walltime"] == "3-00:00:00"
    assert "max_nodes" not in by["cpu"]["limits"]  # UNLIMITED dropped
    assert by["gpu"]["usable"] is True
    assert by["paid"]["usable"] is False and "chemgrp" in by["paid"]["why"]
    assert by["maint"]["usable"] is False
    assert fake.calls[-1][-1] == 'sacctmgr show assoc user="$USER" format=Account,Partition -P -n'


def test_new_allowlist_entries():
    assert cluster.command_problems("id -Gn") == []
    assert cluster.command_problems("id -un") == []
    assert cluster.command_problems("id -Gn root")
    assert cluster.command_problems("sacctmgr show assoc user=$USER") == []
    assert cluster.command_problems("sacctmgr -i delete user bob")
    raw = yaml.safe_load(EXAMPLE.read_text())
    raw["servers"]["example"]["commands"]["queue_access"] = "qstat -Qf"
    assert any("unknown query" in p for p in validate_data(raw))


# ----------------------------------------------------------------- discover-pbs

def test_parse_pbsnodes_formats():
    long = parse_pbsnodes(PBSNODES_A)
    assert long["n001"]["ncpus"] == 48 and long["n001"]["queues"] == ["workq", "long"]
    assert long["n002"]["ngpus"] == 4 and int(long["n002"]["mem_gb"]) == 503
    table = parse_pbsnodes(PBSNODES_ASJ)
    assert table["n001"] == {"mem_gb": 252.0, "ncpus": 48, "ngpus": 0, "queues": []}
    assert table["n002"]["ncpus"] == 32 and table["n002"]["ngpus"] == 4


def test_discover_pbs_draft_validates(tmp_path, project, capsys):
    draft = discover_pbs(QSTAT_QF, PBSNODES_A)
    assert draft.startswith("# DRAFT") and "REVIEW" in draft
    assert "route_all" in draft.split("partitions:")[1].splitlines()[-1]  # skipped route queue listed
    block = yaml.safe_load(draft)["partitions"]
    assert block["workq"] == {"max_walltime": "72:00:00", "cores_per_node": 48, "mem_per_node_gb": 251,
                              "gpus_per_node": 0, "notes": "max_user_run=20"}
    assert block["gpu"]["gpus_per_node"] == 4 and block["gpu"]["cores_per_node"] == 32
    assert block["gpu"]["access"]["groups"] == ["chem", "danagrp"]
    assert "peggy" in block["long"]["access"]["users"]
    raw = yaml.safe_load(EXAMPLE.read_text())
    raw["servers"]["example"]["partitions"] = {k: v for k, v in block.items()}
    for sw in raw["servers"]["example"]["software"].values():
        sw.pop("partitions", None)
    assert validate_data(raw) == []

    # without pbsnodes: per-job queue maxima, TODO where nothing is known
    bare = yaml.safe_load(discover_pbs(QSTAT_QF))["partitions"]
    assert bare["workq"]["cores_per_node"] == 48 and bare["workq"]["mem_per_node_gb"] == 250
    assert bare["long"]["cores_per_node"] is None and "TODO" in discover_pbs(QSTAT_QF)

    from rag_drg.cli import main

    (tmp_path / "qf.txt").write_text(QSTAT_QF)
    (tmp_path / "nodes.txt").write_text(PBSNODES_ASJ)
    assert main(["-c", str(project.root / "rag_drg.yaml"), "servers", "discover-pbs", "--from-file",
                 str(tmp_path / "qf.txt"), "--pbsnodes", str(tmp_path / "nodes.txt")]) == 0
    assert "partitions:" in capsys.readouterr().out
