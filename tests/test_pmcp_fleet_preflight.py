"""PMCP fleet semantic preflight contract tests."""

from pathlib import Path
from types import SimpleNamespace

from click.testing import CliRunner

from mcp_server.cli.setup_commands import setup


class _FakeReport:
    def __init__(self) -> None:
        self.overall_ready = False
        self.can_write_semantic_vectors = False
        self.strict_mode = False
        self.profiles = SimpleNamespace(status=SimpleNamespace(value="ready"), message="ok")
        self.enrichment = SimpleNamespace(status=SimpleNamespace(value="ready"), message="ok")
        self.embedding = SimpleNamespace(status=SimpleNamespace(value="ready"), message="ok")
        self.qdrant = SimpleNamespace(
            status=SimpleNamespace(value="ready"),
            message="Qdrant reachable at http://localhost:6333",
            ok=True,
        )
        self.collection = SimpleNamespace(
            status=SimpleNamespace(value="missing"),
            message="collection bootstrap required",
        )
        self.blocker = SimpleNamespace(
            code="collection_missing",
            message="Qdrant collection is missing for the active semantic profile",
            remediation=["Create or hydrate the expected semantic collection before vector writes"],
        )
        self.warnings = ["dry run only"]
        self.effective_config = {
            "selected_profile": "oss_high",
            "collection_name": "code_index__oss_high__v1",
            "embedding": {
                "base_url": "http://ai:8001/v1",
                "model": "Qwen/Qwen3-Embedding-8B",
                "api_key_env": "OPENAI_API_KEY",
                "api_key_present": False,
            },
            "qdrant": {
                "url": "http://localhost:6333",
            },
        }

    def to_dict(self) -> dict:
        return {
            "overall_ready": self.overall_ready,
            "can_write_semantic_vectors": self.can_write_semantic_vectors,
            "strict_mode": self.strict_mode,
            "profiles": {"status": "ready", "message": "ok"},
            "enrichment": {"status": "ready", "message": "ok"},
            "embedding": {"status": "ready", "message": "ok"},
            "qdrant": {"status": "ready", "message": self.qdrant.message},
            "collection": {"status": "missing", "message": self.collection.message},
            "blocker": {
                "code": self.blocker.code,
                "message": self.blocker.message,
                "remediation": list(self.blocker.remediation),
                "can_write_semantic_vectors": False,
                "failing_checks": [],
            },
            "warnings": list(self.warnings),
            "effective_config": self.effective_config,
        }


def test_setup_semantic_dry_run_expresses_pmcp_bootstrap_contract(monkeypatch):
    runner = CliRunner()
    observed = {}

    def _fake_preflight(*, settings, profile, strict, timeout_s):
        observed["profile"] = profile
        observed["strict"] = strict
        observed["timeout_s"] = timeout_s
        observed["qdrant_host"] = settings.qdrant_host
        observed["qdrant_port"] = settings.qdrant_port
        observed["openai_api_base"] = settings.openai_api_base
        observed["autostart_qdrant"] = settings.semantic_autostart_qdrant
        return _FakeReport()

    monkeypatch.setattr("mcp_server.cli.setup_commands.run_semantic_preflight", _fake_preflight)

    result = runner.invoke(
        setup,
        [
            "semantic",
            "--json",
            "--dry-run",
            "--profile",
            "oss_high",
            "--qdrant-url",
            "http://localhost:6333",
            "--openai-api-base",
            "http://ai:8001/v1",
            "--no-autostart-qdrant",
        ],
    )

    assert result.exit_code == 0
    assert observed == {
        "profile": "oss_high",
        "strict": False,
        "timeout_s": None,
        "qdrant_host": "localhost",
        "qdrant_port": 6333,
        "openai_api_base": "http://ai:8001/v1",
        "autostart_qdrant": False,
    }
    for expected in (
        '"selected_profile": "oss_high"',
        '"collection_name": "code_index__oss_high__v1"',
        '"base_url": "http://ai:8001/v1"',
        '"url": "http://localhost:6333"',
        '"code": "collection_missing"',
        '"can_write_semantic_vectors": false',
        '"status": "dry_run"',
    ):
        assert expected in result.output


def test_pmcp_guide_freezes_semantic_env_contract():
    text = Path("docs/guides/pmcp-fleet-integration.md").read_text(encoding="utf-8")

    for expected in (
        '"MCP_ALLOWED_ROOTS": "/abs/path/to/repos"',
        '"SEMANTIC_SEARCH_ENABLED": "true"',
        '"SEMANTIC_DEFAULT_PROFILE": "oss_high"',
        '"SEMANTIC_EMBEDDING_BASE_URL": "http://ai:8001/v1"',
        '"QDRANT_URL": "http://localhost:6333"',
        '"SEMANTIC_AUTOSTART_QDRANT": "false"',
        '"MCP_AUTO_INDEX": "false"',
    ):
        assert expected in text

    assert '"MCP_QDRANT_URL":' not in text
