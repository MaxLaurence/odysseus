"""Environment policy for untrusted or semi-trusted child processes."""

from __future__ import annotations

import os
from collections.abc import Mapping

SAFE_PARENT_ENV_KEYS = {
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "SHELL",
    "TERM",
    "TMPDIR",
    "TMP",
    "TEMP",
    "LANG",
    "PYTHONIOENCODING",
    "PYTHONUNBUFFERED",
}
SAFE_PARENT_ENV_PREFIXES = ("LC_",)


def safe_parent_env(source: Mapping[str, str] | None = None) -> dict[str, str]:
    """Copy only non-secret usability env from a backend process."""
    source = source if source is not None else os.environ
    return {
        key: value
        for key, value in source.items()
        if key in SAFE_PARENT_ENV_KEYS or any(key.startswith(prefix) for prefix in SAFE_PARENT_ENV_PREFIXES)
    }


def safe_child_env(
    explicit: Mapping[str, str] | None = None,
    *,
    source: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Safe parent env plus explicit child-specific variables."""
    env = safe_parent_env(source)
    for key, value in (explicit or {}).items():
        if value is not None:
            env[str(key)] = str(value)
    return env
