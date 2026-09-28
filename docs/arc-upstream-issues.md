# Suspected ARC bugs to verify and report upstream

We found these while building `knowledge/ess/errors.yaml` from ARC's troubleshooter. All line numbers refer to
[ReactionMechanismGenerator/ARC](https://github.com/ReactionMechanismGenerator/ARC) commit
`d9f47ab9a15ad37cd545b96ff75c05f626207853`, paths relative to `arc/`. Every item comes from **reading the code**.
None was reproduced by running ARC, so please confirm each one (ideally with a unit test) before opening an issue.
"Confirmed" means the code does what is described. "Partly confirmed" means the code path exists but we could not
show a realistic trigger, or the claim depends on program behaviour that we could not check.

## Summary

| # | Where | What | Status |
|---|---|---|---|
| 1 | `job/adapters/qchem.py` `write_input_file` | IRC writes Gaussian syntax into Q-Chem `JOBTYPE` | confirmed |
| 2 | `job/adapters/qchem.py` `write_input_file` | constraint loop keeps only the last constraint and drops the header | confirmed (latent) |
| 3 | `job/trsh.py` `trsh_ess_job` / `trsh_keyword_intaccuracy` | every Gaussian error gets a retry, even errors with no handler; often an identical input | confirmed |
| 4 | `job/trsh.py` `determine_ess_status` | l913 (CCSD/CISD cycle limit) labelled `MaxOptCycles`, so geometry fixes are applied | confirmed |
| 5 | `job/trsh.py` `trsh_keyword_no_qc` / `trsh_keyword_scf`, `job/adapters/gaussian.py` | QC removal after an l508 failure never takes effect | confirmed |
| 6 | `job/trsh.py` `trsh_keyword_cartesian` / `trsh_keyword_opt_maxcycles` | `opt=(cartesian)` dropped and `opt=(...)` duplicated when opt fixes are merged | confirmed |
| 7 | `job/trsh.py` `determine_ess_status` (Molpro) | `'error' in locals()` is always true; the triples memory amount is lost and memory is tripled | confirmed |
| 8 | `job/trsh.py` `determine_ess_status` (Molpro) | lower-case `'the problem occurs'` never matches Molpro's `? The problem occurs in ...` | confirmed (low impact) |
| 9 | `job/trsh.py` (ORCA MDCI memory) | "increase MaxCore **by at least** X" is used as the new total | confirmed (ORCA semantics to be checked) |
| 10 | `job/trsh.py` `determine_ess_status` (Q-Chem) | opt jobs return `'done'` even when max-cycles or SCF-failure lines were found | confirmed code; banner behaviour from ARC's own comment |
| 11 | `job/trsh.py` job-log memory check + ORCA/Molpro branches | "memory requested is too high" holds make ORCA/Molpro recompute or raise memory | confirmed |
| 12 | `job/trsh.py` `determine_ess_status` (ORCA) | SCF-divergence check can raise `UnboundLocalError` / `IndexError` | partly confirmed (no real trigger shown) |
| 13 | `job/trsh.py`, `job/adapters/molpro.py` | Molpro `IGNORE_ERROR` label never reaches the input, and would crash if it did | confirmed |
| 14 | `scheduler.py`, `job/adapters/molpro.py` | Molpro `shift` and `vdz` fixes are never written to the input | confirmed (new, not in the original list) |

## Details

### 1. Q-Chem IRC job gets a Gaussian route (`job/adapters/qchem.py:278-282`)

- **What happens:** For `job_type == 'irc'` the adapter sets
  `input_dict['job_type_1'] = f'irc=(CalcAll, {self.irc_direction}, maxpoints=50, stepsize=7)'` (l.282), and
  with `fine` it sets `input_dict['fine'] = 'scf=(direct) integral=(grid=ultrafine, Acc2E=12)'` (l.281). Both
  go into the `$rem` template (l.45-58): `JOBTYPE irc=(CalcAll, forward, ...)`, with the Gaussian keywords
  appended after `BASIS`.
- **Why it's wrong:** That is Gaussian route syntax. Q-Chem's IRC is `JOBTYPE rpath`, with `RPATH_*` `$rem`
  variables for direction, number of points and step size, and it needs a Hessian. `common.py:700` blocks
  IRC only for Molpro, so an IRC sent to Q-Chem reaches this code.
- **Fix:** Emit `JOBTYPE rpath` plus the `RPATH_*` variables from the Q-Chem manual (reaction-path section).
  Map `forward`/`reverse` to the direction variable and add a frequency step, or read the Hessian. Use
  Q-Chem grid variables (e.g. `XC_GRID`) for `fine`, as the opt branch's template comment already does
  (l.43).

### 2. Q-Chem constraints: only the last one is kept (`job/adapters/qchem.py:284-290`)

- **What happens:** `input_dict['constraint'] = '\n    CONSTRAINT\n'` (l.285). Then, inside the loop,
  `input_dict['constraint'] = f"      {constraint_type} ..."` (l.289) **assigns** instead of appending. So the
  header and all constraints but the last are lost, and `'    ENDCONSTRAINT\n'` is appended after the loop
  (l.290).
- **Also check:** The block is rendered inside `$rem` (template l.53: `BASIS ${basis}${fine}${keywords}${constraint}...`).
  Q-Chem reads `CONSTRAINT ... ENDCONSTRAINT` from a separate `$opt` section, per the manual page cited at l.34.
- **Reachability:** The scheduler currently passes `constraints=None` (`scheduler.py:1159`), so this is
  latent. It triggers for any direct use of the adapter with constraints.
- **Fix:** Use `+=`, keep the header, and write the block as its own `$opt ... $end` section after `$rem`.

### 3. Every Gaussian failure gets a retry, often with identical input (`job/trsh.py:965`, `:1873-1885`, `:1070-1074`; `job/adapters/gaussian.py:256-257`)

- **What happens:** `trsh_ess_job` calls `trsh_keyword_intaccuracy` unconditionally for Gaussian (l.965). That
  function adds `int=(Acc2E=14)` and sets `couldnt_trsh = False` for any error label (l.1877-1883). Labels that
  no `trsh_keyword_*` function handles are `InputError` (GL101/GL108/GL301), `OptOrientation` (GL202),
  `ZMat` (GL716), `MP2` (GL906), `Scratch` and `Unknown`. These are set at trsh.py l.119, 126, 130, 154,
  157, 193 and 213, and never read again. They still get one resubmission. The second attempt stops only
  because `attempted_ess_trsh_methods == ess_trsh_methods` (l.1070-1074).
- **Worse:** The Gaussian adapter removes `int=(Acc2E=14)` from the trsh string (gaussian.py:257). It only
  uses `Acc2E=14` inside the `integral=(grid=ultrafine, ...)` strings of the fine/freq/composite branches
  (l.256, 299, 323-368). So for an ordinary non-fine job, the "retry" is the same input again. For an input
  error (l101/l108/l301) that is guaranteed to fail again, and it wastes queue time.
- **Not affected:** `Syntax` is handled on purpose: `job/adapter.py:1062` raises `JobError`.
- **Fix:** Only call `trsh_keyword_intaccuracy` when some real handler fired, or for labels where integral
  accuracy can matter (SCF/convergence). Treat `InputError`, `ZMat`, `OptOrientation`, `MP2` and `Scratch` as
  untroubleshootable, with a clear message, or give them real handlers (`nosymm` for `OptOrientation`,
  `mkdir -p $GAUSS_SCRDIR` for `Scratch`).

### 4. l913 CC cycle limit treated as an optimisation cycle limit (`job/trsh.py:161-163`, `:1979-1994`)

- **What happens:** `elif 'l913.exe' in line: keywords = ['MaxOptCycles', 'GL913']` with
  `error = 'Maximum optimization cycles reached.'`. `trsh_keyword_opt_maxcycles` then adds
  `opt=(maxcycle=200)`, then `RFO`, `GDIIS` and `GEDIIS`.
- **Why it's wrong:** l913 is the CCSD/CCSD(T)/QCISD/CISD amplitude solver. ARC's own fixture
  `arc/testing/trsh/gaussian/l913.out` is a CBS-QB3 job whose step 3 (`CCSD(T)/6-31+G(d')`) prints
  `*MAX. CYCLES*` after `Iteration Nr.  50` and dies in l913. Geometry-optimiser options cannot help.
- **Fix:** Use a new label (e.g. `CCMaxCycles`) with its own handler that raises the CC iteration limit
  (Gaussian's `MaxCyc` option of CCSD/QCISD). Or mark the error untroubleshootable, with a hint to check the
  reference wave function.

### 5. Removing QC after an l508 failure never takes effect (`job/trsh.py:2109-2120`, `:1905-1943`, `:1014-1017`; `job/adapters/gaussian.py:273-276`)

- **What happens:**
  1. On an l508 failure, `trsh_keyword_no_qc` removes `'scf=(qc)'` and appends **`'no_xqc'`** (l.2116-2117).
     But `trsh_keyword_scf` checks **`'no_qc'`** (l.1910, 1935), and so does the log line at l.1016. On the
     next SCF failure, `'scf=(qc)'` is not in the list and `'no_qc'` is never there, so QC is added again
     (l.1910-1912).
  2. In the same retry, `trsh_keyword_scf` (called at l.970) has already written `scf=(qc,...)` into
     `trsh_keyword` (l.1939-1941), before `trsh_keyword_no_qc` (l.1015) removes it from
     `ess_trsh_methods`. So the l508 retry itself still carries `scf=(qc)`.
  3. The adapter's guard `not('no_xqc' in list(self.args['trsh'].values()))` (gaussian.py:273) is always
     true: `args['trsh']` is `{'trsh': [...]}` (`scheduler.py:1133`), so its values are lists. It then
     rewrites `qc` to `xqc` (l.276).
- **Minor:** `trsh_keyword_inaccurate_quadrature` (l.2046-2049) appends the same `scf=(...)` string again
  whenever any SCF method is recorded, so the route gets it twice.
- **Fix:** Use one spelling (`no_qc` or `no_xqc`) everywhere. Run the QC removal before the SCF keyword is
  assembled, or rebuild `trsh_keyword` afterwards. In the adapter, check `ess_trsh_methods` or the flattened
  trsh list.

### 6. `opt=(cartesian)` lost and `opt=` duplicated when opt fixes are merged (`job/trsh.py:1888-1902`, `:1996-2005`)

- **What happens:** `trsh_keyword_cartesian` appends `'opt=(cartesian)'` to `trsh_keyword` but records
  `'cartesian'` (not `'opt=(cartesian)'`) in `ess_trsh_methods` (l.1894-1895). `trsh_keyword_opt_maxcycles`
  rebuilds the merged keyword only from `ess_trsh_methods` entries that match `opt=\((.*?)\)` (l.1997), so
  `cartesian` is missing. It then replaces **every** item that starts with `opt` (l.2005). Example: a job
  that had an l103 error and later hits l9999 has `trsh_keyword = ['opt=(cartesian)', 'int=(Acc2E=14)', 'nosymm',
  'opt=(maxcycle=200)']`. That becomes `['opt=(maxcycle=200)', 'int=(Acc2E=14)', 'nosymm', 'opt=(maxcycle=200)']`:
  Cartesian coordinates are silently dropped and the `opt=` keyword appears twice.
- **Minor:** `'cartesian' not in trsh_keyword` (l.1898) compares against list items such as
  `'opt=(cartesian)'`, so it is always true. Cartesian is also re-applied only when `job_type == 'opt'`
  (l.1898), not for `conf_opt`.
- **Fix:** Store `'opt=(cartesian)'` in `ess_trsh_methods` (or include `cartesian` when merging). Replace the
  opt items with a single merged item.

### 7. Molpro triples memory: `'error' in locals()` is always true (`job/trsh.py:84`, `:415-425`)

- **What happens:** `error` is assigned at l.84 (`keywords, error, = list(), ''`). So
  `... if 'error' not in locals() else error` (l.424) always keeps the old value, and the amount parsed from
  `A further X Mwords ...` is never used. If the `For full I/O caching in triples ...` line is missing,
  `error` stays `''`. It becomes `'Molpro job terminated for an unknown reason.'` (l.463), and
  `trsh_ess_job` parses the word `unknown` as the amount (l.1166). The fallback then triples the memory
  (l.1173) instead of adding the needed amount.
- **Fix:** `if not error: error = f'Additional memory required: {line.split()[2]} MW'`.

### 8. Molpro: lower-case `'the problem occurs'` never matches (`job/trsh.py:459-462`)

- Molpro prints `? The problem occurs in <module>` (capital T; see
  `arc/testing/trsh/molpro/unrecognized_basis_set.out` and `insufficient_memory_2.out`). The branch is dead
  code. Unknown errors still end up as `Unknown` through the default at l.463-464. But because the reverse
  scan does not stop there, an earlier non-fatal `No convergence` line in the same file can be picked up
  instead (l.407) and mislabel the job `Unconverged`.
- **Fix:** Use `'the problem occurs' in line.lower()`.

### 9. ORCA "increase MaxCore by at least X" is used as the new total (`job/trsh.py:320-344`, `:1111-1141`)

- **What happens:** For MDCI, ARC takes the largest X from `Please increase MaxCore - by at least ( X MB)`,
  adds 500 (l.340), and `trsh_ess_job` uses that as the new **per-core total** (l.1113-1140). In ARC's
  fixture `arc/testing/trsh/orca/orca_mdci_memory_error.log`, the job ran with `%maxcore 8000` and
  `nprocs 32`. The largest request is 9717.9 MB, so ARC retries with ~10 300 MB per core. Read as an increment,
  the job needs at least ~17 700 MB per core, so the retry probably fails again.
- **To verify:** Check the ORCA manual or source for whether "by at least" is an increment. The SCF message
  `increase MaxCore to more than: X MB` is handled correctly as a total.
- **Fix:** For the "by at least" wording, add X to the current `%maxcore` of the failed job.

### 10. Q-Chem opt jobs return `'done'` despite max-cycles or SCF-failure lines (`job/trsh.py:231-266`)

- **What happens:** For opt/conf_opt/ts jobs, the reverse scan sets `done = True` at the `Thank you very much
  for using Q-Chem` banner and keeps scanning (l.234-239). If it then finds `SCF failed` (l.240),
  a generic `error` line (l.244) or `MAXIMUM OPTIMIZATION CYCLES REACHED` (l.255), it sets `keywords` and
  breaks, but l.262-263 `if done: return 'done', keywords, '', ''` still reports success. ARC's own comment
  (l.236-237) says the banner is printed even when the cycle limit is reached, and that is exactly the case
  this scan was meant to catch.
- **Fix:** Return `'errored'` when `keywords` is non-empty, i.e. `if done and not keywords`.

### 11. "Memory requested is too high" holds make ORCA and Molpro raise memory (`job/trsh.py:74-76`, `:528-562`, `:1047`, `:1105-1143`, `:1160-1175`)

- **What happens:** `determine_job_log_memory_issues` turns HTCondor's hold message `Job Is Wasting Memory
  using less than N percent of requested Memory` into `keywords=['Memory']`, `error='Memory requested is too
  high...'` (l.556-560). `determine_ess_status` returns `'errored'` immediately (l.74-76), without reading the
  ESS output. Gaussian checks `'too high' not in job_status['error']` (l.1047), but the ORCA branch (l.1105)
  and the Molpro branch (l.1160) do not.
  - Molpro: the amount parsed from the message is a word (`too`) or the used-GB figure, so memory is tripled
    (l.1173), or 100 MW + 5 GB is added.
  - ORCA: the amount falls back to `estimate_orca_mem_cpu_requirement` (l.1115). Memory is recomputed from
    the heavy-atom estimate, not reduced as the message asks.
- **Minor:** `job/adapter.py:1000` lower-cases the job-log content, so the case-sensitive
  `'MemoryUsage of job'` test (l.549) never matches in production, and the "used only X GB" detail is always
  missing.
- **Fix:** Give the "too high" case its own keyword (e.g. `MemoryTooHigh`) and lower the request for every
  ESS. Make the job-log checks case-insensitive.

### 12. ORCA SCF-divergence check can crash (`job/trsh.py:271-300`) - partly confirmed

- `scf_energy_last_iteration` is only assigned when a `TOTAL SCF ENERGY` line is found (l.279-282). If an
  output has `Starting incremental Fock matrix formation` but no `TOTAL SCF ENERGY`, l.285 raises
  `UnboundLocalError`. The `while not is_str_float(forward_lines[j + 1].split()[1])` loop (l.276) has no
  bounds check and raises `IndexError` on a line with fewer than two tokens or at end of file. We did not find
  a real ORCA output that triggers either case. Also, the loop stops at the first `TOTAL SCF ENERGY` (l.282),
  so in optimisations only the first SCF is checked.
- **Fix:** Initialise both energies to `None`, bound the `while` loop, and compare the last SCF of the file.

### 13. Molpro `IGNORE_ERROR` label never reaches the input (`job/trsh.py:411-414`; `job/adapters/molpro.py:247-250`, `:262-263`)

- `determine_ess_status` sets `keywords = ['IGNORE_ERROR in the ORBITAL directive']`. The Molpro branch of
  `trsh_ess_job` (l.1159-1202) only handles memory, shift and vdz, and never returns this label, so nothing
  puts it into `args['trsh']`. Even if something did, `molpro.py:262-263` appends to `keywords`, and that
  list only exists in the opt branch (l.247-250), where it has already been joined into `job_type_1`. The
  append would have no effect for opt jobs and would raise `NameError` for every other job type.
- **Fix:** Handle the label in `trsh_ess_job`, pass it through `args['trsh']`, and insert `ORBITAL,IGNORE_ERROR`
  into the method block independently of the job type.

### 14. Molpro level shift and basis downgrade are never applied (`scheduler.py:1131-1144`; `job/adapters/molpro.py:229-230`, `:236`)

- `trsh_ess_job` returns `shift='shift,-1.0,-0.5;'` and `trsh_keyword='vdz'` for Molpro (l.1176-1200). The
  scheduler stores them as `args['shift'] = shift` (l.1143-1144) and `args['trsh'] = {'trsh': 'vdz'}`
  (l.1132-1133). The adapter reads `self.args['trsh']['shift']` (molpro.py:236), which never exists, and tests
  `'vdz' in self.args['trsh']` (l.229), a dict whose only key is `'trsh'`. So Molpro retries 1-3 resubmit
  the same input. Only retry 4 changes anything: the memory.
- **Fix:** Read `self.args.get('shift')`, and test `'vdz' in str(self.args['trsh'].get('trsh', ''))`, or
  normalise `args['trsh']` in one place.

## Checked and dropped

- **Slurm walltime detection** (`job/adapter.py:926-927`) compares against lower-case `'cancelled'` /
  `'due to time limit'` while Slurm prints upper case. That is fine: `_get_additional_job_info` lower-cases the
  content first (l.1000).
- **"`Syntax` has no handler":** intended. `job/adapter.py:1062` raises `JobError` instead of retrying.
