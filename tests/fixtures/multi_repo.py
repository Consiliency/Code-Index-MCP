"""Multi-repo test fixtures — IF-0-P5-3.

Provides build_temp_repo() and boot_test_server() for integration tests.

IMPORTANT: Do NOT import mock_file_system from tests/conftest.py here.
That fixture monkey-patches Path.exists() globally and breaks pytest-xdist
parallelism. All repo state is created under tmp_path via subprocess git calls.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# build_temp_repo
# ---------------------------------------------------------------------------


def build_temp_repo(
    tmp_path: Path,
    name: str,
    *,
    seed_files: dict[str, str] | None = None,
) -> tuple[Path, str]:
    """Create a git repo at tmp_path/name with optional seed files committed.

    Returns (repo_path, repo_id). repo_id is computed via compute_repo_id.
    The default branch is always 'main' regardless of the host git config.
    """
    from mcp_server.storage.repo_identity import compute_repo_id

    repo_path = tmp_path / name
    repo_path.mkdir(parents=True, exist_ok=True)

    # Initialize with explicit 'main' branch to avoid host-config variation
    subprocess.run(
        ["git", "init", "-b", "main"],
        cwd=str(repo_path),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=str(repo_path),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=str(repo_path),
        check=True,
        capture_output=True,
    )

    files = {".gitignore": ".mcp-index/\n", **(seed_files or {"placeholder.py": "# placeholder\n"})}
    for filename, content in files.items():
        (repo_path / filename).write_text(content)
        subprocess.run(
            ["git", "add", filename],
            cwd=str(repo_path),
            check=True,
            capture_output=True,
        )

    subprocess.run(
        ["git", "commit", "-m", "initial commit"],
        cwd=str(repo_path),
        check=True,
        capture_output=True,
    )

    repo_id = compute_repo_id(repo_path).repo_id
    return repo_path, repo_id


# ---------------------------------------------------------------------------
# TestServerHandle
# ---------------------------------------------------------------------------


@dataclass
class TestServerHandle:
    """Handle to a running in-process test server."""

    registry: Any
    dispatcher: Any
    repo_resolver: Any
    _gate: Any
    store_registry: Any = field(default=None, repr=False)
    _multi_watcher: Optional[Any] = field(default=None, repr=False)
    _ref_poller: Optional[Any] = field(default=None, repr=False)

    def call_tool(self, name: str, arguments: dict) -> dict:
        """Call a tool synchronously. Returns parsed JSON dict.

        Runs the async handler in a fresh event loop so concurrent threads
        each get an isolated loop (safe for test_concurrent_no_db_lock).
        """
        from mcp_server.cli import tool_handlers

        # HandshakeGate check first
        gate_err = self._gate.check(name, arguments)
        if gate_err is not None:
            return gate_err

        loop = asyncio.new_event_loop()
        try:
            common = dict(
                arguments=arguments,
                dispatcher=self.dispatcher,
                repo_resolver=self.repo_resolver,
            )
            if name == "handshake":
                secret = arguments.get("secret", "")
                if self._gate.verify(secret):
                    return {"authenticated": True}
                return {"error": "Invalid secret.", "code": "handshake_required"}
            elif name == "symbol_lookup":
                coro = tool_handlers.handle_symbol_lookup(**common)
            elif name == "search_code":
                coro = tool_handlers.handle_search_code(**common)
            elif name == "reindex":
                coro = tool_handlers.handle_reindex(**common)
            elif name == "get_status":
                coro = tool_handlers.handle_get_status(**common)
            elif name == "write_summaries":
                coro = tool_handlers.handle_write_summaries(**common)
            elif name == "summarize_sample":
                coro = tool_handlers.handle_summarize_sample(**common)
            else:
                return {"error": "Unknown tool", "tool": name}

            result = loop.run_until_complete(coro)
        finally:
            loop.close()

        if result:
            return json.loads(result[0].text)
        return {}

    def seed_repo_index(self, repo_id: str, repo_path: Path) -> None:
        """Refresh the direct SQLite fixture index and mark HEAD as indexed."""
        _index_repo_into_sqlite(self.store_registry, repo_id, repo_path)
        head = git_head(repo_path)
        branch = git_branch(repo_path)
        self.registry.update_git_state(repo_id)
        self.registry.update_indexed_commit(repo_id, head, branch=branch)

    def stop(self) -> None:
        """Stop all background threads with a 5s bounded join; log-but-swallow on timeout."""
        if self._multi_watcher is not None:
            try:
                self._multi_watcher.stop_watching_all()
            except Exception as exc:
                logger.warning("MultiRepositoryWatcher.stop_watching_all failed: %s", exc)

        if self._ref_poller is not None:
            try:
                # RefPoller.stop() already joins with 5s timeout internally
                self._ref_poller.stop()
            except Exception as exc:
                logger.warning("RefPoller.stop failed: %s", exc)


# ---------------------------------------------------------------------------
# Internal: populate SQLite store from seed files
# ---------------------------------------------------------------------------


def _index_repo_into_sqlite(store_registry: Any, repo_id: str, repo_path: Path) -> None:
    """Walk repo_path and insert Python files into the SQLite store directly.

    Direct SQLite seeding is required because the dispatcher's index path does
    not currently persist to SQLite (plugins are built with sqlite_store=None
    in EnhancedDispatcher). Production indexing via the full plugin pipeline is
    out of scope for SL-3; this helper exists solely to seed test content.
    """
    try:
        store = store_registry.get(repo_id)
    except Exception as exc:
        logger.warning("Could not get sqlite store for %s: %s", repo_id, exc)
        return

    # Create a repository entry in the sqlite store (integer PK used by files table)
    store.create_repository(str(repo_path), repo_path.name)
    with store._get_connection() as conn:
        row = conn.execute(
            "SELECT id FROM repositories WHERE path = ?", (str(repo_path.resolve()),)
        ).fetchone()
    internal_repo_id = (
        int(row[0]) if row else store.create_repository(str(repo_path), repo_path.name)
    )

    with store._get_connection() as conn:
        conn.execute("PRAGMA foreign_keys = OFF")
        file_ids = [
            row[0]
            for row in conn.execute(
                "SELECT id FROM files WHERE repository_id = ?", (internal_repo_id,)
            ).fetchall()
        ]
        if file_ids:
            placeholders = ",".join("?" for _ in file_ids)
            existing_tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
                ).fetchall()
            }
            for table in (
                "symbol_references",
                "imports",
                "embeddings",
                "code_chunks",
            ):
                if table in existing_tables:
                    conn.execute(f"DELETE FROM {table} WHERE file_id IN ({placeholders})", file_ids)
            if "symbol_trigrams" in existing_tables and "symbols" in existing_tables:
                conn.execute(
                    "DELETE FROM symbol_trigrams WHERE symbol_id IN "
                    f"(SELECT id FROM symbols WHERE file_id IN ({placeholders}))",
                    file_ids,
                )
            if "fts_code" in existing_tables:
                conn.execute(f"DELETE FROM fts_code WHERE file_id IN ({placeholders})", file_ids)
            if "symbols" in existing_tables:
                conn.execute(f"DELETE FROM symbols WHERE file_id IN ({placeholders})", file_ids)
            conn.execute(f"DELETE FROM files WHERE id IN ({placeholders})", file_ids)
        conn.execute("PRAGMA foreign_keys = ON")

    for py_file in repo_path.rglob("*.py"):
        if ".mcp-index" in py_file.parts:
            continue
        try:
            content = py_file.read_text(encoding="utf-8")
        except Exception:
            continue

        relative_path = py_file.relative_to(repo_path).as_posix()
        file_id = store.store_file(
            internal_repo_id,
            path=py_file,
            relative_path=relative_path,
            language="python",
            size=len(content.encode()),
        )

        # Store the file content in fts_code for BM25 search
        with store._get_connection() as conn:
            conn.execute(
                "INSERT INTO fts_code (content, file_id) VALUES (?, ?)",
                (content, file_id),
            )

        # Parse and store function/class symbols so symbol_lookup works
        for lineno, line in enumerate(content.splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith("def ") or stripped.startswith("class "):
                kind = "function" if stripped.startswith("def ") else "class"
                name_part = stripped.split("(")[0].split(":")[0]
                sym_name = name_part.split(" ", 1)[-1].strip()
                if sym_name:
                    store.store_symbol(
                        file_id,
                        name=sym_name,
                        kind=kind,
                        line_start=lineno,
                        line_end=lineno,
                        signature=stripped[:120],
                    )


# ---------------------------------------------------------------------------
# boot_test_server
# ---------------------------------------------------------------------------


@contextmanager
def boot_test_server(
    tmp_path: Path,
    repos: list[Path],
    *,
    seed_indexes: bool = True,
    enable_watchers: bool = False,
    poll_interval: float = 2.0,
    client_secret: str | None = None,
    extra_roots: list[Path] | None = None,
):
    """In-process boot via initialize_stateless_services().

    Registers each repo via RepositoryRegistry.register_repository.
    If enable_watchers: start MultiRepositoryWatcher + RefPoller(interval_seconds=poll_interval).
    If client_secret: set MCP_CLIENT_SECRET for this scope and restore on exit.
    Yields TestServerHandle with .call_tool(name, arguments)->dict, .registry,
    .dispatcher, .stop().
    On teardown: .stop_watching_all() + RefPoller.stop() with 5s bounded join;
    log-but-swallow on timeout.
    """
    from mcp_server.cli.bootstrap import initialize_stateless_services
    from mcp_server.cli.handshake import HandshakeGate

    registry_path = tmp_path / "registry.json"

    # Collect all paths that need to be in MCP_ALLOWED_ROOTS
    all_roots = list(repos)
    if extra_roots:
        all_roots.extend(extra_roots)

    allowed_roots_str = ",".join(str(p.resolve()) for p in all_roots)

    # Save and override env vars
    saved_env: dict[str, Optional[str]] = {}
    env_overrides = {
        "MCP_ALLOWED_ROOTS": allowed_roots_str,
        "MCP_REPO_REGISTRY": str(registry_path),
        "SEMANTIC_SEARCH_ENABLED": "false",
    }
    if client_secret is not None:
        env_overrides["MCP_CLIENT_SECRET"] = client_secret

    for key, value in env_overrides.items():
        saved_env[key] = os.environ.get(key)
        os.environ[key] = value

    try:
        store_registry, repo_resolver, dispatcher, repo_registry, git_index_manager = (
            initialize_stateless_services(registry_path=registry_path)
        )

        # Register and optionally seed each repo
        for repo_path in repos:
            repo_id = repo_registry.register_repository(str(repo_path))
            (repo_path / ".mcp-index").mkdir(exist_ok=True)
            repo_registry.update_git_state(repo_id)
            if seed_indexes:
                _index_repo_into_sqlite(store_registry, repo_id, repo_path)
                repo_registry.update_indexed_commit(
                    repo_id,
                    git_head(repo_path),
                    branch=git_branch(repo_path),
                )

        # Build HandshakeGate (reads MCP_CLIENT_SECRET from env, already set above)
        gate = HandshakeGate()

        multi_watcher = None
        ref_poller = None

        if enable_watchers:
            from mcp_server.watcher.ref_poller import RefPoller
            from mcp_server.watcher_multi_repo import MultiRepositoryWatcher

            multi_watcher = MultiRepositoryWatcher(
                registry=repo_registry,
                dispatcher=dispatcher,
                index_manager=git_index_manager,
                repo_resolver=repo_resolver,
            )
            ref_poller = RefPoller(
                registry=repo_registry,
                git_index_manager=git_index_manager,
                dispatcher=dispatcher,
                repo_resolver=repo_resolver,
                interval_seconds=poll_interval,
            )
            multi_watcher.start_watching_all()
            ref_poller.start()

        handle = TestServerHandle(
            registry=repo_registry,
            dispatcher=dispatcher,
            repo_resolver=repo_resolver,
            _gate=gate,
            store_registry=store_registry,
            _multi_watcher=multi_watcher,
            _ref_poller=ref_poller,
        )

        try:
            yield handle
        finally:
            handle.stop()

    finally:
        # Restore env vars
        for key, original in saved_env.items():
            if original is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = original


def git(repo_path: Path, *args: str) -> str:
    """Run git in repo_path and return stdout."""
    result = subprocess.run(
        ["git", *args],
        cwd=str(repo_path),
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def git_head(repo_path: Path) -> str:
    return git(repo_path, "rev-parse", "HEAD")


def git_branch(repo_path: Path) -> str:
    return git(repo_path, "rev-parse", "--abbrev-ref", "HEAD")


@dataclass(frozen=True)
class ProductionRepoFixture:
    path: Path
    repo_id: str
    token: str
    symbol: str

    def write_file(self, relative_path: str, content: str) -> Path:
        target = self.path / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return target

    def commit_all(self, message: str) -> str:
        git(self.path, "add", ".")
        git(self.path, "commit", "-m", message)
        return git_head(self.path)

    def checkout_new_branch(self, branch: str) -> None:
        git(self.path, "checkout", "-b", branch)

    def checkout(self, branch: str) -> None:
        git(self.path, "checkout", branch)

    def rename_file(self, old_relative_path: str, new_relative_path: str, message: str) -> str:
        target = self.path / new_relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        git(self.path, "mv", old_relative_path, new_relative_path)
        git(self.path, "add", new_relative_path)
        git(self.path, "commit", "-m", message)
        return git_head(self.path)

    def delete_file(self, relative_path: str, message: str) -> str:
        target = self.path / relative_path
        if target.exists():
            target.unlink()
        git(self.path, "add", "-A")
        git(self.path, "commit", "-m", message)
        return git_head(self.path)

    def revert_last_commit(self, message: str = "revert last change") -> str:
        git(self.path, "revert", "--no-edit", "HEAD")
        git(self.path, "commit", "--allow-empty", "-m", message)
        return git_head(self.path)


@dataclass(frozen=True)
class LinkedWorktreeFixture:
    source_path: Path
    worktree_path: Path


@dataclass(frozen=True)
class ProductionMatrixFixture:
    alpha: ProductionRepoFixture
    beta: ProductionRepoFixture

    @property
    def repos(self) -> list[Path]:
        return [self.alpha.path, self.beta.path]

    def linked_worktree(self) -> LinkedWorktreeFixture:
        worktree_path = self.alpha.path.parent / f"{self.alpha.path.name}-linked-worktree"
        git(self.alpha.path, "worktree", "add", str(worktree_path), "-b", "p33-linked-worktree")
        return LinkedWorktreeFixture(source_path=self.alpha.path, worktree_path=worktree_path)


def build_production_matrix(tmp_path: Path) -> ProductionMatrixFixture:
    """Build two unrelated git repos with distinct lexical evidence."""
    alpha_symbol = "P33AlphaWidget"
    beta_symbol = "P33BetaWidget"
    alpha_token = "p33_alpha_unique_token"
    beta_token = "p33_beta_unique_token"

    alpha_path, alpha_repo_id = build_temp_repo(
        tmp_path,
        "p33_alpha_repo",
        seed_files={
            "alpha.py": (
                f"class {alpha_symbol}:\n"
                f"    marker = '{alpha_token}'\n\n"
                f"def {alpha_token}():\n"
                f"    return '{alpha_symbol}'\n"
            )
        },
    )
    beta_path, beta_repo_id = build_temp_repo(
        tmp_path,
        "p33_beta_repo",
        seed_files={
            "beta.py": (
                f"class {beta_symbol}:\n"
                f"    marker = '{beta_token}'\n\n"
                f"def {beta_token}():\n"
                f"    return '{beta_symbol}'\n"
            )
        },
    )

    return ProductionMatrixFixture(
        alpha=ProductionRepoFixture(alpha_path, alpha_repo_id, alpha_token, alpha_symbol),
        beta=ProductionRepoFixture(beta_path, beta_repo_id, beta_token, beta_symbol),
    )
