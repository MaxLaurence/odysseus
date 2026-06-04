"""Provider-tool launch configuration for coding-agent harnesses."""

from __future__ import annotations

import json
import shlex
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.coding_effort import codex_effort_config_args, effort_supported, normalize_effort
from src.coding_provider_bridge import scripts_dir


ODYSSEUS_MCP_SERVER_NAME = "odysseus"
ODYSSEUS_PI_EXTENSION_NAME = "odysseus-pi-extension.ts"
ODYSSEUS_OMP_EXTENSION_NAME = "odysseus-omp-hook.ts"
ODYSSEUS_PI_PROVIDER_NAME = "odysseus"
ODYSSEUS_TOOL_DISPATCH_ARG = "--odysseus-tool"


@dataclass(frozen=True)
class CodingAgentLaunchPlan:
    command: str
    metadata: dict[str, Any]


def _toml_string(value: str) -> str:
    # JSON string quoting is valid TOML basic-string syntax for these values.
    return json.dumps(value)


def _codex_mcp_server_command_parts() -> tuple[str, list[str]]:
    if getattr(sys, "frozen", False):
        return sys.executable, [ODYSSEUS_TOOL_DISPATCH_ARG, "mcp-server"]
    return sys.executable, [str(scripts_dir() / "odysseus-tool"), "mcp-server"]


def codex_mcp_config_arg() -> str:
    command, args = _codex_mcp_server_command_parts()
    args_literal = ",".join(_toml_string(arg) for arg in args)
    return (
        f"mcp_servers.{ODYSSEUS_MCP_SERVER_NAME}="
        f"{{command={_toml_string(command)},args=[{args_literal}]}}"
    )


def _pi_provider_registration_source(*, model: str, endpoint_url: str) -> str:
    if not model or not endpoint_url:
        return ""
    provider_name = json.dumps(ODYSSEUS_PI_PROVIDER_NAME)
    model_literal = json.dumps(model)
    endpoint_literal = json.dumps(endpoint_url)
    return f'''
  pi.registerProvider({provider_name}, {{
    baseUrl: {endpoint_literal},
    api: "openai-completions",
    apiKey: process.env.OPENAI_API_KEY || "test",
    authHeader: true,
    models: [
      {{
        id: {model_literal},
        name: {model_literal},
        reasoning: false,
        input: ["text", "image"],
        cost: {{ input: 0, output: 0, cacheRead: 0, cacheWrite: 0 }},
        contextWindow: 262144,
        maxTokens: 32768,
      }},
    ],
  }});
'''


def _write_pi_extension(run_dir: Path, *, model: str = "", endpoint_url: str = "") -> Path:
    path = run_dir / ODYSSEUS_PI_EXTENSION_NAME
    source = _PI_EXTENSION_SOURCE.replace(
        "__ODYSSEUS_PI_PROVIDER_REGISTRATION__",
        _pi_provider_registration_source(model=model, endpoint_url=endpoint_url),
    ).replace("__ODYSSEUS_ODY_SKILL_SUMMARY__", json.dumps(_ODY_SKILL_SUMMARY))
    path.write_text(source, encoding="utf-8")
    return path


def _write_omp_extension(run_dir: Path) -> Path:
    path = run_dir / ODYSSEUS_OMP_EXTENSION_NAME
    source = _OMP_EXTENSION_SOURCE.replace(
        "__ODYSSEUS_ODY_SKILL_SUMMARY__", json.dumps(_ODY_SKILL_SUMMARY)
    )
    path.write_text(source, encoding="utf-8")
    return path


def _write_skill_md(run_dir: Path | None, *, harness: str = "") -> Path | None:
    """Drop a per-run SKILL.md documenting the ``ody`` control-plane CLI.

    Injected per-harness via each one's native context mechanism:
      * Claude → ``<run_dir>/.claude/skills/ody/SKILL.md`` (auto-discovered skill).
      * Codex  → ``<run_dir>/AGENTS.md`` (Codex auto-reads it from the cwd).
      * Pi/OMP → the extension surfaces a summary via ``session_start`` (above);
        the full file is still written so the agent can read it on disk.

    Returns the primary path written (the harness-native one), or None when no
    ``run_dir`` is available.
    """
    if run_dir is None:
        return None
    harness = (harness or "").strip().lower()
    if harness == "claude":
        target = run_dir / ".claude" / "skills" / "ody" / "SKILL.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_ODY_SKILL_MD, encoding="utf-8")
        return target
    if harness == "codex":
        target = run_dir / "AGENTS.md"
        target.write_text(_ODY_SKILL_MD, encoding="utf-8")
        return target
    # Pi/OMP and any other harness: write a plain SKILL.md in the run dir so the
    # agent (surfaced via the extension session_start) can read the full doc.
    target = run_dir / "SKILL.md"
    target.write_text(_ODY_SKILL_MD, encoding="utf-8")
    return target


