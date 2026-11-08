"""Every grammar the language registry advertises loads and parses (#115).

tree-sitter-language-pack 1.20 downloads parser libraries on first use. These
tests fetch any missing grammar into the configured cache. They skip only when
the cache is cold AND the download fails (no network). CI prefetches with
``mcp-index grammars prefetch`` and sets ``MCP_REQUIRE_GRAMMARS=1`` so that skip
becomes a failure there (Consiliency/Code-Index-MCP#115).
"""

from __future__ import annotations

import os

import pytest
from click.testing import CliRunner

from mcp_server.plugins.language_registry import (
    LANGUAGE_CONFIGS,
    get_grammar_name,
    get_required_grammars,
)
from mcp_server.utils import grammar_cache

REQUIRE_ENV = "MCP_REQUIRE_GRAMMARS"


@pytest.fixture(scope="module")
def warm_grammar_cache():
    missing = grammar_cache.missing_grammars()
    if missing:
        try:
            grammar_cache.prefetch(missing)
        except Exception as exc:  # noqa: BLE001 - DownloadError surfaces as RuntimeError
            reason = (
                f"grammar cache {grammar_cache.effective_cache_dir()} is cold for "
                f"{len(missing)} grammar(s) and the download failed (offline?): "
                f"{type(exc).__name__}: {exc}. Run `mcp-index grammars prefetch`."
            )
            if os.environ.get(REQUIRE_ENV) == "1":
                pytest.fail(f"{REQUIRE_ENV}=1 forbids skipping: {reason}")
            pytest.skip(reason)
    return get_required_grammars()


def test_csharp_grammar_name_maps_to_pack_name_but_public_code_is_stable():
    assert LANGUAGE_CONFIGS["c_sharp"]["code"] == "c_sharp"
    assert get_grammar_name("c_sharp") == "csharp"
    assert "c_sharp" not in get_required_grammars()
    assert "csharp" in get_required_grammars()


def test_required_grammars_cover_every_grammar_backed_registry_language():
    required = set(get_required_grammars())
    for language in LANGUAGE_CONFIGS:
        name = get_grammar_name(language)
        if language == "plaintext":
            assert name is None
            continue
        assert name in required, language
    assert "tsx" in required  # TypeScript plugin parses .tsx directly


def test_every_registry_grammar_is_in_the_pack_manifest(warm_grammar_cache):
    import tree_sitter_language_pack as pack

    # A warm cache holds manifest.json, so this reads it without network.
    known = set(pack.manifest_languages())
    assert sorted(set(get_required_grammars()) - known) == []


def test_every_registry_grammar_loads_and_parses_offline(warm_grammar_cache):
    check = grammar_cache.verify(warm_grammar_cache, offline=True)
    assert check.failed == {}
    assert sorted(check.loaded) == sorted(warm_grammar_cache)


def test_chunker_accepts_every_registry_language_code(warm_grammar_cache):
    from chunker import chunk_text

    failures = {}
    for language, config in LANGUAGE_CONFIGS.items():
        if get_grammar_name(language) is None:
            continue
        try:
            chunk_text("x = 1\nfunction f() {}\nclass A {}\n", config["code"])
        except Exception as exc:  # noqa: BLE001 - collect every language failure
            failures[language] = f"{type(exc).__name__}: {exc}"
    assert failures == {}


def test_grammars_check_cli_reports_warm_cache(warm_grammar_cache):
    from mcp_server.cli.grammar_commands import grammars

    result = CliRunner().invoke(grammars, ["check", "--json"])
    assert result.exit_code == 0, result.output
    assert '"ok": true' in result.output


def test_resolve_cache_root_matches_pack_resolution_order():
    env = {"HOME": "/h", "XDG_CACHE_HOME": "/x", grammar_cache.CACHE_DIR_ENV: "/o"}
    assert str(grammar_cache.resolve_cache_root(env)) == "/o"
    env.pop(grammar_cache.CACHE_DIR_ENV)
    assert str(grammar_cache.resolve_cache_root(env)) == "/x"
    env.pop("XDG_CACHE_HOME")
    assert str(grammar_cache.resolve_cache_root(env)) == "/h/.cache"
    assert grammar_cache.resolve_cache_root({}) is None


def test_exported_libs_dir_loads_every_grammar_without_a_cache(warm_grammar_cache, tmp_path):
    """Images export libraries to a shared dir loaded via LIBS_DIR (any owner/mode)."""
    import subprocess
    import sys

    shared = tmp_path / "libs"
    copied = grammar_cache.export_libs(shared)
    assert copied and (shared.stat().st_mode & 0o777) == 0o755
    script = (
        "from mcp_server.utils import grammar_cache as g; "
        "r = g.verify(offline=True); print(r.ok, len(r.loaded), sorted(r.failed))"
    )
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(tmp_path / "empty-home"),
        grammar_cache.LIBS_DIR_ENV: str(shared),
        # An unreachable manifest proves nothing is downloaded.
        "TREE_SITTER_LANGUAGE_PACK_MANIFEST_URL": "http://127.0.0.1:9/unreachable",
    }
    result = subprocess.run(
        [sys.executable, "-c", script], env=env, capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.split()[:2] == ["True", str(len(warm_grammar_cache))]
    assert not (tmp_path / "empty-home" / ".cache").exists()
