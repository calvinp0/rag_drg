# Diagnosing failed ESS jobs (`rag-drg diagnose`, `diagnose_output`)

Output files of failed jobs are big (a stuck Gaussian optimisation easily reaches 10 MB), and agents waste
many tokens paging through them. `diagnose` reads only what it needs and answers four questions:

1. **Which program and version wrote it?** Detected from the content (`Gaussian 16, Revision C.01`,
   `Program Version 6.0.1`, `Q-Chem 6.1.0`, `Molpro 2022.3`, `Psi4 1.9.1`), plus the Gaussian route section
   of the last job step or ORCA's `!` lines.
2. **Did it finish?** One of:
   - `success`: the normal-termination marker is present (see `knowledge/ess/capabilities.md`)
   - `failed`: an error termination or a known error message
   - `incomplete`: no marker and no known error. The job is still running, or it was killed from outside
     (walltime, out-of-memory kill, node failure). The result tells you to check the scheduler
     (`sacct -j <id> --format=JobID,State,ExitCode,Elapsed,Timelimit,MaxRSS,ReqMem`, `qstat -xf`, stderr file).
   - `unknown`: the program could not be identified (xTB, TeraChem, empty file, ...).
3. **Why did it fail?** Curated signatures in `knowledge/ess/errors.yaml` are matched in priority order:
   specific messages first, generic "link lNNN died" entries after them, and fallbacks last. For
   Gaussian, the failing link is read from `Error termination via Lnk1e in .../lNNN.exe` and used to rank
   the matches. For example, `Inaccurate quadrature in CalDSu` means something different in l502 than in an
   l9999 termination. Only the **final job step** is searched. In multi-step (`--Link1--`) jobs and in files
   that several runs appended to, errors from earlier steps are ignored.
4. **What should I do?** You get the meaning, ordered fixes (direct advice first, then what ARC's
   troubleshooter would do), sources, an excerpt of the matched lines ± 4 lines (capped at 30 lines),
   cheap progress information (last optimisation step, last SCF energy, convergence criteria met) and
   pointers to knowledge cards.

## Usage

```bash
rag-drg diagnose job.log                   # human-readable
rag-drg diagnose --json run*/output.out    # machine-readable (one object, or a list for several files)
rag-drg diagnose --software orca job.out   # skip program detection
rag-drg diagnose --tail-lines 5000 big.log # read a longer tail
```

Exit code: `0` all files succeeded, `1` at least one failed, `2` otherwise (incomplete / unknown / missing).

How much is read: files up to 4 MB are read whole. Bigger files are read as the first 300 lines (banner,
version, route) plus the last 2000 lines. If the job failed and nothing in the tail explains it, the whole
file is scanned once (up to 256 MB).

### MCP tool (shared server)

The shared MCP server cannot see your files, so `diagnose_output` takes the **content**:

```
diagnose_output(content: str, filename: str = "", software: str | None = None) -> str
```

For big files, pass only the first ~100 and the last ~300 lines:

```bash
(head -n 100 job.log; echo '[... lines omitted ...]'; tail -n 300 job.log)
```

The tool copes with this: the version and route come from the head, and the status and error come from
the tail. Passing only the tail still works, but the version may be missing (then give `software=`).
If you pass more than 2 MB of content, only its head and tail are used.

### Python

```python
from rag_drg.tools.diagnose import diagnose_output
d = diagnose_output(path="job.log")               # or content=..., filename=..., software=..., tail_lines=...
d.status, d.errors[0]["id"], d.errors[0]["fixes"], d.excerpt
print(d.format_text())                           # what the CLI prints; d.to_dict() for JSON
```

### Search

`knowledge/ess/errors.yaml` is indexed one entry per chunk, with titles like
`ESS errors > gaussian > g-l9999-max-opt-steps`. So `search_knowledge("l9999 error gaussian")` or a pasted
error line finds the entry. Note: these chunks carry no `software` filter value (one file covers several
programs), so search them **without** `software=`.

## The errors database (`knowledge/ess/errors.yaml`)

```yaml
meta: {title: ..., domain: ess, doc_type: gotcha, status: draft, tags: [...]}
errors:
  - software: gaussian          # gaussian | orca | qchem | molpro | psi4 | pyscf | scheduler
    id: g-l9999-max-opt-steps   # unique, lower-case, stable (used in results and search)
    pattern: '-- Number of steps exceeded,\s+NStep=\s*\d+'   # Python re, searched in the output text
    links: [l9999]              # optional, Gaussian: link(s) where this message is fatal
    priority: 60                # optional, default 50; higher wins; <= 0 = fallback only
    severity: error             # optional: error (default) | fatal | warning
    versions: ['09']            # optional: only for these program versions
    meaning: ...                # paraphrased
    fixes: [..., ...]           # ordered, most promising first; "(ARC ...)" = what ARC's troubleshooter does
    sources: [...]
```