def _write_task_hook_scripts(run_dir: Path) -> tuple[Path, Path]:
    """Tiny executable wrappers a config-file harness hook invokes with no args.

    Each calls ``odysseus-tool gate <acquire|release>`` (slot store keyed by
    ODYSSEUS_RUN_ID), so the harness hook config only needs an absolute path and we
    avoid argument-quoting differences between Claude and Codex hook runners.
    """
    acquire = run_dir / "odysseus-task-acquire.sh"
    release = run_dir / "odysseus-task-release.sh"
    acquire.write_text("#!/bin/sh\nexec odysseus-tool gate acquire --max-wait 28\n", encoding="utf-8")
    release.write_text("#!/bin/sh\nexec odysseus-tool gate release\n", encoding="utf-8")
    for path in (acquire, release):
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return acquire, release


def _write_state_hook_script(run_dir: Path) -> Path:
    """A tiny executable wrapper a harness hook invokes as ``<script> <state>``.

    Forwards to ``odysseus-tool state "$1"`` (working|blocked|idle|done|unknown),
    which best-effort POSTs the semantic agent state to the provider bridge and
    emits nothing on stdout (a clean "proceed" for hook parsers). Kept as a file
    so hook configs only need an absolute path + one positional arg, avoiding
    argument-quoting differences between Claude and Codex hook runners.
    """
    script = run_dir / "odysseus-state.sh"
    script.write_text('#!/bin/sh\nexec odysseus-tool state "$1"\n', encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return script


def _write_claude_hooks_settings(run_dir: Path, *, acquire: Path, release: Path, state: Path) -> Path:
    """A `claude --settings` file that gates each turn on a task slot AND reports
    the run's semantic agent state.

    UserPromptSubmit → acquire (blocks until a slot is free) + report ``working``;
    Stop/StopFailure → release + report ``idle``; Notification → report ``blocked``
    (Claude fires Notification when it pauses for input/permission). Passed via
    --settings so the user's global/project config and auth are untouched. Hooks
    in a list run in order, so gating (acquire/release) always still fires.
    """

    def _state_hook(value: str, timeout: int = 10) -> dict[str, Any]:
        return {"type": "command", "command": f"{state} {value}", "timeout": timeout}

    settings = {
        "hooks": {
            "UserPromptSubmit": [
                {
                    "hooks": [
                        {"type": "command", "command": str(acquire), "timeout": 30},
                        _state_hook("working"),
                    ]
                }
            ],
            "Stop": [
                {
                    "hooks": [
                        {"type": "command", "command": str(release), "timeout": 600},
                        _state_hook("idle"),
                    ]
                }
            ],
            "StopFailure": [
                {
                    "hooks": [
                        {"type": "command", "command": str(release), "timeout": 600},
                        _state_hook("idle"),
                    ]
                }
            ],
            "Notification": [
                {"hooks": [_state_hook("blocked")]}
            ],
        }
    }
    path = run_dir / "odysseus-claude-hooks.json"
    path.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    return path


def _codex_hook_config_args(acquire: Path, release: Path, state: Path) -> list[str]:
    """Inline `-c` overrides registering per-turn hooks (no file / no CODEX_HOME).

    Event names use the camelCase keys from Codex's HookEventName enum. Each event
    keeps its task-slot gating command (acquire/release) AND adds an agent-state
    report (``odysseus-state.sh <state>``) so the herdr rollup reflects this run:
    userPromptSubmit → working, stop → idle, permissionRequest → blocked. Hooks in
    a list run in order, so gating always still fires.

    Codex command hooks take a single ``command`` string (no separate ``args``
    array), so the state is baked into the command (``odysseus-state.sh idle``).
    ``permissionRequest`` is Codex's pause-for-approval event (there is no
    ``notification`` event in Codex's HookEventName enum).
    """
    acquire_cmd = _toml_string(str(acquire))
    release_cmd = _toml_string(str(release))
    state_working = _toml_string(f"{state} working")
    state_idle = _toml_string(f"{state} idle")
    state_blocked = _toml_string(f"{state} blocked")
    return [
        "--config",
        (
            "hooks.userPromptSubmit=[{ hooks = ["
            f'{{ type = "command", command = {acquire_cmd}, timeout = 30 }}, '
            f'{{ type = "command", command = {state_working}, timeout = 10 }}'
            "] }]"
        ),
        "--config",
        (
            "hooks.stop=[{ hooks = ["
            f'{{ type = "command", command = {release_cmd}, timeout = 600 }}, '
            f'{{ type = "command", command = {state_idle}, timeout = 10 }}'
            "] }]"
        ),
        "--config",
        (
            "hooks.permissionRequest=[{ hooks = ["
            f'{{ type = "command", command = {state_blocked}, timeout = 10 }}'
            "] }]"
        ),
    ]


def build_coding_agent_launch_plan(
    *,
    harness_id: str,
    base_command: str,
    model: str | None,
    endpoint_url: str | None = "",
    run_dir: Path | None = None,
    default_harness_command: bool = True,
    effort: str | None = None,
    auth_mode: str | None = None,
) -> CodingAgentLaunchPlan:
    """Return a harness command plus visible launch metadata.

    We only rewrite default Pi/Codex harness commands. A caller-provided shell
    command is treated as an explicit override, but still gets the provider
    bridge environment and `odysseus-tool` on PATH at runtime.

    ``effort`` is a normalized reasoning level (see src/coding_effort.py): for Codex
    it becomes a ``-c model_reasoning_effort=`` launch arg; for Claude it is applied
    via env (built in coding_model_config) so only metadata is recorded here.
    ``auth_mode`` ("none"|"endpoint"|"subscription") is recorded for observability;
    the credential env it implies is injected in coding_model_config.build_launch_env.
    """
    harness = (harness_id or "").strip().lower()
    configured_model = (model or "").strip()
    endpoint_url = (endpoint_url or "").strip().rstrip("/")
    normalized_effort = normalize_effort(effort)
    normalized_auth_mode = (auth_mode or "none").strip().lower() or "none"
    metadata: dict[str, Any] = {
        "harness_id": harness,
        "base_command": base_command,
        "default_harness_command": bool(default_harness_command),
        "configured_model": configured_model,
        "effort": normalized_effort,
        "effort_applied": bool(normalized_effort) and effort_supported(harness),
        "auth_mode": normalized_auth_mode,
        "model_explicit": False,
        "provider_tools": {
            "mode": "cli",
            "command": "odysseus-tool",
            "env": [
                "ODYSSEUS_TOOL_URL",
                "ODYSSEUS_TOOL_TOKEN",
                "ODYSSEUS_THREAD_ID",
                "ODYSSEUS_PROJECT_ID",
                "ODYSSEUS_RUN_ID",
            ],
        },
    }
    command = base_command

    if not default_harness_command:
        metadata["provider_tools"]["note"] = "caller-supplied command; provider bridge CLI is available on PATH"
        return CodingAgentLaunchPlan(command=command, metadata=metadata)

    if harness == "pi":
        parts = ["pi"]
        pi_provider_source = ""
        if configured_model and endpoint_url:
            pi_provider_source = "odysseus-endpoint"
            parts.extend(["--provider", ODYSSEUS_PI_PROVIDER_NAME])
            metadata["pi_provider"] = ODYSSEUS_PI_PROVIDER_NAME
            metadata["pi_provider_source"] = pi_provider_source
        if configured_model:
            parts.extend(["--model", configured_model])
            metadata["model_explicit"] = True
            metadata["model_arg"] = "--model"
        if run_dir is not None:
            extension_path = _write_pi_extension(
                run_dir,
                model=configured_model if pi_provider_source else "",
                endpoint_url=endpoint_url if pi_provider_source else "",
            )
            parts.extend(["--extension", str(extension_path)])
            # Put the `ody` control-plane guide into the model's *system prompt*. The
            # extension's session_start ctx.ui.notify only reaches the human-facing UI,
            # and the on-disk SKILL.md sits in run_dir (not the agent's cwd), so neither
            # ever entered the model's context. --append-system-prompt does, additively.
            parts.extend(_ody_skill_prompt_args("pi"))
            _write_skill_md(run_dir, harness="pi")
            metadata["provider_tools"] = {
                "mode": "pi-extension",
                "extension_path": str(extension_path),
                "tools": ["odysseus_provider", "odysseus_list_tools"],
                "command": "odysseus-tool",
                "env": metadata["provider_tools"]["env"],
            }
            metadata["agent_state_hooks"] = {"mode": "pi-extension", "states": ["working", "idle", "blocked"]}
        command = shlex.join(parts)
    elif harness == "codex":
        parts = ["codex"]
        if configured_model:
            parts.extend(["--model", configured_model])
            metadata["model_explicit"] = True
            metadata["model_arg"] = "--model"
        effort_args = codex_effort_config_args(normalized_effort)
        if effort_args:
            parts.extend(effort_args)
            metadata["effort_arg"] = effort_args[-1]
        if endpoint_url:
            parts.extend(["--config", f"openai_base_url={_toml_string(endpoint_url)}"])
            metadata["endpoint_config_arg"] = "openai_base_url"
        mcp_config = codex_mcp_config_arg()
        mcp_executable, mcp_args = _codex_mcp_server_command_parts()
        mcp_command = shlex.join([mcp_executable, *mcp_args])
        parts.extend(["--config", mcp_config])
        metadata["provider_tools"] = {
            "mode": "codex-mcp",
            "server": ODYSSEUS_MCP_SERVER_NAME,
            "config_arg": mcp_config,
            "tools": ["odysseus_provider", "odysseus_list_tools"],
            "command": mcp_command,
            "env": metadata["provider_tools"]["env"],
        }
        if run_dir is not None:
            # Per-turn task-slot gating via inline -c hook overrides (no file write
            # into the user's repo, no CODEX_HOME relocation that would break auth).
            # The same events also report semantic agent state (working/idle/blocked).
            acquire, release = _write_task_hook_scripts(run_dir)
            state_script = _write_state_hook_script(run_dir)
            parts.extend(_codex_hook_config_args(acquire, release, state_script))
            metadata["task_hooks"] = {"mode": "codex-config-hooks", "granularity": "per-turn"}
            metadata["agent_state_hooks"] = {"mode": "codex-config-hooks", "states": ["working", "idle", "blocked"]}
            # Inject the `ody` guide additively via developer_instructions. run_dir/AGENTS.md
            # is off Codex's cwd->repo-root discovery path, so it was never read; this layers
            # the guide on top of the base prompt and any project AGENTS.md (verified additive).
            parts.extend(_ody_skill_prompt_args("codex"))
            _write_skill_md(run_dir, harness="codex")
        command = shlex.join(parts)
    elif harness == "claude":
        # Per-turn task-slot gating via `--settings` (leaves global/project config
        # and auth intact): UserPromptSubmit acquires, Stop/StopFailure releases.
        parts = ["claude"]
        if configured_model:
            # `--model` overrides the ANTHROPIC_MODEL env and the session default,
            # so model selection works for both subscription and api-key auth.
            parts.extend(["--model", configured_model])
            metadata["model_explicit"] = True
            metadata["model_arg"] = "--model"
        if run_dir is not None:
            acquire, release = _write_task_hook_scripts(run_dir)
            state_script = _write_state_hook_script(run_dir)
            hooks_path = _write_claude_hooks_settings(
                run_dir, acquire=acquire, release=release, state=state_script
            )
            parts.extend(["--settings", str(hooks_path)])
            # Claude auto-discovers skills from the *cwd* `.claude/skills` dir, not from
            # run_dir, so the dropped skill file was never surfaced. --append-system-prompt
            # injects the `ody` guide directly (additive to the default system prompt).
            parts.extend(_ody_skill_prompt_args("claude"))
            _write_skill_md(run_dir, harness="claude")
            metadata["provider_tools"] = {
                "mode": "claude-hooks",
                "settings_path": str(hooks_path),
                "command": "odysseus-tool",
                "env": metadata["provider_tools"]["env"],
                "note": "per-turn task-slot gating (UserPromptSubmit/Stop) + agent-state reporting",
            }
            metadata["agent_state_hooks"] = {"mode": "claude-hooks", "states": ["working", "idle", "blocked"]}
        command = shlex.join(parts)
    elif harness == "omp":
        # OMP (oh-my-pi) is Pi-API-compatible; inject a minimal hook extension that
        # gates each LLM call on a per-endpoint task slot (before_agent_start /
        # agent_end). `--hook` is OMP's alias for `--extension`.
        parts = ["omp"]
        if run_dir is not None:
            extension_path = _write_omp_extension(run_dir)
            parts.extend(["--hook", str(extension_path)])
            # Same as Pi: the hook's session_start notify is UI-only and run_dir/SKILL.md
            # is off the cwd discovery path, so inject the `ody` guide into the prompt.
            parts.extend(_ody_skill_prompt_args("omp"))
            _write_skill_md(run_dir, harness="omp")
            metadata["provider_tools"] = {
                "mode": "omp-hook",
                "hook_path": str(extension_path),
                "command": "odysseus-tool",
                "env": metadata["provider_tools"]["env"],
                "note": "task-slot gating via before_agent_start/agent_end + agent-state reporting",
            }
            metadata["agent_state_hooks"] = {"mode": "omp-hook", "states": ["working", "idle", "blocked"]}
        command = shlex.join(parts)

    metadata["command"] = command
    return CodingAgentLaunchPlan(command=command, metadata=metadata)


_PI_EXTENSION_SOURCE = r'''import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { spawn } from "node:child_process";
import { Type } from "typebox";

function runOdysseusTool(args: string[]): Promise<string> {
  return new Promise((resolve, reject) => {
    const child = spawn("odysseus-tool", args, {
      env: process.env,
      stdio: ["ignore", "pipe", "pipe"],
    });
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (chunk) => { stdout += chunk.toString(); });
    child.stderr.on("data", (chunk) => { stderr += chunk.toString(); });
    child.on("error", reject);
    child.on("close", (code) => {
      if (code === 0) resolve(stdout.trim());
      else reject(new Error(stderr.trim() || stdout.trim() || `odysseus-tool exited ${code}`));
    });
  });
}

// --- Agent-state reporting --------------------------------------------------
// Report this run's semantic state for the herdr rollup (working|blocked|idle|
// done|unknown). Fire-and-forget: a failed report must never wedge the agent.
function reportState(state: string): void {
  try {
    runOdysseusTool([
      "call", "agent", "--action", "state", "--json", JSON.stringify({ state }),
    ]).catch(() => {});
  } catch (_err) { /* best-effort */ }
}

// --- Task-slot gating -------------------------------------------------------
// Odysseus caps concurrent LLM calls *per model endpoint*. We hold one slot for
// the WHOLE turn — acquired when the agent starts responding (agent_start), kept
// across every internal LLM call + tool call, and released only when the turn
// finishes (agent_end). The agent can call `odysseus_yield` to release mid-turn
// so other runs can proceed; the next turn re-acquires (blocking if all busy).
// The server also reclaims a run's slots on teardown / lease expiry, so a missed
// release can't strand capacity permanently.
let odysseusHeldSlot: string | null = null;

async function odysseusEnsureSlot(signal?: { aborted?: boolean }): Promise<void> {
  if (odysseusHeldSlot) return;  // already holding this turn's slot
  // Bounded long-poll: the server blocks ~20s then returns "queued"; we re-acquire.
  while (!(signal && signal.aborted)) {
    try {
      const out = await runOdysseusTool([
        "call", "task", "--action", "acquire", "--json", JSON.stringify({ wait_seconds: 20 }),
      ]);
      const parsed = JSON.parse(out);
      const result = (parsed && parsed.result) || parsed || {};
      if (result.granted && result.slot_id) { odysseusHeldSlot = result.slot_id as string; return; }
      // Not granted (queued) → loop and re-acquire.
    } catch (_err) {
      // Don't wedge the agent forever if the bridge is unreachable; brief backoff.
      await new Promise((r) => setTimeout(r, 1000));
    }
  }
}

async function odysseusReleaseSlot(): Promise<void> {
  const slotId = odysseusHeldSlot;
  odysseusHeldSlot = null;
  if (!slotId) return;
  try {
    await runOdysseusTool(["call", "task", "--action", "release", "--json", JSON.stringify({ slot_id: slotId })]);
  } catch (_err) {
    // Best-effort: the server reclaims by lease TTL / run teardown anyway.
  }
}

function odysseusOn(pi: ExtensionAPI, event: string, handler: (event: unknown, ctx: unknown) => unknown): void {
  // Defensive: skip events this Pi build doesn't expose rather than failing to load.
  try { (pi as any).on(event, handler); } catch (_err) { /* unsupported event */ }
}

export default function (pi: ExtensionAPI) {
__ODYSSEUS_PI_PROVIDER_REGISTRATION__
  pi.on("session_start", async (_event, ctx) => {
    ctx.ui.notify("Odysseus provider tools loaded", "info");
    // Surface the Code Station control-plane skill so the agent knows it can drive
    // its own spaces/tabs/panes/agents via the `ody` CLI and report its own state.
    ctx.ui.notify(__ODYSSEUS_ODY_SKILL_SUMMARY__, "info");
  });

  // Hold a slot for the whole turn: acquire when the agent starts responding (and
  // re-acquire at the start of each turn after a yield), release when it finishes.
  // The same events also report semantic agent state for the herdr rollup.
  odysseusOn(pi, "agent_start", async (_event, ctx) => { reportState("working"); await odysseusEnsureSlot(ctx && (ctx as any).signal); });
  odysseusOn(pi, "turn_start", async (_event, ctx) => { reportState("working"); await odysseusEnsureSlot(ctx && (ctx as any).signal); });
  odysseusOn(pi, "agent_end", async () => { await odysseusReleaseSlot(); reportState("idle"); });
  // Surface "blocked" when the agent pauses for a permission / tool approval.
  odysseusOn(pi, "permission_request", async () => { reportState("blocked"); });
  odysseusOn(pi, "tool_approval", async () => { reportState("blocked"); });

  pi.registerTool({
    name: "odysseus_yield",
    label: "Yield Task Slot",
    description: "Release this run's LLM concurrency slot so other coding agents can run. Your next step re-acquires a slot (waiting if all are busy). Use during long autonomous work to let other agents make progress.",
    parameters: Type.Object({}),
    async execute() {
      await odysseusReleaseSlot();
      return { content: [{ type: "text", text: "Released the LLM task slot; will re-acquire on the next step." }], details: {} };
    },
  });

  pi.registerTool({
    name: "odysseus_list_tools",
    label: "Odysseus Tools",
    description: "List Odysseus provider tools available to this run-scoped token.",
    parameters: Type.Object({}),
    async execute() {
      const text = await runOdysseusTool(["list", "--pretty"]);
      return { content: [{ type: "text", text }], details: {} };
    },
  });

  pi.registerTool({
    name: "odysseus_provider",
    label: "Odysseus Provider",
    description: "Call an Odysseus provider tool. Use odysseus_list_tools to discover tool names and actions.",
    parameters: Type.Object({
      tool: Type.String({ description: "Provider tool name, such as coding, terminal, memory, thread_messages, or model_config." }),
      action: Type.String({ description: "Action to invoke on the provider tool." }),
      args: Type.Optional(Type.Record(Type.String(), Type.Unknown(), { description: "JSON object arguments for the provider tool action." })),
    }),
    async execute(_toolCallId, params) {
      const argsJson = JSON.stringify(params.args || {});
      const text = await runOdysseusTool(["call", params.tool, "--action", params.action, "--json", argsJson, "--pretty"]);
      return { content: [{ type: "text", text }], details: {} };
    },
  });
}
'''


# Minimal, untyped, self-contained hook for OMP (oh-my-pi). Deliberately omits the
# Pi-specific provider/tool registration so an API mismatch can't crash the OMP
# session — each registration is also wrapped defensively. Holds one per-endpoint
# task slot for the WHOLE turn (agent_start/turn_start → acquire, agent_end → release).
_OMP_EXTENSION_SOURCE = r'''import { spawn } from "node:child_process";

function runOdysseusTool(args) {
  return new Promise((resolve, reject) => {
    const child = spawn("odysseus-tool", args, { env: process.env, stdio: ["ignore", "pipe", "pipe"] });
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (chunk) => { stdout += chunk.toString(); });
    child.stderr.on("data", (chunk) => { stderr += chunk.toString(); });
    child.on("error", reject);
    child.on("close", (code) => {
      if (code === 0) resolve(stdout.trim());
      else reject(new Error(stderr.trim() || stdout.trim() || `odysseus-tool exited ${code}`));
    });
  });
}

function reportState(state) {
  try {
    runOdysseusTool(["call", "agent", "--action", "state", "--json", JSON.stringify({ state })]).catch(() => {});
  } catch (_err) {}
}

let odysseusHeldSlot = null;

async function odysseusEnsureSlot(signal) {
  if (odysseusHeldSlot) return;
  while (!(signal && signal.aborted)) {
    try {
      const out = await runOdysseusTool(["call", "task", "--action", "acquire", "--json", JSON.stringify({ wait_seconds: 20 })]);
      const parsed = JSON.parse(out);
      const result = (parsed && parsed.result) || parsed || {};
      if (result.granted && result.slot_id) { odysseusHeldSlot = result.slot_id; return; }
    } catch (_err) {
      await new Promise((r) => setTimeout(r, 1000));
    }
  }
}

async function odysseusReleaseSlot() {
  const slotId = odysseusHeldSlot;
  odysseusHeldSlot = null;
  if (!slotId) return;
  try {
    await runOdysseusTool(["call", "task", "--action", "release", "--json", JSON.stringify({ slot_id: slotId })]);
  } catch (_err) {}
}

function odysseusOn(pi, event, handler) {
  try { pi.on(event, handler); } catch (_err) {}
}

export default function (pi) {
  odysseusOn(pi, "session_start", async (_event, ctx) => {
    try { ctx.ui.notify(__ODYSSEUS_ODY_SKILL_SUMMARY__, "info"); } catch (_err) {}
  });
  odysseusOn(pi, "agent_start", async (_event, ctx) => { reportState("working"); await odysseusEnsureSlot(ctx && ctx.signal); });
  odysseusOn(pi, "turn_start", async (_event, ctx) => { reportState("working"); await odysseusEnsureSlot(ctx && ctx.signal); });
  odysseusOn(pi, "agent_end", async () => { await odysseusReleaseSlot(); reportState("idle"); });
  odysseusOn(pi, "permission_request", async () => { reportState("blocked"); });
  odysseusOn(pi, "tool_approval", async () => { reportState("blocked"); });
}
'''


# --- ody control-plane skill -------------------------------------------------
# A concise, accurate guide to the `ody` CLI (the Code Station control plane) that
# is injected per-run so an agent inside a pane can drive its own layout and
# report its own state. Verbs here mirror src/coding_cli.py exactly. The MCP
# server (Codex) and the Pi/OMP extension tools are now thin adapters over the
# same provider bridge — `ody` is the primary agent interface.

_ODY_SKILL_SUMMARY = (
    "Code Station control plane: use the `ody` CLI (on your PATH, talks to a stable "
    "control socket) to see and drive everything outside your own pane — list/read "
    "other agents & panes, spawn subagents, split panes, send input. When the user "
    "says 'my other pane', 'the other agent', or 'what's running', run "
    "`ody agent list --running-only` then `ody agent read <id>` / `ody pane read <run_id>` "
    "— do NOT use tmux/dtach or tail raw.log. Report state with "
    "`ody agent report-state <agent_id> --state working|blocked|idle|done`. "
    "For work tracking this project may have Beads (`bd`) enabled — the repo-scoped, "
    "dependency-aware source of truth for tasks: run `ody bead ready --space <id>` (or "
    "`bd ready`) to find the next unblocked work, `bd create … --discovered-from` to "
    "record work you discover, and close issues as you finish. Project tasks go in "
    "Beads, NOT the memory tool. Full reference in the injected guide."
)

_ODY_SKILL_MD = """\
# Code Station control plane (`ody`)

You are running inside a Code Station terminal *pane*. The `ody` CLI is on your
PATH and is pre-pointed at this workspace's control socket (`$ODYSSEUS_ODY_SOCKET`)
and owner (`$ODYSSEUS_OWNER`). Use it to inspect and drive your own layout
(spaces, tabs, panes) and to start/observe other agents — without touching the
HTTP API. Every command prints a JSON result.

## When to use `ody` (and what NOT to do)
`ody` is how you SEE and DRIVE everything outside your own pane, and it talks to a
**stable control socket** — so it keeps working even if the `odysseus_provider` /
`odysseus-tool` HTTP bridge reports an error. Reach for `ody` whenever the user
mentions another pane/tab/agent, asks what's running, or wants to change the
layout or start more agents.

**Do NOT** read another pane by running `tmux` or `dtach`, by `cat`-ing
`raw.log`, or by poking at `/tmp/odysseus-cs/*` sockets or files under
`…/coding_runs/<id>/`. Those are raw terminal bytes (escape codes, partial
redraws) or internal plumbing and will mislead you. Use `ody … read`, which
returns clean decoded text. If `odysseus_provider` fails, fall back to `ody` —
NOT to tmux/dtach.

Common requests → what to run:
- "look at / check my other pane", "what's the other agent doing", "what's
  running?" → `ody agent list --running-only` (or `ody pane list <tab_id>`) to
  find it, then `ody agent read <agent_id>` / `ody pane read <run_id>`.
- "spawn / start a subagent", "run this in another agent", "kick off a helper" →
  `ody agent start --space <space_id> --harness <id> --command <cmd>`.
- "split the screen", "open a pane beside this" → `ody pane split <pane_id>
  --tab <tab_id> --direction right`.
- "tell the other agent X", "send this to that pane" → `ody agent send
  <agent_id> --text <…>` (or `ody pane send-text <run_id> --text <…>`; submit a
  line with `ody pane send-keys <run_id> enter`).
- "watch / wait on the other agent" → `ody events subscribe --thread <thread_id>`.

IDs cascade: `ody space list` → `ody tab list --space <id>` → `ody pane list
<tab_id>` / `ody agent list --space <id>`; feed the run_id / agent_id you get back
into the read/send/split commands above.

## Concepts
- **space** — a project/workspace (the top-level grouping).
- **tab** — a tab within a space; holds a pane layout tree.
- **pane** — a terminal pane within a tab; each runs one *run*.
- **agent** — a thread (one coding agent); its active run drives a pane.

## Health
- `ody ping` — confirm the control socket is reachable.

## Spaces
- `ody space list [--include-archived]`
- `ody space get <space_id>`
- `ody space rename <space_id> --name <name>`
- `ody space focus <space_id>`

## Tabs
- `ody tab list --space <space_id>`
- `ody tab create --space <space_id> [--label <label>] [--position <n>]`
- `ody tab get <tab_id>`
- `ody tab rename <tab_id> --label <label>`
- `ody tab focus <tab_id>`
- `ody tab close <tab_id>`

## Layout
- `ody layout get <tab_id>`
- `ody layout put <tab_id> --tree '<json>' [--focus-pane-id <id>]`

## Panes
- `ody pane list <tab_id>`
- `ody pane split [<pane_id>] --tab <tab_id> [--direction right|left|up|down]`
- `ody pane close <pane_id> --tab <tab_id>`
- `ody pane rename <pane_id> --tab <tab_id> --title <title>`
- `ody pane send-text <run_id> --text <text>`
- `ody pane send-keys <run_id> <keys...>` (named keys: enter, tab, esc, ctrl-c, up, ...)
- `ody pane read <run_id> [--source visible|recent|recent-unwrapped] [--lines <n>]`
- `ody pane report-agent <thread_id> --state working|blocked|done|idle|unknown [--message <m>] [--run-id <id>]`

## Agents (threads)
- `ody agent list [--space <space_id>] [--running-only]`
- `ody agent get <agent_id>`
- `ody agent start --space <space_id> [--harness <id>] [--command <cmd>] [--cwd <dir>] [--title <t>] [--tab <tab_id>] [--pane <pane_id>]`
- `ody agent send <agent_id> --text <text>`
- `ody agent read <agent_id> [--source visible|recent|recent-unwrapped] [--lines <n>]`
- `ody agent focus <agent_id>`
- `ody agent report-state <agent_id> --state working|blocked|done|idle|unknown [--message <m>] [--run-id <id>]`

## Tracking work (Beads)
This project may have **Beads** (`bd`) enabled — a repo-scoped, dependency-aware
issue tracker that is the **source of truth for work**. It lives in the repo
(`.beads/`, travels with git), so a backlog item survives context compaction and
moves with the branch. Drive it either with the `bd` CLI directly (it is on your
PATH, run it in the repo) or via `ody bead …` (structured, talks to the control
socket — use this to inspect another space's backlog):

- `ody bead ready --space <space_id>` — **unblocked, actionable work. Start here.**
- `ody bead list --space <space_id> [--include-closed]` — the backlog.
- `ody bead status --space <space_id>` — counts (open/ready/blocked/closed).
- `ody bead show <issue_id> --space <space_id>` — one issue + its dependencies.
- `ody bead create --space <space_id> --title "<t>" [--type bug|feature|task] [--priority 0-3] [--discovered-from <issue_id>]`
  — record work. When you find a new problem *while doing something else*, file
  it with `--discovered-from <the issue you were on>` instead of losing it.
- `ody bead update <issue_id> --space <space_id> [--status in_progress|blocked|...] [--priority 0-3] [--title <t>]`
  — change status/priority/title (mark it `in_progress` when you pick it up).
- `ody bead close <issue_id> --space <space_id>` — close as you finish.
- `ody bead dep --space <space_id> --blocked <id> --blocker <id>` — wire a
  dependency (`<blocked>` waits on `<blocker>`); `ody bead undep …` removes one.
- `ody bead graph --space <space_id>` — the dependency DAG.

Workflow: **before starting work, run `ody bead ready`** to pick the next
unblocked item; **record discovered work** as you go (`bd create … --discovered-from`);
**close issues** when done. The equivalent `bd` commands (`bd ready`, `bd create`,
`bd close`, `bd dep`) work too — same store.

## What goes where (do not mix these up)
- **Beads** = *work*: tasks, bugs, the backlog, "what's actionable now". Repo-scoped
  and durable. This is the ONLY place project work items belong.
- **Memory** (`memory` provider tool) = durable facts about the *user* (preferences,
  identity, goals) — cross-project and intentionally forgettable. Do NOT put project
  tasks here: the memory store consolidates/forgets entries, which would silently
  drop a backlog item.
- **RAG / documents** = reference knowledge to read, not work to do.
If the user asks you to "remember to fix X" about *this* project, create a Beads
issue (`bd create`), not a memory entry.

## Events
- `ody events subscribe --thread <thread_id> [--after-seq <n>] [--run-id <id>]`
  Streams JSON event frames (including `agent_state_changed`) until interrupted.

## Reporting your own state
Your harness hooks already report `working`/`idle`/`blocked` automatically, and the
server marks the run `done` on exit. To override or annotate explicitly:

    ody agent report-state <your_agent_id> --state working --message "compiling"

States: `working` (actively running), `blocked` (waiting on input/approval),
`idle` (turn finished, run still alive), `done` (run ended), `unknown`.
"""


def _ody_skill_prompt_args(harness: str) -> list[str]:
    """Launch-command args that put the full ``ody`` guide into the agent's
    *model context*, so the agent actually knows the ``ody`` CLI exists and how
    to drive it.

    This is the delivery the per-harness file drops never achieved: ``run_dir``
    is not the agent's cwd (it is ``RUN_ROOT/<run_id>``; the process runs in the
    project dir), so a ``SKILL.md`` / ``AGENTS.md`` written there falls outside
    every harness's cwd-based context discovery — and the Pi/OMP extension only
    surfaced the summary via ``ctx.ui.notify`` (the human-facing UI, never the
    model). These flags inject directly into the prompt instead.

    Each lever is *additive* (it layers on top of the built-in system prompt
    rather than replacing it), verified against the installed CLIs:

      * pi / omp / claude → ``--append-system-prompt <text>``.
      * codex             → ``-c developer_instructions=<text>``. Coexists with
        the base prompt and any project ``AGENTS.md``; Codex's ``-c`` TOML parser
        falls back to the raw string for the non-TOML markdown body. (``codex
        debug prompt-input`` confirms: AGENTS.md content survives and the body is
        appended, vs. ``model_instructions_file`` which *replaces* base
        instructions.)

    Returns ``[]`` for harnesses with no known system-prompt injection lever
    (e.g. ``generic``), leaving the command untouched.
    """
    harness = (harness or "").strip().lower()
    if harness in ("pi", "omp", "claude"):
        return ["--append-system-prompt", _ODY_SKILL_MD]
    if harness == "codex":
        return ["-c", f"developer_instructions={_ODY_SKILL_MD}"]
    return []


__all__ = [
    "CodingAgentLaunchPlan",
    "ODYSSEUS_MCP_SERVER_NAME",
    "ODYSSEUS_PI_EXTENSION_NAME",
    "ODYSSEUS_OMP_EXTENSION_NAME",
    "ODYSSEUS_PI_PROVIDER_NAME",
    "build_coding_agent_launch_plan",
    "codex_mcp_config_arg",
]
