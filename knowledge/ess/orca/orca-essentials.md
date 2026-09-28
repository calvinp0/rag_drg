---
title: ORCA input essentials and gotchas
domain: ess
software: orca
version: ["5", "6"]
doc_type: gotcha
status: draft
tags: [maxcore, pal, nprocs, moread, gbw, hess, optts, irc, neb, dlpno, rijcosx, cpcm, mpi, openmpi]
---
# ORCA input essentials and gotchas

## Input skeleton

```
! wB97X-D3 def2-TZVP Opt Freq TightSCF
%pal nprocs 16 end
%maxcore 3000
* xyz 0 1
C   0.000000   0.000000   0.000000
H   ...
*
```

* `!` lines hold simple keywords (case-insensitive, can span several `!` lines).
* `%block ... end` holds detailed settings. Every block ends with `end`.
* Coordinates: `* xyz charge mult` ... `*`, or read a file with `* xyzfile 0 1 geom.xyz`.
* Multiplicity > 1 switches to UHF/UKS automatically. Use `! ROHF`/`! ROKS` explicitly if wanted.

## Memory and cores (gotcha)

* `%maxcore` is **MB per process (core)**, not total. Total ≈ nprocs × maxcore.
  ORCA can go beyond `%maxcore`, so set it to about **75% of (memory per core)** that you
  ask the scheduler for.
* `%pal nprocs N end` or `! PAL8` (the `!PALn` form only exists for some n; `%pal` always works).
* For parallel runs, ORCA must be called with its **full (absolute) path** (`/abs/path/orca_6_0_x/orca job.inp > job.out`),
  **never** through `mpirun orca`; ORCA starts its own MPI processes. The OpenMPI version must match
  the one ORCA was built against (check the release notes for your ORCA build); put that
  OpenMPI's `bin/` on `PATH` and `lib/` on `LD_LIBRARY_PATH` (see `hpc/templates/slurm_orca.sh`).

## Restarting / reading orbitals and Hessians

```
! MORead
%moinp "previous.gbw"
```
* The `.gbw` being read must **not** have the same basename as the new job (ORCA overwrites it).
* Hessian for a TS optimisation: `%geom inhess read inhessname "freq_job.hess" end`.
* Restart an optimisation from the last geometry: use `jobname.xyz` (last step) as `* xyzfile`.

## Transition states, IRC, NEB

```
! wB97X-D3 def2-SVP OptTS Freq
%geom
  Calc_Hess true      # exact Hessian at the first step
  Recalc_Hess 5       # recompute every 5 steps (expensive, robust)
end
```
* IRC: `! IRC` with `%irc MaxIter 50 InHess read Hess_Filename "ts_freq.hess" end`
  (or let it compute the Hessian). Check your version's manual for the exact keyword spelling.
* NEB-TS: `! NEB-TS` with `%neb NEB_End_XYZFile "product.xyz" Nimages 8 end`; the
  reactant is the geometry in the input. ARC's `orca_neb` TS adapter drives this.
* A good TS has exactly one imaginary frequency; ORCA prints imaginary modes as negative numbers.

## DFT defaults that changed

* ORCA 5 replaced the old `Grid4`/`FinalGrid5` grid keywords with `DefGrid1`/`DefGrid2`
  (default)/`DefGrid3`. Old `GridX` keywords from ORCA 4 inputs do not work.
* RI-J is on by default for pure (GGA/meta-GGA) functionals and RIJCOSX for hybrids (ORCA 5+),
  with the auxiliary basis picked automatically (`def2/J`). Use `! NoRI` / `! NoCOSX` for
  exact integrals when benchmarking.

## Coupled cluster

```
! DLPNO-CCSD(T) cc-pVTZ cc-pVTZ/C TightPNO TightSCF
```
* DLPNO needs a correlation auxiliary basis (`/C`). For F12: `DLPNO-CCSD(T)-F12` with
  `cc-pVTZ-F12 cc-pVTZ-F12-CABS cc-pVTZ/C` (ARC level dicts carry these as `basis`,
  `cabs`, `auxiliary_basis`).
* Open-shell DLPNO uses a UHF/QRO reference automatically; check `T1` diagnostics.

## Solvation

* `! CPCM(Water)` for C-PCM. SMD: `%cpcm smd true SMDsolvent "water" end`.

## Output parsing

* Success: `****ORCA TERMINATED NORMALLY****`.
* Energies: `FINAL SINGLE POINT ENERGY` (last occurrence = final geometry).
* Files: `.gbw` (orbitals), `.hess`, `.xyz` (final geometry), `_trj.xyz` (opt trajectory),
  `.engrad`, and a property file (`_property.txt` in ORCA 5, `.property.txt` in ORCA 6; check).

## ORCA 6 specific

* ORCA 6 aborts on a detected SCF instability instead of only reporting it (see ARC's stability
  notes). Stability analysis: `%scf STABPerform true end`.