- `severity: error` entries are only checked when the job did **not** terminate normally. `fatal` entries
  are checked even after a normal-termination marker and turn the status into `failed`. According to a
  comment in ARC's troubleshooter, Q-Chem prints its "Thank you" banner even after
  `MAXIMUM OPTIMIZATION CYCLES REACHED`. `warning` entries are reported but never change the status.
- `scheduler` entries (Slurm walltime / OOM kill, PBS walltime, HTCondor memory holds) are checked for every
  program, because a merged stdout file often ends with such a line.
- Messages that also occur in successful jobs need extra context in the pattern. For example,
  `Delta-x Convergence NOT Met` is only fatal when the next line is `Maximum number of corrector steps exceded`,
  and `Change in point group or standard orientation` only when an l202 termination follows.
- `q-generic-error-line` (any line containing "error" that is not a DIIS line) matches most normal
  Gaussian/ORCA logs. It has `priority: 0`, so it is only used when nothing else matched **and** the job did
  not terminate normally.
- `rag-drg lint` validates the file: meta fields, unique ids, patterns compile and do not match the empty
  string, required fields, known software and severities, and well-formed `links`.

### Adding an error

1. Take a real failing output. Copy a **short** excerpt into `tests/fixtures/outputs/<program>_<what>_fail.out` (use `.out`: `*.log` is git-ignored):
   the banner/version lines, the route or input echo, and the last ~20-40 lines around the message. Put the
   provenance in the first line (`# rag-drg test fixture - excerpt of ...`). Do not commit whole outputs,
   and do not commit outputs whose license forbids redistribution.
2. Add the entry to `knowledge/ess/errors.yaml`; the file stays `status: draft`. Keep literal output strings
   verbatim and write meaning/fixes in your own words. Only write fixes you can back with the manual or with
   a job that worked. Cite the source.
3. Add the fixture to `EXPECTED` in `tests/test_diagnose.py`:

   ```python
   "orca6_trah_fail.out": ("orca", "failed", "o-my-new-id", "Program Version 6.0.1"),
   ```

   The test checks that `diagnose_output` returns this status and first error. A second test runs **every**
   pattern over all `*_success*` fixtures, so a pattern that also matches a normal job fails the suite.
   If your message can appear in successful jobs, add a successful excerpt that contains it.
4. Run `.venv/bin/rag-drg lint` and `.venv/bin/python -m pytest -q`, then re-ingest so search sees the entry.

## Provenance and credit

- **ARC** ([ReactionMechanismGenerator/ARC](https://github.com/ReactionMechanismGenerator/ARC), MIT License,
  commit `d9f47ab`): most Gaussian/ORCA/Q-Chem/Molpro signatures and the "(ARC retry N)" fixes come from
  `arc/job/trsh.py` (`determine_ess_status`, `trsh_ess_job`, `trsh_keyword_*`) and ARC's parser adapters.
  Sources are cited per entry as `arc:<file>:<function>:<line>`. The test fixtures are short excerpts of
  ARC's `arc/testing/` outputs. The first line of each fixture names the file and the line ranges kept.
  Suspected ARC bugs found while doing this are listed in [arc-upstream-issues.md](arc-upstream-issues.md).
- **Gaussian common errors and solutions** by wongzit,
  <https://wongzit.github.io/gaussian-common-errors-and-solutions/>. That page has no license, so meanings and
  fixes taken from it are **paraphrased**. Only the literal Gaussian output strings (the patterns) are
  reproduced. Entries from it cite `wongzit`. The page itself credits the Alliance Canada wiki page
  "Gaussian error messages" and liyuanhe211/Solution_for_Every_Gaussian_Error_Message.
- **Psi4** entries: message formats from psi4/psi4 (`psi4/driver/p4util/exceptions.py`, `psi4/extras.py`,
  `psi4/run_psi4.py`); fixes from the Psi4 manual (SCF and optking chapters).
- **Slurm** messages from SchedMD/slurm (`src/slurmd/slurmstepd/req.c`,
  `src/plugins/task/cgroup/task_cgroup_memory.c`).
- A few fixtures are synthetic (Q-Chem SCF failure and cycle limit, Psi4 SCF failure, Slurm walltime). Their
  first line says so and names the real strings they are built around.

Everything in `errors.yaml` is `status: draft` until a group member checks it.
