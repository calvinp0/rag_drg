---
title: ARC input, output and settings essentials
domain: arc
software: arc
doc_type: card
status: draft
tags: [levels_ess, qchem, input.yml, output.yml, schema, level_of_theory, job_types, ess_settings, servers, settings.py, restart.yml, specific_job_type, ts_adapters]
---
# ARC input, output and settings essentials

ARC's user-facing surface is indexed: docs, examples, `arc/settings/settings.py` + `submit.py`,
and `arc/schemas/output_yml_schema.json`. The ARC code itself is not; instead every input key is
indexed from the schema generated from ARC's source (`knowledge/arc/input_schema.snapshot.yaml`,
one chunk per key: search "ARC input > job_memory"). Search with `software=arc`; this card is the map.

**Before running ARC, check the input**: `rag-drg arc check input.yml` or the MCP tool
`check_arc_input(content)` (unknown keys with did-you-mean, types, species/reaction consistency,
job types, levels vs. the ESS ARC routes them to, `ess_settings`). See `docs/arc-input.md`.

**Thermo with Arkane:** if the sp level has no Arkane AEC/BAC, ARC only warns and writes thermo *without*
corrections. See the card "Arkane energy corrections (AEC/BAC)" (`software=arkane`) for the check and the
fitting recipe.

## Input file (`input.yml`)

* A YAML dict passed straight to `ARC(**input_dict)`. Top-level keys must be `ARC.__init__` arguments
  (authoritative list: `docs/source/input_reference.rst`, `arc/main.py`).
* Minimal:
  ```yaml
  project: my_project
  species:
    - label: ethanol
      smiles: CCO
  level_of_theory: wb97xd/def2svp      # 'sp//opt' style shortcut, or a composite method
  ess_settings:
    gaussian: local
  ```
* Levels: `level_of_theory`, `composite_method`, `conformer_opt_level` (alias `conformer_level`),
  `conformer_sp_level`, `ts_guess_level`, `opt_level`, `freq_level`, `sp_level`, `scan_level`,
  `irc_level`, `orbitals_level`, `arkane_level_of_theory`, `adaptive_levels`.
  A level can be a string `method/basis` or a dict with keys `method, basis, auxiliary_basis,
  dispersion, cabs, method_type, software, software_version, compatible_ess, solvation_method,
  solvent, solvation_scheme_level, args, year`. `year` is only for Arkane correction matching.
* `job_types` keys: `conf_opt, conf_sp, opt, fine, freq, sp, rotors, irc, orbitals, stability,
  onedmin, bde` (`fine_grid` -> `fine`, `lennard_jones` -> `onedmin` are legacy aliases).
  Defaults true: conf_opt, opt, fine, freq, sp, rotors, irc. Defaults false: the rest.
* Species entries: `label` + one of `smiles`/`inchi`/`adjlist`/`xyz` (xyz may be a path to an ESS
  output), plus `charge`, `multiplicity`, `is_ts`, `bdes`, `directed_rotors`, ...
* Reactions: `label: 'A + B <=> C + D'` or `reactants`/`products` lists of species labels,
  optional `ts_xyz_guess`, `family`, `multiplicity`, `charge`.
* Resources: `job_memory` (GB per job), `max_job_time` (hours).
* Worked examples: `examples/minimal`, `examples/Stationary/*`, `examples/Reactions/*`.

## Gotchas

* `specific_job_type: stability` runs **nothing**: it replaces `job_types` with only that key,
  which switches off the opt/freq/sp jobs stability analysis depends on. Enable
  `stability` via `job_types` (Gaussian and ORCA only).
* Unknown top-level keys make `ARC(**input)` raise TypeError, and unknown keys in a level dict raise
  "illegal key"; unknown keys in a `species`/`reactions` entry are silently ignored (a typo such as
  `multiplicty` just loses the setting).
* ARC loads YAML with only `true`/`false` as booleans: `yes`/`no`/`on`/`off` stay strings, so
  `rotors: no` in `job_types` is the non-empty string "no" and counts as true.
* Every server named in `ess_settings` must exist in `servers` (settings). A `local` server entry
  is required for in-core ESS such as `pyscf`.
* ARC does **not** read `~/.ssh/config` (no ProxyJump/IdentityFile); set `key` per server or use
  an ssh-agent.
* ARC chooses the ESS for a level from `levels_ess` in settings (phrase matching on the method,
  e.g. `'orca': ['dlpno']`, `'qchem': ['m06-2x']`) unless the level dict sets `software`. Set
  `software` explicitly when several ESS could run the method (details below).
