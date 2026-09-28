"""ARC run on a cluster: servers.yaml `arc.runner` / `arc.ess_queues`, the runner script, per-user
paths, 'local' ARC settings, and compose_arc_run cross-checks."""

import ast
import copy
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from rag_drg.tools import servers as srv
from rag_drg.tools._servers.arc_runner import resolve_arc_user_env
from rag_drg.tools._servers.model import _parse_server, load_yaml_text, validate_data
from rag_drg.tools.compose_arc import compose_arc_run

EXAMPLE = Path(__file__).resolve().parent.parent / "servers.example.yaml"
INPUT = """project: ethanol_demo
job_memory: 14
max_job_time: 2
ess_settings:
  gaussian: local
  orca: local
species:
  - label: ethanol
    smiles: CCO
"""
MINE = {"arc_path": "/home/me/Code/ARC", "conda_sh": "/home/me/miniforge3/etc/profile.d/conda.sh"}


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    """No user config file / conda / ARC_PATH from the machine running the tests."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "home" / ".config"))
    for var in ("ARC_PATH", "arc_path", "CONDA_EXE", "CONDA_PREFIX", "RAG_DRG_SERVER_MODE"):
        monkeypatch.delenv(var, raising=False)


def _raw():
    return load_yaml_text(EXAMPLE.read_text())


def _servers(raw):
    assert validate_data(raw) == []
    return {n: _parse_server(n, s) for n, s in raw["servers"].items()}


@pytest.fixture
def pbs():
    return _servers(_raw())["example-pbs"]


def _with_scheduler(server, sched):
    s = copy.deepcopy(server)
    s.scheduler = sched
    return s


def _bash_n(script: str):
    if shutil.which("bash") is None:
        pytest.skip("bash not available")
    subprocess.run(["bash", "-n"], input=script, text=True, check=True)


# ----------------------------------------------------------------- servers.yaml validation

def test_example_runner_parsed(pbs):
    r = pbs.arc_runner
    assert r.queue == "group_q" and r.host == "node01" and r.cores == 1 and r.mem_gb == 8
    assert r.host_cores == 32 and r.host_mem_gb == 192
    assert pbs.arc["ess_queues"] == ["group_q", "long_q", "short_q"]
    assert _servers(_raw())["example"].arc_runner is None


@pytest.mark.parametrize("mutate, expected", [
    (lambda r: r.update(queue="nope"), "unknown partition 'nope'"),
    (lambda r: r.pop("queue"), "queue: required"),
    (lambda r: r.update(host="n170;rm -rf"), "not a node name"),
    (lambda r: r.update(cores=64), "64 > 32 cores on node node01"),
    (lambda r: r.update(host_cores=4, cores=8), "8 > 4 cores on node node01"),
    (lambda r: r.update(cores=0), "cores: must be a positive integer"),
    (lambda r: r.update(mem_gb=500), "500 GB > 192 GB on node node01"),
    (lambda r: (r.pop("host"), r.pop("host_cores"), r.pop("host_mem_gb"), r.update(mem_gb=500)),
     "500 GB > 192 GB per node of group_q"),
    (lambda r: r.pop("host"), "host_cores: only meaningful with `host`"),
    (lambda r: r.update(walltime="4000:00:00"), "> max 3600:00:00"),
    (lambda r: r.update(walltime="soon"), "walltime"),
    (lambda r: r.update(extra_setup="module load x"), "extra_setup"),
    (lambda r: r.update(arc_path="/home/me/ARC"), "arc_path: per-user setting"),
    (lambda r: r.update(conda_env="arc_env"), "conda_env: per-user setting"),
    (lambda r: r.update(conda_sh="/x/conda.sh"), "conda_sh: per-user setting"),
    (lambda r: r.update(typo=1), "unknown key 'typo'"),
])
def test_runner_validation(mutate, expected):
    raw = _raw()
    mutate(raw["servers"]["example-pbs"]["arc"]["runner"])
    problems = validate_data(raw)
    assert any(expected in p for p in problems), problems


@pytest.mark.parametrize("value, expected", [
    (["gpu_q"], "unknown partition 'gpu_q'"),
    (["group_q", "group_q"], "twice"),
    ([], "non-empty list"),
    ("group_q", "non-empty list"),
])
def test_ess_queues_validation(value, expected):
    raw = _raw()
    raw["servers"]["example-pbs"]["arc"]["ess_queues"] = value
    assert any(expected in p for p in validate_data(raw))


def test_runner_needs_batch_scheduler():
    raw = _raw()
    raw["servers"]["example-pbs"]["scheduler"] = "local"
    assert any("needs a batch scheduler" in p for p in validate_data(raw))


# ----------------------------------------------------------------- per-user paths

def test_user_env_resolution_order(tmp_path, monkeypatch):
    conf = tmp_path / "home" / ".config" / "rag-drg" / "user.yaml"
    monkeypatch.setenv("ARC_PATH", "/env/ARC")
    monkeypatch.setenv("CONDA_EXE", "/env/miniforge3/bin/conda")
    u = resolve_arc_user_env()
    assert (u.arc_path, u.conda_sh, u.conda_env) == ("/env/ARC", "/env/miniforge3/etc/profile.d/conda.sh", "arc_env")
    assert u.sources["arc_path"] == "$ARC_PATH" and u.sources["conda_sh"] == "$CONDA_EXE"
    conf.parent.mkdir(parents=True)
    conf.write_text("arc_path: /cfg/ARC\nconda_env: arc_env_dev\nconda_sh: /cfg/conda.sh\n")
    u = resolve_arc_user_env()
    assert (u.arc_path, u.conda_sh, u.conda_env) == ("/cfg/ARC", "/cfg/conda.sh", "arc_env_dev")
    u = resolve_arc_user_env(arc_path="/given/ARC")
    assert u.arc_path == "/given/ARC" and u.sources["arc_path"] == "given" and u.conda_env == "arc_env_dev"
    # the shared server reads neither the config file nor the environment
    monkeypatch.setenv("RAG_DRG_SERVER_MODE", "1")
    u = resolve_arc_user_env()
    assert (u.arc_path, u.conda_sh, u.conda_env) == (None, None, "arc_env")


def test_user_env_conda_prefix_and_bad_values(monkeypatch):
    monkeypatch.setenv("CONDA_PREFIX", "/opt/mf3/envs/other")
    assert resolve_arc_user_env().conda_sh == "/opt/mf3/etc/profile.d/conda.sh"
    monkeypatch.setenv("ARC_PATH", "/my ARC")
    u = resolve_arc_user_env()
    assert u.arc_path is None and any("ignored $ARC_PATH" in n for n in u.notes)
    with pytest.raises(srv.SubmitError, match="arc_path"):
        resolve_arc_user_env(arc_path="relative/ARC")
    with pytest.raises(srv.SubmitError, match="conda_env"):
        resolve_arc_user_env(conda_env="arc env")


def test_unresolved_paths_fail_loudly_in_the_script(pbs, tmp_path):
    script, notes = srv.render_arc_runner_script(pbs)
    assert 'ARC_PATH="${ARC_PATH:-${arc_path:?set ARC_PATH (or arc_path) to your ARC clone' in script
    assert 'then . ~/.bashrc; fi' in script  # the group's ~/.bashrc exports arc_path
    assert 'CONDA_SH="$(conda info --base)/etc/profile.d/conda.sh"' in script
    assert "#PBS -V" not in script
    assert any("unknown here" in n for n in notes)
    _bash_n(script)
    if shutil.which("bash"):  # the job stops before ARC with the message when ARC_PATH is unset
        home = tmp_path / "home"
        home.mkdir()
        run = subprocess.run(["bash", "-c", script.split("CONDA_SH=")[0]], text=True, capture_output=True,
                             env={"PATH": "/usr/bin:/bin", "HOME": str(home)})
        assert run.returncode != 0 and "set ARC_PATH (or arc_path) to your ARC clone" in run.stderr
        # ... and picks up arc_path from ~/.bashrc, as the group's .bashrc exports it
        (home / ".bashrc").write_text('export arc_path="/home/me/Code/ARC/"\n')
        run = subprocess.run(["bash", "-c", script.split("CONDA_SH=")[0] + 'echo "ARC=$ARC_PATH"'], text=True,
                             capture_output=True, env={"PATH": "/usr/bin:/bin", "HOME": str(home)})
        assert run.returncode == 0 and "ARC=/home/me/Code/ARC/" in run.stdout


# ----------------------------------------------------------------- runner script

@pytest.mark.parametrize("sched", ["pbspro", "pbs", "torque", "slurm"])
@pytest.mark.parametrize("pin", [True, False])
def test_runner_script(pbs, sched, pin):
    server = _with_scheduler(pbs, sched)
    if not pin:
        server.arc_runner.host = server.arc_runner.host_cores = server.arc_runner.host_mem_gb = None
    script, notes = srv.render_arc_runner_script(server, "input.yml", "ARC_demo", **MINE)
    _bash_n(script)
    assert "CONDA_SH=/home/me/miniforge3/etc/profile.d/conda.sh" in script and 'source "$CONDA_SH"' in script
    assert "conda activate arc_env" in script
    assert "ARC_PATH=/home/me/Code/ARC" in script and 'ARC_PY="$ARC_PATH/ARC.py"' in script
    assert "ARC_INPUT=input.yml" in script and 'python "$ARC_PY" "$ARC_INPUT" &' in script
    assert "trap cleanup EXIT" in script and "trap 'exit 143' TERM INT" in script
    if sched == "slurm":
        assert "#SBATCH --partition=group_q" in script and "#SBATCH --time=240:00:00" in script
        assert "#SBATCH --cpus-per-task=1" in script and "#SBATCH --mem=8G" in script
        assert ("#SBATCH --nodelist=node01" in script) is pin
        assert 'WORKDIR="$SLURM_SUBMIT_DIR"' in script
    elif sched == "torque":
        assert "#PBS -q group_q" in script
        assert ("#PBS -l nodes=node01:ppn=1" if pin else "#PBS -l nodes=1:ppn=1") in script
        assert "#PBS -l mem=8gb" in script and "#PBS -N ARC_demo" in script
    else:
        sel = "#PBS -l select=1:ncpus=1:mem=8gb" + (":host=node01" if pin else "")
        assert sel + "\n" in script
        assert "#PBS -q group_q" in script and "#PBS -l walltime=240:00:00" in script
        assert "#PBS -o ARC_demo.out" in script and "#PBS -e ARC_demo.err" in script
        assert 'WORKDIR="$PBS_O_WORKDIR"' in script
    assert any("walltime 240:00:00" in n for n in notes)
    assert any("/home/me/Code/ARC (from given)" in n for n in notes)


def test_runner_overrides_and_quoting(pbs):
    with pytest.raises(srv.SubmitError):
        srv.render_arc_runner_script(pbs, "runs/a b.yml")
    with pytest.raises(srv.SubmitError):
        srv.render_arc_runner_script(pbs, "../input.yml")
    script, _ = srv.render_arc_runner_script(pbs, "runs/restart.yml", cores=4, mem_gb=16, walltime="48:00:00",
                                             queue="long_q", host=None, conda_env="/home/me/envs/arc_env")
    assert "conda activate /home/me/envs/arc_env" in script
    assert "select=1:ncpus=4:mem=16gb:host=node01" in script  # host None = keep the configured one
    assert "#PBS -q long_q" in script and "walltime=48:00:00" in script
    assert "ARC_INPUT=runs/restart.yml" in script
    _bash_n(script)
    with pytest.raises(srv.SubmitError, match="cores on node node01"):
        srv.render_arc_runner_script(pbs, cores=64)
    with pytest.raises(srv.SubmitError, match="unknown runner override"):
        srv.render_arc_runner_script(pbs, colour="red")
    with pytest.raises(srv.SubmitError, match="arc_path"):
        srv.render_arc_runner_script(pbs, arc_path="/home/me/my ARC")


def test_runner_default_walltime_is_queue_max(pbs):
    pbs.arc_runner.walltime = None
    script, notes = srv.render_arc_runner_script(pbs, **MINE)
    assert "#PBS -l walltime=3600:00:00" in script and any("queue's maximum" in n for n in notes)


def test_runner_access_denied(pbs):
    with pytest.raises(srv.SubmitError, match="group_q"):
        srv.render_arc_runner_script(pbs, user="bob", groups=["other"])
    script, _ = srv.render_arc_runner_script(pbs, user="bob", groups=["examplegrp"])
    assert "#PBS -q group_q" in script


def test_runner_missing_block():
    with pytest.raises(srv.SubmitError, match="no `arc.runner` block"):
        srv.render_arc_runner_script(_servers(_raw())["example"])


# ----------------------------------------------------------------- ARC settings ('local')

def _exec(code: str) -> dict:
    ns: dict = {}
    exec(compile(ast.parse(code), "settings", "exec"), ns)
    return ns


def test_arc_settings_local_entry(pbs):
    settings_src, submit_src = srv.arc_settings_parts({"example-pbs": pbs})
    ns = _exec(settings_src)
    local = ns["servers"]["local"]
    assert "example-pbs" not in ns["servers"]
    assert local["cluster_soft"] == "PBS"
    assert local["queues"] == {"group_q": "3600:00:00", "long_q": "168:00:00", "short_q": "03:00:00"}
    assert list(local["queues"])[0] == "group_q"  # ARC's default queue
    assert local["cpus"] == 32 and local["memory"] == 192  # the default queue's node
    assert isinstance(local["un"], str) and local["un"]
    assert "address" not in local and "key" not in local and "excluded_queues" not in local
    assert ns["global_ess_settings"] == {"gaussian": "local", "orca": "local"}
    assert "is ARC's 'local' server" in settings_src and "restricted (groups examplegrp)" in settings_src
    sub = _exec(submit_src)["submit_scripts"]
    assert set(sub) == {"local"} and set(sub["local"]) == {"gaussian", "orca"}
    text = sub["local"]["gaussian"].format(name="j", un="u", queue="long_q", t_max="24:00:00", memory=1000, cpus=8)
    assert "#PBS -q long_q" in text
    _bash_n(text)


def test_arc_settings_drop_inaccessible_queues(pbs):
    ns = _exec(srv.arc_settings({"example-pbs": pbs}, user="bob", groups=["other"]))
    local = ns["servers"]["local"]
    assert list(local["queues"]) == ["long_q", "short_q"] and local["excluded_queues"] == ["group_q"]
    assert local["memory"] == 96 and local["cpus"] == 40  # now long_q is the default


def test_arc_settings_default_ess_queues_and_remote_mix(pbs):
    del pbs.arc["ess_queues"]  # default: the non-restricted, non-GPU partitions
    example = _servers(_raw())["example"]
    ns = _exec(srv.arc_settings({"example": example, "example-pbs": pbs}))
    assert set(ns["servers"]) == {"example", "local"}
    assert list(ns["servers"]["local"]["queues"]) == ["long_q", "short_q"]
    assert ns["servers"]["local"]["excluded_queues"] == ["group_q"]
    assert ns["servers"]["example"]["address"] == "login.example.org"
    assert ns["global_ess_settings"]["gaussian"] == ["example", "local"]
    assert set(ns["submit_scripts"]) == {"example", "local"}
    # ARC on a workstation: everything remote
    ns = _exec(srv.arc_settings({"example-pbs": pbs}, local=None))
    assert set(ns["servers"]) == {"example-pbs"} and set(ns["submit_scripts"]) == {"example-pbs"}


def test_remote_entry_uses_ess_queues():
    raw = _raw()
    raw["servers"]["example"]["arc"]["ess_queues"] = ["gpu", "cpu"]
    ns = _exec(srv.arc_settings(_servers(raw), ["example"]))
    assert list(ns["servers"]["example"]["queues"]) == ["gpu", "cpu"]


def test_arc_settings_two_runners_need_choice(pbs):
    other = copy.deepcopy(pbs)
    with pytest.raises(KeyError, match="--local"):
        srv.arc_settings({"a": pbs, "b": other})
    ns = _exec(srv.arc_settings({"a": pbs, "b": other}, local="b"))
    assert set(ns["servers"]) == {"a", "local"}


def test_cli_arc_settings_flags(project, capsys):
    from rag_drg.cli import main

    shutil.copy(EXAMPLE, project.root / "servers.yaml")
    cfg_file = str(project.root / "rag_drg.yaml")
    assert main(["-c", cfg_file, "servers", "arc-settings", "example-pbs"]) == 0
    assert "'local': {" in capsys.readouterr().out
    assert main(["-c", cfg_file, "servers", "arc-settings", "example-pbs", "--no-local"]) == 0
    assert "'example-pbs': {" in capsys.readouterr().out


# ----------------------------------------------------------------- compose

def _codes(res, severity=None):
    return {f["code"] for f in res["findings"] if severity is None or f["severity"] == severity}


def test_compose_ok(pbs):
    res = compose_arc_run(INPUT, pbs, **MINE)
    assert _codes(res, "error") == set(), res["findings"]
    assert _codes(res, "warning") == set(), res["findings"]
    assert "#PBS -N ARC_ethanol_demo" in res["submit_sh"] and "ARC_PATH=/home/me/Code/ARC" in res["submit_sh"]
    _bash_n(res["submit_sh"])
    assert "'local': {" in res["arc_settings_py"] and "submit_scripts = {" in res["arc_submit_py"]
    ast.parse(res["arc_settings_py"])
    ast.parse(res["arc_submit_py"])
    assert any("ARC submits every ESS job to group_q" in n for n in res["notes"])
    json.dumps(res)


def test_compose_job_memory_fits_no_queue(pbs):
    # 180 GB -> ceil(180 x 1024 x 1.1) MiB: too big for group_q (192 GB) and long_q (96 GB);
    # short_q (384 GB) is only 3 h
    res = compose_arc_run(INPUT.replace("job_memory: 14", "job_memory: 180").replace("max_job_time: 2", "max_job_time: 48"),
                          pbs)
    assert "arc-job-fits-no-queue" in _codes(res, "error"), res["findings"]
    # one ESS queue only: too big for it is an error
    pbs.arc["ess_queues"] = ["group_q"]
    res = compose_arc_run(INPUT.replace("job_memory: 14", "job_memory: 180"), pbs)
    assert "arc-job-fits-no-queue" in _codes(res, "error")


def test_compose_misfit_some_queues_is_a_warning(pbs):
    res = compose_arc_run(INPUT.replace("job_memory: 14", "job_memory: 180"), pbs)  # fits short_q
    assert "arc-job-fits-no-queue" not in _codes(res)
    w = [f for f in res["findings"] if f["code"] == "arc-job-misfits-some-queues"]
    assert w and w[0]["severity"] == "warning"
    assert "group_q is ARC's default queue" in w[0]["message"] and "long_q" in w[0]["message"]
    assert "fits: short_q" in w[0]["message"]


def test_compose_memory_capped(pbs):
    res = compose_arc_run(INPUT.replace("job_memory: 14", "job_memory: 500"), pbs)
    assert "arc-job-memory-capped" in _codes(res, "warning")
    assert "arc-job-fits-no-queue" not in _codes(res)  # capped at 95% of group_q's node, which fits it


def test_compose_max_job_time(pbs):
    res = compose_arc_run(INPUT.replace("max_job_time: 2", "max_job_time: 4000"), pbs)
    assert "arc-job-fits-no-queue" in _codes(res, "error")
    res = compose_arc_run(INPUT.replace("max_job_time: 2", "max_job_time: 48"), pbs)
    assert "arc-job-misfits-some-queues" in _codes(res, "warning")  # short_q is 3 h
    assert "arc-job-fits-no-queue" not in _codes(res)
    res = compose_arc_run(INPUT.replace("max_job_time: 2\n", ""), pbs)  # ARC default: 120 h
    assert "arc-job-misfits-some-queues" in _codes(res, "warning")


def test_compose_queue_access(pbs):
    # bob cannot use group_q (and the runner cannot run there either)
    inp = INPUT.replace("job_memory: 14", "job_memory: 180")
    res = compose_arc_run(inp, pbs, user="bob", groups=["other"], queue="long_q", host=None, mem_gb=8)
    assert "arc-queue-access-denied" in _codes(res, "info")
    local = _exec(res["arc_settings_py"])["servers"]["local"]
    assert list(local["queues"]) == ["long_q", "short_q"]
    res = compose_arc_run(INPUT, pbs)
    assert "arc-queue-access-unknown" in _codes(res, "info")
    res = compose_arc_run(INPUT, pbs, user="bob", groups=["examplegrp"])
    assert not _codes(res, "info") & {"arc-queue-access-unknown", "arc-queue-access-denied"}


def test_compose_ess_settings(pbs):
    inp = INPUT.replace("  orca: local\n", "  orca: local\n  molpro: local\n  qchem: example-pbs\n  xtb: local\n"
                                           "  psi4: local\n")
    res = compose_arc_run(inp, pbs)
    errors = _codes(res, "error")
    assert {"arc-ess-not-installed", "arc-ess-self-remote", "arc-ess-unknown"} <= errors
    assert "arc-ess-incore" in _codes(res, "info")
    msgs = " ".join(f["message"] for f in res["findings"])
    assert "molpro" in msgs and "psi4" in msgs


def test_compose_ess_other_server():
    servers = _servers(_raw())
    inp = INPUT.replace("  orca: local\n", "  orca: example\n  molpro: nowhere\n")
    res = compose_arc_run(inp, "example-pbs", servers=servers)
    assert "arc-ess-other-server" in _codes(res, "warning")
    assert "arc-ess-unknown-server" in _codes(res, "error")


def test_compose_bad_input_and_unknown_server(pbs):
    assert "arc-input-yaml" in _codes(compose_arc_run("species: [\n", pbs), "error")
    assert "arc-input-project" in _codes(compose_arc_run("species: []\n", pbs), "error")
    res = compose_arc_run(INPUT, "nope", servers={})
    assert "unknown-server" in _codes(res, "error") and res["submit_sh"] is None


def test_compose_without_runner_is_an_error():
    res = compose_arc_run(INPUT, _servers(_raw())["example"])
    assert "arc-runner" in _codes(res, "error") and res["submit_sh"] is None


def test_cli_compose(project, tmp_path, capsys):
    from rag_drg.cli import main

    shutil.copy(EXAMPLE, project.root / "servers.yaml")
    cfg_file = str(project.root / "rag_drg.yaml")
    run = tmp_path / "run"
    run.mkdir()
    (run / "input.yml").write_text(INPUT)
    args = ["-c", cfg_file, "arc", "compose", str(run / "input.yml"), "--server", "example-pbs",
            "--arc-path", "/home/me/Code/ARC", "--conda-env", "arc_env"]
    assert main(args) == 0
    for name in ("submit.sh", "arc_settings.py", "arc_submit.py"):
        assert (run / name).is_file()
    sh = (run / "submit.sh").read_text()
    assert "ARC_INPUT=input.yml" in sh and "ARC_PATH=/home/me/Code/ARC" in sh
    assert main(args) == 1  # no overwrite without --force
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "input.yml").write_text(INPUT.replace("max_job_time: 2", "max_job_time: 4000"))
    assert main(["-c", cfg_file, "arc", "compose", str(bad / "input.yml"), "--server", "example-pbs"]) == 1
    assert not (bad / "submit.sh").exists()  # errors: nothing written
    capsys.readouterr()
    assert main(args + ["--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert set(out) == {"submit_sh", "arc_settings_py", "arc_submit_py", "findings", "notes"}


def test_cli_compose_refuses_dot_arc(project, tmp_path):
    from rag_drg.cli import main

    shutil.copy(EXAMPLE, project.root / "servers.yaml")
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    (home / "input.yml").write_text(INPUT)
    rc = main(["-c", str(project.root / "rag_drg.yaml"), "arc", "compose", str(home / "input.yml"),
               "--server", "example-pbs", "--out-dir", str(home / ".arc")])
    assert rc == 2 and not (home / ".arc").exists()


class _FakeMCP:
    def __init__(self):
        self.tools = {}

    def tool(self, name=None, **_kw):
        def deco(fn):
            self.tools[name or fn.__name__] = fn
            return fn
        return deco


class _Ctx:
    def __init__(self, cfg):
        self.cfg = cfg
        self.events = []

    def emit(self, e):
        self.events.append(e)


def test_mcp_compose_tool(project, monkeypatch):
    from rag_drg.tools import compose_arc

    shutil.copy(EXAMPLE, project.root / "servers.yaml")
    mcp = _FakeMCP()
    compose_arc.register_mcp(mcp, _Ctx(project))
    tool = mcp.tools["compose_arc_run"]
    out = json.loads(tool(INPUT, "example-pbs", arc_path="/home/me/Code/ARC"))
    assert out["submit_sh"].startswith("#!/bin/bash") and "ARC_PATH=/home/me/Code/ARC" in out["submit_sh"]
    assert not [f for f in out["findings"] if f["severity"] == "error"]
    # shared server: the server's own environment is never used for the user's paths
    monkeypatch.setenv("RAG_DRG_SERVER_MODE", "1")
    monkeypatch.setenv("ARC_PATH", "/srv/service-account/ARC")
    out = json.loads(tool(INPUT, "example-pbs"))
    assert "/srv/service-account" not in out["submit_sh"] and "${arc_path:?" in out["submit_sh"]


# ----------------------------------------------------------------- arc: local overrides (zeus)


def _with_arc(**extra):
    raw = _raw()
    raw["servers"]["example-pbs"]["arc"].update(extra)
    return raw


def test_arc_local_overrides_render_like_the_groups_settings():
    raw = _with_arc(cpus=16, memory_gb=160,
                    default_job_settings={"job_total_memory_gb": 32, "job_cpu_cores": 16},
                    commands={"submit": "/opt/pbs/bin/qsub", "status": "/opt/pbs/bin/qstat",
                              "delete": "/opt/pbs/bin/qdel"},
                    ess_installs={"gaussian": "gaussian-16"})
    s = _servers(raw)
    settings, submit = srv.arc_settings_parts(s, ["example-pbs"], local="example-pbs")
    ns: dict = {}
    exec(compile(ast.parse(settings.replace("__import__('getpass').getuser()", "'me'")), "s", "exec"), ns)
    local = ns["servers"]["local"]
    assert (local["cpus"], local["memory"]) == (16, 160)
    assert ns["default_job_settings"] == {"job_total_memory_gb": 32, "job_cpu_cores": 16}
    assert ns["submit_command"] == {"PBS": "/opt/pbs/bin/qsub"}
    assert ns["check_status_command"] == {"PBS": "/opt/pbs/bin/qstat"}
    assert ns["delete_command"] == {"PBS": "/opt/pbs/bin/qdel"}
    assert "/opt/gaussian/g16-C.02/g16/g16" in submit


def test_arc_ess_installs_can_pick_a_gpu_build():
    raw = _with_arc(ess_installs={"gaussian": "gaussian-16-gpu"})
    raw["servers"]["example-pbs"]["software"]["gaussian-16-gpu"] = {
        "ess": "gaussian", "version": "C.02", "executable": "/opt/g16-gpu/g16/g16", "parallel": "threads"}
    s = _servers(raw)["example-pbs"]
    from rag_drg.tools._servers.render import arc_ess_install

    assert arc_ess_install(s, "gaussian").key == "gaussian-16-gpu"
    assert arc_ess_install(_servers(_raw())["example-pbs"], "gaussian").key == "gaussian-16"


def test_arc_job_request_uses_the_overrides():
    from rag_drg.tools.compose_arc import arc_job_request

    s = _servers(_with_arc(cpus=16, memory_gb=160,
                           default_job_settings={"job_cpu_cores": 16}))["example-pbs"]
    req = arc_job_request(s, 200, [s.partitions["group_q"]])
    assert req["cores"] == 16 and req["arc_memory"] == 160 and req["capped"]


@pytest.mark.parametrize("extra, problem", [
    ({"cpus": 0}, "arc.cpus"),
    ({"memory_gb": "lots"}, "arc.memory_gb"),
    ({"commands": {"submit": "qsub"}}, "arc.commands.submit"),
    ({"commands": {"launch": "/opt/pbs/bin/qsub"}}, "arc.commands.launch"),
    ({"ess_installs": {"gaussian": "nope"}}, "arc.ess_installs.gaussian"),
    ({"ess_installs": {"gaussian": "orca-6"}}, "arc.ess_installs.gaussian"),
    ({"default_job_settings": {"job_cpu_cores": "many"}}, "arc.default_job_settings"),
])
def test_arc_local_overrides_are_validated(extra, problem):
    problems = validate_data(_with_arc(**extra))
    assert any(problem in p for p in problems), problems


def test_gpu_build_on_a_cpu_partition_is_not_flagged():
    raw = _raw()
    raw["servers"]["example-pbs"]["software"]["gaussian-16-gpu"] = {
        "ess": "gaussian", "version": "C.02", "executable": "/opt/g16-gpu/g16/g16", "parallel": "threads",
        "partitions": ["group_q"]}
    s = _servers(raw)["example-pbs"]
    probs = srv.check_resources(s, "group_q", 8, 32, 24, 0, software="gaussian-16-gpu", check_access=False)
    assert not any("GPU build" in p["message"] for p in probs)
