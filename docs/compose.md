# Composing ESS inputs (`rag-drg compose` / `compose_ess_job`)

Agents should not hand-write quantum-chemistry inputs. The composer takes an **explicit spec**
(program, job, method, basis, molecule, resources), resolves every choice through the shared
knowledge base, writes an idiomatic input plus the matching submit script, and validates both
with `check_input`. If the checker reports any error, **no files are returned**.

```
rag-drg compose SPEC.yaml [--protocol my_protocol.yaml --step sp] [--out-dir DIR] [--json]
rag-drg compose --program orca --version 6 --job opt+freq --method wB97X-D3 --basis def2-TZVP \
    --charge 0 --mult 2 --xyz radical.xyz --server zeus --cores 16 --mem 64 --time 24:00:00 --out-dir run1
```

MCP tool: `compose_ess_job(spec, protocol=None, step=None)` returns JSON
`{ok, input_name, input_text, submit_name, submit_text, notes, findings, errors}`; the agent writes
the two texts to files itself. Python: `from rag_drg.tools.compose_ess import compose_ess_job`.

## Spec

| Field | Meaning |
|---|---|
| `program` | `orca`, `gaussian`, `qchem`, `psi4`, `molpro`, `pyscf` |
| `version` | optional, e.g. `"6"` (ORCA), `"16"`/`"09"` (Gaussian), `"2024"` (Molpro); also picks the servers.yaml install |
| `job` | `sp`, `opt`, `freq`, `opt+freq`, `ts`, `irc` (see the support table) |
| `method` | e.g. `B3LYP`, `wB97X-D3`, `DLPNO-CCSD(T)`; looked up in `knowledge/ess/levels_of_theory.yaml` |
| `basis` | e.g. `def2-TZVP`; checked in the Basis Set Exchange for the molecule's elements (omit for `*-3c`) |
| `dispersion` | `D3BJ`, `D3` (zero damping) or `D4`; `B3LYP-D3BJ` as a method name is split automatically |
| `solvation` | `{model: smd \| pcm \| cpcm, solvent: water}` |
| `scf` | `{convergence: loose \| normal \| tight \| verytight, max_iter: N}` |
| `grid` | DFT grid: ORCA `DefGrid1-3` (or `fine`/`ultrafine`), Gaussian `fine`/`ultrafine`/`superfine` |
| `extra_keywords` | raw strings passed through (ORCA: `!` keywords or `%block` lines; Gaussian: route words; Q-Chem/Psi4: `key value`; Molpro: lines; PySCF: `mf.` lines) |
| `charge`, `multiplicity` | multiplicity is 2S+1 in the spec for **every** program (Molpro/PySCF get 2S written for them) |
| `molecule` | `{xyz: "<xyz text>"}`, `{xyz_file: path}` (local CLI/Python only), or `{smiles: "..."}` |
| `resources` | `{server, cores, mem_gb, walltime, partition, software?}`, or `{cores, mem_gb}` for a standalone input without a submit script |
| `name` | file stem (default `<formula>_<job>`) |
| `allow_unverified` | default `false`; see below |

**Method resolution.** The program's own spelling comes from the levels table (`wB97X-D` ->
Gaussian `wB97XD`, `PBE0` -> Gaussian `PBE1PBE`, `M06-2X` -> ORCA `M062X`, `HF` -> Psi4 `scf`).
Table support `yes` is written; `partial` is written with the table's caveat in the notes;
`variant` (a *different* method with a similar name, e.g. `wB97X-D` in ORCA) and `unknown` are
refused unless `allow_unverified: true`, in which case the requested name is written as asked
(never silently substituted) and flagged `UNVERIFIED` in the notes; `no` is always refused.
Methods not in the table are refused unless `allow_unverified` (then treated as a DFT functional).

**Basis sets.** Written in the program's spelling (`Def2TZVP` in Gaussian, lower case in
Psi4/PySCF). Missing element coverage is an error. ECP elements are reported; for def2 basis sets
ORCA, Gaussian, Molpro and Psi4 apply the ECP themselves and PySCF gets `ecp=` written. Other
program/ECP combinations (e.g. Q-Chem) need `allow_unverified`. Gaussian `-PP` sets need a
GenECP block and are refused. ORCA auxiliary sets are added automatically and reported:
`<basis>/C` for DLPNO, RI-MP2 and double hybrids (`AutoAux` when BSE has no /C set), and
`-F12-CABS` + `/C` for CCSD(T)-F12.

**Memory and cores** in the input (`%pal`/`%maxcore`, `%nprocshared`/`%mem`, `MEM_TOTAL`,
`memory,N,m`, Psi4 `memory`, PySCF `max_memory`/`num_threads`) come from the same code that
`render_submit_script` uses for its suggestions, with the cores/memory of the rendered script,
so input and script always agree. Without a server the same formulas are applied to the given
cores/mem_gb.

**SMILES** input is embedded with RDKit (ETKDG + MMFF94) and always flagged as a rough starting
geometry. Charge comes from the SMILES; the multiplicity from its radical count unless given.

## What is generated

| Program | Jobs | Methods |
|---|---|---|
| ORCA | sp, opt, freq, opt+freq, ts (`OptTS Freq` + `Calc_Hess`), irc | HF, DFT, double hybrids, 3c composites, MP2/RI-MP2, CCSD(T), DLPNO-CCSD(T), CCSD(T)-F12 |
| Gaussian | sp, opt, freq, opt+freq, ts (`Opt=(TS,CalcFC,NoEigenTest) Freq`), irc | HF, DFT, double hybrids, MP2, CCSD(T) |
| Q-Chem | sp, opt, freq, opt+freq (`@@@`), ts (freq `@@@` ts with the Hessian `@@@` freq), irc (freq `@@@` rpath) | HF, DFT, MP2, CCSD(T) |
| Psi4 (psithon) | sp, opt, freq, opt+freq, ts (`opt_type ts`) | HF, DFT, double hybrids (sp), MP2, CCSD(T), DLPNO-CCSD(T) (closed shell) |
| Molpro | sp, opt (`optg`), freq, opt+freq, ts (`optg,root=2`) | HF, DFT, MP2, CCSD(T), CCSD(T)-F12 |
| PySCF | sp, opt (geomeTRIC), freq (Hessian + thermo), opt+freq | HF, DFT, MP2 (sp), CCSD(T) (sp) |

Coupled-cluster levels are single points only. Deliberately **not** generated: multireference
methods (CASSCF, NEVPT2, CASPT2, MRCI: they need an active space), composite protocols (G4,
CBS-QB3), IRC in Psi4/Molpro/PySCF and TS in PySCF (not in their cards), solvation in
Psi4/Molpro/PySCF, and GPU builds (pass `resources.software` explicitly to use one).

## Protocols (project-specific choices stay in your project)

A protocol is a YAML file **in your own project repository** with defaults for any spec field and
optional named `steps`; see `examples/protocols/example_protocol.yaml` (generic, not a
recommendation). Precedence, lowest first: protocol top level < `steps.<step>` < spec file <
CLI options / tool arguments. Mappings (`scf`, `resources`, ...) merge key by key; `null` removes
an inherited value. A spec file may name its protocol (`protocol: path`, `step: sp`, relative to
the spec file). Over MCP, pass the parsed protocol as the `protocol` object: the shared server
never reads files named in a request (`xyz_file` and protocol paths are refused).
