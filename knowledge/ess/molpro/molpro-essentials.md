---
title: Molpro essentials and gotchas
domain: ess
software: molpro
version: ["2024", "2026"]
doc_type: gotcha
status: draft
tags: [memory, words, wf, spin, symmetry, nosym, rccsd, uccsd, f12, casscf, mrci, caspt2, optg, frequencies, molpro -n]
---
# Molpro essentials and gotchas

## Input skeleton

```
***,ethyl radical
memory,500,m              ! 500 mega-WORDS per process = 4 GB (1 word = 8 bytes)
gthresh,energy=1.d-8
geometry={
C  0.000  0.000  0.000
C  ...
H  ...
}                         ! xyz-style block -> Ångström
basis=cc-pVTZ-F12
set,charge=0
set,spin=1                ! 2S = unpaired electrons, NOT multiplicity
{hf}
{rccsd(t)-f12}
```

## Memory (gotcha)

* `memory,N,m` is in **mega-words (8 bytes) per MPI process**. `memory,1000,m` = 8 GB **per process**;
  with `molpro -n 8` that is 64 GB total. Agents often write `memory,8000,m` meaning 8 GB -> 64 GB/process.
* Command line `-m` overrides the input (same units; check `molpro --help` for your version's suffixes).

## Spin and symmetry (gotcha)

* `wf,nelec,irrep,spin`: `spin` = 2S (doublet 1, triplet 2). Same for `set,spin=`.
* Molpro uses Abelian point-group symmetry automatically and the state's irrep must be right.
  For automated workflows use `symmetry,nosym` (before/inside the geometry) or supply the correct
  `wf` card; a wrong irrep converges to the wrong state silently.

## Methods

* Closed shell: `{hf}` then `{ccsd(t)}`; F12: `{ccsd(t)-f12}` with `-F12` basis sets
  (Molpro picks matching CABS/fitting sets).
* Open shell: `{rhf}` (ROHF when spin>0) then `{rccsd(t)}` (partially spin-adapted, RHF-RCCSD(T)) or
  `{uccsd(t)}`; F12: `{rccsd(t)-f12}`/`{uccsd(t)-f12}`.
* Multireference: `{casscf; closed,..; occ,..; wf,..}` then `{mrci}` or `{rs2c}` (CASPT2).
* Geometry optimisation `{optg}` (TS: `{optg,root=2}`), frequencies `{frequencies}`.

## Running

```
molpro -n 16 -d $SCRATCH_DIR job.in       # -n MPI processes, -d scratch directory
```
* `-t N` sets threads per process (OpenMP). Total cores = n × t.
* Use node-local scratch (`-d`); Molpro writes large integral files.
* Output: `job.out` (and `job.xml`). Results are Molpro variables, e.g. `energy`; `show,energy`
  or `put,xyz,final.xyz` to export geometry.
