"""Each hook-capable harness injects task-slot acquire/release at launch."""

from __future__ import annotations

import json
import os

from src.coding_provider_launch import build_coding_agent_launch_plan


def _plan(harness: str, run_dir):
    return build_coding_agent_launch_plan(
        harness_id=harness,
        base_command=harness,
        model="",
        endpoint_url="",
        run_dir=run_dir,
        default_harness_command=True,
    )


def test_pi_plan_injects_per_turn_gating_extension(tmp_path):
    plan = _plan("pi", tmp_path)
    assert "--extension" in plan.command
    src = (tmp_path / "odysseus-pi-extension.ts").read_text(encoding="utf-8")
    # Slot held for the whole turn: acquire at agent_start/turn_start, release at agent_end.
    assert '"agent_start"' in src
    assert '"turn_start"' in src
    assert '"agent_end"' in src
    assert '"task", "--action", "acquire"' in src
    assert '"task", "--action", "release"' in src
    # The agent can deliberately yield its slot mid-turn.
    assert "odysseus_yield" in src
    # Not the old per-LLM-call gating.
    assert 'pi.on("context"' not in src
    assert "after_provider_response" not in src


def test_omp_plan_injects_per_turn_hook_extension(tmp_path):
    plan = _plan("omp", tmp_path)
    assert "--hook" in plan.command
    src = (tmp_path / "odysseus-omp-hook.ts").read_text(encoding="utf-8")
    assert "agent_start" in src
    assert "turn_start" in src
    assert "agent_end" in src


def test_claude_plan_injects_settings_and_executable_scripts(tmp_path):
    plan = _plan("claude", tmp_path)
    assert "--settings" in plan.command

    settings = json.loads((tmp_path / "odysseus-claude-hooks.json").read_text(encoding="utf-8"))
    hooks = settings["hooks"]
    assert {"UserPromptSubmit", "Stop", "StopFailure"} <= set(hooks)

    acquire = tmp_path / "odysseus-task-acquire.sh"
    release = tmp_path / "odysseus-task-release.sh"
    assert acquire.exists() and release.exists()
    assert os.access(acquire, os.X_OK) and os.access(release, os.X_OK)
    assert hooks["UserPromptSubmit"][0]["hooks"][0]["command"] == str(acquire)
    assert hooks["Stop"][0]["hooks"][0]["command"] == str(release)


def test_codex_plan_injects_inline_hook_config(tmp_path):
    plan = _plan("codex", tmp_path)
    assert "hooks.userPromptSubmit=" in plan.command
    assert "hooks.stop=" in plan.command
    assert (tmp_path / "odysseus-task-acquire.sh").exists()


def test_generic_harness_gets_no_task_hooks(tmp_path):
    plan = _plan("generic", tmp_path)
    assert "--settings" not in plan.command
    assert not (tmp_path / "odysseus-claude-hooks.json").exists()
    assert not (tmp_path / "odysseus-task-acquire.sh").exists()
