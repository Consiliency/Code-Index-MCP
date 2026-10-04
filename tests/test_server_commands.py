from click.testing import CliRunner

from mcp_server.cli.server_commands import serve, stdio


def _compact(text: str) -> str:
    return " ".join(text.split())


def test_serve_uses_explicit_host_port(monkeypatch):
    runner = CliRunner()
    calls = []

    def fake_run(app, host, port, reload, proxy_headers):
        assert proxy_headers is False
        calls.append((app, host, port, reload))

    monkeypatch.setattr("uvicorn.run", fake_run)

    result = runner.invoke(serve, ["--host", "127.0.0.1", "--port", "9123"])

    assert result.exit_code == 0
    assert calls == [("mcp_server.gateway:app", "127.0.0.1", 9123, False)]
    assert "MCP_INDEX_STORAGE_PATH" in __import__("os").environ
    assert "not the MCP Streamable HTTP transport" in result.output


def test_serve_uses_env_defaults(monkeypatch):
    runner = CliRunner()
    calls = []

    def fake_run(app, host, port, reload, proxy_headers):
        assert proxy_headers is False
        calls.append((app, host, port, reload))

    monkeypatch.setenv("MCP_SERVER_HOST", "0.0.0.0")
    monkeypatch.setenv("MCP_SERVER_PORT", "8877")
    monkeypatch.setattr("uvicorn.run", fake_run)

    result = runner.invoke(serve, [])

    assert result.exit_code == 0
    assert calls == [("mcp_server.gateway:app", "0.0.0.0", 8877, False)]


def test_serve_preserves_explicit_storage_env(monkeypatch):
    runner = CliRunner()
    calls = []

    def fake_run(app, host, port, reload, proxy_headers):
        assert proxy_headers is False
        calls.append((app, host, port, reload))

    monkeypatch.setenv("MCP_INDEX_STORAGE_PATH", "/tmp/custom-indexes")
    monkeypatch.setattr("uvicorn.run", fake_run)

    result = runner.invoke(serve, [])

    assert result.exit_code == 0
    assert calls == [("mcp_server.gateway:app", "127.0.0.1", 8765, False)]
    assert __import__("os").environ["MCP_INDEX_STORAGE_PATH"] == "/tmp/custom-indexes"


def test_serve_help_marks_fastapi_admin_gateway():
    runner = CliRunner()

    result = runner.invoke(serve, ["--help"])

    assert result.exit_code == 0
    compact = _compact(result.output)
    assert "FastAPI admin/debug HTTP gateway" in compact
    assert "not the MCP Streamable HTTP transport" in compact


def test_stdio_help_marks_primary_mcp_surface():
    runner = CliRunner()

    result = runner.invoke(stdio, ["--help"])

    assert result.exit_code == 0
    assert "primary MCP client transport over STDIO" in result.output
