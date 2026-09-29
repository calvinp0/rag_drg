"""rag-drg agent-eval: graders, task validation, the eval-of-the-eval, and a full run with a fake agent."""

import json
import sys
import textwrap
from pathlib import Path

import pytest
import yaml

from rag_drg.config import load_config
from rag_drg.tools._agent_eval import runner
from rag_drg.tools._agent_eval.graders import grade, grade_check
from rag_drg.tools._agent_eval.tasks import load_suite, validate
from rag_drg.tools.agent_eval import check_graders

REPO = Path(__file__).resolve().parents[1]


def _g(spec, work, answer=""):
    return grade_check(spec, work, answer)


def test_file_and_regex_graders(tmp_path):
    (tmp_path / "a.inp").write_text("! B3LYP def2-SVP\n%maxcore 3000\n# note: %GPUCPU=0=0 is wrong\n")
    assert _g({"type": "file_exists", "path": "a.inp"}, tmp_path).passed
    assert not _g({"type": "file_exists", "path": "*.sub"}, tmp_path).passed
    assert _g({"type": "regex", "path": "*.inp", "pattern": "b3lyp", "flags": "i"}, tmp_path).passed
    assert not _g({"type": "regex", "path": "missing.inp", "pattern": "x"}, tmp_path).passed
    assert _g({"type": "not_regex", "path": "*.sh", "pattern": "x"}, tmp_path).passed  # no files: nothing forbidden
    assert not _g({"type": "not_regex", "path": "a.inp", "pattern": "maxcore"}, tmp_path).passed
    # comment lines can be excluded in the pattern itself
    assert _g({"type": "not_regex", "path": "a.inp", "pattern": r"^[^#\n]*%GPUCPU=0=0"}, tmp_path).passed
    num = {"type": "number", "path": "a.inp", "pattern": r"%maxcore\s+(\d+)", "min": 1000, "max": 4000}
    assert _g(num, tmp_path).passed
    assert not _g({**num, "max": 2000}, tmp_path).passed
    assert _g({"type": "answer_regex", "pattern": "not installed", "flags": "i"}, tmp_path, "G16 is NOT installed").passed
    assert not _g({"type": "answer_not_regex", "pattern": "wB97XD"}, tmp_path, "! wB97XD def2-TZVP").passed


def test_broken_check_is_a_failure_not_a_crash(tmp_path):
    (tmp_path / "x").write_text("1")
    res = grade([{"type": "number", "path": "x", "pattern": "(", "name": "bad"}], tmp_path, "")
    assert not res[0].passed and "grader error" in res[0].detail


def test_check_input_grader_uses_rag_drg_checker(tmp_path):
    (tmp_path / "w.gjf").write_text("%nprocshared=4\n%mem=8GB\n#P B3LYP/6-31G(d) Opt\n\nwater\n\n0 1\nO 0 0 0\nH 0 0 0.96\nH 0 0.93 -0.26\n\n")
    assert _g({"type": "check_input", "path": "w.gjf"}, tmp_path).passed
    (tmp_path / "w.gjf").write_text("#P B3LYP/6-31G(d) Opt\nwater\n0 1\nO 0 0 0\n")  # blank lines lost
    r = _g({"type": "check_input", "path": "w.gjf"}, tmp_path)
    assert not r.passed and "gaussian" in r.detail


@pytest.mark.parametrize("task, problem", [
    ({"id": "Bad Id", "prompt": "p", "checks": [{"type": "file_exists", "path": "a"}]}, "id must be"),
    ({"id": "t", "prompt": "", "checks": [{"type": "file_exists", "path": "a"}]}, "prompt is required"),
    ({"id": "t", "prompt": "p", "checks": []}, "at least one check"),
    ({"id": "t", "prompt": "p", "checks": [{"type": "llm_judge"}]}, "type must be one of"),
    ({"id": "t", "prompt": "p", "checks": [{"type": "regex", "path": "a", "pattern": "("}]}, "bad regex"),
    ({"id": "t", "prompt": "p", "checks": [{"type": "regex", "path": "../x", "pattern": "a"}]}, "inside the work"),
    ({"id": "t", "prompt": "p", "split": "test", "checks": [{"type": "file_exists", "path": "a"}]}, "split"),
])
def test_validation(task, problem):
    assert any(problem in p for p in validate({"tasks": [task]})), validate({"tasks": [task]})


def test_repo_task_suite_is_valid_and_its_graders_are_sound(capsys):
    """Every reference passes and every bad/ solution fails: the eval of the eval."""
    suite = load_suite(REPO / "eval" / "tasks.yaml")
    assert len(suite.tasks) >= 10 and {t.split for t in suite.tasks} == {"dev", "holdout"}
    assert all(t.why for t in suite.tasks)
    assert check_graders(suite, load_config(REPO / "rag_drg.yaml")) == 0, capsys.readouterr().out


def test_wilson_interval():
    lo, hi = runner.wilson(0, 10)
    assert lo == 0 and 0.25 < hi < 0.35
    lo, hi = runner.wilson(10, 10)
    assert 0.65 < lo < 0.75 and hi == 1.0
    assert runner.wilson(0, 0) == (0.0, 1.0)


