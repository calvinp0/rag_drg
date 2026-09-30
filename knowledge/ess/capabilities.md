---
title: ESS capability and naming matrix
domain: ess
doc_type: card
status: draft
tags: [capabilities, comparison, orca, gaussian, qchem, psi4, molpro, pyscf, naming, b3lyp, wb97xd, spin, multiplicity]
---
# ESS capability and naming matrix

For "does code X support method Y, and how is it written?" use the structured table in
`ess/levels_of_theory.yaml` (MCP tool `lookup_level_of_theory`, CLI `rag-drg level <name>`).

## Which code for which job (group defaults)

| Task | First choice | Notes |
|---|---|---|
| DFT opt/freq, TS search, IRC | Gaussian 16 or ORCA 6 | Both robust; Gaussian `Opt=TS` / ORCA `OptTS`. ORCA also has NEB-TS. |
| ωB97X-V / ωB97M-V, fast DFT, rich solvation (SMD, PCM variants) | Q-Chem 6.1 | Home of the ωB97 family and VV10 functionals. |
| DFT on GPU | Gaussian 16 GPU build, or PySCF + gpu4pyscf | G16 GPUs speed up HF/DFT energies, gradients and frequencies only. |
| DLPNO-CCSD(T) single points | ORCA | `DLPNO-CCSD(T)`, `DLPNO-CCSD(T1)`; needs a `/C` auxiliary basis. |
| Canonical CCSD(T), CCSD(T)-F12, MRCI, CASPT2 | Molpro | Molpro is the reference for multireference and F12 work. |
| Strong static correlation (multireference character: bond breaking, diradicals, near-degenerate states) | Molpro | See "Strong static correlation" below. |
| SAPT, quick scripted workflows, open-source reproducibility | Psi4 | Python API; SAPT0/2+/(DFT). |
| Job types | all | TS: Gaussian `Opt=TS`, ORCA `OptTS`, Q-Chem `JOBTYPE ts`, Psi4 `opt_type ts`, Molpro `{optg,root=2}`. IRC: Gaussian `IRC`, ORCA `IRC`, Q-Chem `JOBTYPE rpath`, Psi4 `opt_type irc`. |
| Custom methods, ML/data pipelines, in-Python loops | PySCF | Everything is a Python object; easy to batch and to get integrals/densities. |

## Strong static correlation (multireference): which method and program

For accurate energies of a molecule with strong static correlation (bond breaking, diradicals,
twisted double bonds, near-degenerate states), single-reference methods (HF, DFT, CCSD(T)) are
unreliable. The ORCA manual ("Static versus Dynamic Correlation") shows this for the C2H4 twist
and F2 dissociation: RHF fails, and CCSD overbinds F2 compared with the MRACPF reference.

* **Group default: Molpro.** CASSCF for the static part, then dynamic correlation on top: CASPT2
  (`{rs2c}`), MRCI(+Q) or NEVPT2. Molpro is the group's reference code for multireference work.
* **ORCA**: CASSCF + NEVPT2 (and MRCI/MRACPF, as in the manual's examples).
* **PySCF**: CASSCF + NEVPT2, scriptable.
* Warning sign in a coupled-cluster run: the T1 diagnostic. The ORCA manual's rule of thumb: above
  0.02, do not trust the single-reference result.
* Method keywords per code: `levels_of_theory.yaml` (CASSCF, CASPT2, NEVPT2, MRCI).

## Charge / spin conventions (the most common agent mistake)

| Code | How spin is specified |
|---|---|
| Gaussian | `charge multiplicity` line, e.g. `0 2` |
| ORCA | `* xyz charge multiplicity`, e.g. `* xyz 0 2` |
| Q-Chem | first line of `$molecule`: `0 2` (charge multiplicity) |
| Psi4 | first line of `molecule {}`: `0 2` (charge multiplicity) |
| Molpro | `wf,nelec,symmetry,spin` where **spin = 2S = number of unpaired electrons** (doublet -> 1), or `set,charge=..,spin=..` |
| PySCF | `gto.M(..., charge=0, spin=1)` where **spin = 2S = N_alpha - N_beta** (doublet -> 1, triplet -> 2), *not* the multiplicity |

## Functional names are not portable

* **B3LYP** differs by VWN flavour. Gaussian's B3LYP uses VWN(III, RPA). ORCA's `B3LYP` uses
  VWN5 (`B3LYP/G` is the Gaussian variant). Check Q-Chem's definition in its manual. Psi4 `b3lyp` vs `b3lyp5` differ the same way.
  PySCF >= 2.3 switched `B3LYP` to the VWN-RPA (Gaussian-like) definition. Energies differ at the
  mEh level, so **do not mix codes within one energy difference.**
* **wB97X-D (Gaussian `wB97XD`)** is Chai & Head-Gordon 2008 with its own damped dispersion.
  ORCA's `wB97X-D3` is a *different* parametrisation (wB97X re-fitted with D3). ORCA 6 also has
  `wB97X-D4`, `wB97X-V`, `wB97M-V`. Q-Chem's `wB97X-D` is the original 2008 functional (same as Gaussian's).
  Name the exact functional in papers and ARC levels.
* Dispersion keywords: Gaussian `EmpiricalDispersion=GD3BJ`; ORCA `D3BJ` / `D4` in the `!` line; Q-Chem `DFT_D D3_BJ`;
  Psi4 suffix `-d3bj` (e.g. `b3lyp-d3bj`); PySCF via `mf.disp = 'd3bj'` (needs the dftd3
  interface installed; check your version).

## Basis set spelling

| Basis | Gaussian | ORCA | Q-Chem | Psi4 / PySCF | Molpro |
|---|---|---|---|---|---|
| def2-TZVP | `Def2TZVP` | `def2-TZVP` | `def2-TZVP` | `def2-tzvp` | `def2-TZVP` |
| cc-pVTZ | `cc-pVTZ` | `cc-pVTZ` | `cc-pVTZ` | `cc-pvtz` | `cc-pVTZ` (or `vtz`) |
| aug-cc-pVTZ | `aug-cc-pVTZ` | `aug-cc-pVTZ` | `aug-cc-pVTZ` | `aug-cc-pvtz` | `aug-cc-pVTZ` (or `avtz`) |

## Normal-termination markers (for parsers)

| Code | Success marker in output |
|---|---|
| Gaussian | `Normal termination of Gaussian` (one per link step in multi-step jobs) |
| ORCA | `****ORCA TERMINATED NORMALLY****` |
| Q-Chem | `Thank you very much for using Q-Chem.  Have a nice day.` |
| Psi4 | `*** Psi4 exiting successfully. Buy a developer a beer!` |
| Molpro | No error trailer and a `Molpro calculation terminated` line |
| PySCF | Python exits 0; check `mf.converged` (and `opt.converged` for optimisations) |
