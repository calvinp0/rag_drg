---
title: Gaussian 09/16 input essentials and gotchas
domain: ess
software: gaussian
version: ["09", "16"]
doc_type: gotcha
status: draft
tags: [link0, nprocshared, mem, chk, oldchk, route, opt, ts, calcfc, irc, freq, scf, gpu, cpu, gpucpu, l9999, l502, int, ultrafine]
---
# Gaussian 09/16 input essentials and gotchas

## Input skeleton (blank lines matter)

```
%nprocshared=16
%mem=48GB
%chk=job.chk
#P wB97XD/Def2TZVP Opt Freq Int=UltraFine SCF=XQC

title line

0 1
C   0.000000   0.000000   0.000000
H   ...

```
* Sections: Link0 (`%...`) -> route (`#...`, may continue on several lines) -> **blank** -> title
  -> **blank** -> `charge multiplicity` + coordinates -> **blank line at the end of the file**.
  A missing final blank line is a classic error.
* `#P` gives verbose output (recommended for parsing).

## Memory and cores (gotcha)

* `%mem` is the **total** memory for the job (all threads share it), unlike ORCA's per-core
  `%maxcore`. Leave ~10-20% of the scheduler allocation free for the OS and Gaussian itself;
  `galloc: could not allocate memory` means `%mem` is larger than the node or queue gives.
* `%nprocshared=N` (G09 and G16). G16 also has `%cpu=0-15` to pin specific cores; use only one
  of `%cpu`/`%nprocshared`.
* Multi-node needs Linda (`%lindaworkers=`); we usually run single-node.

## GPUs (G16 GPU build only; G09 has no GPU support)

```
%cpu=0-15
%gpucpu=0-1=0,1
%mem=64GB
```
* `%gpucpu=gpu-list=controlling-cpu-list`: each GPU needs a dedicated controlling CPU core, and
  those cores must be part of `%cpu`.
* GPUs accelerate HF/DFT energies, gradients and frequencies. Post-HF (MP2, CCSD(T)) runs on the
  CPUs; there is nothing to gain from GPUs for those jobs.
* Request the GPUs from the scheduler (e.g. `#SBATCH --gres=gpu:2`) and make sure `%gpucpu` indices
  match what the job was given (`nvidia-smi`, `CUDA_VISIBLE_DEVICES`).

## Grid and SCF defaults (G09 vs G16)

* Default integration grid: **G16 = UltraFine**, **G09 = FineGrid**. To reproduce/compare across
  versions, set `Int=UltraFine` (or `Int=Grid=UltraFine` in G09) explicitly.
* Hard SCF: `SCF=(XQC,MaxCycle=512)` or `SCF=QC`; for open-shell, check `Stable=Opt`.

## Optimisations, TS, IRC

* Minimum: `Opt Freq`. Tight: `Opt=Tight` (or `VeryTight`).
* A TS search needs the curvature (Hessian) at the start: the default guess Hessian must be improved
  (G09 `Opt` keyword page, "Options related to initial force constants"). `CalcFC` computes the
  force constants at the first point; `ReadFC` reads them from a checkpoint, preferably from a
  lower-level frequency job.
* TS: `Opt=(TS,CalcFC,NoEigenTest)` (`CalcAll` for a Hessian every step, expensive but robust),
  or `Opt=(TS,ReadFC)` with `%oldchk=freq.chk`.
* Restart an opt/geometry from a checkpoint: `%oldchk=old.chk` + `Geom=AllCheck Guess=Read`
  (`Geom=AllCheck` also takes charge/mult/title from the chk, so omit those sections).
* IRC: `IRC=(CalcFC,MaxPoints=50,StepSize=10)` or `IRC=(RCFC)` to read force constants from chk.
* Frequency checks: exactly one imaginary frequency (printed negative) for a TS.

## Solvation, dispersion, basis

* `SCRF=(SMD,Solvent=Water)`, `SCRF=(PCM,Solvent=Water)`.
* `EmpiricalDispersion=GD3BJ` (or `GD3`). `wB97XD` already includes its own dispersion; do not add GD3.
* Mixed/custom basis: `Gen` (or `GenECP` with ECPs) and a basis block after the coordinates.

## Common error terminations

| Message | Usual meaning / fix |
|---|---|
| `Error termination via Lnk1e in .../l9999.exe` | Optimisation ran out of steps -> restart from last geometry, `Opt=(MaxCycles=..)`, better Hessian (`CalcFC`) |
| `... l502.exe` | SCF did not converge -> `SCF=XQC`, better guess, check charge/multiplicity |
| `... l101.exe` | Input/format error (charge/mult/basis/coordinates) |
| `Erroneous write` / `write error` | Disk/scratch full -> check `GAUSS_SCRDIR` and quota |
| `galloc: could not allocate memory` | `%mem` too large for the allocation |
| `Convergence failure -- run terminated.` | SCF failure (see l502) |

* Success marker: `Normal termination of Gaussian`.
