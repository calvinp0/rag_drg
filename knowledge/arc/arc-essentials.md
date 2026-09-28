---
title: ARC input, output and settings essentials
domain: arc
software: arc
doc_type: card
status: draft
tags: [levels_ess, qchem, input.yml, output.yml, schema, level_of_theory, job_types, ess_settings, servers, settings.py, restart.yml, specific_job_type, ts_adapters]
---
# ARC input, output and settings essentials

The ARC repository itself is indexed (docs, examples, `arc/` source, and
`arc/schemas/output_yml_schema.json`). Search with `software=arc` for details; this card is the map.

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
* Every server named in `ess_settings` must exist in `servers` (settings). A `local` server entry
  is required for in-core ESS such as `pyscf`.
* ARC does **not** read `~/.ssh/config` (no ProxyJump/IdentityFile); set `key` per server or use
  an ssh-agent.
* ARC chooses the ESS for a level from `levels_ess` in settings (phrase matching on the method,
  e.g. `'orca': ['dlpno']`, `'qchem': ['m06-2x']`) unless the level dict sets `software`. Set
  `software` explicitly when several ESS could run the method.
* Q-Chem adapter (`arc/job/adapters/qchem.py`), as of the indexed commit: the IRC branch writes
  Gaussian syntax (`irc=(CalcAll, ...)`) into the Q-Chem job type (Q-Chem IRC is `JOBTYPE rpath`),
  and the constraint loop overwrites `input_dict['constraint']` instead of appending. Don't rely on
  ARC for Q-Chem IRCs or multi-constraint Q-Chem jobs until someone confirms or fixes this.
* Put personal settings in `~/.arc/settings.py` (and `~/.arc/submit.py`), which override
  `arc/settings/settings.py`. Do not edit the repo copy for personal servers.

## Settings (`arc/settings/settings.py`)

* `servers = {name: {'cluster_soft': 'Slurm'|'PBS'|'OGE'|'SGE'|'HTCondor'|'local', 'address', 'un',
  'key', 'path', 'cpus', 'memory', 'queues': {'queue': 'HH:MM:SS'}, 'excluded_queues',
  'max_simultaneous_jobs'}}`. Remote runs go under `<path>/<un>/runs/ARC_Projects/`.
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
