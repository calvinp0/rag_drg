---
title: 'ORCA 6: %freq HessGridX 4 does not exist (only COSX grids 1-3 generated);
  use 3,3,3,3 for the finest'
domain: ess
software: orca
version: 6.0.0
doc_type: lesson
status: unreviewed
tags:
- HessGridX
- COSX
- freq
- gridx4.tmp
author: someuser
date: '2026-09-29'
---

# ORCA 6: %freq HessGridX 4 does not exist (only COSX grids 1-3 generated); use 3,3,3,3 for the finest

## Mistake

Set `%freq HessGridX 4,4,4,4` to raise the Hessian COSX grid. Job died: `CANNOT OPEN FILE input.gridx4.tmp`.

## Correct approach

ORCA 6.0.0 generates only COSX grids 1-3 here and HessGridX indexes those grids (manual 7.3.6), so 4 is invalid. Use `HessGridX 3,3,3,3` (finest generated). The default 2,2,2,2 is inferred from the manual's example, not stated. Measured (D202): with 3,3,3,3 the spectrum still reproduced the pilot's -9.5 cm-1 imaginary mode, so the grid was not its cause.

## Evidence / source

molecular-discovery docs/DECISIONS.md D200 execution addendum (idx 3 of JOBID[]) and D202 row 3
