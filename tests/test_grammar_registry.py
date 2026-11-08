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


# --- export_libs safety and libs-dir checks (#116 review S2, N1) -----------


@pytest.fixture
def fake_cache_libs(monkeypatch, tmp_path):
    source = tmp_path / "cache-libs"
    source.mkdir()
    (source / "libtree_sitter_c.so").write_bytes(b"new-c")
    (source / "libtree_sitter_cpp.so").write_bytes(b"new-cpp")
    monkeypatch.setattr(grammar_cache, "effective_cache_dir", lambda: str(source))
    return source


def test_export_libs_replaces_libraries_atomically(fake_cache_libs, tmp_path):
    """Re-exporting on a live host must not truncate a library a process has
    mapped; os.replace swaps in a new inode instead."""
    import mmap

    shared = tmp_path / "shared"
    shared.mkdir(mode=0o755)
    os.chmod(shared, 0o755)
    live = shared / "libtree_sitter_c.so"
    live.write_bytes(b"old-c")
    with open(live, "rb") as handle:
        mapped = mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ)
        old_inode = os.fstat(handle.fileno()).st_ino
        copied = grammar_cache.export_libs(shared)
        assert mapped[:] == b"old-c"  # the mapped file was not rewritten in place
        mapped.close()
    assert copied == ["libtree_sitter_c.so", "libtree_sitter_cpp.so"]
    assert live.read_bytes() == b"new-c"
    assert os.stat(live).st_ino != old_inode
    assert (os.stat(live).st_mode & 0o777) == 0o644
    assert sorted(p.name for p in shared.iterdir()) == copied  # no temp files left


def test_export_libs_creates_missing_dirs_0755_and_keeps_existing_modes(fake_cache_libs, tmp_path):
    old_umask = os.umask(0o077)
    try:
        created = tmp_path / "new" / "libs"
        grammar_cache.export_libs(created)
    finally:
        os.umask(old_umask)
    assert (os.stat(created).st_mode & 0o777) == 0o755
    assert (os.stat(created.parent).st_mode & 0o777) == 0o755

    existing = tmp_path / "existing"
    existing.mkdir()
    os.chmod(existing, 0o750)
    grammar_cache.export_libs(existing)
    assert (os.stat(existing).st_mode & 0o777) == 0o750  # never re-chmodded


def test_export_libs_refuses_symlinked_target_and_components(fake_cache_libs, tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "link").symlink_to(real)
    with pytest.raises(grammar_cache.UnsafeLibsDirError, match="symlink"):
        grammar_cache.export_libs(tmp_path / "link")
    with pytest.raises(grammar_cache.UnsafeLibsDirError, match="symlink"):
        grammar_cache.export_libs(tmp_path / "link" / "libs")
    assert list(real.iterdir()) == []


