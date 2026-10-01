# Input checker and basis-set checks

Two plugins in `rag_drg/tools/` catch the mistakes agents keep making in ESS inputs and
submit scripts before a job is queued:

* `basis.py`: `rag-drg basis` / MCP `check_basis`, which looks up a basis set in the Basis Set Exchange.
* `inputcheck.py`: `rag-drg check-input` / MCP `check_input` / a Claude Code hook, with static
  checks for Gaussian, ORCA, Q-Chem, Molpro, Psi4 and PySCF inputs and Slurm/PBS scripts.

```bash
pip install -e '.[mcp,chem]'      # chem = basis_set_exchange (offline data) + rdkit (SMILES)
```

Both work without the `chem` extra. Basis checks are then skipped silently, and `--smiles` reports that RDKit is missing.

## Basis sets: `rag-drg basis`

```bash
rag-drg basis def2-TZVP --elements C,H,I     # coverage, ECPs (I: 28 core electrons), /C /J /JK sets
rag-drg basis "6-31G(d,p)" --smiles CCBr     # implicit H counted (RDKit)
rag-drg basis avtz --xyz geom.xyz --json
rag-drg basis Def2TZVP --software gaussian   # also prints the spelling that program uses
```

Exit code: 0 when the basis is found and covers every element, 1 when it is unknown or elements are missing, and 2 on bad input.

Before lookup, the name is normalised: case is ignored and hyphens are optional (Gaussian `Def2TZVP` =
`def2-TZVP`); `6-31G(d)` = `6-31G*`, `6-31+G(d,p)` = `6-31+G**`; the Molpro shorthands
`vdz vtz vqz v5z avdz avtz avqz` (also `-f12`, `-pp`, `wcv`) expand to `cc-pVnZ` / `aug-cc-pVnZ`; and the ORCA
auxiliary names `X/C`, `X/J`, `X/JK`, `def2/J`, `def2/JK` and `cc-pVnZ-F12-CABS` map to BSE's `-RIFIT`,
`-JFIT`, `-JKFIT` and `-OPTRI` sets, where BSE lists one for that orbital basis. An unknown name gets
`difflib` suggestions.

