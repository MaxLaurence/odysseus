"""Regression tests for the ChromaDB singleton client.

Covers the embedded-vs-HTTP selection (CHROMADB_HOST chooses an HTTP service,
otherwise an embedded PersistentClient under DATA_DIR keeps the desktop app
self-contained) and the fast-fail preflight (issue #326) so an unreachable HTTP
ChromaDB fails fast instead of blocking startup on the OS connection timeout —
and must not poison the cached singleton.
"""
import importlib
import socket
import sys
import time

import pytest

import src.chroma_client as cc


def _free_port() -> int:
    """Bind to port 0, grab the assigned port, release it — nothing listens."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_port_open_false_for_closed_port_and_is_fast():
    port = _free_port()
    t0 = time.monotonic()
    assert cc._port_open("127.0.0.1", port, timeout=1.0) is False
    # The whole point: we fail fast, nowhere near the 30-60s OS timeout.
    assert time.monotonic() - t0 < 5.0


def test_port_open_true_for_listening_socket():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    host, port = srv.getsockname()
    try:
        assert cc._port_open(host, port, timeout=1.0) is True
    finally:
        srv.close()


class _FakeClient:
    def heartbeat(self):
        return 1


class _FakeChroma:
    def __init__(self):
        self.http_calls = []
        self.persistent_calls = []

    def HttpClient(self, *, host, port):
        self.http_calls.append((host, port))
        return _FakeClient()

    def PersistentClient(self, *, path):
        self.persistent_calls.append(path)
        return _FakeClient()


def _load_client(monkeypatch, fake_chroma):
    monkeypatch.setitem(sys.modules, "chromadb", fake_chroma)
    import src.chroma_client as chroma_client

    return importlib.reload(chroma_client)


def test_chroma_defaults_to_embedded_persistent_store(monkeypatch, tmp_path):
    fake_chroma = _FakeChroma()
    monkeypatch.delenv("CHROMADB_HOST", raising=False)
    monkeypatch.delenv("CHROMADB_PERSIST_PATH", raising=False)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    chroma_client = _load_client(monkeypatch, fake_chroma)

    chroma_client.get_chroma_client()

    assert fake_chroma.http_calls == []
    assert fake_chroma.persistent_calls == [str(tmp_path / "data" / "chroma")]


def test_chroma_uses_http_client_when_host_configured(monkeypatch, tmp_path):
    fake_chroma = _FakeChroma()
    monkeypatch.setenv("CHROMADB_HOST", "chromadb")
    monkeypatch.setenv("CHROMADB_PORT", "8000")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    chroma_client = _load_client(monkeypatch, fake_chroma)
    # Preflight passes so the HTTP path is exercised (the socket probe itself is
    # covered separately by the _port_open tests above).
    monkeypatch.setattr(chroma_client, "_port_open", lambda *a, **k: True)

    chroma_client.get_chroma_client()

    assert fake_chroma.http_calls == [("chromadb", 8000)]
    assert fake_chroma.persistent_calls == []


def test_chroma_http_unreachable_fails_fast_and_does_not_cache(monkeypatch, tmp_path):
    fake_chroma = _FakeChroma()
    monkeypatch.setenv("CHROMADB_HOST", "chromadb")
    monkeypatch.setenv("CHROMADB_PORT", "8000")
    chroma_client = _load_client(monkeypatch, fake_chroma)
    monkeypatch.setattr(chroma_client, "_port_open", lambda *a, **k: False)

    with pytest.raises(RuntimeError):
        chroma_client.get_chroma_client()
    # A failed connection must not poison the cached singleton.
    assert chroma_client._client is None
    assert fake_chroma.http_calls == []
