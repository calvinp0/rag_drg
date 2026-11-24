---
title: ORCA 5.0.4 ZORA/DKH2 frequency jobs run an analytic SCF Hessian (one-centre
  auto-enabled), confirmed by test runs
domain: ess
software: orca
version: '5'
doc_type: lesson
status: unreviewed
tags:
- hessian
- frequencies
- zora
- dkh
- relativistic
- onecenter
- analytic
- orca5
- tckdb
- hessian_method
author: someuser
date: '2026-10-31'
similar:
- knowledge/lessons/ess/orca/2026-10-31-orca-zora-dkh-hessians-are-analytic-via-the-one-centre-appro.md
---

# ORCA 5.0.4 ZORA/DKH2 frequency jobs run an analytic SCF Hessian (one-centre auto-enabled), confirmed by test runs

## Mistake

ORCA 5.0.4 manual §9.25 (chunk 14573) lists "relativistic corrections" among cases where analytical Hessians cannot be performed; read literally, ZORA/DKH frequencies on ORCA 5 would be numerical.

## Correct approach

On ORCA 5.0.4 (and 5.0.3), `! Freq ZORA` and `! Freq DKH2` print "WARNING: In scalar relativistic gradient/hessian runs we need the one-center approximation ===> : %rel OneCenter is turned on", then run "ORCA SCF HESSIAN" / "Hessian of the Kohn-Sham DFT energy" with no "ORCA NUMERICAL FREQUENCIES" header, and terminate normally. So ZORA/DKH Hessians are analytic on ORCA >= 5 with the default one-centre approximation (not with RelFull / OneCenter false). X2C Hessians are semi-numeric (ORCA 6).

## Evidence / source

Zeus test runs 2026-10-31 (PBS JOBID, JOBID; ~/runs/hessian_check_20261031): water PBE0/def2-SVP `! Freq ZORA` and `! Freq DKH2` with /opt/orca-5.0.4/orca; plus ORCA 5.0.3 log github.com/Xyunyao/CdSe_nmr_DFT DFT_processing/output/me2cd_se_tzvp.out. Supersedes the "ORCA 5 unconfirmed" caveat in the 2026-10-31 ZORA/DKH lesson.
