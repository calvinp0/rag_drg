"""Regression tests for false positives / misses found in the input-checker review.

Each test failed before its fix. Errors from the checker block the agent (Claude Code hook), so a
false-positive error is the worst outcome; most tests assert that valid inputs give no error.
"""

import io
import json
import shutil
from pathlib import Path

import pytest

from rag_drg.tools._inputcheck.submit import parse_submit
from rag_drg.tools.inputcheck import check_input, detect_program, hook_main

ROOT = Path(__file__).resolve().parent.parent


def _errors(findings):
    return [f for f in findings if f.severity == "error"]


def _run(name, text, sub=None):
    return check_input(content=text, filename=name, submit_content=sub)


# ------------------------------------------------------------------ 1. GAMESS / CP2K .inp are not ORCA

GAMESS = """! GAMESS input for water
 $CONTRL SCFTYP=RHF RUNTYP=OPTIMIZE $END
 $BASIS GBASIS=N31 NGAUSS=6 $END
 $DATA
Water
C1
O 8.0 0.0 0.0 0.0
H 1.0 0.0 0.0 0.96
H 1.0 0.9 0.0 -0.2
 $END
"""

CP2K = """! CP2K input
&GLOBAL
  PROJECT water
  RUN_TYPE ENERGY
&END GLOBAL
&FORCE_EVAL
  METHOD Quickstep
&END FORCE_EVAL
"""


@pytest.mark.parametrize("text", [GAMESS, CP2K, "! just a comment line\nfoo bar\n"])
def test_non_orca_inp_with_bang_comment_not_detected(text, tmp_path):
    assert detect_program("job.inp", text) is None
    assert not _errors(_run("job.inp", text))
    p = tmp_path / "job.inp"
    p.write_text(text)
    err = io.StringIO()
    assert hook_main(json.dumps({"tool_input": {"file_path": str(p)}}), err=err) == 0
    assert err.getvalue() == ""


def test_real_orca_still_detected():
    assert detect_program("job.inp", "! B3LYP def2-SVP Opt\n%maxcore 2000\n") == "orca"
    assert detect_program("job.inp", "! Opt\n* xyzfile 0 1 g.xyz\n") == "orca"


# ------------------------------------------------------------------ 2. diagnostics are not invocations


@pytest.mark.parametrize("line", ["which orca", "type orca", "command -v orca", "ldd $(which orca)",
                                  'echo "running orca"', "test -x /opt/orca/orca && echo ok", "[ -x /opt/orca/orca ]"])
def test_diagnostic_lines_are_not_program_calls(line):
    sub = parse_submit(f"#!/bin/bash\n#SBATCH -n 8\n{line}\n/opt/orca/orca job.inp > job.out\n", "run.sh")
    assert [(e.program, e.line) for e in sub.executables] == [("orca", 4)]


@pytest.mark.parametrize("line,launcher", [("time /opt/orca/orca j.inp", None), ("nohup /opt/orca/orca j.inp &", None),
                                           ("OMP_NUM_THREADS=1 /opt/orca/orca j.inp", None),
                                           ("srun --mpi=pmix /opt/orca/orca j.inp", "srun"),
                                           ("mpirun -machinefile $PBS_NODEFILE /opt/orca/orca j.inp", "mpirun")])
def test_real_invocations_still_found(line, launcher):
    sub = parse_submit(f"#!/bin/bash\n#SBATCH -n 8\n{line}\n", "run.sh")
    assert [(e.program, e.launcher) for e in sub.executables] == [("orca", launcher)]


def test_which_diagnostic_gives_no_orca_path_error():
    orca = "! B3LYP def2-SVP\n%maxcore 3000\n%pal nprocs 8 end\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n"
    sub = ("#!/bin/bash\n#SBATCH --ntasks=8\n#SBATCH --mem-per-cpu=4000\nmodule load orca\nwhich orca\n"
           "$(which orca) job.inp > job.out\n")
    assert not _errors(_run("job.inp", orca, sub))


# ------------------------------------------------------------------ 3. Gen basis from @file


def test_gen_basis_from_include_file():
    g = "%mem=4GB\n#p b3lyp/gen opt\n\nt\n\n0 1\nO 0 0 0\nH 0 0 0.96\nH 0.9 0 -0.2\n\n@/home/u/basis/my.gbs/N\n\n"
    assert not _errors(_run("genat.gjf", g))


def test_gen_without_basis_block_still_an_error():
    g = "#p b3lyp/gen\n\nt\n\n0 1\nH 0 0 0\nH 0 0 0.74\n\n"
    assert [f.code for f in _errors(_run("gen.gjf", g))] == ["gaussian-gen-basis"]


# ------------------------------------------------------------------ 4. Z-matrix with Variables:


@pytest.mark.parametrize("kw", ["Variables:", "   variables", "Constants:"])
def test_gaussian_zmatrix_variables_in_molecule_block(kw):
    g = f"#p hf/6-31g(d) opt\n\nwater\n\n0 1\nO\nH 1 B1\nH 1 B2 2 A1\n{kw}\nB1 0.96\nB2 0.96\nA1 104.5\n\n"
    assert not _errors(_run("zv.gjf", g))


