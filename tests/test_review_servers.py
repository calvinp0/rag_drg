"""Regression tests for the servers-registry review fixes: unquoted walltimes, discover-pbs wildcards,
PBS group ACLs, ARC submit_scripts stubs, Psi4 memory units, executable quoting and cleanup traps."""

import copy
import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest
import yaml

from rag_drg.tools import servers as srv
from rag_drg.tools._servers.live_access import discover_pbs, pbs_queue_report
from rag_drg.tools._servers.model import (
    SoftwareInstall,
    check_file,
    load_servers_file,
    load_yaml_text,
    validate_data,
)
from rag_drg.tools._servers.submit import input_lines, render_arc_template

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "servers.example.yaml"
BASH = shutil.which("bash")
ALL_KEYS = ["orca-5", "orca-6", "gaussian-09", "gaussian-16", "gaussian-16-gpu", "qchem-6.1", "psi4",
            "molpro-2024", "molpro-2026", "pyscf"]
INPUTS = {"orca": "job.inp", "gaussian": "job.gjf", "qchem": "job.in", "molpro": "job.in",
          "psi4": "job.in", "pyscf": "job.py"}


@pytest.fixture
def example():
    return load_servers_file(EXAMPLE)["example"]


def _flavour(server, sched):
    s = copy.deepcopy(server)
    s.scheduler = sched
    return s


# ----------------------------------------------------------------- 1. unquoted walltimes

WALL_YAML = textwrap.dedent("""\
    servers:
      zeus:
        scheduler: pbs
        host: zeus.example.org
        partitions:
          unquoted: {{max_walltime: {w1}, cores_per_node: 8, mem_per_node_gb: 32, default: true}}
          short: {{max_walltime: {w2}, cores_per_node: 8, mem_per_node_gb: 32}}
          quoted: {{max_walltime: "72:00:00", cores_per_node: 8, mem_per_node_gb: 32}}
          days: {{max_walltime: 3-00:00:00, cores_per_node: 8, mem_per_node_gb: 32}}
        software:
          orca: {{ess: orca, executable: /opt/orca/orca, parallel: mpi}}
    """)


def test_unquoted_walltimes_are_not_base60(tmp_path):
    path = tmp_path / "servers.yaml"
    path.write_text(WALL_YAML.format(w1="72:00:00", w2="1:30"))
    assert yaml.safe_load(path.read_text())["servers"]["zeus"]["partitions"]["unquoted"]["max_walltime"] == 259200
    assert check_file(path) == []
    parts = load_servers_file(path)["zeus"].partitions
    assert parts["unquoted"].max_walltime == "72:00:00"
    assert parts["unquoted"].max_walltime_seconds == 72 * 3600
    assert parts["short"].max_walltime_seconds == 90 * 60  # H:MM
    assert parts["quoted"].max_walltime_seconds == 72 * 3600
    assert parts["days"].max_walltime_seconds == 72 * 3600
    # other numbers still parse as numbers
    assert load_yaml_text("a: 12\nb: 1.5\nc: 0x10\nd: 1_000\ne: '7'") == {"a": 12, "b": 1.5, "c": 16, "d": 1000,
                                                                           "e": "7"}


def test_implausible_numeric_walltime_is_flagged():
    raw = load_yaml_text(WALL_YAML.format(w1="259200", w2="24"))
    problems = validate_data(raw)
    assert len(problems) == 1 and "unquoted.max_walltime" in problems[0] and "Quote it" in problems[0]
    raw["servers"]["zeus"]["partitions"]["unquoted"]["max_walltime"] = 48  # 48 hours is plausible
    assert validate_data(raw) == []


# ----------------------------------------------------------------- 3. discover-pbs wildcards

QF_WILDCARD = """Queue: open
    queue_type = Execution
    acl_user_enable = True
    acl_users = *
    resources_max.walltime = 24:00:00
    resources_max.ncpus = 16
    resources_max.mem = 64gb
    enabled = True
    started = True

Queue: mixed
    queue_type = Execution
    acl_user_enable = True
    acl_users = +alice@zeus,*@zeus,-mallory
    acl_group_enable = True
    acl_groups = *
    resources_max.walltime = 48:00:00
    resources_max.ncpus = 16
    resources_max.mem = 64gb
    enabled = True
    started = True
"""


def test_discover_pbs_drops_wildcards_and_round_trips():
    draft = discover_pbs(QF_WILDCARD)
    assert "[*]" not in draft
    block = yaml.safe_load(draft)["partitions"]
    assert "access" not in block["open"]
    assert block["mixed"]["access"]["users"] == ["alice"] and "groups" not in block["mixed"]["access"]
    assert "left out" in draft and "*@zeus" in draft
    raw = yaml.safe_load(EXAMPLE.read_text())
    block["open"]["default"] = True
    raw["servers"]["example"]["partitions"] = block
    for sw in raw["servers"]["example"]["software"].values():
        sw.pop("partitions", None)
    assert validate_data(raw) == []


# ----------------------------------------------------------------- 4. PBS group ACL, one group at a time

def test_pbs_group_acl_admits_any_single_group():
    attrs = {"acl_group_enable": "True", "acl_groups": "-badgrp,+danagrp"}
    r = pbs_queue_report("q", attrs, "bob", ["badgrp", "danagrp"])
    assert r["usable"] is True and "group_list=danagrp" in r["why"]
    r = pbs_queue_report("q", attrs, "bob", ["danagrp", "badgrp"])
    assert r["usable"] is True and "group_list" not in r["why"] and "primary group danagrp" in r["why"]
    r = pbs_queue_report("q", attrs, "bob", ["badgrp"])
    assert r["usable"] is False


# ----------------------------------------------------------------- 5. ARC settings

