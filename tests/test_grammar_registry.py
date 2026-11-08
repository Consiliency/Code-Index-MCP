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


def test_resolve_cache_root_falls_back_to_passwd_home_like_the_pack(monkeypatch, tmp_path):
    from pathlib import Path

    monkeypatch.setattr(grammar_cache, "_passwd_home", lambda: Path("/p"))
    assert str(grammar_cache.resolve_cache_root({})) == "/p/.cache"
    monkeypatch.setattr(grammar_cache, "_passwd_home", lambda: None)
    assert grammar_cache.resolve_cache_root({}) is None


def test_relative_grammar_paths_resolve_against_the_given_cwd(tmp_path):
    env = {grammar_cache.CACHE_DIR_ENV: "rel/cache", grammar_cache.LIBS_DIR_ENV: "rel/libs"}
    assert grammar_cache.resolve_cache_root(env, cwd=tmp_path) == tmp_path / "rel" / "cache"
    assert grammar_cache.resolve_libs_dir(env, cwd=tmp_path) == tmp_path / "rel" / "libs"
    assert grammar_cache.resolve_libs_dir({}, cwd=tmp_path) is None


class _FakePackError(Exception):
    pass


def _fake_pack(downloaded):
    import types

    calls = []
    pack = types.ModuleType("fake_tree_sitter_language_pack")
    pack.DownloadError = _FakePackError
    pack.downloaded_languages = lambda: list(downloaded)

    def recorder(name):
        def entry(*args, **kwargs):
            calls.append((name, args))
            return name

        return entry

    for name in (
        "get_language",
        "get_parser",
        "process",
        "download",
        "download_all",
        "download_group",
        "prefetch",
        "init",
        "configure",
        "clean_cache",
        "manifest_languages",
        "manifest_groups",
    ):
        setattr(pack, name, recorder(name))
    return pack, calls


def test_offline_guard_loads_only_extracted_grammars(monkeypatch, tmp_path):
    """#116 review B2: a network-denied process never downloads or extracts."""
    pack, calls = _fake_pack(["c", "csharp"])
    monkeypatch.setattr(grammar_cache, "_pack", lambda: pack)
    monkeypatch.delenv(grammar_cache.LIBS_DIR_ENV, raising=False)
    monkeypatch.setenv(grammar_cache.MANIFEST_URL_ENV, "https://attacker.invalid/m.json")
    monkeypatch.setenv("TREE_SITTER_LANGUAGE_PACK_TLS_ROOTS", "/tmp/roots.pem")

    guard = grammar_cache.install_offline_guard()
    assert grammar_cache.install_offline_guard() is guard  # idempotent
    assert os.environ[grammar_cache.MANIFEST_URL_ENV] == grammar_cache.OFFLINE_MANIFEST_URL
    assert grammar_cache.OFFLINE_MANIFEST_URL.split(":", 1)[0] not in {"http", "https", "file"}
    assert "TREE_SITTER_LANGUAGE_PACK_TLS_ROOTS" not in os.environ

    assert pack.get_language("c") == "get_language"
    assert pack.get_parser("c_sharp") == "get_parser"  # registry code -> pack grammar
    assert pack.process("int x;", {"language": "c"}) == "process"
    for name in ("cpp", "zig", None):
        with pytest.raises(_FakePackError, match="not in the local grammar cache"):
            pack.get_language(name)
    with pytest.raises(_FakePackError):
        pack.process("x", {"language": "zig"})
    with pytest.raises(_FakePackError):
        pack.process("x", object())
    for name in grammar_cache._OFFLINE_REFUSED:
        with pytest.raises(_FakePackError, match="network-denied"):
            getattr(pack, name)(["c"])
    assert [name for name, _ in calls] == ["get_language", "get_parser", "process"]


def test_offline_guard_accepts_shared_libs_dir_grammars(monkeypatch, tmp_path):
    shared = tmp_path / "libs"
    shared.mkdir()
    (shared / "libtree_sitter_cpp.so").write_bytes(b"")
    pack, _calls = _fake_pack([])
    monkeypatch.setattr(grammar_cache, "_pack", lambda: pack)
    monkeypatch.setenv(grammar_cache.LIBS_DIR_ENV, str(shared))
    grammar_cache.install_offline_guard()
    assert pack.get_language("cpp") == "get_language"
    with pytest.raises(_FakePackError):
        pack.get_language("c")


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
