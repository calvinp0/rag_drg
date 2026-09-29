---
title: "Arkane energy corrections (AEC/BAC): what happens when a level of theory has none, and how to fit them"
domain: arc
software: arkane
doc_type: card
status: draft
tags: [arkane, arc, thermo, aec, bac, atom_energies, atomEnergies, bond_additivity, petersson, melius, pbac, mbac, quantum_corrections, level_of_theory, arkane_level_of_theory, bac_type, freq_scale_factor, reference_sets, isodesmic]
---
# Arkane energy corrections (AEC/BAC)

Sources: RMG-Py `documentation/source/users/arkane/input.rst`, `arkane/encorr/{ae,bac,corr,reference}.py`,
`arkane/input.py`, `examples/arkane/bac/` (RMG-Py b342f9b); ARC `arc/statmech/arkane.py`,
`arc/scripts/get_qm_corrections.py`, `data/AEC.yml`, `docs/source/{input_reference,advanced}.rst` (ARC d9f47ab).

## What they are

* **AEC** (atom energy corrections, Arkane `atomEnergies` / `useAtomCorrections`): per-element atomic energies
  **in Hartree** at the single-point level. Arkane uses them (plus spin-orbit corrections) to put computed
  energies on the usual gas-phase reference states. Without them, **enthalpies and thermo are not meaningful**.
  Per the Arkane docs, kinetics and equilibrium constants are still correct with `useAtomCorrections = False`.
* **BAC** (bond additivity corrections, `useBondCorrections`, `bondCorrectionType`): `'p'` Petersson-type,
  one parameter per bond type; `species()` needs `bonds`, which are filled in automatically if the structure is
  given. `'m'` Melius-type, three parameters per atom type plus an optional molecular term; connectivity
  is inferred from the output. BAC and isodesmic-reaction corrections are mutually exclusive.
* The corrections are keyed by level of theory, `LevelOfTheory(method=..., basis=..., software=..., year=...)`
  (or `CompositeLevelOfTheory(freq=..., energy=...)`). They live in the RMG-database file
  `input/quantum_corrections/data.py` in the sections `atom_energies`, `pbac` and `mbac`, plus frequency scale factors.
  **A correction set belongs to a method/basis *and* the ESS that produced the energies.**

## What ARC does when the level has no corrections

ARC matches the **sp level** against `RMG_DB_PATH/input/quantum_corrections/data.py`. The method is compared
ignoring hyphens and an optional 4-digit year; the basis ignoring hyphens and spaces.
* If a year is given via `arkane_level_of_theory.year`, only that year matches. Never put the year into the
  method name, e.g. `wb97xd32023`.
* Without a year, an entry with no year wins; otherwise the latest year is used.

The possible outcomes:
1. **AEC and BAC found** → a normal Arkane run with corrections (log: "Arkane energy corrections matched for ...").
2. **AEC found, no BAC** → warning "... but bond additivity corrections (BAC) were NOT found ...". Thermo
   uses AEC without BAC.
3. **No AEC in RMG-database** → ARC falls back to its own `data/AEC.yml`, keyed by `Level.simple()`. That file
   currently only has `gfn2` and `torchani`.
4. **Neither** → warning "SP level ... is not recognized by Arkane and has no AEC entry in ARC. Atom and bond
   energy corrections will be DISABLED for this Arkane run". **ARC continues**: the thermo it writes has no
   AEC/BAC, so its H298 is not meaningful. Treat this warning as a blocker for thermo, not as noise.

Before a thermo run, check whether the level is covered. Either look at the ARC log for the lines above, or
use ARC's helper from the RMG environment: `python arc/scripts/get_qm_corrections.py in.yaml out.yaml` with
`matched_key` and `bac_type`. A missing section comes back as null.

ARC input keys involved: `arkane_level_of_theory` (the level used for Arkane corrections, when it differs
from `sp_level`), `bac_type` (`p`, `m` or `null`), `freq_scale_factor`, and `calc_freq_factor` (default true:
ARC computes a frequency scale factor if none is found).

## Recipe: new corrections for a new ESS / level of theory

