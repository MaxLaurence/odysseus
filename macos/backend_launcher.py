"""PyInstaller entry point for the bundled Odysseus backend."""

from __future__ import annotations

import os
import sys
from pathlib import Path

ODYSSEUS_TOOL_DISPATCH_ARG = "--odysseus-tool"
# Kept in sync with src.builtin_mcp.python_mcp_launch. In the frozen bundle
# sys.executable is this app binary, not a Python interpreter, so the built-in
# stdio MCP servers are launched as `odysseus_backend --odysseus-mcp <script>`
# and this entry point runpy-executes the script in place.
ODYSSEUS_MCP_DISPATCH_ARG = "--odysseus-mcp"


def _bundle_root() -> Path:
    frozen_root = getattr(sys, "_MEIPASS", None)
    if frozen_root:
        return Path(frozen_root)
    return Path(__file__).resolve().parent.parent


def _provider_cli_args(argv: list[str]) -> list[str] | None:
    if len(argv) >= 2 and argv[1] == ODYSSEUS_TOOL_DISPATCH_ARG:
        return argv[2:]
    if len(argv) >= 3 and Path(argv[1]).name == "odysseus-tool":
        return argv[2:]
    return None


def _mcp_server_script(argv: list[str]) -> str | None:
    """When launched as `odysseus_backend --odysseus-mcp <script.py>`, return the
    script path so it can be run in-process instead of booting the web app."""
    if len(argv) >= 3 and argv[1] == ODYSSEUS_MCP_DISPATCH_ARG:
        return argv[2]
    return None


def _configure_runtime(root: Path) -> Path:
    os.environ.setdefault("ODYSSEUS_BASE_DIR", str(root))
    os.environ.setdefault("DATA_DIR", str(Path.home() / "Library/Application Support/Odysseus/data"))
    os.environ.setdefault("CHROMADB_PERSIST_PATH", str(Path(os.environ["DATA_DIR"]) / "chroma"))
    data_dir = Path(os.environ["DATA_DIR"]).expanduser().resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    app_support_root = data_dir.parent
    app_support_root.mkdir(parents=True, exist_ok=True)

    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

    return app_support_root


def _run_provider_cli(args: list[str]) -> None:
    from src.coding_provider_cli import main as provider_cli_main

    sys.argv = ["odysseus-tool", *args]
    raise SystemExit(provider_cli_main(args))


def _run_mcp_server(script_path: str, app_support_root: Path) -> None:
    """Run a bundled stdio MCP server script in this (frozen) interpreter.

    The script's `if __name__ == "__main__"` block serves MCP over stdio and
    blocks until the parent closes the pipe; we exit cleanly when it returns.
    """
    import runpy

    sys.argv = [script_path]
    # image_gen and similar write to CWD-relative paths; match the main backend.
    os.chdir(app_support_root)
    runpy.run_path(script_path, run_name="__main__")
    raise SystemExit(0)


def main() -> None:
    root = _bundle_root()
    app_support_root = _configure_runtime(root)
    provider_args = _provider_cli_args(sys.argv)
    if provider_args is not None:
        _run_provider_cli(provider_args)

    mcp_script = _mcp_server_script(sys.argv)
    if mcp_script is not None:
        _run_mcp_server(mcp_script, app_support_root)

    os.chdir(app_support_root)

    host = os.environ.get("ODYSSEUS_HOST", "127.0.0.1")
    port = int(os.environ.get("ODYSSEUS_PORT", "7001"))

    import uvicorn

    from app import app

    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
