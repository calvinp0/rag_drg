---
title: ORCA 6.0.0 orca_vpot needs input.K.tmp, which only `! KeepInts` keeps; the
  .gbw alone is not enough
domain: ess
software: orca
version: 6.0.0
doc_type: lesson
status: unreviewed
tags:
- orca_vpot
- KeepInts
- K.tmp
- densities
- electrostatic potential
author: someuser
date: '2026-10-03'
---

# ORCA 6.0.0 orca_vpot needs input.K.tmp, which only `! KeepInts` keeps; the .gbw alone is not enough

## Mistake

Assumed orca_vpot needs only the .gbw plus a .scfp density file (as the manual's usage says: `orca_vpot MyJob.gbw MyJob.scfp pts out`). On 6.0.0 there is no .scfp file on disk (the density is a name inside input.densities), and with gbw+densities+densitiesinfo present it still dies: `gtovpot.cpp line 1193: Error: Prescreening matrix could not be read!` KeepDens and KeepFock do not fix it.

## Correct approach

Keep four files under one base name: input.gbw, input.densities, input.densitiesinfo, input.K.tmp. K.tmp is deleted at the end of every normal run unless the input has `! KeepInts`. Call `orca_vpot input.gbw input.scfp pts.xyz out.txt` (points in bohr; output rows are x y z V in a.u.). K.tmp depends only on geometry+basis; it can be regenerated from the .gbw with `! <method> MORead NoIter KeepInts` + `%moinp "prev.gbw"` (byte-identical on test). Sanity check: (V(+400,0,0)-V(-400,0,0))/2*400^2 reproduces ORCA's Total Dipole Moment to ~1e-4 a.u.

## Evidence / source

Zeus probe jobs JOBID-JOBID (strace showed orca_vpot opening input.K.tmp, ENOENT) and backfill jobs JOBID/62/63, 2026-10-03
