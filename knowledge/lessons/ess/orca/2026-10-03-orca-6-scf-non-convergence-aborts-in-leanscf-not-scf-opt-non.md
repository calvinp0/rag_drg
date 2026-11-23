---
title: ORCA 6 SCF non-convergence aborts in LEANSCF, not SCF; opt non-convergence
  message wraps at a variable point
domain: ess
software: orca
version: 6.0.0
doc_type: lesson
status: unreviewed
author: someuser
date: '2026-10-03'
---

# ORCA 6 SCF non-convergence aborts in LEANSCF, not SCF; opt non-convergence message wraps at a variable point

## Mistake

Built a retry trigger for ORCA SCF non-convergence on the pattern `ORCA finished by error termination in SCF` (the knowledge base's `o-scf-error-termination` entry), and a trigger for optimisation non-convergence on the single-line fragment `did not converge but reached the maximum number of`. Both FAILED to match real ORCA 6.0.0 output in a deliberate-failure run on Zeus (jobs JOBID[0] and [1], wB97M-D3BJ/def2-TZVPP Opt Freq TightOpt DEFGRID3 RIJCOSX def2/J TightSCF, nitromethane), so both failures were classified as unknown and NEITHER retry ever fired.

1. With `%scf MaxIter 2 end` forcing failure, ORCA printed `ORCA finished by error termination in LEANSCF` (plus `orca_leanscf_mpi`, `qcmsg.cpp line 394`, `.... aborting the run`). The string `in SCF` never appears, and `This wavefunction IS NOT FULLY CONVERGED!` was absent too because the run aborted rather than continuing.

2. With `%geom MaxIter 2 end`, the message is emitted inside an `ERROR !!!` block across two lines, and the WRAP POSITION DEPENDS ON THE RUN. Same ORCA 6.0.0 binary: BP86/def2-SVP on 3 atoms broke after "number of"; wB97M-D3BJ/def2-TZVPP on 7 atoms broke after "maximum ". A fragment calibrated on one sample fails on the other.

## Correct approach

1. ORCA 6 uses LeanSCF by default, so LEANSCF is the EXPECTED module for an SCF abort, not an exotic case. Detect SCF non-convergence on `error termination in SCF` OR `error termination in LEANSCF`, anchored at end of line so `error termination in SCF gradient` (the RIJK gradient bug) is excluded. Do NOT widen to the generic `error termination in \w+`: that also catches MDCI, GTOInt and CASSCF aborts, which are not SCF problems and must not trigger an SCF retry. The knowledge base's generic entry `o-error-termination-other-module` does list LEANSCF as an example, so the information was there -- the trap is that the SCF-specific entry looks like the right one and is too narrow.

2. For optimisation non-convergence match the stable prefix `The optimization did not converge`, or normalise newlines before matching. Never match a multi-word fragment that spans ORCA's line wrap.

3. Do not treat `****ORCA TERMINATED NORMALLY****` or exit status 0 as success: the non-converged optimisation printed both alongside its ERROR block. ORCA does NOT compute frequencies on an unconverged geometry -- it prints "As a subsequent Frequencies calculation has been requested ORCA will abort at this point of the run" and advises restarting "with the lowest energy geometry and/or a larger maxiter", which is ORCA's own endorsement of a restart-with-larger-MaxIter continuation.

4. Related measurements on the same cluster: `%geom MaxIter` IS honoured on 6.0.0 despite the 6.1.1 changelog entry "%geom MAXITER 50 was ignored" (probe JOBID); the `%geom MaxIter` default is max(3N,50), not 200 (the manual's "default 200" belongs to the L-BFGS optimizer); and ORCA silently takes the LAST of duplicate `%block` settings (probe JOBID), so an appended keyword overrides an earlier one.
