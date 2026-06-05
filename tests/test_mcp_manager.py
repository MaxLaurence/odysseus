import json
from types import SimpleNamespace

import pytest

from src.mcp_manager import McpManager, _format_mcp_connection_error
from src.mcp_manager import _mcp_identity_hint, _mcp_stdio_env


def _route_endpoint(router, path: str, method: str):
    method = method.upper()
    for route in router.routes:
        if route.path == path and method in getattr(route, "methods", set()):
            return route.endpoint
    raise AssertionError(f"route not found: {method} {path}")


def test_playwright_mcp_connection_error_includes_install_hint():
    msg = _format_mcp_connection_error(
        "Browser (Playwright)",
        "npx",
        ["-y", "@playwright/mcp@latest", "--headless"],
        RuntimeError("package not found"),
    )

    assert "package not found" in msg
    assert "Browser MCP could not start" in msg
    assert "npx -y @playwright/mcp@latest --version" in msg
    assert "restart Odysseus" in msg


def test_generic_mcp_connection_error_preserves_original_error():
    msg = _format_mcp_connection_error(
        "Custom MCP",
        "python",
        ["server.py"],
        RuntimeError("boom"),
    )

    assert msg == "boom"


def test_mcp_stdio_env_drops_backend_secrets(monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("HOME", "/home/tester")
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    monkeypatch.setenv("ODYSSEUS_INTERNAL_TOKEN", "server-admin-token")
    monkeypatch.setenv("OPENAI_API_KEY", "ambient-openai")
    monkeypatch.setenv("HF_TOKEN", "ambient-hf")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "ambient-aws")

    env = _mcp_stdio_env({"PYTHONPATH": "/app", "CUSTOM_TOKEN": "server-specific"})

    assert env["PATH"] == "/usr/bin"
    assert env["HOME"] == "/home/tester"
    assert env["LANG"] == "en_US.UTF-8"
    assert env["PYTHONPATH"] == "/app"
    assert env["CUSTOM_TOKEN"] == "server-specific"
    assert "ODYSSEUS_INTERNAL_TOKEN" not in env
    assert "OPENAI_API_KEY" not in env
    assert "HF_TOKEN" not in env
    assert "AWS_SECRET_ACCESS_KEY" not in env


def test_mcp_identity_hint_does_not_expose_secret_like_env_values():
    identity = _mcp_identity_hint({
        "EMAIL_ADDRESS": "me@example.com",
        "ACCOUNT_TOKEN": "sk-leaky",
        "SERVICE_ACCOUNT_JSON": '{"private_key":"secret"}',
        "USER_PASSWORD": "hunter2",
        "USERNAME": "display-user",
    })

    assert "me@example.com" in identity
    assert "display-user" in identity
    assert "sk-leaky" not in identity
    assert "private_key" not in identity
    assert "hunter2" not in identity


def test_mcp_tool_prompt_identity_omits_secret_env_values():
    mgr = McpManager()
    identity = _mcp_identity_hint({
        "EMAIL_ADDRESS": "me@example.com",
        "ACCOUNT_TOKEN": "sk-leaky",
        "SERVICE_ACCOUNT_JSON": '{"private_key":"secret"}',
    })
    mgr._connections["srv"] = {
        "status": "connected",
        "name": "Example MCP",
        "identity": identity,
    }
    mgr._tools["srv"] = [{
        "name": "list",
        "description": "List things",
        "input_schema": {"type": "object", "properties": {}},
    }]

    prompt = mgr.get_tool_descriptions_for_prompt()
    schemas = mgr.get_all_openai_schemas()
    rendered = prompt + repr(schemas)

    assert "me@example.com" in rendered
    assert "sk-leaky" not in rendered
    assert "private_key" not in rendered


@pytest.mark.asyncio
async def test_mcp_call_tool_blocks_disabled_tool(monkeypatch):
    import src.mcp_manager as mcp_manager

    called = False

    class FakeSession:
        async def call_tool(self, tool_name, arguments):
            nonlocal called
            called = True
            return SimpleNamespace(content=[], isError=False)

    monkeypatch.setattr(mcp_manager, "_mcp_disabled_tool_set", lambda server_id: {"run_shell"})

    mgr = McpManager()
    mgr._sessions["srv"] = FakeSession()

    result = await mgr.call_tool("mcp__srv__run_shell", {})

    assert result["exit_code"] == 1
    assert "disabled" in result["error"]
    assert called is False


@pytest.mark.asyncio
async def test_builtin_npx_probe_drops_backend_secrets(monkeypatch):
    import asyncio
    from src import builtin_mcp

    captured = {}

    class FakeProc:
        returncode = 0

        async def communicate(self):
            return b"1.0.0\n", b""

    async def fake_create_subprocess_exec(*args, **kwargs):
        captured["args"] = args
        captured["env"] = kwargs.get("env")
        return FakeProc()

    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("ODYSSEUS_INTERNAL_TOKEN", "server-admin-token")
    monkeypatch.setenv("OPENAI_API_KEY", "ambient-openai")
    monkeypatch.setenv("HF_TOKEN", "ambient-hf")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    assert await builtin_mcp._is_npx_package_cached("npx", "@playwright/mcp@latest")

    assert captured["args"][:3] == ("npx", "--no-install", "@playwright/mcp@latest")
    assert captured["env"]["PATH"] == "/usr/bin"
    assert "ODYSSEUS_INTERNAL_TOKEN" not in captured["env"]
    assert "OPENAI_API_KEY" not in captured["env"]
    assert "HF_TOKEN" not in captured["env"]


def test_mcp_server_list_scrubs_env_secret_values(monkeypatch):
    import routes.mcp_routes as mcp_routes

    server = SimpleNamespace(
        id="srv",
        name="Example",
        transport="stdio",
        command="python",
        args=json.dumps([]),
        env=json.dumps({
            "EMAIL_ADDRESS": "me@example.com",
            "ACCOUNT_TOKEN": "sk-leaky",
            "SERVICE_ACCOUNT_JSON": '{"private_key":"secret"}',
        }),
        url=None,
        is_enabled=True,
        oauth_config=None,
        disabled_tools=None,
    )

    class FakeQuery:
        def all(self):
            return [server]

    class FakeDb:
        def query(self, _model):
            return FakeQuery()

        def close(self):
            pass

    class FakeManager:
        def get_server_status(self, _server_id):
            return {"status": "connected", "tool_count": 1}

    monkeypatch.setattr(mcp_routes, "SessionLocal", lambda: FakeDb())
    monkeypatch.setattr(mcp_routes, "require_admin", lambda _request: None)
    endpoint = _route_endpoint(mcp_routes.setup_mcp_routes(FakeManager()), "/api/mcp/servers", "GET")

    result = endpoint(SimpleNamespace())

    env = result[0]["env"]
    assert env["EMAIL_ADDRESS"] == "me@example.com"
    assert env["ACCOUNT_TOKEN"] == ""
    assert env["SERVICE_ACCOUNT_JSON"] == ""
    assert "sk-leaky" not in repr(result)
    assert "private_key" not in repr(result)