def test_build_command_and_mcp_configs(tmp_path):
    agent = runner.DEFAULT_AGENT
    cmd = runner.build_command(agent, "without", "do it", tmp_path / "m.json", tmp_path, "some-model")
    assert cmd[:3] == ["claude", "-p", "do it"] and "--strict-mcp-config" in cmd
    assert cmd[cmd.index("--allowedTools") + 1] == "Read Write Edit Glob Grep" and cmd[-2:] == ["--model", "some-model"]
    assert runner.mcp_config("without", None) == {"mcpServers": {}}
    w = runner.mcp_config("with", tmp_path / "rag_drg.yaml")["mcpServers"]["rag-drg"]
    assert w["command"].endswith("bin/rag-drg") and w["args"] == ["serve"]


def test_parse_answer():
    text, meta = runner.parse_answer(json.dumps({"result": "hi", "total_cost_usd": 0.02, "num_turns": 3, "x": 1}))
    assert text == "hi" and meta == {"total_cost_usd": 0.02, "num_turns": 3}
    assert runner.parse_answer("plain text answer") == ("plain text answer", {})


FAKE_AGENT = textwrap.dedent('''
    """Fake agent: with rag-drg (an MCP config naming it) it writes the right file, without it a wrong one."""
    import json, sys
    prompt, mcp = sys.argv[1], json.load(open(sys.argv[2]))
    good = "rag-drg" in mcp["mcpServers"]
    if "maxcore" in prompt:
        open("job.inp", "w").write("%maxcore " + ("3000" if good else "64000") + "\\n")
    print(json.dumps({"result": "done " + ("with" if good else "without"), "total_cost_usd": 0.01}))
''')


def test_full_run_with_a_fake_agent(tmp_path):
    fake = tmp_path / "fake_agent.py"
    fake.write_text(FAKE_AGENT)
    tasks = {
        "agent": {"command": [sys.executable, str(fake), "{prompt}", "{mcp_config}"]},
        "tasks": [
            {"id": "maxcore", "split": "dev", "prompt": "fix the maxcore", "why": "w",
             "checks": [{"type": "number", "path": "job.inp", "pattern": r"%maxcore\s+(\d+)", "max": 4000}]},
            {"id": "says-with", "split": "holdout", "prompt": "say something", "why": "w",
             "checks": [{"type": "answer_regex", "pattern": "with$"}]},
        ],
    }
    (tmp_path / "tasks.yaml").write_text(yaml.safe_dump(tasks))
    suite = load_suite(tmp_path / "tasks.yaml")
    out = tmp_path / "run"
    spec = runner.RunSpec(suite, suite.tasks, ["with", "without"], 2, out, timeout_s=60)
    runner.run(spec, progress=lambda m: None)
    results = runner.load_results(out)
    assert len(results) == 8
    s = runner.summarize(results)
    assert s["overall"]["all"]["with"]["passed"] == 4 and s["overall"]["all"]["without"]["passed"] == 0
    assert s["overall"]["holdout"]["with"]["tasks_all_reps"] == 1
    assert s["tasks"]["maxcore"]["without"] == {"passed": 0, "n": 2, "all": False}
    assert abs(s["cost_usd"]["with"] - 0.04) < 1e-9
    report = runner.format_report(s)
    assert "| maxcore | dev | 2/2 | 0/2 |" in report and "maxcore-" not in report
    assert any("without | maxcore" in k for k, _ in s["top_failures"])
    # resume: finished runs are not repeated
    (out / "maxcore" / "with" / "rep1" / "agent.out").write_text("sentinel")
    runner.run(spec, progress=lambda m: None)
    assert (out / "maxcore" / "with" / "rep1" / "agent.out").read_text() == "sentinel"
    # regrade with a stricter check: the 'with' runs now fail too
    suite.tasks[0].checks[0]["max"] = 1000
    assert runner.regrade(out, suite, None) == 8
    assert runner.summarize(runner.load_results(out))["overall"]["all"]["with"]["passed"] == 2


def test_missing_agent_binary_is_recorded(tmp_path):
    (tmp_path / "tasks.yaml").write_text(yaml.safe_dump({
        "agent": {"command": ["definitely-not-an-agent-binary", "{prompt}"]},
        "tasks": [{"id": "t", "prompt": "p", "why": "w", "checks": [{"type": "file_exists", "path": "a"}]}]}))
    suite = load_suite(tmp_path / "tasks.yaml")
    runner.run(runner.RunSpec(suite, suite.tasks, ["without"], 1, tmp_path / "run"), progress=lambda m: None)
    res = runner.load_results(tmp_path / "run")[0]
    assert res["returncode"] == -2 and not res["passed"] and res["agent_error"]
    s = runner.summarize(runner.load_results(tmp_path / "run"))
    assert s["overall"] == {} and len(s["agent_errors"]) == 1  # an outage is not a task failure
    assert "agent itself failed" in runner.format_report(s)


def test_api_error_json_is_an_agent_error():
    _, meta = runner.parse_answer(json.dumps({"result": "", "terminal_reason": "api_error", "total_cost_usd": 0}))
    assert runner.agent_error(0, meta, "")
