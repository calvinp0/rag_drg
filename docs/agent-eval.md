# Agent-task evaluation (`rag-drg agent-eval`)

`rag-drg eval` asks whether search finds the right card. This eval asks the question that
matters: **does an agent do the group's real tasks correctly, and is it better with rag-drg than
without?**

For each task in `eval/tasks.yaml` the harness:
1. creates an empty work directory outside the repository and puts the task's `files:` in it;
2. runs the agent twice:
   * `with`: rag-drg is its only MCP server;
   * `without`: it has no MCP servers;
3. repeats step 2 `repeats` times (default 3), because agents are not deterministic;
4. grades the files the agent wrote and its final answer with pass/fail checks;
5. reports pass rates with 95% confidence intervals, per task, per split and per condition.

## Quick start

```bash
bin/rag-drg agent-eval validate            # the task file is well-formed, every task has a reference
bin/rag-drg agent-eval check-graders       # references pass, known-bad solutions fail
bin/rag-drg agent-eval run --ids orca-maxcore-fix --repeats 1       # one cheap real run first
bin/rag-drg agent-eval run --split dev                              # the dev set, 3 repeats
bin/rag-drg agent-eval report eval/runs/<id>
```

A full run is 10 tasks × 2 conditions × 3 repeats, i.e. 60 agent sessions. They cost time and
API money; each result shows `total_cost_usd`. Start small.

## The agent

By default the agent is Claude Code in headless mode:

```
claude -p "<task>" --output-format json --bare --setting-sources project \
       --permission-mode acceptEdits --strict-mcp-config --mcp-config <with|without>.json \
       --allowedTools "Read Write Edit Glob Grep [mcp__rag-drg]" \
       --disallowedTools "Read(//<repo>/**) Glob(//<repo>/**) ..."
```

* `--strict-mcp-config` + `--mcp-config` make the two conditions differ in exactly one thing.
* `--setting-sources project` keeps your own hooks, plugins and user settings out.
  * With `ANTHROPIC_API_KEY` set, `--bare` also keeps out your `~/.claude/CLAUDE.md` and skills.
  * `--bare` never reads a Claude subscription's OAuth login, so without a key the runner drops it.
    Your `~/.claude/CLAUDE.md` and skills are then visible to both conditions alike, and
    `run.json` says so.
* Each run works in a fresh directory in the system temp dir, outside the repository. It is
  copied to `<rep>/work` afterwards.
* The file tools are denied on the repository (`--disallowedTools`), so rag-drg's cards reach the
  agent only through MCP. In the first real run, `without` agents working inside `eval/runs/` read
  `knowledge/` and `servers.yaml` from disk and passed everything.
* A `without` answer citing repository paths is counted as a leak, with a warning in the report.
* The agent gets no shell (no Bash): it writes files; it does not run jobs.
* `--model M` picks the model.

**Other agents.** A local model with a tool-calling wrapper, aider, and so on: set `agent.command`
in `eval/tasks.yaml`, using the placeholders `{prompt}`, `{mcp_config}`, `{allowed_tools}`,
`{denied_tools}`, `{workdir}` and `{model}`. The command runs in the work directory. Its stdout is the final
answer, or it can print Claude Code's JSON (`{"result": ...}`).

## Graders

Graders are deterministic and cheap, and there is no LLM judge (see
`rag_drg/tools/_agent_eval/graders.py`):

| type | passes when |
|---|---|
| `file_exists` | a file matching `path` (a glob is allowed) exists |
| `regex` / `not_regex` | some / no matching file contains `pattern` (`flags: i`) |
| `number` | the first group of `pattern` is a number within `min`..`max` |
| `answer_regex` / `answer_not_regex` | the final answer does / doesn't contain `pattern` |
| `check_input` | rag-drg's input checker finds no errors (optionally with `submit:`) |
| `arc_check` | rag-drg's ARC `input.yml` checker finds no errors |

A task passes only if **every** check passes. Write checks for what the group needs, not for one
wording. For example, the GPU task accepts either GPU queue, and its "no hard-coded GPU 0" check
ignores comment lines, because a correct answer may explain the mistake.

**Graders need tests too.** Each task has:
* `eval/tasks/<id>/reference/`: a correct solution (files + `answer.txt`) that must pass;
* `eval/tasks/<id>/bad/<variant>/`: realistic wrong solutions that must fail. Examples: `%maxcore`
  written as total memory, a missing `/C` auxiliary basis, `EmpiricalDispersion=D3BJ`, a
  hard-coded GPU 0, ARC's queues including `zeus_long_q`, a PBS script for Atlas, an invented g16
  on Atlas.

`check-graders` runs both, and the unit tests run it on every commit. A grader that fails a
correct answer, or passes a wrong one, is a bug in the eval, not in the agent.

## Reading the results

* **Pass rate with a 95% interval.** With 7 tasks × 3 repeats, one run is about 5 points. If the
  `with` and `without` intervals overlap, you haven't shown a difference. Add tasks before
  concluding anything.
* **"Tasks passing every repeat"** (pass^k) matters more than the average for job scripts: a
  script that is right 2 times out of 3 is not safe to hand out.
* **Most frequent failed checks:** open those runs (`<run>/<task>/<condition>/rep<k>/`: `work/`,
  `answer.txt`, `agent.out`) and read them. Error analysis on real transcripts is where the next
  card, lesson or task comes from.
* **Agent errors:** a missing login, a network failure or a timeout is shown separately and left
  out of the rates. It says nothing about rag-drg. Re-running with `--out <run>` retries only those.
* **`regrade <run>`** re-applies the current checks to finished runs, so a grader fix needs no new
  agent sessions.

## Keeping the eval honest

* **Tune on `dev` only.** Keep about a third of the tasks as `split: holdout`, and look at holdout
  results rarely. If cards, ranking or instructions change only to pass a holdout task, it stops
  telling you anything.
* **New tasks come from real failures:** a recorded lesson (`knowledge/lessons/`), a job
  `diagnose_output` explained, a correction in a chat, a question that appears often in the query
  log. Each task has a `why:` saying which failure it guards against.
* **Keep a task once it passes:** it is now a regression test. Remove a task only when the
  group's practice changes, and then update its reference in the same PR.
* **Say what changed.** Compare runs with the same model and repeats, and write the `rag-drg`
  commit, the model and the date next to each result.
