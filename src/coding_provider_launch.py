"""Provider-tool launch configuration for coding-agent harnesses."""

from __future__ import annotations

import json
import shlex
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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
    )
    path.write_text(source, encoding="utf-8")
    return path


def _write_omp_extension(run_dir: Path) -> Path:
    path = run_dir / ODYSSEUS_OMP_EXTENSION_NAME
    path.write_text(_OMP_EXTENSION_SOURCE, encoding="utf-8")
    return path


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


def _write_claude_hooks_settings(run_dir: Path, *, acquire: Path, release: Path) -> Path:
    """A `claude --settings` file that gates each turn on a task slot.

    UserPromptSubmit → acquire (blocks until a slot is free), Stop/StopFailure →
    release. Passed via --settings so the user's global/project config and auth are
    untouched.
    """
    settings = {
        "hooks": {
            "UserPromptSubmit": [
                {"hooks": [{"type": "command", "command": str(acquire), "timeout": 30}]}
            ],
            "Stop": [
                {"hooks": [{"type": "command", "command": str(release), "timeout": 600}]}
            ],
            "StopFailure": [
                {"hooks": [{"type": "command", "command": str(release), "timeout": 600}]}
            ],
        }
    }
    path = run_dir / "odysseus-claude-hooks.json"
    path.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    return path


def _codex_hook_config_args(acquire: Path, release: Path) -> list[str]:
    """Inline `-c` overrides registering per-turn hooks (no file / no CODEX_HOME).

    Event names use the camelCase keys from Codex's HookEventName enum.
    """
    acquire_cmd = _toml_string(str(acquire))
    release_cmd = _toml_string(str(release))
    return [
        "--config",
        f'hooks.userPromptSubmit=[{{ hooks = [{{ type = "command", command = {acquire_cmd}, timeout = 30 }}] }}]',
        "--config",
        f'hooks.stop=[{{ hooks = [{{ type = "command", command = {release_cmd}, timeout = 600 }}] }}]',
    ]


def build_coding_agent_launch_plan(
    *,
    harness_id: str,
    base_command: str,
    model: str | None,
    endpoint_url: str | None = "",
    run_dir: Path | None = None,
    default_harness_command: bool = True,
) -> CodingAgentLaunchPlan:
    """Return a harness command plus visible launch metadata.

    We only rewrite default Pi/Codex harness commands. A caller-provided shell
    command is treated as an explicit override, but still gets the provider
    bridge environment and `odysseus-tool` on PATH at runtime.
    """
    harness = (harness_id or "").strip().lower()
    configured_model = (model or "").strip()
    endpoint_url = (endpoint_url or "").strip().rstrip("/")
    metadata: dict[str, Any] = {
        "harness_id": harness,
        "base_command": base_command,
        "default_harness_command": bool(default_harness_command),
        "configured_model": configured_model,
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
            metadata["provider_tools"] = {
                "mode": "pi-extension",
                "extension_path": str(extension_path),
                "tools": ["odysseus_provider", "odysseus_list_tools"],
                "command": "odysseus-tool",
                "env": metadata["provider_tools"]["env"],
            }
        command = shlex.join(parts)
    elif harness == "codex":
        parts = ["codex"]
        if configured_model:
            parts.extend(["--model", configured_model])
            metadata["model_explicit"] = True
            metadata["model_arg"] = "--model"
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
            acquire, release = _write_task_hook_scripts(run_dir)
            parts.extend(_codex_hook_config_args(acquire, release))
            metadata["task_hooks"] = {"mode": "codex-config-hooks", "granularity": "per-turn"}
        command = shlex.join(parts)
    elif harness == "claude":
        # Per-turn task-slot gating via `--settings` (leaves global/project config
        # and auth intact): UserPromptSubmit acquires, Stop/StopFailure releases.
        parts = ["claude"]
        if run_dir is not None:
            acquire, release = _write_task_hook_scripts(run_dir)
            hooks_path = _write_claude_hooks_settings(run_dir, acquire=acquire, release=release)
            parts.extend(["--settings", str(hooks_path)])
            metadata["provider_tools"] = {
                "mode": "claude-hooks",
                "settings_path": str(hooks_path),
                "command": "odysseus-tool",
                "env": metadata["provider_tools"]["env"],
                "note": "per-turn task-slot gating (UserPromptSubmit/Stop)",
            }
        command = shlex.join(parts)
    elif harness == "omp":
        # OMP (oh-my-pi) is Pi-API-compatible; inject a minimal hook extension that
        # gates each LLM call on a per-endpoint task slot (before_agent_start /
        # agent_end). `--hook` is OMP's alias for `--extension`.
        parts = ["omp"]
        if run_dir is not None:
            extension_path = _write_omp_extension(run_dir)
            parts.extend(["--hook", str(extension_path)])
            metadata["provider_tools"] = {
                "mode": "omp-hook",
                "hook_path": str(extension_path),
                "command": "odysseus-tool",
                "env": metadata["provider_tools"]["env"],
                "note": "task-slot gating via before_agent_start/agent_end",
            }
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
  });

  // Hold a slot for the whole turn: acquire when the agent starts responding (and
  // re-acquire at the start of each turn after a yield), release when it finishes.
  odysseusOn(pi, "agent_start", async (_event, ctx) => { await odysseusEnsureSlot(ctx && (ctx as any).signal); });
  odysseusOn(pi, "turn_start", async (_event, ctx) => { await odysseusEnsureSlot(ctx && (ctx as any).signal); });
  odysseusOn(pi, "agent_end", async () => { await odysseusReleaseSlot(); });

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
  odysseusOn(pi, "agent_start", async (_event, ctx) => { await odysseusEnsureSlot(ctx && ctx.signal); });
  odysseusOn(pi, "turn_start", async (_event, ctx) => { await odysseusEnsureSlot(ctx && ctx.signal); });
  odysseusOn(pi, "agent_end", async () => { await odysseusReleaseSlot(); });
}
'''


__all__ = [
    "CodingAgentLaunchPlan",
    "ODYSSEUS_MCP_SERVER_NAME",
    "ODYSSEUS_PI_EXTENSION_NAME",
    "ODYSSEUS_OMP_EXTENSION_NAME",
    "ODYSSEUS_PI_PROVIDER_NAME",
    "build_coding_agent_launch_plan",
    "codex_mcp_config_arg",
]
