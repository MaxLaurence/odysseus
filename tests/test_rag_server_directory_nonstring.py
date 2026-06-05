"""Regression: rag_server add/remove_directory must not crash on a non-string path.

`directory = arguments.get("directory", "").strip()` runs before the surrounding
try, so a non-string `directory` in the tool args (e.g. a number) raised
AttributeError out of call_tool. Coerce non-strings to "".
"""
import asyncio

import pytest

pytest.importorskip("mcp")

import mcp_servers.rag_server as rs


def _call(monkeypatch, action, directory, **extra):
    monkeypatch.setattr(rs, "_ensure_init", lambda: None)
    return asyncio.run(rs.call_tool("manage_rag", {"action": action, "directory": directory, **extra}))


def test_add_directory_non_string_does_not_crash(monkeypatch):
    out = _call(monkeypatch, "add_directory", 123)
    assert "needs a directory path" in out[0].text


def test_remove_directory_non_string_does_not_crash(monkeypatch):
    out = _call(monkeypatch, "remove_directory", ["x"])
    assert "needs a directory path" in out[0].text


def test_add_directory_rejects_path_outside_personal_root(monkeypatch, tmp_path):
    personal = tmp_path / "personal"
    outside = tmp_path / "outside"
    personal.mkdir()
    outside.mkdir()
    monkeypatch.setattr(rs, "PERSONAL_DIR", str(personal))

    class FakeRag:
        def index_personal_documents(self, directory, owner=None):
            raise AssertionError(f"outside path reached rag: {directory}/{owner}")

    rs._rag_manager = FakeRag()
    rs._personal_docs_manager = object()

    out = _call(monkeypatch, "add_directory", str(outside), _odysseus_owner="alice")

    assert "inside personal documents" in out[0].text


def test_add_directory_passes_hidden_owner(monkeypatch, tmp_path):
    personal = tmp_path / "personal"
    docs = personal / "docs"
    docs.mkdir(parents=True)
    monkeypatch.setattr(rs, "PERSONAL_DIR", str(personal))
    calls = []

    class FakeRag:
        def index_personal_documents(self, directory, owner=None):
            calls.append(("rag", directory, owner))
            return {"indexed_count": 3}

    class FakeDocs:
        def add_directory(self, directory, *, index=True, owner=None):
            calls.append(("docs", directory, index, owner))

    rs._rag_manager = FakeRag()
    rs._personal_docs_manager = FakeDocs()

    out = _call(monkeypatch, "add_directory", str(docs), _odysseus_owner="alice")

    assert "3 chunks" in out[0].text
    assert ("rag", str(docs), "alice") in calls
    assert ("docs", str(docs), False, "alice") in calls


def test_add_directory_fails_closed_when_owner_unaware(monkeypatch, tmp_path):
    personal = tmp_path / "personal"
    docs = personal / "docs"
    docs.mkdir(parents=True)
    monkeypatch.setattr(rs, "PERSONAL_DIR", str(personal))

    class FakeRag:
        def index_personal_documents(self, directory):
            raise AssertionError(f"owner-unaware rag method should not be called for {directory}")

    rs._rag_manager = FakeRag()
    rs._personal_docs_manager = object()

    out = _call(monkeypatch, "add_directory", str(docs), _odysseus_owner="alice")

    assert "owner-scoped RAG method does not accept owner" in out[0].text
