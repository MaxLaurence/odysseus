"""Backup import must dedup memories against the importing user only.

import_data deduped incoming memories against memory_manager.load_all()
(every tenant\'s rows), so a memory whose text matched ANY other user\'s
memory was silently skipped - the importing user lost their own data. The
dedup must be scoped to the caller\'s own memories. The full multi-tenant
store is still saved back.
"""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import routes.backup_routes as br


class _Req:
    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


def _setup(monkeypatch, store, user="alice"):
    monkeypatch.setattr(br, "require_admin", lambda request: None)
    monkeypatch.setattr(br, "get_current_user", lambda request: user)

    mem = MagicMock()
    mem.load_all.return_value = list(store)
    saved = {}
    mem.save.side_effect = lambda entries: saved.__setitem__("entries", entries)

    skills = MagicMock()
    skills.load_all.return_value = []
    router = br.setup_backup_routes(mem, MagicMock(), skills)
    endpoint = None
    for r in router.routes:
        if r.path == "/api/import" and "POST" in getattr(r, "methods", set()):
            endpoint = r.endpoint
    assert endpoint is not None
    return endpoint, saved


def test_user_can_import_memory_matching_another_users_text(monkeypatch):
    # bob already has "buy milk"; alice imports her own "Buy Milk".
    endpoint, saved = _setup(monkeypatch, [{"text": "buy milk", "owner": "bob"}])
    body = {"memories": [{"text": "Buy Milk"}]}
    asyncio.run(endpoint(_Req(body)))
    texts_by_owner = {(e.get("owner"), e.get("text")) for e in saved["entries"]}
    assert ("alice", "Buy Milk") in texts_by_owner  # not dropped as a "duplicate"
    assert ("bob", "buy milk") in texts_by_owner     # other tenant preserved


def test_users_own_duplicate_is_still_skipped(monkeypatch):
    endpoint, saved = _setup(monkeypatch, [{"text": "buy milk", "owner": "alice"}])
    body = {"memories": [{"text": "Buy Milk"}]}
    asyncio.run(endpoint(_Req(body)))
    alice_milk = [e for e in saved["entries"]
                  if e.get("owner") == "alice" and e.get("text", "").lower() == "buy milk"]
    assert len(alice_milk) == 1  # the real duplicate is still deduped


def test_imported_memory_owner_is_overwritten_to_importing_user(monkeypatch):
    endpoint, saved = _setup(monkeypatch, [])
    body = {"memories": [{"text": "owned elsewhere", "owner": "bob"}]}

    asyncio.run(endpoint(_Req(body)))

    assert saved["entries"] == [{"text": "owned elsewhere", "owner": "alice"}]


def test_imported_skill_owner_is_overwritten_to_importing_user(monkeypatch):
    monkeypatch.setattr(br, "require_admin", lambda request: None)
    monkeypatch.setattr(br, "get_current_user", lambda request: "alice")

    mem = MagicMock()
    mem.load_all.return_value = []
    skills = MagicMock()
    skills.load_all.return_value = []
    saved = {}
    skills.save.side_effect = lambda entries: saved.__setitem__("entries", entries)

    router = br.setup_backup_routes(mem, MagicMock(), skills)
    endpoint = next(
        r.endpoint
        for r in router.routes
        if r.path == "/api/import" and "POST" in getattr(r, "methods", set())
    )

    asyncio.run(endpoint(_Req({"skills": [{"id": "s1", "title": "Skill", "owner": "bob"}]})))

    assert saved["entries"] == [{"id": "s1", "title": "Skill", "owner": "alice"}]


def test_export_omits_secret_settings_and_preferences(monkeypatch):
    monkeypatch.setattr(br, "require_admin", lambda request: None)
    monkeypatch.setattr(br, "get_current_user", lambda request: "alice")
    monkeypatch.setattr(
        br,
        "load_settings",
        lambda: {
            "brave_api_key": "brave-secret",
            "carddav_password": "carddav-secret",
            "agent_input_token_budget": 1234,
            "search_provider": "brave",
            "nested": {"hfToken": "hf-secret", "visible": "ok"},
        },
    )
    monkeypatch.setattr(br, "load_features", lambda: {})

    import routes.prefs_routes as prefs_routes
    monkeypatch.setattr(
        prefs_routes,
        "_load_for_user",
        lambda user: {"caldav": {"password": "pref-secret", "url": "https://cal.example"}},
    )

    mem = MagicMock()
    mem.load.return_value = []
    presets = MagicMock()
    presets.get_all.return_value = {}
    skills = MagicMock()
    skills.load.return_value = []

    router = br.setup_backup_routes(mem, presets, skills)
    endpoint = next(
        r.endpoint
        for r in router.routes
        if r.path == "/api/export" and "GET" in getattr(r, "methods", set())
    )

    response = asyncio.run(endpoint(SimpleNamespace()))
    payload = json.loads(response.body.decode("utf-8"))

    assert payload["settings"]["search_provider"] == "brave"
    assert payload["settings"]["agent_input_token_budget"] == 1234
    assert "brave_api_key" not in payload["settings"]
    assert "carddav_password" not in payload["settings"]
    assert "hfToken" not in payload["settings"]["nested"]
    assert payload["settings"]["nested"]["visible"] == "ok"
    assert "password" not in payload["preferences"]["caldav"]
    assert payload["preferences"]["caldav"]["url"] == "https://cal.example"
