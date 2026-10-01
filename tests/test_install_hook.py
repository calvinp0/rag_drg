import json
import shlex

from rag_drg.tools import install_hook as ih


def test_command_is_an_absolute_launcher_path_and_a_valid_env_prefix():
    cmd = ih.hook_command()
    assert cmd.endswith(" check-input --hook")
    exe = shlex.split(cmd)[0]
    assert exe.startswith("/") and exe.endswith("/rag-drg")
    with_env = ih.hook_command("/opt/env with space/bin/rag-drg")
    words = shlex.split(with_env)
    assert words[0] == "RAG_DRG_BIN=/opt/env with space/bin/rag-drg"  # no leading "/" before the name
    assert words[1] == exe


def test_merge_replaces_a_broken_rag_drg_hook_and_keeps_the_others(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({
        "model": "opus",
        "hooks": {
            "PostToolUse": [
                {"matcher": "Write|Edit|MultiEdit", "hooks": [
                    {"type": "command", "command": "/RAG_DRG_BIN=/env/bin/rag-drg /x/bin/rag-drg check-input --hook"},
                    {"type": "command", "command": "ruff format $FILE"}]},
                {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo bash"}]},
            ],
            "Stop": [{"hooks": [{"type": "command", "command": "~/.claude/stop-hook-git-check.sh"}]}],
        },
    }))
    msgs = []
    assert ih.install(settings, "/repo/bin/rag-drg check-input --hook", verify=False, out=msgs.append) == 0
    data = json.loads(settings.read_text())
    commands = [h["command"] for e in data["hooks"]["PostToolUse"] for h in e["hooks"]]
    assert commands.count("/repo/bin/rag-drg check-input --hook") == 1
    assert not any(c.startswith("/RAG_DRG_BIN") for c in commands)
    assert "ruff format $FILE" in commands and "echo bash" in commands
    assert data["hooks"]["Stop"] and data["model"] == "opus"
    assert (tmp_path / "settings.json.bak").exists() and "replaced 1" in msgs[0]
    # idempotent
    ih.install(settings, "/repo/bin/rag-drg check-input --hook", verify=False, out=msgs.append)
    again = json.loads(settings.read_text())
    assert [h["command"] for e in again["hooks"]["PostToolUse"] for h in e["hooks"]].count(
        "/repo/bin/rag-drg check-input --hook") == 1


def test_a_command_that_does_not_run_is_refused_before_writing(tmp_path):
    settings = tmp_path / "settings.json"
    msgs = []
    bad = "/RAG_DRG_BIN=/nowhere/bin/rag-drg /nowhere/bin/rag-drg check-input --hook"
    assert ih.install(settings, bad, out=msgs.append) == 1
    assert not settings.exists() and "nothing written" in msgs[0]


def test_the_real_command_runs_as_a_hook(tmp_path):
    settings = tmp_path / "s.json"
    assert ih.install(settings, ih.hook_command(), out=lambda m: None) == 0
    assert json.loads(settings.read_text())["hooks"]["PostToolUse"][0]["matcher"] == ih.MATCHER


def test_invalid_settings_json_is_left_alone(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text("{ not json")
    msgs = []
    assert ih.install(settings, "/x check-input --hook", verify=False, out=msgs.append) == 1
    assert settings.read_text() == "{ not json" and "not valid JSON" in msgs[0]
