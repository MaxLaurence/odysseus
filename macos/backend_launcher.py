"""PyInstaller entry point for the bundled Odysseus backend."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import uvicorn


def _bundle_root() -> Path:
    frozen_root = getattr(sys, "_MEIPASS", None)
    if frozen_root:
        return Path(frozen_root)
    return Path(__file__).resolve().parent.parent


def main() -> None:
    root = _bundle_root()
    os.environ.setdefault("ODYSSEUS_BASE_DIR", str(root))
    os.environ.setdefault("DATA_DIR", str(Path.home() / "Library/Application Support/Odysseus/data"))
    os.environ.setdefault("CHROMADB_PERSIST_PATH", str(Path(os.environ["DATA_DIR"]) / "chroma"))
    os.environ.setdefault("ODYSSEUS_DISABLE_MCP", "1")
    data_dir = Path(os.environ["DATA_DIR"]).expanduser().resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    app_support_root = data_dir.parent
    app_support_root.mkdir(parents=True, exist_ok=True)

    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

    os.chdir(app_support_root)

    host = os.environ.get("ODYSSEUS_HOST", "127.0.0.1")
    port = int(os.environ.get("ODYSSEUS_PORT", "7001"))

    from app import app

    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
