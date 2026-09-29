---
title: 'Arkane AEC/BAC for a new level: use the ARC-driven template scripts (knowledge/arc/templates/aec_bac)'
domain: arc
software: arkane
doc_type: lesson
status: unreviewed
tags:
- AEC
- BAC
- atom energy corrections
- bond additivity corrections
- arkane
- encorr
- reference_sets
- AEJob
- BACJob
- fit_aec
- fit_bac
- template
author: calvin
date: '2026-09-29'
---

# Arkane AEC/BAC for a new level: use the ARC-driven template scripts (knowledge/arc/templates/aec_bac)

## Mistake

Explained how to fit new Arkane AEC/BAC from the RMG-Py source only, with hand-built ae()/bac() Arkane
input files. The group already has a scripted workflow that runs the reference species through ARC.

## Correct approach

Use the templates in `knowledge/arc/templates/aec_bac/` (README: "Template: fit Arkane AEC and BAC for a
new level of theory"). Copy the folder into a fresh run directory and set `LEVEL` in `aec_bac_common.py`;
every script reads it from there. Run the scripts in rmg_env and ARC in arc_env.
AEC:
1. `write_arc_input.py`: sp only on the 16 AEC species (Br2, HBr, CH3, CH3Cl, CH4, Cl2, HCl, F2, HF, H2,
   H2O, H2S, NH3, N2, O2, S2) at their experimental CCCBDB geometries.
2. Run ARC.
3. `fit_aec.py`: `AEJob`, then put the AECs into RMG-database `quantum_corrections/data.py` (`atom_energies`).
BAC (needs those AECs in data.py):
1. `bac_write_arc_input.py`: opt+freq+sp for every species in `reference_sets['main']`.
2. Run ARC.
3. `fit_bac.py`: H298 with AECs, stored as `calculated_data` in the reference database.
4. `run_bac_fit.py`: `BACJob` Petersson, then Melius.
The `LevelOfTheory` key must match the level ARC later uses for thermo (`sp_level` or
`arkane_level_of_theory`), including the software. Commit the RMG-database changes, or they are lost on
the next pull/reset of that checkout.

## Evidence / source

An earlier group AEC and BAC run (2026-01); the templates reproduce that
AEC fit and the H298 values exactly (checked 2026-09-29). RMG-Py arkane/encorr/{ae,bac,reference}.py.