* Q-Chem adapter (`arc/job/adapters/qchem.py`), as of the indexed commit: the IRC branch writes
  Gaussian syntax (`irc=(CalcAll, ...)`) into the Q-Chem job type (Q-Chem IRC is `JOBTYPE rpath`),
  and the constraint loop overwrites `input_dict['constraint']` instead of appending. Don't rely on
  ARC for Q-Chem IRCs or multi-constraint Q-Chem jobs until someone confirms or fixes this.
* Put personal settings in `~/.arc/settings.py` (and `~/.arc/submit.py`), which override
  `arc/settings/settings.py`. Do not edit the repo copy for personal servers.

## Wavefunction stability analysis

Turn it on with `job_types: {stability: true}` (off by default); never with `specific_job_type`
(see Gotchas). Implemented for Gaussian and ORCA only. It runs once per species after the
optimization converges and before freq/sp/IRC/scans, only for TSs or species optimised with a
restricted reference at a DFT/HF level, and needs the optimization's checkfile. The verdict is logged
and written to `output.yml` (`wavefunction_stability` in the output schema).
(`docs/source/input_reference.rst`, "Wavefunction Stability Analysis".)

## How ARC decides which ESS runs a level

`Level.deduce_software` (`arc/level.py`), in this order: the level's own `software`; `uma*` methods
-> `ase`; a method containing `dlpno` -> `orca`; composite methods, IRC jobs and `iop` args ->
`gaussian`; `torchani`, `xtb`/`gfn` -> those codes; then the first `levels_ess` phrase found in the
method or basis (settings order; e.g. `b3lyp`/`apfd`/`m062x` -> gaussian, `ccsd`/`cisd`/`vpz` ->
molpro, `m06-2x` -> qchem, `pbe` -> terachem, `casscf` -> cfour); finally the ESS listed as
compatible for the method in `data/ess_methods.yml`, preferring gaussian/qchem/orca/molpro by method
type. Phrase matching is substring matching: `pbe` also catches `pbe0`, and the chosen code may run a
differently parametrised method with a similar name (`wb97xd` in ORCA); `rag-drg arc check` warns
about that using the levels table (`knowledge/ess/levels_of_theory.yaml`).

## Settings (`arc/settings/settings.py`)

* `servers = {name: {'cluster_soft': 'Slurm'|'PBS'|'OGE'|'SGE'|'HTCondor'|'local', 'address', 'un',
  'key', 'path', 'cpus', 'memory', 'queues': {'queue': 'HH:MM:SS'}, 'excluded_queues',
  'max_simultaneous_jobs'}}`. Remote runs go under `<path>/<un>/runs/ARC_Projects/`.
* Defining a cluster: add an entry to the `servers` dictionary in your own `~/.arc/settings.py`.
  A Slurm cluster, as in ARC's example (`server2`):
  ```python
  servers = {
      'server2': {
          'cluster_soft': 'Slurm',          # or 'PBS', 'OGE', 'SGE', 'HTCondor', 'local'
          'address': 'server2.host.edu',
          'path': '/home',                  # runs go under <path>/<un>/runs/ARC_Projects/
          'un': '<username>',
          'key': 'path_to_rsa_key',
          'cpus': 24,                       # cores per node (default 8)
          'memory': 256,                    # GB per node (default 16)
      },
  }
  ```
  Then point each ESS at it in `global_ess_settings`. For a cluster that is already in
  `servers.yaml`, `rag-drg servers arc-settings <name>` prints the entry.
* `global_ess_settings = {'gaussian': ['local', 'server2'], 'orca': 'local', ...}` (list = priority).
* `supported_ess`, `ts_adapters` (default heuristics, linear, AutoTST, GCN, xtb_gsm, orca_neb),
  `default_job_types`, and per-ESS submit script templates in `arc/settings/submit.py`.

## Output

* Project folder: `arc.log`, `<project>.info`, `restart.yml` (restart ARC with it),
  `calcs/Species/<label>/<job>/`, `calcs/TSs/<label>/<job>/` (input, submit script, output),
  `output/` (Arkane files, `status.yml`, thermo/kinetics RMG libraries, geometry files, plots).
* **`output/output.yml`** is the consolidated machine-readable result, validated by
  `arc/schemas/output_yml_schema.json` (JSON Schema 2020-12). Header keys include `schema_version`,
  `project`, `arc_version`, `arc_git_commit`, `opt_level`/`freq_level`/`sp_level`,
  `freq_scale_factor`, `bac_type`, `atom_energy_corrections`, `bond_additivity_corrections`, then
  lists `species`, `transition_states`, `reactions`. Non-converged species appear with
  `converged: false` and null results. Read the schema chunks (search "output.yml schema <field>")
  before writing a parser.
* Remote copies of jobs live on each server under `~/runs/ARC_Projects/<project>/`.
