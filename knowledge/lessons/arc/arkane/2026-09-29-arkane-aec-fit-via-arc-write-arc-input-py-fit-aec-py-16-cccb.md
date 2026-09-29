---
title: 'Arkane AEC fit via ARC: write_arc_input.py + fit_aec.py (16 CCCBDB species,
  sp only)'
domain: arc
software: arkane
doc_type: lesson
status: unreviewed
tags:
- AEC
- atom energy corrections
- AEJob
- fit_aec
- write_arc_input
- CCCBDB
- quantum_corrections
- data.py
- arkane
- zeus
author: calvin
date: '2026-09-29'
similar:
- knowledge/lessons/arc/arkane/2026-09-29-arkane-aec-bac-for-a-new-level-use-the-group-s-arc-driven-sc.md
---

# Arkane AEC fit via ARC: write_arc_input.py + fit_aec.py (16 CCCBDB species, sp only)

## Mistake

Treated the AEC fit as a hand-written Arkane ae() input, collecting the 16 single-point energies by hand. The group builds it with two scripts around an ARC sp-only run (zeus:~/runs/ARC/AE_Corr_wb97xd3).

## Correct approach

Run both scripts in rmg_env and ARC in arc_env. Set the same LOT dict in both scripts, e.g. {"method": "wb97x-d3", "basis": "def2tzvp", "software": "orca"}.
The label map is used by both scripts (ARC label -> Arkane reference label, which must match SPECIES_LABELS in arkane/encorr/ae.py):
```python
SPECIES_LABELS = {"Br2": "Dibromine", "BrH": "Hydrogen bromide", "CH3": "Methyl", "CH3Cl": "Chloromethane",
    "CH4": "Methane", "Cl2": "Dichlorine", "ClH": "Hydrogen chloride", "F2": "Difluorine", "FH": "Hydrogen fluoride",
    "H2": "Dihydrogen", "H2O": "Water", "H2S": "Hydrogen sulfide", "H3N": "Ammonia", "N2": "Dinitrogen",
    "O2": "Dioxygen", "S2": "Disulfur"}
```
1. write_arc_input.py writes input.yml using the experimental CCCBDB geometry. Do not optimise:
```python
from arkane.encorr.reference import ReferenceDatabase
db = ReferenceDatabase(); db.load()
ref = db.get_species_from_label(human_label)[0]
xyz_dict = ref.reference_data["CCCBDB"].xyz_dict   # also use ref.smiles, ref.charge, ref.multiplicity
```
input.yml header: `sp_level: {method, basis, software}`, `compute_thermo: false`, `job_types: {conf_opt: false, opt: false, fine_grid: false, freq: false, sp: true, rotors: false}`. Each species gets label = formula, plus smiles, charge, multiplicity and xyz.
2. Run ARC (submit.sh on alon_q: `conda activate arc_env; python ~/Code/ARC/ARC.py input.yml`).
3. fit_aec.py: for each species, take input.log from the newest calcs/Species/<label>/sp_* folder (by mtime; folders are named sp_a<N>, so ignore failed attempts that have no log). Then:
```python
import rmgpy.constants as constants
from arkane.ess.factory import ess_factory
from arkane.encorr.ae import AEJob
from arkane.modelchem import LevelOfTheory
e_h = ess_factory(path).load_energy() / (constants.E_h * constants.Na)   # electronic energy only, Hartree
energies[human_label] = e_h
AEJob(species_energies=energies, level_of_theory=LevelOfTheory(**LOT)).execute(output_directory=".")
```
4. AEJob writes AEC_<method>_<basis>.out in append mode, with a 95% CI per element and a ready-to-paste dict. Paste the dict into RMG-database/input/quantum_corrections/data.py under atom_energies, or pass write_to_database=True. Start each fit in a fresh directory, or delete old AEC_*.out first: because of append mode, a copied directory can end up with a file labelled for one level but holding another level's numbers.

## Evidence / source

zeus:~/runs/ARC/AE_Corr_wb97xd3/{write_arc_input.py,input.yml,fit_aec.py,submit.sh} and AE_Corr (the DLPNO-CCSD(T)/cc-pVTZ version), read 2026-09-29. RMG-Py arkane/encorr/ae.py (AEJob.execute opens the output file with 'a'). In AE_Corr_wb97xd3, AEC_dlpnoccsd(t)_ccpvtz.out holds the wB97X-D3 numbers.
