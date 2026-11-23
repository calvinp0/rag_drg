---
title: ORCA ZORA/DKH Hessians are analytic via the one-centre approximation (ORCA
  6; observed on 5.0.3)
domain: ess
software: orca
version: '6'
doc_type: lesson
status: unreviewed
tags:
- hessian
- frequencies
- zora
- dkh
- x2c
- relativistic
- onecenter
- analytic
- tckdb
- hessian_method
author: someuser
date: '2026-10-31'
---

# ORCA ZORA/DKH Hessians are analytic via the one-centre approximation (ORCA 6; observed on 5.0.3)

## Mistake

ORCA 5.0.4 §9.25 (chunk 14573) lists "relativistic corrections" among cases where "Analytical Hessians cannot be performed", and this was read as: any ZORA/DKH frequency job is non-analytic on every version.

## Correct approach

ORCA 6.x: ZORA and DKH frequencies use an analytic SCF Hessian because the one-centre approximation is on by default for gradients/Hessians (null only with RelFull / OneCenter false). X2C/DLU-X2C Hessians are semi-numeric. ORCA 5.0.3 also ran an analytic ZORA Hessian (it turns %rel OneCenter on automatically), contradicting the 5.0.4 manual list; treat ORCA 5 as unconfirmed until a second 5.x log agrees. ORCA 4 relativistic Hessians: not analytic per the 4.x manuals.

## Evidence / source

ORCA 6.1.1 manual §2.12.2.1 (chunk 15757): one-centre approximation "is enabled by default for DKH and ZORA calculations that require analytic gradients or Hessians, and indeed such calculations are not implemented without it"; ORCA 6.0/6.1 frequency pages dropped the "relativistic corrections" bullet; ORCA 6.1 chunk 15770 (X2C Hessian semi-numeric); ORCA 5.0.3 log github.com/Xyunyao/CdSe_nmr_DFT DFT_processing/output/me2cd_se_tzvp.out (`! PBE0 ZORA ... FREQ`: "WARNING: In scalar relativistic gradient/hessian runs we need the one-center approximation ===> : %rel OneCenter is turned on", then ORCA SCF HESSIAN, normal termination).