def test_gaussian_zmatrix_variables_parity_still_checked():
    g = "#p hf/6-31g(d)\n\nt\n\n0 2\nO\nH 1 R1\nH 1 R1 2 A1\nVariables:\nR1 0.96\nA1 104.5\n\n"
    assert [f.code for f in _errors(_run("zv.gjf", g))] == ["parity"]


# ------------------------------------------------------------------ 5. ORCA ghost atoms


@pytest.mark.parametrize("ghost", ["H : 3 0 0.96", "H: 3 0 0.96"])
def test_orca_ghost_atoms_not_counted(ghost):
    o = f"! B3LYP def2-SVP\n%maxcore 3000\n* xyz 0 2\nO 0 0 0\nH 0 0 0.97\n{ghost}\n*\n"
    assert not _errors(_run("ghost.inp", o))


# ------------------------------------------------------------------ 6. PySCF with several molecules


def test_pyscf_two_molecules_not_mixed():
    py = """from pyscf import gto, scf
mol = gto.Mole()
mol.atom = 'H 0 0 0'
mol.spin = 1
mol.basis = 'cc-pvdz'
mol.build()
mol2 = gto.Mole()
mol2.atom = 'H 0 0 0; H 0 0 0.74'
mol2.basis = 'cc-pvdz'
mol2.build()
"""
    assert not _errors(_run("a.py", py))


def test_pyscf_second_molecule_still_checked():
    py = """from pyscf import gto
mol = gto.M(atom='H 0 0 0; H 0 0 0.74', basis='sto-3g')
mol2 = gto.Mole()
mol2.atom = 'O 0 0 0; H 0 0 0.97'
mol2.spin = 2
mol2.build()
"""
    errs = _errors(_run("b.py", py))
    assert [f.code for f in errs] == ["parity"] and "mol2" in errs[0].message


def test_pyscf_reassigned_molecule_is_info_only():
    py = "from pyscf import gto\nmol = gto.M(atom='H 0 0 0', spin=1, basis='sto-3g')\nmol.spin = 0\nmol.build()\n"
    found = _run("c.py", py)
    assert not _errors(found)
    assert any(f.code == "parity" and f.severity == "info" for f in found)


# ------------------------------------------------------------------ 7. ORCA %Compound


def test_orca_compound_geometry_in_steps():
    o = """%maxcore 3000
%Compound
  New_Step
    ! B3LYP def2-SVP Opt
    * xyz 0 1
    O 0 0 0
    H 0 0 0.96
    H 0.9 0 -0.2
    *
  Step_End
End
"""
    assert not _errors(_run("comp.inp", o))


# ------------------------------------------------------------------ 8. Psi4 memory units are SI


def test_psi4_gb_is_si():
    from rag_drg.tools.inputcheck import parse_input

    inp, _ = parse_input("memory 2 GB\nmolecule {\n0 1\nHe 0 0 0\n}\nset basis cc-pvdz\nenergy('scf')\n", "a.dat")
    assert inp.memory_total_mb == pytest.approx(2e9 / 2**20)
    inp, _ = parse_input("memory 2 GiB\nmolecule {\n0 1\nHe 0 0 0\n}\nset basis cc-pvdz\nenergy('scf')\n", "a.dat")
    assert inp.memory_total_mb == pytest.approx(2048)


# ------------------------------------------------------------------ 9. GPU directives


@pytest.mark.parametrize("gpu,ntasks,expect", [("--gpus-per-task=1", 4, 4), ("--gpus=2", 1, 2),
                                               ("--gres=gpu:a100:2", 1, 2), ("--gres=gpu:1", 1, 1)])
def test_slurm_gpu_directives(gpu, ntasks, expect):
    sub = parse_submit(f"#!/bin/bash\n#SBATCH --ntasks={ntasks}\n#SBATCH {gpu}\n", "x.sh")
    assert sub.gpus == expect


def test_gaussian_gpu_with_gpus_per_task_no_error():
    g = "%mem=56GB\n%cpu=0-15\n%gpucpu=0=0\n#p b3lyp/6-31g(d) opt\n\nt\n\n0 1\nH 0 0 0\nH 0 0 0.74\n\n"
    sub = ("#!/bin/bash\n#SBATCH --ntasks=1\n#SBATCH --cpus-per-task=16\n#SBATCH --gpus-per-task=1\n"
           "#SBATCH --mem=64G\ng16 < g.gjf > g.log\n")
    assert not _errors(_run("g.gjf", g, sub))


# ------------------------------------------------------------------ 11. PBS walltime in seconds


def test_pbs_bare_walltime_is_seconds_slurm_minutes():
    assert parse_submit("#!/bin/bash\n#PBS -l walltime=36000\n", "x.pbs").walltime_s == 36000
    assert parse_submit("#!/bin/bash\n#SBATCH -t 90\n", "x.sh").walltime_s == 90 * 60


# ------------------------------------------------------------------ 12. hook robustness


@pytest.mark.parametrize("stdin", ["[]", '{"tool_input":"x"}', '{"tool_input":{"file_path":123}}', "null", "3",
                                   '{"tool_input":{"file_path":"/nonexistent/x.gjf"},"cwd":5}',
                                   '{"tool_response":"x"}', "not json"])