def test_export_libs_refuses_a_planted_library_symlink(fake_cache_libs, tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir()
    victim = tmp_path / "victim"
    victim.write_bytes(b"precious")
    (shared / "libtree_sitter_c.so").symlink_to(victim)
    with pytest.raises(grammar_cache.UnsafeLibsDirError, match="not a regular file"):
        grammar_cache.export_libs(shared)
    assert victim.read_bytes() == b"precious"


def test_export_libs_refuses_world_writable_and_foreign_dirs(fake_cache_libs, tmp_path):
    sticky = tmp_path / "tmp-like"
    sticky.mkdir()
    os.chmod(sticky, 0o1777)
    with pytest.raises(grammar_cache.UnsafeLibsDirError, match="writable"):
        grammar_cache.export_libs(sticky)
    assert (os.stat(sticky).st_mode & 0o7777) == 0o1777  # left alone
    assert list(sticky.iterdir()) == []

    populated = tmp_path / "populated"
    populated.mkdir()
    (populated / "etc").mkdir()
    with pytest.raises(grammar_cache.UnsafeLibsDirError, match="other than parser libraries"):
        grammar_cache.export_libs(populated)

    with pytest.raises(grammar_cache.UnsafeLibsDirError):
        grammar_cache.export_libs("/")
    with pytest.raises(grammar_cache.UnsafeLibsDirError):
        grammar_cache.export_libs("/tmp")


def test_libs_dir_problems_flags_unsafe_ownership_modes_and_symlinks(tmp_path):
    shared = tmp_path / "libs"
    shared.mkdir()
    os.chmod(shared, 0o755)
    lib = shared / "libtree_sitter_c.so"
    lib.write_bytes(b"")
    os.chmod(lib, 0o644)
    assert grammar_cache.libs_dir_problems(shared) == []

    os.chmod(lib, 0o664)
    assert any(
        "libtree_sitter_c.so" in p and "writable" in p
        for p in grammar_cache.libs_dir_problems(shared)
    )
    os.chmod(lib, 0o644)
    os.chmod(shared, 0o775)
    assert any("writable" in p for p in grammar_cache.libs_dir_problems(shared))
    os.chmod(shared, 0o755)
    (shared / "libtree_sitter_cpp.so").symlink_to(tmp_path / "elsewhere.so")
    assert any("symlink" in p for p in grammar_cache.libs_dir_problems(shared))
    assert grammar_cache.libs_dir_problems(tmp_path / "missing") == [
        f"{tmp_path / 'missing'}: does not exist"
    ]


def test_grammars_check_fails_on_an_unsafe_libs_dir(monkeypatch, tmp_path):
    import json

    from mcp_server.cli.grammar_commands import grammars

    shared = tmp_path / "libs"
    shared.mkdir()
    os.chmod(shared, 0o777)
    monkeypatch.setenv(grammar_cache.LIBS_DIR_ENV, str(shared))
    result = CliRunner().invoke(grammars, ["check", "--json", "--language", "c"])
    assert result.exit_code == 1, result.output
    report = json.loads(result.output[result.output.index("{") :])
    assert report["ok"] is False
    assert report["libs_dir"] == str(shared)
    assert any("writable" in p for p in report["libs_dir_problems"])


# --- grammar unavailability is visible and fails closed (#116 review S3) ---


@pytest.fixture
def fresh_offline_set(monkeypatch):
    monkeypatch.setattr(grammar_cache, "_offline_grammars", None)


def test_unavailable_grammar_maps_registry_codes_and_rereads_on_miss(
    monkeypatch, fresh_offline_set
):
    reads = [set(), {"rust", "csharp"}]
    monkeypatch.setattr(grammar_cache, "cached_grammars", lambda: sorted(reads[0]))
    assert grammar_cache.unavailable_grammar("c_sharp") == "csharp"
    assert grammar_cache.unavailable_grammar("plaintext") is None
    assert grammar_cache.unavailable_grammar("not-a-registry-language") is None
    assert grammar_cache.unavailable_grammar(None) is None
    # A prefetch after startup is picked up on the next miss.
    reads.pop(0)
    assert grammar_cache.unavailable_grammar("c_sharp") is None
    assert grammar_cache.unavailable_grammar("rust") is None
    assert grammar_cache.unavailable_grammar("go") == "go"


def test_grammar_status_names_missing_languages(monkeypatch):
    monkeypatch.setattr(grammar_cache, "missing_grammars", lambda: ["csharp"])
    monkeypatch.setattr(grammar_cache, "libs_dir_problems", lambda: [])
    monkeypatch.delenv(grammar_cache.LIBS_DIR_ENV, raising=False)
    status = grammar_cache.grammar_status()
    assert status["state"] == "grammar_unavailable"
    assert status["ready"] is False
    assert status["missing_grammars"] == ["csharp"]
    assert status["languages"] == ["c_sharp"]
    assert "mcp-index grammars prefetch" in status["remediation"]

    monkeypatch.setattr(grammar_cache, "missing_grammars", lambda: [])
    status = grammar_cache.grammar_status()
    assert status["state"] == "ready" and status["remediation"] is None

    monkeypatch.setattr(grammar_cache, "libs_dir_problems", lambda: ["/x: group-writable"])
    assert grammar_cache.grammar_status()["state"] == "grammar_unavailable"


def test_get_status_reports_grammar_state(monkeypatch):
    import asyncio
    import json

    from mcp_server.cli.tool_handlers import handle_get_status
    from mcp_server.dispatcher.simple_dispatcher import SimpleDispatcher

    monkeypatch.setattr(grammar_cache, "missing_grammars", lambda: ["rust"])
    monkeypatch.setattr(grammar_cache, "libs_dir_problems", lambda: [])
    result = asyncio.new_event_loop().run_until_complete(
        handle_get_status(arguments={}, dispatcher=SimpleDispatcher(), repo_resolver=None)
    )
    grammars = json.loads(result[0].text)["grammars"]
    assert grammars["state"] == "grammar_unavailable"
    assert grammars["languages"] == ["rust"]


class _ChunklessRustPlugin:
    """A plugin that, like the real ones, swallows a missing grammar."""

    lang = "rust"
    language = "rust"

    def supports(self, path):
        return str(path).endswith(".rs")

    def bind(self, ctx):
        return None

    def indexFile(self, path, content):
        return {"file": str(path), "symbols": [], "language": "rust"}


def _rust_repo(tmp_path):
    from types import SimpleNamespace

    from mcp_server.core.repo_context import RepoContext
    from mcp_server.storage.sqlite_store import SQLiteStore

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "main.rs").write_text("fn main() {}\n", encoding="utf-8")
    store = SQLiteStore(str(tmp_path / "index.db"))
    ctx = RepoContext(
        repo_id="grammar-repo-0001",
        sqlite_store=store,
        workspace_root=repo,
        tracked_branch="main",
        registry_entry=SimpleNamespace(
            tracked_branch="main", path=repo, name="repo", repository_id="grammar-repo-0001"
        ),
    )
    return repo, store, ctx


def _indexed_paths(store):
    with store._get_connection() as conn:
        return [row[0] for row in conn.execute("SELECT path FROM files").fetchall()]