ARC_PARAMS = {"name": "a1234", "un": "bob", "queue": "cpu", "t_max": "1-0:00:00", "memory": 2048, "cpus": 8}


@pytest.mark.skipif(BASH is None, reason="needs bash")
def test_arc_settings_has_submit_scripts(example):
    code = srv.arc_settings({"example": example})
    assert "REQUIRED" in code and "submit_scripts[server][job_adapter]" in code
    ns: dict = {}
    exec(code, ns)
    assert list(ns["servers"]["example"]["queues"]) == ["cpu"]
    assert "queue 'gpu' left out" in code and "restricted" in code and "GPU partition" in code
    scripts = ns["submit_scripts"]["example"]
    assert set(scripts) == {"gaussian", "orca", "molpro", "qchem"}
    assert "g16-C.02/g16/g16" in scripts["gaussian"] and "gpu" not in scripts["gaussian"]  # newest non-GPU build
    assert "orca_6_0_1" in scripts["orca"]
    for ess, tmpl in scripts.items():
        text = tmpl.format(**ARC_PARAMS)  # what ARC's write_submit_script does
        assert "#SBATCH --partition=cpu" in text and "#SBATCH --time=1-0:00:00" in text
        assert "#SBATCH --mem-per-cpu=2048" in text and "#SBATCH -o out.txt" in text
        assert 'JOBID="$SLURM_JOB_ID"' in text and "trap cleanup EXIT" in text
        subprocess.run([BASH, "-n"], input=text, text=True, check=True)
    assert '"$ORCA_BIN" "$INPUT" > "$WORKDIR/input.log"' in scripts["orca"].format(**ARC_PARAMS)
    assert '"$QCHEM" -nt 8 "input.in" "output.out"' in scripts["qchem"].format(**ARC_PARAMS)


@pytest.mark.skipif(BASH is None, reason="needs bash")
def test_arc_template_pbs(example):
    text = render_arc_template(_flavour(example, "pbspro"), "orca-6").format(**ARC_PARAMS)
    assert "#PBS -l select=1:ncpus=8:mpiprocs=8:mem=2048mb" in text and "#PBS -l walltime=1-0:00:00" in text
    assert 'JOBID="${PBS_JOBID%%.*}"' in text
    subprocess.run([BASH, "-n"], input=text, text=True, check=True)


# ----------------------------------------------------------------- 6. Psi4 memory units

def test_psi4_memory_in_mb_for_small_requests(example):
    sw = example.software["psi4"]
    assert 'psi4.set_memory("901 MB")' in input_lines(sw, 2, 1, 0, "a.py")
    assert "memory 450 MB" in input_lines(sw, 1, 0.5, 0, "a.in")


# ----------------------------------------------------------------- 7. executable quoting / validation

def test_executable_with_shell_metacharacters_is_rejected():
    raw = yaml.safe_load(EXAMPLE.read_text())
    raw["servers"]["example"]["software"]["orca-6"]["executable"] = "/opt/my orca/orca;rm"
    assert any("shell metacharacters" in p for p in validate_data(raw))


def test_executable_is_shell_quoted(example):
    s = copy.deepcopy(example)
    s.software["orca-6"] = SoftwareInstall(key="orca-6", ess="orca", executable="/opt/my orca/orca", parallel="mpi")
    script, _ = srv.render_submit_script(s, "orca-6", "a.inp", cores=4, mem_gb=8)
    assert "ORCA_BIN='/opt/my orca/orca'" in script


# ----------------------------------------------------------------- 8. cleanup trap

@pytest.mark.skipif(BASH is None, reason="needs bash")
@pytest.mark.parametrize("sched", ["slurm", "pbs", "torque", "local"])
def test_rendered_scripts_have_trap_and_parse(example, sched):
    server = _flavour(example, sched)
    for key in ALL_KEYS:
        sw = server.software[key]
        gpus = 1 if key.endswith("-gpu") else 0
        script, _ = srv.render_submit_script(server, key, INPUTS[sw.ess], cores=4, mem_gb=8, gpus=gpus)
        assert "trap cleanup EXIT" in script and "trap 'exit 143' TERM INT" in script
        assert 'rm -rf "$SCRATCH"' in script.split("cleanup() {")[1].split("}")[0]
        subprocess.run([BASH, "-n"], input=script, text=True, check=True)


@pytest.mark.skipif(BASH is None or os.name != "posix", reason="needs bash")
def test_trap_copies_back_and_cleans_scratch_after_sigterm(example, tmp_path):
    """A walltime kill (SIGTERM) still copies ORCA's .gbw back and removes the scratch directory."""
    fake = tmp_path / "bin" / "orca"
    fake.parent.mkdir()
    # writes restart files into the (scratch) cwd, then the job gets killed like at the walltime
    fake.write_text("#!/bin/bash\necho gbw > job.gbw\necho xyz > job.xyz\nkill -TERM $PPID\nsleep 0.2\n")
    fake.chmod(0o755)
    s = _flavour(example, "local")
    s.scratch.path = str(tmp_path / "scratch")
    s.software["orca-6"] = SoftwareInstall(key="orca-6", ess="orca", executable=str(fake), parallel="mpi")
    script, _ = srv.render_submit_script(s, "orca-6", "job.inp", cores=2, mem_gb=2)
    work = tmp_path / "work"
    work.mkdir()
    (work / "job.inp").write_text("! HF\n")
    (work / "run.sh").write_text(script)
    proc = subprocess.run([BASH, "run.sh"], cwd=work, capture_output=True, text=True, timeout=30)
    assert proc.returncode == 143, proc.stderr
    assert (work / "job.gbw").read_text() == "gbw\n" and (work / "job.xyz").is_file()
    assert (tmp_path / "scratch").is_dir() and not any((tmp_path / "scratch").iterdir())
