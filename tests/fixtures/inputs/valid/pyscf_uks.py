from pyscf import gto, dft

mol = gto.M(
    atom="""C 0.000 0.000 0.000
            H 1.089 0.000 0.000
            H -0.363 1.027 0.000
            H -0.363 -0.513 0.889""",
    basis="def2-tzvp",
    charge=0,
    spin=1,                      # 2S = N_alpha - N_beta, NOT multiplicity (doublet -> 1)
    unit="Angstrom",
    verbose=4,
)
mol.max_memory = 56000           # MB

mf = dft.UKS(mol)
mf.xc = "pbe0"
mf = mf.density_fit()
e = mf.kernel()
assert mf.converged
