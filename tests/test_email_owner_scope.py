import sqlite3
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from types import SimpleNamespace

import pytest


def _route_endpoint(router, path: str, method: str):
    method = method.upper()
    for route in router.routes:
        if route.path == path and method in getattr(route, "methods", set()):
            return route.endpoint
    raise AssertionError(f"route not found: {method} {path}")


def _create_mcp_email_accounts_db(path):
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE email_accounts (
            id TEXT PRIMARY KEY,
            owner TEXT,
            name TEXT NOT NULL,
            is_default INTEGER NOT NULL DEFAULT 0,
            enabled INTEGER NOT NULL DEFAULT 1,
            imap_host TEXT,
            imap_port INTEGER,
            imap_user TEXT,
            imap_password TEXT,
            imap_starttls INTEGER,
            smtp_host TEXT,
            smtp_port INTEGER,
            smtp_security TEXT,
            smtp_user TEXT,
            smtp_password TEXT,
            from_address TEXT,
            created_at TEXT
        )
        """
    )
    return conn


def test_email_tag_clause_excludes_legacy_owner_rows_for_authenticated_owner(monkeypatch):
    import routes.email_routes as email_routes

    monkeypatch.setattr(
        email_routes,
        "_email_tag_owner_aliases",
        lambda account_id, owner="": ["alice", "alice@example.com"],
    )

    clause, params = email_routes._email_tag_owner_clause("acct-alice", "alice")

    assert clause == "owner IN (?,?)"
    assert params == ["alice", "alice@example.com"]
    assert "owner IS NULL" not in clause


def test_email_tag_clause_keeps_legacy_rows_for_single_user_mode(monkeypatch):
    import routes.email_routes as email_routes

    monkeypatch.setattr(
        email_routes,
        "_email_tag_owner_aliases",
        lambda account_id, owner="": [""],
    )

    clause, params = email_routes._email_tag_owner_clause(None, "")

    assert clause == "(owner IN (?) OR owner IS NULL)"
    assert params == [""]


def test_assert_owns_account_rejects_unmatched_legacy_account(monkeypatch):
    import core.database as database
    import routes.email_helpers as email_helpers
    from fastapi import HTTPException

    class FakeQuery:
        def __init__(self, row):
            self.row = row

        def filter(self, *args, **kwargs):
            return self

        def first(self):
            return self.row

    class FakeDb:
        def __init__(self, row):
            self.row = row

        def query(self, model):
            return FakeQuery(self.row)

        def close(self):
            pass

    row = SimpleNamespace(
        owner=None,
        imap_user="bob@example.com",
        from_address="bob@example.com",
    )
    monkeypatch.setattr(database, "SessionLocal", lambda: FakeDb(row))

    with pytest.raises(HTTPException) as exc:
        email_helpers._assert_owns_account("legacy-bob", "alice@example.com")

    assert exc.value.status_code == 404


def test_assert_owns_account_accepts_matching_legacy_mailbox(monkeypatch):
    import core.database as database
    import routes.email_helpers as email_helpers

    class FakeQuery:
        def filter(self, *args, **kwargs):
            return self

        def first(self):
            return SimpleNamespace(
                owner=None,
                imap_user="alice@example.com",
                from_address="alias@example.com",
            )

    class FakeDb:
        def query(self, model):
            return FakeQuery()

        def close(self):
            pass

    monkeypatch.setattr(database, "SessionLocal", lambda: FakeDb())

    email_helpers._assert_owns_account("legacy-alice", "alice@example.com")


def test_mcp_email_accounts_are_owner_scoped(tmp_path, monkeypatch):
    import mcp_servers.email_server as email_server

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    conn = _create_mcp_email_accounts_db(data_dir / "app.db")
    rows = [
        ("alice-acct", "alice", "Alice Mail", 1, "alice@example.com", "alice@example.com"),
        ("bob-acct", "bob", "Bob Mail", 1, "bob@example.com", "bob@example.com"),
        ("legacy-alice", None, "Legacy Alice", 0, "alice", "alice"),
        ("legacy-shared", None, "Legacy Shared", 0, "shared@example.com", "shared@example.com"),
    ]
    for row in rows:
        conn.execute(
            """
            INSERT INTO email_accounts
            (id, owner, name, is_default, enabled, imap_host, imap_port, imap_user,
             imap_password, imap_starttls, smtp_host, smtp_port, smtp_security,
             smtp_user, smtp_password, from_address, created_at)
            VALUES (?, ?, ?, ?, 1, 'imap.example.com', 993, ?, '', 0,
                    'smtp.example.com', 465, 'ssl', ?, '', ?, '2026-01-01')
            """,
            (row[0], row[1], row[2], row[3], row[4], row[4], row[5]),
        )
    conn.commit()
    conn.close()

    monkeypatch.setattr(email_server, "DATA_DIR", data_dir)
    email_server._ACCOUNT_CACHE.clear()

    alice_ids = {row["id"] for row in email_server._list_accounts_raw(owner="alice")}
    bob_ids = {row["id"] for row in email_server._list_accounts_raw(owner="bob")}
    ownerless_ids = {row["id"] for row in email_server._list_accounts_raw(owner="")}

    assert alice_ids == {"alice-acct", "legacy-alice"}
    assert bob_ids == {"bob-acct"}
    assert ownerless_ids == {"legacy-alice", "legacy-shared"}
    assert email_server._resolve_account("bob-acct", owner="alice") is None
    assert email_server._resolve_account("legacy-shared", owner="alice") is None


def test_email_mcp_rejects_destructive_message_sets(monkeypatch):
    import mcp_servers.email_server as email_server

    monkeypatch.setattr(
        email_server,
        "_imap_connect",
        lambda *_, **__: (_ for _ in ()).throw(AssertionError("invalid UID reached IMAP")),
    )

    assert email_server._set_flag("1:*", "INBOX", "\\Seen", owner="alice") is False
    assert email_server._bulk_set_flag(["1:*"], "INBOX", "\\Seen", owner="alice") == 0
    assert email_server._bulk_move(["1:*"], "INBOX", "Archive", owner="alice") == 0
    assert email_server._move_message("1:*", "INBOX", "Archive", owner="alice") is False
    assert email_server._download_attachment("1:*", 0, "INBOX", owner="alice") == {
        "error": "UID must be a decimal IMAP UID"
    }


def test_email_mcp_rejects_crlf_search_before_imap(monkeypatch):
    import mcp_servers.email_server as email_server

    monkeypatch.setattr(
        email_server,
        "_imap_connect",
        lambda *_, **__: (_ for _ in ()).throw(AssertionError("CRLF input reached IMAP")),
    )

    with pytest.raises(ValueError):
        email_server._search_emails("subject\r\nUID SEARCH 1:*", owner="alice")

    assert email_server._read_email(message_id="<x>\r\nUID SEARCH 1:*", owner="alice") == {
        "error": "message_id must not contain CR/LF"
    }


def test_email_mcp_attachment_download_uses_confined_extract_dir(tmp_path, monkeypatch):
    import routes.email_helpers as email_helpers
    import mcp_servers.email_server as email_server

    monkeypatch.setattr(email_helpers, "ATTACHMENTS_DIR", tmp_path)

    msg = EmailMessage()
    msg["Subject"] = "Attachment"
    msg.set_content("body")
    msg.add_attachment(b"secret", maintype="application", subtype="octet-stream", filename="secret.txt")
    raw = msg.as_bytes()
    selected = []

    class FakeConn:
        def select(self, folder, readonly=True):
            selected.append(folder)
            return "OK", [b"1"]

        def uid(self, command, uid, query):
            return "OK", [(b"1", raw)]

        def logout(self):
            pass

    monkeypatch.setattr(email_server, "_imap_connect", lambda *_, **__: FakeConn())

    result = email_server._download_attachment("12", 0, folder="../../escape", owner="alice")

    assert "error" not in result
    saved = Path(result["path"])
    assert saved.read_bytes() == b"secret"
    assert saved.parent.parent == tmp_path
    assert tmp_path in saved.parents
    assert selected == ['"../../escape"']


def test_email_summary_and_reply_caches_are_owner_scoped(tmp_path, monkeypatch):
    import routes.email_helpers as email_helpers

    db_path = tmp_path / "scheduled_emails.db"
    monkeypatch.setattr(email_helpers, "SCHEDULED_DB", db_path)

    email_helpers._init_scheduled_db()

    conn = sqlite3.connect(db_path)
    try:
        for table in ("email_summaries", "email_ai_replies"):
            cols = conn.execute(f"PRAGMA table_info({table})").fetchall()
            by_name = {row[1]: row for row in cols}
            assert "owner" in by_name
            assert by_name["message_id"][5] == 1
            assert by_name["owner"][5] == 2
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_ai_reply_cache_lookup_is_owner_scoped(tmp_path, monkeypatch):
    import routes.email_helpers as email_helpers
    import routes.email_routes as email_routes
    import src.endpoint_resolver as endpoint_resolver

    db_path = tmp_path / "scheduled_emails.db"
    monkeypatch.setattr(email_helpers, "SCHEDULED_DB", db_path)
    monkeypatch.setattr(email_routes, "SCHEDULED_DB", db_path)
    email_helpers._init_scheduled_db()

    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        INSERT INTO email_ai_replies
        (message_id, owner, uid, folder, reply, model_used, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        ("<shared-message-id>", "bob", "42", "INBOX", "bob cached secret", "cached-model", "2026-01-01"),
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(endpoint_resolver, "resolve_endpoint", lambda *args, **kwargs: (None, None, None))

    router = email_routes.setup_email_routes()
    ai_reply = _route_endpoint(router, "/api/email/ai-reply", "POST")
    body = {
        "to": "sender@example.com",
        "subject": "Shared",
        "original_body": "Please reply",
        "message_id": "<shared-message-id>",
        "fast": True,
    }

    alice_result = await ai_reply(body, owner="alice")
    bob_result = await ai_reply(body, owner="bob")

    assert "bob cached secret" not in str(alice_result)
    assert alice_result["success"] is False
    assert bob_result == {
        "success": True,
        "reply": "bob cached secret",
        "model_used": "cached-model",
        "cached": True,
    }


@pytest.mark.asyncio
async def test_email_mcp_dispatch_overwrites_hidden_owner(monkeypatch):
    import src.tool_execution as tool_execution

    captured = {}

    class FakeMcp:
        async def call_tool(self, qualified_name, arguments):
            captured["qualified_name"] = qualified_name
            captured["arguments"] = arguments
            return {"stdout": "ok", "stderr": "", "exit_code": 0}

    monkeypatch.setattr(tool_execution, "get_mcp_manager", lambda: FakeMcp())
    monkeypatch.setattr(tool_execution, "is_public_blocked_tool", lambda tool: False)

    block = SimpleNamespace(
        tool_type="mcp__email__list_email_accounts",
        content='{"_odysseus_owner":"bob"}',
    )

    desc, result = await tool_execution.execute_tool_block(block, owner="alice")

    assert desc == "mcp: mcp__email__list_email_accounts"
    assert result["exit_code"] == 0
    assert captured == {
        "qualified_name": "mcp__email__list_email_accounts",
        "arguments": {"_odysseus_owner": "alice"},
    }


@pytest.mark.asyncio
async def test_rag_mcp_dispatch_overwrites_hidden_owner(monkeypatch):
    import src.tool_execution as tool_execution

    captured = {}

    class FakeMcp:
        async def call_tool(self, qualified_name, arguments):
            captured["qualified_name"] = qualified_name
            captured["arguments"] = arguments
            return {"stdout": "ok", "stderr": "", "exit_code": 0}

    monkeypatch.setattr(tool_execution, "get_mcp_manager", lambda: FakeMcp())
    monkeypatch.setattr(tool_execution, "is_public_blocked_tool", lambda tool: False)

    block = SimpleNamespace(
        tool_type="mcp__rag__manage_rag",
        content='{"action":"list","_odysseus_owner":"bob"}',
    )

    desc, result = await tool_execution.execute_tool_block(block, owner="alice")

    assert desc == "mcp: mcp__rag__manage_rag"
    assert result["exit_code"] == 0
    assert captured == {
        "qualified_name": "mcp__rag__manage_rag",
        "arguments": {"action": "list", "_odysseus_owner": "alice"},
    }


def test_scheduled_mcp_owner_context_overwrites_hidden_owner():
    from src.task_scheduler import _mcp_args_with_owner

    original = {"account": "default", "_odysseus_owner": "bob"}

    assert _mcp_args_with_owner("mcp__email__list_emails", original, "alice") == {
        "account": "default",
        "_odysseus_owner": "alice",
    }
    assert _mcp_args_with_owner("mcp__rag__manage_rag", {"action": "list"}, "alice") == {
        "action": "list",
        "_odysseus_owner": "alice",
    }
    assert _mcp_args_with_owner("mcp__other__send_email", original, "alice") == original
    assert original["_odysseus_owner"] == "bob"


@pytest.mark.asyncio
async def test_scheduled_mcp_delivery_passes_task_owner(monkeypatch):
    import routes.email_helpers as email_helpers
    import src.agent_tools as agent_tools
    from src.task_scheduler import TaskScheduler

    captured = {}

    class FakeMcp:
        async def call_tool(self, qualified_name, arguments):
            captured["qualified_name"] = qualified_name
            captured["arguments"] = arguments
            return {"stdout": "sent", "stderr": "", "exit_code": 0}

    def _fake_email_config(*, owner="", **kwargs):
        captured["email_config_owner"] = owner
        return {"from_address": f"{owner}@example.com"}

    monkeypatch.setattr(agent_tools, "get_mcp_manager", lambda: FakeMcp())
    monkeypatch.setattr(email_helpers, "_get_email_config", _fake_email_config)

    scheduler = TaskScheduler(session_manager=None)
    task = SimpleNamespace(id="task-1", name="Digest", owner="alice")

    await scheduler._deliver_via_mcp("mcp__email__send_email", task, "done")

    assert captured["email_config_owner"] == "alice"
    assert captured["qualified_name"] == "mcp__email__send_email"
    assert captured["arguments"]["_odysseus_owner"] == "alice"
    assert captured["arguments"]["to"] == "alice@example.com"


@pytest.mark.asyncio
async def test_scheduled_email_routes_are_owner_scoped(tmp_path, monkeypatch):
    import routes.email_helpers as email_helpers
    import routes.email_routes as email_routes

    db_path = tmp_path / "scheduled_emails.db"
    monkeypatch.setattr(email_helpers, "SCHEDULED_DB", db_path)
    monkeypatch.setattr(email_routes, "SCHEDULED_DB", db_path)
    email_helpers._init_scheduled_db()

    router = email_routes.setup_email_routes()
    schedule_email = _route_endpoint(router, "/api/email/schedule", "POST")
    list_scheduled = _route_endpoint(router, "/api/email/scheduled", "GET")
    cancel_scheduled = _route_endpoint(router, "/api/email/scheduled/{sid}", "DELETE")

    send_at = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    alice = await schedule_email(
        {"to": "a@example.com", "body": "alice body", "send_at": send_at},
        owner="alice",
    )
    bob = await schedule_email(
        {"to": "b@example.com", "body": "bob body", "send_at": send_at},
        owner="bob",
    )

    assert alice["success"] is True
    assert bob["success"] is True

    alice_rows = await list_scheduled(owner="alice")
    bob_rows = await list_scheduled(owner="bob")

    assert [row["id"] for row in alice_rows["scheduled"]] == [alice["id"]]
    assert [row["id"] for row in bob_rows["scheduled"]] == [bob["id"]]

    await cancel_scheduled(bob["id"], owner="alice")
    bob_rows = await list_scheduled(owner="bob")
    assert [row["id"] for row in bob_rows["scheduled"]] == [bob["id"]]

    await cancel_scheduled(alice["id"], owner="alice")
    alice_rows = await list_scheduled(owner="alice")
    assert alice_rows["scheduled"] == []


def test_scheduled_poller_resolves_config_with_row_owner(tmp_path, monkeypatch):
    import routes.email_helpers as email_helpers
    import routes.email_pollers as email_pollers

    db_path = tmp_path / "scheduled_emails.db"
    monkeypatch.setattr(email_helpers, "SCHEDULED_DB", db_path)
    monkeypatch.setattr(email_pollers, "SCHEDULED_DB", db_path)
    email_helpers._init_scheduled_db()

    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        INSERT INTO scheduled_emails
        (id, to_addr, subject, body, attachments, send_at, created_at, status, account_id, owner)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
        """,
        (
            "sched-1",
            "recipient@example.com",
            "Subject",
            "Body",
            "[]",
            "2000-01-01T00:00:00",
            "1999-12-31T00:00:00",
            "acct-alice",
            "alice",
        ),
    )
    conn.commit()
    conn.close()

    calls = []

    def fake_get_email_config(account_id=None, owner=""):
        calls.append(("config", account_id, owner))
        return {
            "from_address": "alice@example.com",
            "smtp_host": "smtp.example.com",
            "smtp_user": "alice@example.com",
            "smtp_password": "secret",
        }

    class FakeImap:
        def __init__(self, account_id=None, owner=""):
            calls.append(("imap", account_id, owner))

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def append(self, folder, flags, date_time, message):
            calls.append(("append", folder))

    monkeypatch.setattr(email_pollers, "_get_email_config", fake_get_email_config)
    monkeypatch.setattr(email_pollers, "_send_smtp_message", lambda *args, **kwargs: calls.append(("send", args[1], args[2])))
    monkeypatch.setattr(email_pollers, "_imap", FakeImap)
    monkeypatch.setattr(email_pollers, "_detect_sent_folder", lambda imap: "Sent")
    monkeypatch.setattr(email_pollers, "_cleanup_compose_uploads", lambda attachments: calls.append(("cleanup", attachments)))

    result = email_pollers._scheduled_poll_once()

    assert result == {"sent": ["sched-1"], "failed": []}
    assert ("config", "acct-alice", "alice") in calls
    assert ("imap", "acct-alice", "alice") in calls
