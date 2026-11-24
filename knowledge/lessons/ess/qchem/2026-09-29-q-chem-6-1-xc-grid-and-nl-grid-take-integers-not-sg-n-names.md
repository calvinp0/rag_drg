---
title: 'Q-Chem 6.1: XC_GRID and NL_GRID take integers, not SG-n names (''XC_GRID SG-2''
  -> Illegal rem input)'
domain: ess
software: qchem
version: '6.1'
doc_type: lesson
status: unreviewed
tags:
- XC_GRID
- NL_GRID
- SG-2
- SG-1
- Illegal rem input
- wB97M-V
author: someuser
date: '2026-09-29'
---

# Q-Chem 6.1: XC_GRID and NL_GRID take integers, not SG-n names ('XC_GRID SG-2' -> Illegal rem input)

## Mistake

Wrote `XC_GRID SG-2` and `NL_GRID SG-1` in $rem (copied from the effective-grid names Q-Chem prints in its log). All 7 jobs of array JOBID[] failed in ~30 s with "Illegal rem input".

## Correct approach

Q-Chem 6.1 manual specifies the integer form: use `XC_GRID 2` (SG-2) and `NL_GRID 1` (SG-1). The resubmitted array JOBID[] ran normally and the SG-2 grid was confirmed in each log. The names SG-2/SG-1 are what the log reports, not valid input values.

## Evidence / source

molecular-discovery docs/DECISIONS.md D211 (failed run JOBID[], fixed run JOBID[]), 2026-09-27; Q-Chem 6.1 manual
