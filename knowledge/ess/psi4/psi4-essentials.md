---
title: Psi4 essentials and gotchas
domain: ess
software: psi4
doc_type: gotcha
status: draft
tags: [psithon, python api, memory, set_options, scf_type, reference, optking, psi_scratch, threads]
---
# Psi4 essentials and gotchas

## PsiAPI (Python) skeleton

```python
import psi4

psi4.set_memory("16 GB")           # default is only ~500 MiB -> always set it
psi4.set_num_threads(8)
psi4.core.set_output_file("job.out", False)

mol = psi4.geometry("""
0 2
C  0.0 0.0 0.0
H  ...
symmetry c1
""")

psi4.set_options({"basis": "def2-tzvp", "reference": "uks", "scf_type": "df"})
e, wfn = psi4.energy("b3lyp-d3bj", return_wfn=True)
```
* Psithon input files (`input.dat`, run with `psi4 -n 8 input.dat`) use the same pieces:
  `memory 16 GB`, `molecule { ... }`, `set { basis def2-tzvp }`, `energy('scf')`.
* `psi4.energy("method/basis")` shorthand also works (e.g. `"mp2/cc-pvtz"`).

## Gotchas

* **Open-shell**: set `reference` explicitly (`uhf`/`uks`/`rohf`); the default is RHF/RKS.
* **Keywords are validated per module**: a typo or a keyword for the wrong module raises
  `ValidationError`. The complete list with defaults and allowed values is `psi4/src/read_options.cc`
  (indexed here as "Psi4 options > <MODULE>"); search it before inventing a keyword.
* `scf_type` defaults to `DF` (density fitting). For canonical post-HF comparisons use
  `scf_type pk` (or `direct`) and `mp2_type conv` / `cc_type conv`.
* Units are Ångström unless `units bohr` is in the molecule block.
* Symmetry: Psi4 detects point groups; add `symmetry c1` for orbital/occupation control and
  for automated open-shell work.
* Scratch: set `PSI_SCRATCH` to fast local disk on the node (not your home directory).

## Common drivers

| Want | Call |
|---|---|
| Energy | `psi4.energy("wb97x-d/def2-tzvp")` |
| Gradient | `psi4.gradient(...)` |
| Optimisation | `psi4.optimize(...)`; TS: `psi4.set_options({"opt_type": "ts", "full_hess_every": 0})` (check your optking version) |
| Frequencies | `psi4.frequency(..., return_wfn=True)` (analytic Hessian when available, else finite differences) |
| Composite / CBS | `psi4.energy("mp2/cc-pv[tq]z")` style CBS syntax |
| SAPT | `psi4.energy("sapt0/jun-cc-pvdz")` with a two-fragment molecule separated by `--` |

## Output

* Success marker: `*** Psi4 exiting successfully.` Values also land in `psi4.variable("CURRENT ENERGY")`.
