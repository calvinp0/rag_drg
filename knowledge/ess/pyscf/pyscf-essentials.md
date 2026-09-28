---
title: PySCF essentials and gotchas
domain: ess
software: pyscf
doc_type: gotcha
status: draft
tags: [gto.M, spin, charge, max_memory, num_threads, geometric, hessian, thermo, gpu4pyscf, b3lyp, vwn, density fitting]
---
# PySCF essentials and gotchas

## Skeleton

```python
from pyscf import gto, dft

mol = gto.M(
    atom="""C 0 0 0
            H 0 0 1.09""",      # or an .xyz path
    basis="def2-tzvp",
    charge=0,
    spin=1,                      # 2S = N_alpha - N_beta, NOT multiplicity (doublet -> 1)
    unit="Angstrom",             # default
    verbose=4,
)
mol.max_memory = 16000           # MB, default is small

mf = dft.UKS(mol)                # UKS/UHF for open shell; RKS/RHF otherwise
mf.xc = "pbe0"                   # libxc names; check spelling/aliases in pyscf/dft/libxc.py
mf = mf.density_fit()            # RI-J(K); much faster for DFT
e = mf.kernel()
assert mf.converged
```

## Gotchas

* **`spin` is 2S**, and `gto.M` raises if `spin` is inconsistent with the electron count.
* **B3LYP**: since PySCF 2.3 `B3LYP` means the VWN-RPA (Gaussian-like) variant (a warning is printed);
  the VWN5 variant is selected through a config setting. Do not compare B3LYP energies across codes
  without checking the definition.
* Threads: `OMP_NUM_THREADS` or `pyscf.lib.num_threads(n)`; memory via `mol.max_memory` (MB).
* Always check `mf.converged`; PySCF does not raise on SCF non-convergence.
  Help it with `mf = mf.newton()` (second-order SCF) or `mf.level_shift`, `mf.damp`.

## Geometry optimisation, Hessian, thermochemistry

```python
from pyscf.geomopt.geometric_solver import optimize   # needs `pip install geometric`
mol_eq = optimize(mf, maxsteps=100)

from pyscf.hessian import thermo
mf_eq = dft.UKS(mol_eq); mf_eq.xc = mf.xc; mf_eq.kernel()
h = mf_eq.Hessian().kernel()
freq = thermo.harmonic_analysis(mol_eq, h)
tdata = thermo.thermo(mf_eq, freq["freq_au"], 298.15, 101325)
```
* The `examples/` folder of the PySCF repo is indexed here (`examples/geomopt`, `examples/dft`,
  `examples/scf`, ...); search it for the idiomatic call before writing new code.

## GPU

* `gpu4pyscf` provides GPU SCF/DFT/gradients/Hessians: `mf = mf.to_gpu()` (check installed version
  and supported functionals; not every method is ported).
