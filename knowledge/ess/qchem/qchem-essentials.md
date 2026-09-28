---
title: Q-Chem 6.1 input essentials and gotchas
domain: ess
software: qchem
version: "6.1"
doc_type: gotcha
status: draft
tags: [rem, molecule, jobtype, method, basis, mem_total, qcscratch, qcenv, nt, np, "@@@", scf_guess, geom_opt_hessian, rpath, smd, dft_d]
---
# Q-Chem 6.1 input essentials and gotchas

## Input skeleton

```
$molecule
0 2
C   0.000000   0.000000   0.000000
H   ...
$end

$rem
   JOBTYPE          opt
   METHOD           wB97X-D
   BASIS            def2-TZVP
   UNRESTRICTED     true
   SCF_CONVERGENCE  8
   MEM_TOTAL        16000
$end
```
* Sections are `$name ... $end`. `$molecule` starts with `charge multiplicity` (multiplicity,
  not 2S). `$rem` holds `KEYWORD value` pairs (case-insensitive, one per line).
* `METHOD` + `BASIS` is the modern form; older inputs use `EXCHANGE`/`CORRELATION` instead of `METHOD`.
* `JOBTYPE`: `sp` (default), `opt`, `ts`, `freq`, `rpath` (IRC), `force`.

## Multi-step jobs (`@@@`)

```
$molecule
0 1
...
$end
$rem
   JOBTYPE freq
   METHOD  wB97X-D
   BASIS   def2-SVP
$end

@@@

$molecule
read
$end
$rem
   JOBTYPE           ts
   METHOD            wB97X-D
   BASIS             def2-SVP
   SCF_GUESS         read
   GEOM_OPT_HESSIAN  read
$end
```
* Later jobs `read` the geometry/orbitals/Hessian of the previous one. A TS search with an exact
  initial Hessian is done this way (freq job, then `JOBTYPE ts` with `GEOM_OPT_HESSIAN read`).

## Memory and cores (gotcha)

* `MEM_TOTAL` (MB) is the **total** memory for the job (the default is small). `MEM_STATIC`
  is the part reserved for integrals/fixed arrays; raise it for large post-HF jobs.
* Threads: `qchem -nt N input.in output.out` (OpenMP, one node). MPI: `qchem -np N ...`.
  Use `-nt` for single-node DFT jobs.

## Environment (absolute paths, no modules)

```bash
export QC=/abs/path/to/qchem-6.1          # install directory
export QCAUX=$QC/qcaux                    # basis sets, grids (check your install layout)
source $QC/qcenv.sh
export QCSCRATCH=/scratch/$USER/$JOB_ID   # node-local; Q-Chem writes large files here
export QCLOCALSCR=$QCSCRATCH/local
mkdir -p "$QCLOCALSCR"
qchem -nt 16 job.in job.out
```
* `qchem -save input.in output.out savedir` keeps the scratch directory (needed to `read` across
  separate runs rather than `@@@` steps).

## DFT, dispersion, solvation

* Dispersion: `DFT_D D3_BJ` (or `D3_ZERO`, ...). ωB97X-D, ωB97X-V and ωB97M-V already include
  their own dispersion/non-local term; do not add `DFT_D`.
* SMD: `SOLVENT_METHOD SMD` + a `$smx` section: `$smx` / `solvent water` / `$end`.
* PCM: `SOLVENT_METHOD PCM` + `$solvent` / `SolventName Water` / `$end`.
* Hard SCF: `SCF_ALGORITHM DIIS_GDM` (or `GDM`), `MAX_SCF_CYCLES 200`.

## Output parsing

* Success marker: `Thank you very much for using Q-Chem.  Have a nice day.`
* Final SCF energy: `Total energy in the final basis set = ...`; optimisation converged:
  `**  OPTIMIZATION CONVERGED  **`; TS jobs print the same marker.
* ARC supports Q-Chem as an ESS (`ess_settings: {qchem: <server>}`); its adapter and submit
  template live in `arc/job/adapters/qchem.py` and `arc/settings/submit.py`.