def test_hook_never_crashes(stdin):
    assert hook_main(stdin, err=io.StringIO()) == 0


# ------------------------------------------------------------------ 13. silent misses (warnings)


def test_oniom_gen_without_basis_is_warning():
    g = "#p oniom(b3lyp/gen:uff)\n\nt\n\n0 1 0 1 0 1\nH 0 0 0 H\nH 0 0 0.74 H\n\n"
    found = _run("o.gjf", g)
    assert not _errors(found)
    assert any(f.code == "gaussian-gen-basis" and f.severity == "warning" for f in found)


def test_pp_basis_in_gaussian_is_warning():
    g = "#p b3lyp/aug-cc-pvtz-pp\n\nt\n\n0 1\nI 0 0 0\nI 0 0 2.67\n\n"
    found = _run("pp.gjf", g)
    assert any(f.code == "gaussian-pp-basis" and f.severity == "warning" for f in found)


# ------------------------------------------------------------------ 10. multi-node cluster limits

ORCA32 = "! B3LYP def2-SVP\n%maxcore 3000\n%pal nprocs 32 end\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n"


@pytest.fixture
def multinode_project(project, monkeypatch):
    pytest.importorskip("rag_drg.tools.cluster_limits")
    monkeypatch.delenv("RAG_DRG_SERVER", raising=False)
    text = (ROOT / "servers.example.yaml").read_text()
    # cpu partition: 16 cores / 128 GB per node, up to 4 nodes
    text = text.replace("cores_per_node: 48\n        mem_per_node_gb: 256\n        gpus_per_node: 0\n"
                        "        max_nodes: 1",
                        "cores_per_node: 16\n        mem_per_node_gb: 128\n        gpus_per_node: 0\n"
                        "        max_nodes: 4", 1)
    assert "max_nodes: 4" in text
    (project.root / "servers.yaml").write_text(text)
    return project


def _limits(found):
    return [f for f in found if f.code == "cluster-limits" and f.severity in ("error", "warning")]


@pytest.mark.parametrize("res", ["#SBATCH -N 2\n#SBATCH --ntasks-per-node=16\n#SBATCH --mem=100G",
                                 "#SBATCH -N 2\n#SBATCH --ntasks-per-node=16\n#SBATCH --mem-per-cpu=6G"])
def test_multinode_job_checked_per_node(multinode_project, res):
    sub = f"#!/bin/bash\n#SBATCH -p cpu\n{res}\n#SBATCH -t 24:00:00\n/opt/orca/orca_6_0_1/orca job.inp > job.out\n"
    assert not _limits(check_input(content=ORCA32, filename="job.inp", submit_content=sub, cfg=multinode_project))


def test_too_many_nodes_and_too_much_per_node(multinode_project):
    sub = ("#!/bin/bash\n#SBATCH -p cpu\n#SBATCH -N 8\n#SBATCH --ntasks-per-node=4\n#SBATCH --mem=200G\n"
           "#SBATCH -t 24:00:00\n/opt/orca/orca_6_0_1/orca job.inp > job.out\n")
    found = _limits(check_input(content=ORCA32, filename="job.inp", submit_content=sub, cfg=multinode_project))
    msgs = " | ".join(f.message for f in found if f.severity == "error")
    assert "8 nodes > max 4" in msgs and "200 GB > 128 GB" in msgs


def test_pbs_select_chunks_per_node(multinode_project):
    orca = ORCA32.replace("32", "16")
    sub = ("#!/bin/bash\n#PBS -q cpu\n#PBS -l select=2:ncpus=8:mpiprocs=8:mem=64gb\n#PBS -l walltime=36000\n"
           "/opt/orca/orca_6_0_1/orca job.inp > job.out\n")
    assert not _limits(check_input(content=orca, filename="job.inp", submit_content=sub, cfg=multinode_project))


def test_multinode_on_default_single_node_partition_is_warning(project, monkeypatch):
    pytest.importorskip("rag_drg.tools.cluster_limits")
    monkeypatch.delenv("RAG_DRG_SERVER", raising=False)
    shutil.copy(ROOT / "servers.example.yaml", project.root / "servers.yaml")  # cpu: max_nodes 1
    sub = ("#!/bin/bash\n#SBATCH -p cpu\n#SBATCH -N 2\n#SBATCH --ntasks-per-node=16\n#SBATCH --mem=100G\n"
           "#SBATCH -t 24:00:00\n/opt/orca/orca_6_0_1/orca job.inp > job.out\n")
    found = _limits(check_input(content=ORCA32, filename="job.inp", submit_content=sub, cfg=project))
    assert found and all(f.severity == "warning" for f in found)


def test_pyscf_rebound_name_is_a_new_molecule():
    py = ("from pyscf import gto\nmol = gto.M(atom='H 0 0 0', spin=1, basis='sto-3g')\n"
          "mol = gto.M(atom='H 0 0 0; H 0 0 0.74', basis='sto-3g')\n")
    assert not _errors(_run("r.py", py))
