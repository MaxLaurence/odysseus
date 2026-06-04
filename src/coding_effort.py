"""Normalized reasoning-effort levels and per-harness translation for Code Station.

Odysseus exposes ONE effort scale to the user (``minimal|low|medium|high|max``). Each
coding-agent CLI expresses reasoning differently, so this module is the SINGLE place
that maps the normalized level onto the harness-specific knob:

  - Codex  : ``-c model_reasoning_effort=<minimal|low|medium|high>`` launch arg.
  - Claude : ``MAX_THINKING_TOKENS`` env (+ ``CLAUDE_CODE_DISABLE_ADAPTIVE_THINKING=1``
             so the fixed budget actually applies — adaptive-reasoning models ignore
             ``MAX_THINKING_TOKENS`` unless adaptive thinking is disabled).
  - Pi/OMP/others: no confirmed flag yet -> no-op (the chosen level is still recorded
             in launch metadata as ``effort_applied: false`` for UI visibility).

Centralising the mapping means a wrong guess about a CLI flag/env name is a one-line
fix here rather than spread across the launch path. Re-verify the Claude env names and
the Codex effort values against the installed CLI build before relying on them.
"""

from __future__ import annotations

# Ordered low -> high. "" / None means "inherit" (omit any effort knob; let the
# harness use its own default / adaptive reasoning).
EFFORT_LEVELS: tuple[str, ...] = ("minimal", "low", "medium", "high", "max")

# Harnesses for which an effort level is actually wired to a CLI knob today.
EFFORT_SUPPORTED_HARNESSES: frozenset[str] = frozenset({"codex", "claude"})


def normalize_effort(value: str | None) -> str:
    """Return a valid effort level lowercased, or "" for unset/unknown values."""
    candidate = (value or "").strip().lower()
    return candidate if candidate in EFFORT_LEVELS else ""


def effort_supported(harness_id: str | None) -> bool:
    """Whether the harness has a real effort knob wired up (vs. best-effort no-op)."""
    return (harness_id or "").strip().lower() in EFFORT_SUPPORTED_HARNESSES


# --- Codex ------------------------------------------------------------------
# Codex reasoning effort accepts minimal|low|medium|high. We map the normalized
# "max" down to "high" (Codex has no higher tier).
_CODEX_EFFORT: dict[str, str] = {
    "minimal": "minimal",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "max": "high",
}


def codex_effort_value(effort: str | None) -> str:
    return _CODEX_EFFORT.get(normalize_effort(effort), "")


def codex_effort_config_args(effort: str | None) -> list[str]:
    """``["-c", "model_reasoning_effort=<v>"]`` for Codex, or [] when unset."""
    value = codex_effort_value(effort)
    return ["-c", f"model_reasoning_effort={value}"] if value else []


# --- Claude -----------------------------------------------------------------
# Fixed extended-thinking token budget per level. We pair this with
# CLAUDE_CODE_DISABLE_ADAPTIVE_THINKING=1 so the budget is honoured on adaptive
# models (which otherwise ignore MAX_THINKING_TOKENS).
_CLAUDE_THINKING_TOKENS: dict[str, int] = {
    "minimal": 2048,
    "low": 4096,
    "medium": 10000,
    "high": 24000,
    "max": 32000,
}


def claude_effort_env(effort: str | None) -> dict[str, str]:
    """Env vars that apply the chosen effort to the Claude CLI, or {} when unset."""
    level = normalize_effort(effort)
    budget = _CLAUDE_THINKING_TOKENS.get(level)
    if not budget:
        return {}
    return {
        "MAX_THINKING_TOKENS": str(budget),
        "CLAUDE_CODE_DISABLE_ADAPTIVE_THINKING": "1",
    }