**BSE is the reference, not the program.** Gaussian, ORCA, Q-Chem, Molpro, Psi4 and PySCF ship
their own basis libraries, and their element coverage can differ (for example, Gaussian's 6-31G* covers more
elements than BSE's). A gap reported here means "check the program's library". It does not
mean the job will certainly fail.

## Input checks: `rag-drg check-input`

```bash
rag-drg check-input job.inp                   # finds a submit script next to it that names job.inp
rag-drg check-input job.gjf --submit run.sh   # or name the script explicitly
rag-drg check-input run.sh                    # a submit script: checked alone + with the inputs it names
rag-drg check-input *.inp --json
```

Exit code 1 means at least one error. Each finding has a `severity`, a short `code`, a `message`, a `line`,
a suggested `fix` and a `ref` to the knowledge card that explains the rule.
You can open that card with `read_document`, e.g. `ess/orca/orca-essentials.md`.

The checker is built for **precision**: `error` means the job will fail or silently compute the
wrong thing, `warning` means it is very likely a mistake, and `info` means the checker is not sure.
Valid inputs written like the cards and `knowledge/hpc/templates/` produce no errors or
warnings. The tests check this for every supported program.

The program is detected from the content, with the extension as a hint. `.com` files can be Gaussian
or Molpro. Documentation and code files are never treated as inputs.

### What is checked

| Scope | Check | Severity |
|---|---|---|
| all | electron count (sum of Z − charge) vs multiplicity, or vs 2S for Molpro/PySCF | error |
| all | unknown element symbols; atoms closer than 0.5 Å (duplicate atom, Bohr/Å mix-up) | error |
| all | basis coverage of the elements (BSE; skipped for Gen/unknown names or without BSE) | warning |
| all | method/functional support in that code, from `knowledge/ess/levels_of_theory.yaml` | no → error, variant (e.g. Gaussian-style `wB97XD` in ORCA) → warning, partial/unknown → info |
| Gaussian | Link0 → route → blank → title → blank → charge/mult → atoms → blank; **file ends with a blank line**; `Geom=AllCheck` (no title/molecule, needs `%oldchk`/`%chk`) | error |
| Gaussian | `%mem` without a unit (read as 8-byte words) or with an unknown unit; `%cpu` together with `%nprocshared` | warning |
| Gaussian | `%gpucpu` control cores missing from `%cpu` (error); GPU/core count mismatch, `%gpucpu` without `%cpu` | error / warning |
| Gaussian | `wB97XD` + `EmpiricalDispersion` (dispersion counted twice); `Opt=TS` without CalcFC/ReadFC/CalcAll | warning |
| Gaussian | `Gen`/`GenECP` without a basis block (`****` or an `@/path/basis.gbs/N` include) after the geometry | error (inside `oniom(...)`: warning) |
| Gaussian | `cc-pVnZ-PP` basis in the route (not built in; needs GenECP) | warning |
| ORCA | `%maxcore` missing; < 250 (GB meant?); ≥ 32000 with nprocs > 1 (total meant?) | warning |
| ORCA | `%pal nprocs` and `!PALn` disagree; unclosed `%block … end`; `* xyz c m … *` structure | error |
| ORCA | DLPNO / RI-MP2 without a `/C` basis (or AutoAux) | error (double hybrids: warning) |
| ORCA | `MORead` with `%moinp` of the same basename as the input (ORCA overwrites it) | error |
| ORCA | ORCA 4 grid keywords (`Grid4`, `FinalGrid5`) | warning |
| Q-Chem | `$section` without `$end`; `$rem` without METHOD/EXCHANGE or BASIS; `@@@` job without `$molecule` (`read`) | error |
| Q-Chem | common non-Q-Chem JOBTYPEs (`energy`, `optts`, …); no `MEM_TOTAL`; `DFT_D` with ωB97X-D/-V, ωB97M-V | warning (other unknown JOBTYPE: info) |
| Molpro | `memory,N,m` ≥ 64 GB **per process** (units are 8-byte words, so GB was probably meant) | warning |
| Molpro | `wf,nelec,sym,spin` / `set,spin=` parity (spin is 2S) | error |
| PySCF | `spin=` is 2S: parity with the electron count (with a hint when the multiplicity was given), per molecule variable; Python syntax errors | error (molecule re-assigned different atoms/spin/charge: info) |
| PySCF | `max_memory` not set (default 4000 MB) | info |
| Psi4 | memory not set (default ~500 MiB); open shell without `reference uhf/uks/rohf` | warning |

Psi4 memory units follow Psi4: `GB`/`MB`/`kB` are SI (`memory 2 GB` = 2·10⁹ bytes), `GiB`/`MiB` are binary.

Checks against a submit script (Slurm `#SBATCH`, PBS/Torque `#PBS`):

| Program | Check |
|---|---|
| any | the script runs a different program than the input (warning); `-nt`/`-n` more threads/processes than cores (error) |
| Gaussian | `%mem` > allocation (error) or > 90 % of it (warning); cores used > allocated (error); `%gpucpu` GPUs > requested GPUs (error) |
| ORCA | nprocs > cores (error) or ≠ MPI tasks (warning); `%maxcore` × nprocs > allocation (error) / > 90 % (warning); **called via mpirun/srun** (error); not by absolute path (error when parallel, warning in a script on its own) |
| Q-Chem | `MEM_TOTAL` > allocation (error) / > 90 % (warning) |
| Molpro | memory per process × `molpro -n` (or ntasks) > allocation (error) / > 90 % (warning) |
| Psi4 / PySCF | memory > allocation (error for Psi4, warning for PySCF, whose `max_memory` is soft) |

The script parser reads cores (`--ntasks`, `--cpus-per-task`, `--nodes`, `--ntasks-per-node`,
`select=N:ncpus=:mpiprocs=`, `nodes=N:ppn=`), memory (`--mem`, `--mem-per-cpu`, `mem=`),
walltime (a bare number is minutes for Slurm, seconds for PBS), partition/queue and GPUs (`--gres=gpu:[type:]N`
per node, `--gpus`, `--gpus-per-node`, `--gpus-per-task` × tasks, `ngpus=`). It also finds the program call,
expanding simple `VAR=value` assignments and `$(which prog)`. A program counts as called only when it is the
command word (after `VAR=val` prefixes, `time`/`nohup`/`exec`/`env`, or an MPI launcher), so `which orca`,
`ldd $(which orca)`, `echo ... orca`, `test -x .../orca` are not calls.

With `servers.yaml`, partition limits are checked **per node**: Slurm `--mem` is per node, `--mem-per-cpu` ×
the cpus on a node, PBS `select=` chunks per chunk. More nodes than the partition's `max_nodes` is an error when
`max_nodes` > 1 is set, otherwise a warning.

### Content mode (MCP)

The shared MCP server cannot see users' files, so the tool takes the text:

```
check_input(content="<file text>", filename="job.inp", submit_script_content="<run.sh text>")
check_basis(basis="Def2TZVP", elements=["C", "H", "I"])          # or smiles= / xyz=
```

Python: `check_input(content=..., filename=..., submit_content=...)` or `check_input(path=...)`.

## Claude Code hook

With the hook, Claude Code runs the checker after every `Write`/`Edit`/`MultiEdit`.
If the edited file is an ESS input or a submit script with errors, the hook exits with code 2 and prints a short
summary on stderr, which Claude sees and fixes. Warnings exit 0 with a note on stderr.
Every other file is ignored silently, including `.inp` files of programs the checker does not know (GAMESS
`$CONTRL`, CP2K `&GLOBAL`, ...): a `!` line alone does not make a file ORCA. If the hook itself fails
(unexpected stdin, checker bug) it exits 0.

Install it with

```bash
bin/rag-drg install-hook             # ~/.claude/settings.json (all projects)
bin/rag-drg install-hook --project   # ./.claude/settings.json (this project only)
```

It writes the absolute path of this checkout's `bin/rag-drg` (hooks do not activate a venv or
conda env; the launcher finds the install and points `RAG_DRG_CONFIG` at this repo's
`rag_drg.yaml`), runs the command once before saving, replaces any earlier rag-drg hook,
including a broken one, and keeps your other hooks. `--dry-run` shows the result. The entry it
writes, also in [`integrations/claude-code/hooks.json`](../integrations/claude-code/hooks.json):

