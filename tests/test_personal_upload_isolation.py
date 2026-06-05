import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from routes import personal_routes


def _route_endpoint(router, path: str, method: str):
    method = method.upper()
    for route in router.routes:
        if route.path == path and method in getattr(route, "methods", set()):
            return route.endpoint
    raise AssertionError(f"route not found: {method} {path}")


def test_personal_upload_paths_are_owner_scoped_and_unique(tmp_path, monkeypatch):
    monkeypatch.setattr(personal_routes, "UPLOADS_DIR", str(tmp_path))

    alice_dir = personal_routes._personal_upload_dir_for_owner("alice")
    bob_dir = personal_routes._personal_upload_dir_for_owner("bob")

    assert Path(alice_dir).parent == tmp_path
    assert Path(bob_dir).parent == tmp_path
    assert alice_dir != bob_dir

    first_path, first_stored, first_display = personal_routes._unique_personal_upload_path(
        alice_dir,
        "notes.txt",
    )
    second_path, second_stored, second_display = personal_routes._unique_personal_upload_path(
        alice_dir,
        "notes.txt",
    )

    assert first_display == second_display == "notes.txt"
    assert first_stored != second_stored
    assert first_path != second_path
    assert Path(first_path).parent == Path(alice_dir)
    assert Path(second_path).parent == Path(alice_dir)


def test_personal_upload_paths_do_not_collide_after_sanitization(tmp_path, monkeypatch):
    monkeypatch.setattr(personal_routes, "UPLOADS_DIR", str(tmp_path))

    plus_owner = personal_routes._personal_upload_dir_for_owner("alice+prod@example.com")
    plain_owner = personal_routes._personal_upload_dir_for_owner("aliceprod@example.com")

    assert Path(plus_owner).parent == tmp_path
    assert Path(plain_owner).parent == tmp_path
    assert plus_owner != plain_owner


def test_personal_upload_paths_stay_under_upload_root(tmp_path, monkeypatch):
    monkeypatch.setattr(personal_routes, "UPLOADS_DIR", str(tmp_path))

    upload_dir = personal_routes._personal_upload_dir_for_owner("../alice")
    file_path, stored_name, display_name = personal_routes._unique_personal_upload_path(
        upload_dir,
        "../../.env",
    )

    assert os.path.commonpath([file_path, upload_dir]) == upload_dir
    assert Path(file_path).name == stored_name
    assert display_name == "env"


@pytest.mark.asyncio
async def test_personal_rag_delete_rejects_cross_owner_upload(tmp_path, monkeypatch):
    monkeypatch.setattr(personal_routes, "UPLOADS_DIR", str(tmp_path / "uploads"))
    bob_dir = personal_routes._personal_upload_dir_for_owner("bob")
    bob_file = Path(bob_dir) / "secret.txt"
    bob_file.write_text("bob private note", encoding="utf-8")

    class FakeManager:
        index = []

        def exclude_file(self, filepath):
            raise AssertionError(f"cross-owner delete reached manager for {filepath}")

    class FakeRag:
        def delete_by_source(self, filepath, owner=None):
            raise AssertionError(f"cross-owner delete reached rag for {filepath}/{owner}")

    monkeypatch.setattr(personal_routes, "get_rag_manager", lambda: FakeRag())
    endpoint = _route_endpoint(
        personal_routes.setup_personal_routes(FakeManager(), None, True),
        "/api/personal/file",
        "DELETE",
    )

    with pytest.raises(HTTPException) as exc:
        await endpoint(filepath=str(bob_file), owner="alice", _admin=None)

    assert exc.value.status_code == 403
    assert bob_file.exists()


@pytest.mark.asyncio
async def test_personal_rag_delete_owner_scopes_vector_removal(tmp_path, monkeypatch):
    monkeypatch.setattr(personal_routes, "UPLOADS_DIR", str(tmp_path / "uploads"))
    alice_dir = personal_routes._personal_upload_dir_for_owner("alice")
    alice_file = Path(alice_dir) / "note.txt"
    alice_file.write_text("alice note", encoding="utf-8")
    calls = []

    class FakeManager:
        index = []

        def exclude_file(self, filepath):
            calls.append(("exclude", filepath))

    class FakeRag:
        def delete_by_source(self, filepath, owner=None):
            calls.append(("rag", filepath, owner))
            return 2

    monkeypatch.setattr(personal_routes, "get_rag_manager", lambda: FakeRag())
    endpoint = _route_endpoint(
        personal_routes.setup_personal_routes(FakeManager(), None, True),
        "/api/personal/file",
        "DELETE",
    )

    result = await endpoint(filepath=str(alice_file), owner="alice", _admin=None)

    assert result["removed_chunks"] == 2
    assert result["deleted_from_disk"] is True
    assert not alice_file.exists()
    assert ("rag", str(alice_file), "alice") in calls
    assert ("exclude", str(alice_file)) in calls


@pytest.mark.asyncio
async def test_personal_remove_directory_rejects_outside_roots(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()

    class FakeManager:
        index = []

        def remove_directory(self, directory, owner=None):
            raise AssertionError(f"outside directory reached manager: {directory}/{owner}")

    endpoint = _route_endpoint(
        personal_routes.setup_personal_routes(FakeManager(), None, True),
        "/api/personal/remove_directory",
        "DELETE",
    )

    with pytest.raises(HTTPException) as exc:
        await endpoint(directory=str(outside), owner="alice", _admin=None)

    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_personal_upload_requires_authenticated_or_local_user(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")

    class Req:
        state = SimpleNamespace(current_user=None)
        client = SimpleNamespace(host="203.0.113.7")
        app = SimpleNamespace(state=SimpleNamespace(auth_manager=SimpleNamespace(is_configured=True)))

    class FakeManager:
        index = []

    endpoint = _route_endpoint(
        personal_routes.setup_personal_routes(FakeManager(), None, True),
        "/api/personal/upload",
        "POST",
    )

    with pytest.raises(HTTPException) as exc:
        await endpoint(Req(), files=[])

    assert exc.value.status_code == 401
