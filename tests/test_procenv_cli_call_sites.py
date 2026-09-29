from __future__ import annotations

from unittest.mock import AsyncMock, Mock, patch

import pytest

from mcp_server.cli import artifact_commands, preflight_commands
from mcp_server.core.path_utils import PathUtils
from mcp_server.core.preflight_validator import PreFlightValidator
from scripts import agent_validation


def test_artifact_git_ref_info_uses_helper_env() -> None:
    sentinel_env = {"PATH": "/procenv"}

    with patch("mcp_server.cli.artifact_commands.get_full_env", return_value=sentinel_env):
        with patch("mcp_server.cli.artifact_commands.subprocess.run") as mock_run:
            mock_run.side_effect = [Mock(stdout="abc123\n"), Mock(stdout="main\n")]
            artifact_commands._get_git_ref_info()

    assert mock_run.call_args_list[0].kwargs["env"] == sentinel_env
    assert mock_run.call_args_list[1].kwargs["env"] == sentinel_env


def test_preflight_git_queries_use_helper_env() -> None:
    sentinel_env = {"PATH": "/procenv"}

    with patch("mcp_server.cli.preflight_commands.get_full_env", return_value=sentinel_env):
        with patch("mcp_server.cli.preflight_commands.subprocess.run") as mock_run:
            mock_run.side_effect = [Mock(stdout="origin/main\n"), Mock(stdout="1 0\n")]
            preflight_commands._get_upstream_ref()
            preflight_commands._get_ahead_behind("origin/main")

    assert mock_run.call_args_list[0].kwargs["env"] == sentinel_env
    assert mock_run.call_args_list[1].kwargs["env"] == sentinel_env


@pytest.mark.asyncio
async def test_preflight_validator_dependency_checks_use_helper_env() -> None:
    sentinel_env = {"PATH": "/procenv"}
    validator = PreFlightValidator()

    with patch("mcp_server.core.preflight_validator.get_full_env", return_value=sentinel_env):
        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_spawn:
            proc = AsyncMock()
            proc.returncode = 0
            mock_spawn.return_value = proc

            await validator._validate_dependencies()

    assert mock_spawn.call_args_list[0].kwargs["env"] == sentinel_env
    assert mock_spawn.call_args_list[1].kwargs["env"] == sentinel_env


def test_pathutils_python_executable_uses_helper_path(monkeypatch) -> None:
    monkeypatch.delenv(PathUtils.ENV_PYTHON_PATH, raising=False)

    with patch("mcp_server.core.path_utils.get_full_env", return_value={"PATH": "/procenv"}):
        with patch("mcp_server.core.path_utils.shutil.which") as mock_which:
            mock_which.side_effect = [None, "/procenv/python"]
            assert PathUtils.get_python_executable() == "python"

    assert mock_which.call_args_list[0].kwargs["path"] == "/procenv"
    assert mock_which.call_args_list[1].kwargs["path"] == "/procenv"


def test_agent_validation_git_and_execution_use_helper_env(monkeypatch) -> None:
    sentinel_env = {"PATH": "/procenv"}
    calls = []

    def fake_run(cmd, cwd=None, check=False, capture_output=False, text=False, env=None):
        calls.append({"cmd": list(cmd), "env": env, "capture_output": capture_output, "text": text})
        if capture_output:
            assert "-z" in cmd
            return Mock(returncode=0, stdout="README.md\0")
        return Mock(returncode=0)

    monkeypatch.setattr(agent_validation, "get_full_env", lambda: sentinel_env)
    monkeypatch.setattr(agent_validation.subprocess, "run", fake_run)

    assert agent_validation._changed_paths() == ["README.md"]
    assert agent_validation.cmd_run("fast") == 0

    assert calls[0]["env"] == sentinel_env
    assert calls[1]["env"] == sentinel_env


def test_agent_validation_run_output_redacts_command_details(capsys, monkeypatch) -> None:
    monkeypatch.setattr(agent_validation, "_run", lambda cmd: 0)
    monkeypatch.setattr(
        agent_validation,
        "build_offload_plan",
        lambda target: agent_validation.OffloadPlan(
            "remote-host", ["ssh", "tailnet-host", "token=secret make agent-fast-local"]
        ),
    )

    assert agent_validation.cmd_run("fast") == 0
    output = capsys.readouterr().out

    assert "selected_mode=remote-host" in output
    assert "command_name=ssh" in output
    assert "command_redacted=true" in output
    assert "tailnet-host" not in output
    assert "secret" not in output
