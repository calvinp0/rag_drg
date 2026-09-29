---
title: 'ORCA: wB97X-D3 keyword is wb97x-d3, not wb97xd-3 (ARC passes the method string
  straight through)'
domain: ess
software: orca
doc_type: lesson
status: unreviewed
tags:
- wb97x-d3
- wb97xd3
- keyword
- syntax error
- ORCA
- ARC
- level of theory
author: calvin
date: '2026-09-29'
---

# ORCA: wB97X-D3 keyword is wb97x-d3, not wb97xd-3 (ARC passes the method string straight through)

## Mistake

ARC level `method: wb97xd-3, software: orca` wrote `!rKS wb97xd-3 ...`. ORCA stopped with: "keywords WB97XD-3 can either be duplicated or illegal", and ARC raised JobError "Got a syntax error in orca".

## Correct approach

Use `method: wb97x-d3` for ORCA. ARC then writes `!rKS wb97x-d3 def2-tzvp tightscf defgrid2` and the job runs. Arkane's LevelOfTheory normalises both spellings to method 'wb97xd3', so AEC/BAC keys in data.py look the same either way. Only the ORCA input breaks.

## Evidence / source

zeus:~/runs/ARC/AE_Corr_wb97xd3: out.txt/err.txt (failed run with wb97xd-3) vs calcs/Species/CH4/sp_a2595/input.log (successful run with wb97x-d3), 2026-01-06.