```json
{
  "hooks": {
    "PostToolUse": [
      {
        "matcher": "Write|Edit|MultiEdit",
        "hooks": [
          { "type": "command", "command": "/path/to/rag_drg/bin/rag-drg check-input --hook" }
        ]
      }
    ]
  }
}
```

If you edit it by hand: the command is one shell line. Start it with the absolute path, and put
nothing in front of it. In particular, don't write `/RAG_DRG_BIN=...`: the leading `/` makes the
shell look for a program of that name, and Claude Code only reports a non-blocking error, so the
checks silently stop. Usually no variable is needed; if one is, write `NAME=value /path/...`
(or `rag-drg install-hook --rag-drg-bin PATH`). Test the line with
`echo '{}' | <command>; echo $?`, which should print 0.

## Extension point

Other plugins can add checks (the cluster-registry plugin will add partition and limit checks from `servers.yaml`):

```python
from rag_drg.tools.inputcheck import EXTRA_CHECKS, Finding

def partition_limits(inp, sub, cfg):
    # inp: ParsedInput (inp.program is None when only a submit script is checked)
    # sub: ParsedSubmit | None; cfg: Config | None
    if sub and sub.walltime_s and sub.walltime_s > 72 * 3600:
        return [Finding("error", "walltime-limit", "partition allows 72 h", fix="--time=72:00:00")]
    return []

EXTRA_CHECKS.append(partition_limits)
```

`ParsedInput` and `ParsedSubmit` are dataclasses in `rag_drg/tools/_inputcheck/model.py`.
Their docstrings list every field. The main ones are `program`, `charge`, `multiplicity`, `spin_2s`, `atoms`,
`method`, `basis`, `aux_basis`, `job_type`, `memory_total_mb`, `memory_per_core_mb`,
`memory_per_process_mb`, `nprocs`, `gpus` for inputs, and `scheduler`, `total_cores`, `mpi_tasks`,
`cpus_per_task`, `mem_total_mb`, `walltime_s`, `partition`, `gpus`, `executables` for scripts.
An exception raised by an extra check becomes an `info` finding and never breaks the checker.

## Limits

* The electron count ignores geometries the checker cannot fully read, such as `* xyzfile` in content mode,
  Gaussian `Geom=Check`, Psi4 fragments, PubChem, and PySCF periodic cells.
  In those cases it checks what it can and skips the rest.
* Only the first job of Gaussian `--Link1--` and ORCA `$new_job` chains is checked for geometry.
  Geometries inside ORCA `%Compound` steps are not checked.
  Q-Chem `@@@` jobs are each checked for structure.
* The ORCA block-balance check knows the common sub-blocks (`Constraints`, `Scan`, `NewGTO`, `coords`, …).
  If a sub-block it does not know adds extra `end` lines, it only reports `info`.
* Rules marked "verify" or "check" in the cards are not encoded as errors. Add them here once a group member
  has confirmed them against the manual.
