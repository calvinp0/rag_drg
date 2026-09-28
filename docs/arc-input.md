# ARC input schema and `input.yml` checker

Goal: an agent writing an ARC `input.yml` gets it right the first time. Instead of indexing ARC's
whole codebase (3520 of 3860 ARC chunks were code, and "how do I set job memory in ARC input"
returned adapter code), rag-drg indexes ARC's user-facing surface (docs, examples, output schema,
`settings.py`, `submit.py`) plus an **input schema generated from ARC's source**, and checks input
files against that schema.

```bash
rag-drg arc schema                        # sources_cache/arc -> index/arc_input_schema.json
rag-drg arc schema --out knowledge/arc/input_schema.snapshot.yaml   # refresh the committed snapshot
rag-drg arc check input.yml [more.yml] [--json]                     # exit 1 on errors
rag-drg check-input input.yml             # the ESS checker routes ARC inputs here too (and its hook)
```

MCP tool: `check_arc_input(content, filename="input.yml")`. Python:
`from rag_drg.tools.arc_input import check_arc_input` -> `list[Finding]` (the ESS checker's
`rag_drg.tools.inputcheck.Finding`). Code: `rag_drg/tools/arc_input.py`, `rag_drg/tools/_arc/`.

## The schema

`rag_drg/tools/_arc/schema.py` reads ARC's source with `ast` (ARC is never imported):

| From | What |
|---|---|
| `arc/main.py` `ARC.__init__` | every top-level key (51 at ARC `d9f47ab`): type hint, default (`ast.literal_eval` when literal, else the source text, e.g. `logging.INFO`), description from the class docstring `Args:` (fallback `Attributes:`), required keys (`if project is None: raise`), settings fallbacks (`job_memory or default_job_settings.get('job_total_memory_gb')`) |
| `arc/species/species.py` `ARCSpecies` | `__init__` arguments + keys `from_dict` reads and `as_dict` writes = keys allowed in a `species` entry |
| `arc/reaction/reaction.py` `ARCReaction` | same for `reactions` entries |
| `arc/level.py` `Level` | `__init__` arguments and the dict keys `Level.build` accepts (anything else raises "illegal key") |
| `arc/common.py` | job types `initialize_job_types` accepts, legacy aliases (`fine_grid -> fine`, `lennard_jones -> onedmin`), renamed types (`1d_rotors`), extra `ess_settings` keys `check_ess_settings` accepts |
| `arc/settings/settings.py` | `supported_ess`, `ts_adapters`, `default_job_types`, `levels_ess`, `default_levels_of_theory`, `default_job_settings`, `valid_chars` |
| `arc/job/**/*.py` | adapters registered with `register_job_adapter` (valid `ts_adapters`) |
| `arc/statmech/adapter.py` | `StatmechEnum` (valid `thermo_adapter` / `kinetics_adapter`) |
| `docs/source/input_reference.rst` | legacy job-type aliases (cross-check) |

The schema records the ARC commit it came from (`arc_commit`). Which schema is used:

1. An ARC clone (`sources_cache/arc`, i.e. the `arc` source): `index/arc_input_schema.json` if it
   was generated from the clone's current commit, otherwise it is regenerated on the fly (about a
   second) and the cache rewritten. `deploy/refresh.sh` runs `rag-drg arc schema` after
   `ingest --fetch`, so the shared server always checks against the fetched ARC.
2. No clone (CI, a laptop without `rag-drg fetch`): the committed
   `knowledge/arc/input_schema.snapshot.yaml`.

The snapshot is also what search sees: it is chunked one entry per key (`rag_drg/chunking.py`
branch for files with a top-level `arc_input_schema:`), titled `ARC input > job_memory`,
`ARC input > species > smiles`, `ARC input > reactions > label`, `ARC input > level dict > method`,
with type, default, settings fallback and description; `doc_type: schema`, `software: arc`.
Restart-only bookkeeping keys are folded into one chunk per entry type so they don't crowd search.
**Refresh the snapshot after updating the ARC clone** (`rag-drg arc schema --out
knowledge/arc/input_schema.snapshot.yaml`, review the diff, commit); the server never rewrites it
because `git pull --ff-only` in `refresh.sh` must not conflict. When the clone is at the snapshot's
commit, `test_generator_on_clone` asserts that the generator reproduces the snapshot exactly.

