---
title: Opt MaxCycles can lower but not raise the step limit on G09 D.01; the 'maximum
  allowed number of steps' line never shows it
domain: ess
software: gaussian
version: 09
doc_type: lesson
status: unreviewed
tags:
- berny
- maxcycles
- l9999
- optimization
author: calvin
date: '2026-09-29'
---

# Opt MaxCycles can lower but not raise the step limit on G09 D.01; the 'maximum allowed number of steps' line never shows it

## Mistake

l9999 fix: restart with opt=(maxcycles=N) at 2-3x the previous count. And: read 'Number of steps in this run= N maximum allowed number of steps= M' to learn the limit Gaussian applied.

## Correct approach

On G09 Revision D.01 the applied Berny limit is min(MaxCycles, max(100, 6*NAtoms)): MaxCycles= only LOWERS the limit; requesting more than the default has no effect (measured on 437 logs). MaxCycle= (singular) and IOp(1/6=N) were shown to LOWER it identically, but neither has been tested in the RAISING direction on this build, so do not assume either can raise it -- that is untested, not established. The 'maximum allowed number of steps' line always prints the default max(100,6N) and never reflects the keyword -- read the applied limit from 'Step number k out of a maximum of M' or from '-- Number of steps exceeded, NStep= M'. So restarting an l9999 with a larger maxcycles does nothing when the default already binds; restart from the last geometry, or use RFO/GDIIS/cartesian instead.

## Evidence / source

471 of 471 returned opt logs (chemprop_cmpnn cohort DFT phase, 2026-09) obey min(requested, max(100,6N)); 437 requested 200 with a default below 200 and every one applied the default, never 200; 16 with NAtoms>=34 applied 200 below their default of 234 etc. Controlled test on propanal (10 atoms) from one displaced start: opt=(maxcycles=5), opt=(maxcycle=5) and IOp(1/6=5,2/9=2000) each stopped at NStep=5 ('Step number 5 out of a maximum of 5') while all three printed 'maximum allowed number of steps= 100'. Commit and data: paper/literature_cohort/dft_phase_decks_20260916/STEPCAP_RESULT.md, d083_results/stepcap_test/.
