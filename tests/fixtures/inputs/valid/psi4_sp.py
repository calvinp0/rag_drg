import psi4

psi4.set_memory("16 GB")
psi4.set_num_threads(8)
psi4.core.set_output_file("psi4_sp.out", False)

mol = psi4.geometry("""
0 2
C  0.000000  0.000000  0.000000
H  1.089000  0.000000  0.000000
H -0.363000  1.027000  0.000000
H -0.363000 -0.513000  0.889000
symmetry c1
""")

psi4.set_options({"basis": "def2-tzvp", "reference": "uks", "scf_type": "df"})
e, wfn = psi4.energy("b3lyp-d3bj", return_wfn=True)
