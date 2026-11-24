---
title: "RMG-Py/RMS: pin Julia 1.10.11 \u2014 Julia >=1.10.12 segfaults under juliacall"
domain: project
software: rmg
version: RMG-Py 4.0.0, Julia 1.10.11, pyjuliacall 0.9.28
doc_type: lesson
status: unreviewed
tags:
- julia
- juliacall
- juliaup
- RMS
- ReactionMechanismSimulator
- install_rms.sh
- segfault
- rmg-py
- install
author: someuser
date: '2026-11-05'
---

# RMG-Py/RMS: pin Julia 1.10.11 — Julia >=1.10.12 segfaults under juliacall

## Mistake

Installing Julia for RMG-Py with the floating channel (`curl -fsSL https://install.julialang.org | sh -s -- --yes --default-channel 1.10`) picks the newest 1.10 patch (1.10.13 in Nov 2026). Then `source install_rms.sh` segfaults when Julia is embedded via juliacall: crash in copy(ENV) at env.jl:217, triggered whenever Julia spawns a subprocess (package compilecache, p7zip in Pkg.status). Precompiling packages beforehand (e.g. CompilerSupportLibraries_jll) only moves the crash to a later spawn; it is not a fix.

## Correct approach

Pin the exact version: `wget -qO- https://install.julialang.org | sh -s -- --yes --default-channel 1.10.11`, check `julia --version` gives 1.10.11, then do the libstdc++ symlink fix and `source install_rms.sh`. Existing install: `juliaup add 1.10.11; juliaup default 1.10.11`, redo the libstdc++ step, rerun install_rms.sh. If juliaup is interrupted before finishing, ~/.juliaup/bin/julia is missing: `ln -s julialauncher ~/.juliaup/bin/julia`. 1.10.10 also works; 1.10.12 and 1.10.13 crash.

## Evidence / source

Reproduced on the aluf cluster (Rocky 9.8), 2026-11-05: same install_rms.sh block crashed with 1.10.13, ran cleanly with 1.10.10 and 1.10.11 (full install + minimal RMG example passed). Upstream RMG-Py commit 62eb728c0 "Pin Julia and openbabel to unbreak CI" pins 1.10.11 in CI and Dockerfile.
