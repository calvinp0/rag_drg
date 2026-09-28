***,ethyl radical
memory,500,m              ! 500 mega-WORDS per process = 4 GB (1 word = 8 bytes)
gthresh,energy=1.d-8
symmetry,nosym
geometry={
C    0.000000    0.000000    0.000000
C    1.490000    0.000000    0.000000
H   -0.380000    1.020000    0.000000
H   -0.380000   -0.510000    0.880000
H   -0.380000   -0.510000   -0.880000
H    2.050000    0.930000    0.000000
H    2.050000   -0.930000    0.000000
}
basis=cc-pVTZ-F12
set,charge=0
set,spin=1                ! 2S = unpaired electrons, NOT multiplicity
{rhf}
{rccsd(t)-f12}