@pytest.mark.parametrize("available", [False, True])
def test_index_directory_fails_closed_for_an_unavailable_grammar(monkeypatch, tmp_path, available):
    from mcp_server.dispatcher import EnhancedDispatcher

    monkeypatch.setattr(
        grammar_cache,
        "unavailable_grammar",
        lambda language: None if available or language != "rust" else "rust",
    )
    repo, store, ctx = _rust_repo(tmp_path)
    dispatcher = EnhancedDispatcher(
        plugins=[_ChunklessRustPlugin()],
        use_plugin_factory=False,
        enable_advanced_features=False,
        semantic_search_enabled=False,
    )
    stats = dispatcher.index_directory(ctx, repo)
    if available:
        assert stats["indexed_files"] == 1 and stats["failed_files"] == 0
        return
    # A failed file keeps the staged generation from publishing.
    assert stats["indexed_files"] == 0
    assert stats["failed_files"] == 1
    assert any("grammar_unavailable" in error for error in stats["errors"]), stats
    assert _indexed_paths(store) == []


def test_index_file_guarded_fails_closed_for_an_unavailable_grammar(monkeypatch, tmp_path):
    import hashlib

    from mcp_server.dispatcher import EnhancedDispatcher
    from mcp_server.dispatcher.dispatcher_enhanced import IndexResultStatus

    monkeypatch.setattr(grammar_cache, "unavailable_grammar", lambda language: "rust")
    repo, store, ctx = _rust_repo(tmp_path)
    dispatcher = EnhancedDispatcher(
        plugins=[_ChunklessRustPlugin()],
        use_plugin_factory=False,
        enable_advanced_features=False,
        semantic_search_enabled=False,
    )
    path = repo / "main.rs"
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    result = dispatcher.index_file_guarded(ctx, path, digest)
    assert result.status == IndexResultStatus.ERROR
    assert result.error.startswith("grammar_unavailable:")
    assert _indexed_paths(store) == []


def test_generic_plugin_raises_instead_of_swallowing_a_missing_grammar(monkeypatch):
    from mcp_server.plugins import generic_treesitter_plugin as generic

    def _missing(content, language):
        raise RuntimeError("Language 'rust' not found")

    monkeypatch.setattr(generic, "chunk_text", _missing)
    plugin = generic.GenericTreeSitterPlugin(
        LANGUAGE_CONFIGS["rust"], sqlite_store=None, enable_semantic=False
    )
    monkeypatch.setattr(grammar_cache, "unavailable_grammar", lambda language: "rust")
    with pytest.raises(grammar_cache.GrammarUnavailableError, match="grammar_unavailable"):
        plugin.indexFile("main.rs", "fn main() {}\n")
    # A chunker failure with the grammar present keeps the basic fallback.
    monkeypatch.setattr(grammar_cache, "unavailable_grammar", lambda language: None)
    shard = plugin.indexFile("main.rs", "fn main() {}\n")
    assert shard["language"] == "rust"


def test_cold_cache_reindex_fails_closed_end_to_end(tmp_path):
    """Real pack, cold cache, no network: every grammar-backed file fails
    indexing (sandboxed or not) instead of being stored without chunks."""
    import json
    import subprocess
    import sys

    cache = tmp_path / "cold"
    cache.mkdir(mode=0o700)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.c").write_text("int add(int a, int b) { return a + b; }\n")
    (repo / "b.rs").write_text("fn main() {}\n")
    (repo / "c.go").write_text("package main\nfunc main() {}\n")
    script = f"""
import json, logging
from pathlib import Path
from types import SimpleNamespace
logging.disable(logging.CRITICAL)
from mcp_server.core.repo_context import RepoContext
from mcp_server.dispatcher import EnhancedDispatcher
from mcp_server.storage.sqlite_store import SQLiteStore
repo = Path({str(repo)!r})
store = SQLiteStore({str(tmp_path / "index.db")!r})
ctx = RepoContext(repo_id="cold-0001", sqlite_store=store, workspace_root=repo,
    tracked_branch="main", registry_entry=SimpleNamespace(tracked_branch="main",
    path=repo, name="repo", repository_id="cold-0001"))
stats = EnhancedDispatcher(semantic_search_enabled=False).index_directory(ctx, repo)
with store._get_connection() as conn:
    files = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
print(json.dumps({{"indexed": stats["indexed_files"], "failed": stats["failed_files"],
    "errors": stats.get("errors") or [], "files": files}}))
"""
    env = {k: v for k, v in os.environ.items() if not k.startswith("TREE_SITTER_LANGUAGE_PACK_")}
    env.update(
        {
            grammar_cache.CACHE_DIR_ENV: str(cache),
            # Offline: the unknown scheme fails before any socket is created.
            grammar_cache.MANIFEST_URL_ENV: grammar_cache.OFFLINE_MANIFEST_URL,
            "TMPDIR": str(tmp_path),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    result = subprocess.run(
        [sys.executable, "-c", script], env=env, capture_output=True, text=True, timeout=240
    )
    assert result.returncode == 0, result.stderr[-2000:]
    report = json.loads(result.stdout.strip().splitlines()[-1])
    assert report["indexed"] == 0, report
    assert report["failed"] == 3, report
    assert report["files"] == 0, report
    assert all(
        "grammar_unavailable" in error or "not in the local grammar cache" in error
        for error in report["errors"]
    ), report