## Checks

"error" only when ARC raises or silently does something other than what was written; "warning" for
very likely mistakes; "info" otherwise. The YAML is read the way ARC reads it (`ARCYAMLLoader`: only
`true`/`false` are booleans, so a bare `NO` label is the string "NO").

| Code | Severity | Check | ARC behaviour it is based on |
|---|---|---|---|
| `arc-yaml-error` | error | YAML does not parse (with line) | `read_yaml_file` fails |
| `arc-not-mapping`, `arc-empty` | error | top level is not a mapping / file empty | `ARC(**input)` impossible |
| `arc-duplicate-key` | warning | a key appears twice in a mapping | YAML keeps the last one |
| `arc-unknown-key` | error | top-level key is not an `ARC.__init__` argument (difflib suggestion; hint when it is a species key) | TypeError |
| `arc-project-missing` | error | no `project` (skipped when restart keys `running_jobs`/`output` are present) | "A project name must be provided" |
| `arc-project-name` | error | characters outside settings `valid_chars` | `check_project_name` raises |
| `arc-type` | error / warning | value type vs. the type hint: numbers (`job_memory`, `max_job_time`, `n_confs`, `e_confs`, `T_count`, species `charge`/`multiplicity` ...) -> error; list/dict-only -> error; others (e.g. bool) -> warning | later arithmetic / iteration fails |
| `arc-species-type` | error | `species` not a list, or an entry not a mapping | ValueError |
| `arc-species-unknown-key` | error | key not accepted by `ARCSpecies` (init args + from_dict/as_dict keys), with suggestion | silently ignored (the setting is lost) |
| `arc-species-key-ignored` | info | an `ARCSpecies()` argument that `from_dict` never reads (`keep_mol`, `project_directory`) | no effect in input files |
| `arc-species-label` | error | missing / non-string label; non-TS named `TS` or `TS<n>` | InputError / TypeError / SpeciesError |
| `arc-species-duplicate` | error | label used twice | "Species label ... is not unique" |
| `arc-species-no-structure` | error | non-TS species without smiles/inchi/adjlist/xyz/yml_path (or restart geometry) | nothing to compute |
| `arc-ts-no-source` | warning | `is_ts` species with no xyz that no reaction points to (`ts_label`/`rxn_label`) | nothing to build the TS from |
| `arc-smiles`, `arc-charge`, `arc-parity`, `arc-multiplicity`, `arc-singlet-diradical` | warning / info (error for multiplicity < 1) | with RDKit: SMILES parses; `charge` vs. SMILES formal charge; multiplicity parity vs. electron count; open-shell singlet hint | wrong spin state / ESS failure |
| `arc-reaction-type` | error | `reactions` not a list / entry not a mapping / reactants not lists | ValueError |
| `arc-reaction-unknown-key` / `-key-ignored` | error / info | key not accepted by `ARCReaction` / argument not read from dicts (`kinetics`) | silently ignored |
| `arc-reaction-label` | error (warning for `A+B`) | no label and no reactants+products; label without `' <=> '` (spaces required) | InputError / ReactionError |
| `arc-reaction-species` | error | reactant/product label (from lists or parsed from the label) not defined under `species` (suggestion) | ValueError |
| `arc-reaction-size` | error | more than 3 reactants/products without a TS guess | ReactionError |
| `arc-reaction-ts` | warning | `ts_label` not a species, or not `is_ts: true` | |
| `arc-job-types-type` | error | `job_types` not a mapping | |
| `arc-job-type-unknown` | error | unknown job type (suggestion; `1d_rotors -> rotors`) | InputError |
| `arc-job-type-legacy` | info | `fine_grid`, `lennard_jones` | converted by ARC |
| `arc-job-type-value` | error (string) / warning | value not true/false (`no` is a truthy string for ARC) | job silently on |
| `arc-specific-job-type` | error | not a job type | InputError |
| `arc-specific-stability` | error | `specific_job_type: stability` | runs nothing (docs/source/input_reference.rst) |
| `arc-specific-with-job-types` | warning | both given | specific_job_type replaces job_types wholesale |
| `arc-specific-job-type-alias` | warning | `specific_job_type: fine`/`onedmin` while settings' `default_job_types` still uses `fine_grid`/`lennard_jones` | the legacy key's False overwrites it in `initialize_job_types` |
| `arc-level-conflict` | error | `level_of_theory` with `opt_level`/`sp_level`/`composite_method` | InputError |
| `arc-level-type` | error | `level_of_theory` not a string; any level neither string nor dict | InputError / ValueError |
| `arc-level-format` | error | more than one `//`; a level string with spaces or 2+ `/` | ValueError |
| `arc-level-unknown-key` | error | level dict key outside `Level.build`'s list (suggestion) | "Got an illegal key" |
| `arc-level-method` | error | level dict without `method` | ValueError |
| `arc-level-solvation` | error | only one of `solvation_method` / `solvent` | ValueError |
| `arc-level-year` | error / warning | year not 4 digits; `year` on a non-Arkane level (no effect) | ValueError / ARC warns |
| `arc-level-year-in-method` | warning | method name ends in a year (`b97d32023`) | ESS keyword does not exist; use `year:` in `arkane_level_of_theory` |
| `arc-level-software` | error | `software` not in `supported_ess` nor a registered adapter | no adapter |
| `arc-level-unsupported` | error (explicit `software` or `dlpno`->ORCA) / warning (routed by a `levels_ess` phrase, which ~/.arc settings may change) | the levels table (`knowledge/ess/levels_of_theory.yaml`) says the method is not available in the ESS ARC will use | ESS error |
| `arc-level-variant` | warning | the ESS has a similarly named but different method (e.g. `wb97xd` in ORCA) | silently different method |
| `arc-adaptive-levels` | error | `adaptive_levels` not a list of `{atom_range, levels}` | InputError |
| `arc-ess-settings-type` | error | `ess_settings` not a mapping / servers not str or list of str | SettingsError |
| `arc-ess-unknown` | error | ESS key not in `supported_ess` + `check_ess_settings` extras (suggestion) | SettingsError |
| `arc-ess-server` | warning | server neither `local` nor in `servers.yaml` (only when servers.yaml exists) | SettingsError unless ~/.arc defines it |
| `arc-ts-adapters-type`, `arc-ts-adapter-unknown` | error | `ts_adapters` not a list / not a registered adapter | InputError |
| `arc-statmech-adapter` | error | `thermo_adapter`/`kinetics_adapter` not in `StatmechEnum` | ValueError |
| `arc-dont-gen-confs` | warning | `dont_gen_confs` label not a species | |

