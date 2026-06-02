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


# --- The `ody` guide must reach the agent's MODEL CONTEXT, not just the UI -----
# Regression guard: dropping a SKILL.md/AGENTS.md into run_dir (which is NOT the
# agent's cwd) or firing ctx.ui.notify never put the guide in the model's prompt.
# Each hook-capable harness now injects it via its own additive prompt lever.

_ODY_GUIDE_MARKER = "Code Station control plane"  # heading of _ODY_SKILL_MD


def test_pi_injects_ody_guide_into_system_prompt(tmp_path):
    plan = _plan("pi", tmp_path)
    assert "--append-system-prompt" in plan.command
    assert _ODY_GUIDE_MARKER in plan.command


def test_omp_injects_ody_guide_into_system_prompt(tmp_path):
    plan = _plan("omp", tmp_path)
    assert "--append-system-prompt" in plan.command
    assert _ODY_GUIDE_MARKER in plan.command


def test_claude_injects_ody_guide_into_system_prompt(tmp_path):
    plan = _plan("claude", tmp_path)
    assert "--append-system-prompt" in plan.command
    assert _ODY_GUIDE_MARKER in plan.command


def test_codex_injects_ody_guide_via_developer_instructions(tmp_path):
    plan = _plan("codex", tmp_path)
    # Additive developer-instructions layer (coexists with base prompt + AGENTS.md),
    # NOT model_instructions_file, which would replace the base instructions.
    assert "developer_instructions=" in plan.command
    assert "model_instructions_file" not in plan.command
    assert _ODY_GUIDE_MARKER in plan.command


def test_generic_harness_does_not_inject_ody_guide(tmp_path):
    plan = _plan("generic", tmp_path)
    assert "--append-system-prompt" not in plan.command
    assert "developer_instructions=" not in plan.command


def test_ody_guide_tells_agent_when_to_use_it(tmp_path):
    # The guide must steer agents to `ody` for other panes/agents and AWAY from raw
    # terminal reads — the failure mode the user hit (agent ran `tmux capture-pane`
    # / tailed raw.log instead of using the tool).
    cmd = _plan("pi", tmp_path).command
    assert "agent list --running-only" in cmd   # how to find other agents
    assert "pane read" in cmd                    # how to read another pane cleanly
    assert "agent start" in cmd                  # spawning subagents is called out
    assert "raw.log" in cmd and "tmux" in cmd    # named in the "do NOT" anti-pattern


# --- Phase 3b: agent-state reporting added ALONGSIDE the task-slot gating ----


def test_pi_plan_reports_state_and_keeps_slot_gating(tmp_path):
    _plan("pi", tmp_path)
    src = (tmp_path / "odysseus-pi-extension.ts").read_text(encoding="utf-8")
    # Task-slot gating still present (unchanged contract).
    assert '"task", "--action", "acquire"' in src
    assert '"task", "--action", "release"' in src
    # New: agent-state reporting on the same lifecycle events.
    assert "reportState" in src
    assert '"agent", "--action", "state"' in src
    assert 'reportState("working")' in src
    assert 'reportState("idle")' in src
    assert 'reportState("blocked")' in src
    # The ody control-plane skill is dropped on disk and surfaced via session_start.
    assert (tmp_path / "SKILL.md").exists()
    assert "ody agent report-state" in (tmp_path / "SKILL.md").read_text(encoding="utf-8")


def test_omp_plan_reports_state_and_keeps_slot_gating(tmp_path):
    _plan("omp", tmp_path)
    src = (tmp_path / "odysseus-omp-hook.ts").read_text(encoding="utf-8")
    assert '"task", "--action", "acquire"' in src
    assert '"task", "--action", "release"' in src
    assert "reportState" in src
    assert 'reportState("working")' in src
    assert 'reportState("idle")' in src
    assert (tmp_path / "SKILL.md").exists()


def test_claude_plan_reports_state_and_keeps_slot_gating(tmp_path):
    _plan("claude", tmp_path)
    settings = json.loads((tmp_path / "odysseus-claude-hooks.json").read_text(encoding="utf-8"))
    hooks = settings["hooks"]

    acquire = str(tmp_path / "odysseus-task-acquire.sh")
    release = str(tmp_path / "odysseus-task-release.sh")
    state = str(tmp_path / "odysseus-state.sh")

    # Task-slot gating is still the FIRST hook on each turn boundary.
    up_cmds = [h["command"] for h in hooks["UserPromptSubmit"][0]["hooks"]]
    stop_cmds = [h["command"] for h in hooks["Stop"][0]["hooks"]]
    assert up_cmds[0] == acquire
    assert stop_cmds[0] == release
    # New: agent-state reporting added alongside (not replacing) the gating.
    assert f"{state} working" in up_cmds
    assert f"{state} idle" in stop_cmds
    assert "Notification" in hooks
    assert hooks["Notification"][0]["hooks"][0]["command"] == f"{state} blocked"

    # The executable state shim exists and is runnable.
    assert os.access(tmp_path / "odysseus-state.sh", os.X_OK)
    # Skill injected via Claude's native skills dir.
    assert (tmp_path / ".claude" / "skills" / "ody" / "SKILL.md").exists()


def test_codex_plan_reports_state_and_keeps_slot_gating(tmp_path):
    plan = _plan("codex", tmp_path)
    # Task-slot gating still wired on userPromptSubmit/stop (unchanged contract).
    assert "hooks.userPromptSubmit=" in plan.command
    assert "hooks.stop=" in plan.command
    assert (tmp_path / "odysseus-task-acquire.sh").exists()
    # New: a permissionRequest hook reporting "blocked" + working/idle on the gated events.
    assert "hooks.permissionRequest=" in plan.command
    state = str(tmp_path / "odysseus-state.sh")
    assert f"{state} working" in plan.command
    assert f"{state} idle" in plan.command
    assert f"{state} blocked" in plan.command
    assert (tmp_path / "odysseus-state.sh").exists()
    # Skill injected via Codex's native AGENTS.md.
    assert (tmp_path / "AGENTS.md").exists()