**The group's way is scripted around ARC runs**, using the templates in `knowledge/arc/templates/aec_bac/`.
Copy the folder into a fresh run directory and set `LEVEL` in `aec_bac_common.py`. Then:
`write_arc_input.py` → ARC → `fit_aec.py` (`AEJob`) for AEC, and
`bac_write_arc_input.py` → ARC → `fit_bac.py` → `run_bac_fit.py` (`BACJob`) for BAC. The folder's
README ("Template: fit Arkane AEC and BAC for a new level of theory") has the full procedure and how to
adapt the scripts. The steps below explain what the scripts do and
what to check. The hand-written `ae()`/`bac()` Arkane inputs are the manual alternative.

**1. AEC (needed for any meaningful thermo).**
* Compute **single-point electronic energies (Hartree, no ZPE)** at the target level for the fitting species.
* Use the **experimental geometries from the reference database** (`RMG-database/input/reference_sets/`).
* The fitting species are `SPECIES_LABELS` in `arkane/encorr/ae.py`: Dihydrogen, Dinitrogen, Dioxygen,
  Disulfur, Difluorine, Dichlorine, Dibromine, Hydrogen fluoride, Hydrogen chloride, Hydrogen bromide,
  Hydrogen sulfide, Water, Methane, Methyl, Ammonia, Chloromethane.
* `AE.fit` fits one energy per element that appears in the species you provide. It is a least-squares fit
  against CCCBDB atomization energies, with the reference ZPE added from the database. So:
  * you may leave out species whose elements you don't need (Arkane warns for each one missing);
  * give more species than elements, or the confidence intervals are undefined (n - k = 0).

Then fit with an Arkane input file:
```python
lot = LevelOfTheory(method='wB97X-D', basis='def2-TZVP', software='Gaussian')   # the level you ran
ae(species_energies={'Dihydrogen': -1.17..., 'Water': -76.4..., 'Methane': -40.5..., ...},  # Hartree
   level_of_theory=lot,
   write_to_database=False)   # True writes into your RMG-database quantum_corrections/data.py
```
As a stop-gap without touching the database, put the fitted values directly in an Arkane input as
`atomEnergies = {'H': ..., 'C': ..., ...}` (Hartree). Per the docs, BACs are then not applied.

**2. BAC (optional; improves thermo).**
* `bac()` takes its training data **from the reference database**. Each reference species needs
  `calculated_data` for your `LevelOfTheory`.
* So first compute H298 for the reference species at that level and store it (the template `fit_bac.py`
  does this from the ARC outputs). By hand: run them through Arkane and add the results. `ReferenceSpecies`
  has `update_from_arkane_spcs(arkane_species)`, which stores H298 and geometry under the Arkane species'
  level of theory; save with `save_yaml`.

Then fit, following `examples/arkane/bac/wb97m-v_def2-tzvpd/input.py`:
```python
bac(level_of_theory=lot, bac_type='p', weighted=True, write_to_database=False)
bac(level_of_theory=lot, bac_type='m', fit_mol_corr=True, global_opt=True, global_opt_iter=10)
```
`crossval_n_folds` (1 = no cross-validation, -1 = leave-one-out) checks transferability before writing;
nothing is written to the database while cross-validating.

**3. Use them.**
* Once AEC/BAC for `lot` are in the `quantum_corrections/data.py` of the RMG-database that `RMG_DB_PATH`
  points to, ARC picks them up (use `arkane_level_of_theory` if the key differs from `sp_level`).
* Keep method/basis/software identical to the key.
* Alternative without BACs: `useIsodesmicReactions = True` in Arkane (turns AEC on, BAC off; uses the
  reference sets).

## Caution: ARC `data/AEC.yml` units

The header of ARC's `data/AEC.yml` says **kJ/mol**, and ARC writes the entry into the Arkane input as
`atomEnergies = {...}` without converting it. Arkane's `get_atom_correction` treats `atomEnergies` as
**Hartree**. Before relying on (or adding) an `AEC.yml` entry, check which unit it really is. For values
fitted with `ae()` (Hartree), put them in the RMG-database, not in `AEC.yml`, until this is settled.
