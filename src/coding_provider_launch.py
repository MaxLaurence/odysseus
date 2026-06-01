"""Provider-tool launch configuration for coding-agent harnesses."""

from __future__ import annotations

import json
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.coding_provider_bridge import scripts_dir


ODYSSEUS_MCP_SERVER_NAME = "odysseus"
ODYSSEUS_PI_EXTENSION_NAME = "odysseus-pi-extension.ts"
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

export default function (pi: ExtensionAPI) {
__ODYSSEUS_PI_PROVIDER_REGISTRATION__
  pi.on("session_start", async (_event, ctx) => {
    ctx.ui.notify("Odysseus provider tools loaded", "info");
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


__all__ = [
    "CodingAgentLaunchPlan",
    "ODYSSEUS_MCP_SERVER_NAME",
    "ODYSSEUS_PI_EXTENSION_NAME",
    "ODYSSEUS_PI_PROVIDER_NAME",
    "build_coding_agent_launch_plan",
    "codex_mcp_config_arg",
]
