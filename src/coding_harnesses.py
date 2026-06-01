"""Harness registry for the Odysseus coding station.

The runtime treats every harness as a command launcher. Tool-specific agent
registries can build richer commands later, but this layer deliberately stays
neutral so queued terminal runs work before those tools exist.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class HarnessDefinition:
    id: str
    name: str
    description: str
    default_command: str
    stdin_supported: bool = True
    resize_supported: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "default_command": self.default_command,
            "stdin_supported": self.stdin_supported,
            "resize_supported": self.resize_supported,
        }


_HARNESS_REGISTRY: dict[str, HarnessDefinition] = {
    "generic": HarnessDefinition(
        id="generic",
        name="Generic Shell",
        description="Run any shell command or open a login shell.",
        default_command="${SHELL:-/bin/bash} -l",
    ),
    "pi": HarnessDefinition(
        id="pi",
        name="Pi",
        description="Run the pi CLI in a coding terminal.",
        default_command="pi",
    ),
    "codex": HarnessDefinition(
        id="codex",
        name="Codex",
        description="Run the Codex CLI in a coding terminal.",
        default_command="codex",
    ),
    "claude": HarnessDefinition(
        id="claude",
        name="Claude",
        description="Run the Claude CLI in a coding terminal.",
        default_command="claude",
    ),
    "opencode": HarnessDefinition(
        id="opencode",
        name="OpenCode",
        description="Run the OpenCode CLI in a coding terminal.",
        default_command="opencode",
    ),
    "omp": HarnessDefinition(
        id="omp",
        name="OMP",
        description="Run the OMP CLI in a coding terminal.",
        default_command="omp",
    ),
    "hermes": HarnessDefinition(
        id="hermes",
        name="Hermes",
        description="Run the Hermes CLI in a coding terminal.",
        default_command="hermes",
    ),
    "custom": HarnessDefinition(
        id="custom",
        name="Custom",
        description="Run a caller-provided command.",
        default_command="",
    ),
}


def list_harnesses() -> list[dict[str, Any]]:
    """Return harnesses in a stable UI-friendly order."""
    return [definition.to_dict() for definition in _HARNESS_REGISTRY.values()]


def get_harness(harness_id: str | None) -> HarnessDefinition:
    """Resolve a harness id, falling back to generic for blank values."""
    normalized = (harness_id or "generic").strip().lower() or "generic"
    try:
        return _HARNESS_REGISTRY[normalized]
    except KeyError as exc:
        raise ValueError(f"Unknown coding harness: {harness_id}") from exc


def build_harness_command(
    harness_id: str | None,
    command: str | None,
    metadata: dict[str, Any] | None = None,
) -> str:
    """Build the shell command for a run.

    `command` always wins. For custom harnesses, callers may also provide
    `metadata.command`; other harnesses fall back to their registered CLI.
    """
    definition = get_harness(harness_id)
    explicit = (command or "").strip()
    if explicit:
        return explicit

    meta_command = ""
    if isinstance(metadata, dict):
        meta_command = str(metadata.get("command") or "").strip()
    if meta_command:
        return meta_command

    if definition.default_command:
        return definition.default_command
    raise ValueError("Custom coding harness requires a command")
