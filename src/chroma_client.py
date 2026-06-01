"""Shared ChromaDB client.

Uses an HTTP Chroma service when CHROMADB_HOST is configured, otherwise uses an
embedded persistent Chroma store under DATA_DIR. The embedded mode keeps the
desktop app self-contained.
"""

import os
import socket
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_client = None

# A short connect probe so an unreachable ChromaDB fails fast instead of
# blocking on the OS connection timeout (~30-60s, WinError 10060 on Windows),
# which otherwise stalls app startup. Tunable via CHROMADB_CONNECT_TIMEOUT.
_CONNECT_TIMEOUT = float(os.getenv("CHROMADB_CONNECT_TIMEOUT", "2.0"))


def _port_open(host: str, port: int, timeout: float = None) -> bool:
    """Return True if a TCP connection to host:port succeeds within timeout."""
    try:
        with socket.create_connection((host, port), timeout=timeout or _CONNECT_TIMEOUT):
            return True
    except OSError:
        return False


def get_chroma_client():
    """Get or create the singleton ChromaDB client.

    Raises RuntimeError with a clear install hint if the `chromadb` package
    is not installed — it's an optional dependency (RAG + memory vectors).
    """
    global _client
    if _client is not None:
        return _client

    try:
        import chromadb
    except ImportError as e:
        raise RuntimeError(
            "ChromaDB integration is not installed. Install the optional "
            "dependency with: pip install chromadb"
        ) from e

    host = os.getenv("CHROMADB_HOST")
    if host:
        port = int(os.getenv("CHROMADB_PORT", "8100"))
        # Fast-fail preflight (issue #326): an unreachable HTTP ChromaDB otherwise
        # blocks app startup on the OS connection timeout (~30-60s).
        if not _port_open(host, port):
            raise RuntimeError(
                f"ChromaDB is not reachable at {host}:{port}. Start the ChromaDB "
                f"service (e.g. `docker compose up chromadb`) or set CHROMADB_HOST / "
                f"CHROMADB_PORT to point at a running instance."
            )
        # Health check before caching — if the port is open but the service isn't
        # healthy yet, don't poison the singleton with a dead client.
        client = chromadb.HttpClient(host=host, port=port)
        client.heartbeat()
        _client = client
        logger.info(f"ChromaDB HTTP connected: {host}:{port}")
        return _client

    # No CHROMADB_HOST → embedded persistent store under DATA_DIR, which keeps the
    # desktop app self-contained (no separate ChromaDB service required).
    persist_path = os.getenv("CHROMADB_PERSIST_PATH")
    if not persist_path:
        data_dir = Path(os.getenv("DATA_DIR", "data"))
        persist_path = str(data_dir / "chroma")

    Path(persist_path).expanduser().mkdir(parents=True, exist_ok=True)
    _client = chromadb.PersistentClient(path=persist_path)
    _client.heartbeat()
    logger.info(f"ChromaDB embedded persistent store ready: {persist_path}")
    return _client


def reset_client():
    """Reset the singleton (e.g. after config change)."""
    global _client
    _client = None