Level routing mirrors `Level.deduce_software`: explicit `software`, then `dlpno` -> orca, then the
first `levels_ess` phrase found in the method or basis. Composite/IRC/`iop` routing to Gaussian and
the `ess_methods.yml` fallback are not modelled (no finding rather than a guess).

All ARC examples in `sources_cache/arc/examples` produce no errors (`test_all_clone_examples_have_no_errors`).

## Input-checker integration

`rag_drg/tools/inputcheck.py` has a small extension point, `INPUT_ROUTERS`: a plugin registers
`fn(filename, content) -> checker | None`. `arc_input.py` registers one that claims `*.yml`/`*.yaml`
files whose top-level keys include `project` and (`species` or `reactions`) (a line scan when the
YAML does not parse, so parse errors are reported too). So `rag-drg check-input input.yml`, the
Claude Code PostToolUse hook (`check-input --hook`, exit 2 on errors), the `check_input` MCP tool
and the REST `/check_input` endpoint all check ARC inputs as well. Other `.yml` files stay ignored.

## REST

`/check_input` (POST `{content, filename: "input.yml"}`) already routes ARC files. A dedicated
endpoint, if wanted in `rest_api.py`: `POST /check_arc_input` with `{content, filename?}` ->
`{"findings": [Finding.to_dict()...], "text": format_findings(...)}` calling
`rag_drg.tools.arc_input.check_arc_input(content, filename or "input.yml", cfg=cfg)` in a worker thread.
