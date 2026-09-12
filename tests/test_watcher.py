"""
Comprehensive tests for the File Watcher component.

Tests cover:
- File system event handling
- Automatic re-indexing
- File type filtering
- Event type handling (create, modify, delete, move)
- Error handling
- Performance under load
"""

import os
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock

import pytest
from watchdog.events import (
    DirCreatedEvent,
    DirModifiedEvent,
    FileCreatedEvent,
    FileModifiedEvent,
    FileMovedEvent,
)

from mcp_server.watcher import FileWatcher, _Handler
from tests.test_v13_data_storage import runtime


@contextmanager
def measure_time(_test_name, _benchmark_results):
    yield


@pytest.fixture
def dispatcher_with_mock():
    dispatcher = Mock()
    dispatcher.index_file = Mock()
    dispatcher.remove_file = Mock()
    dispatcher.move_file = Mock()
    return dispatcher


class TestHandlerEventHandling:
    """Test the _Handler class event handling."""

    def test_handler_init(self, dispatcher_with_mock):
        """Test handler initialization."""
        handler = _Handler(dispatcher_with_mock)

        assert handler.dispatcher == dispatcher_with_mock
        for extension in {".py", ".js", ".c", ".cpp", ".dart", ".html", ".css"}:
            assert extension in handler.code_extensions

    def test_trigger_reindex_supported_file(self, dispatcher_with_mock):
        """Test triggering reindex for supported file."""
        handler = _Handler(dispatcher_with_mock)

        test_path = Path("/test/file.py")
        handler.trigger_reindex(test_path)

        dispatcher_with_mock.remove_file.assert_called_once_with(test_path)
        dispatcher_with_mock.index_file.assert_called_once_with(test_path)
        # remove_file must precede index_file so stale chunks are cleared first
        names = [c[0] for c in dispatcher_with_mock.method_calls]
        assert names.index("remove_file") < names.index("index_file")

    def test_trigger_reindex_uses_ctx_aware_dispatcher_shape_when_guarded_method_exists(
        self, tmp_path
    ):
        class GuardedDispatcher:
            def __init__(self):
                self.calls = []

            def remove_file(self, ctx, path):
                self.calls.append(("remove", ctx, path))

            def index_file_guarded(self, ctx, path, observed_hash):
                self.calls.append(("guarded", ctx, path, observed_hash))

        dispatcher = GuardedDispatcher()
        handler = _Handler(dispatcher, ctx=FileWatcher(tmp_path, dispatcher)._handler.ctx)

        test_path = tmp_path / "guarded.py"
        test_path.write_text("print('hello')\n")
        handler.trigger_reindex(test_path)

        assert dispatcher.calls[0][0] == "remove"
        assert dispatcher.calls[1][0] == "guarded"
        assert dispatcher.calls[0][1] is handler.ctx
        assert dispatcher.calls[1][1] is handler.ctx

    def test_trigger_reindex_unsupported_file(self, dispatcher_with_mock):
        """Test triggering reindex for unsupported file."""
        handler = _Handler(dispatcher_with_mock)

        test_path = Path("/test/file.png")
        handler.trigger_reindex(test_path)

        # Should not index unsupported file
        dispatcher_with_mock.index_file.assert_not_called()

    def test_trigger_reindex_error_handling(self, dispatcher_with_mock):
        """Test error handling during reindex."""
        handler = _Handler(dispatcher_with_mock)
        dispatcher_with_mock.index_file.side_effect = Exception("Index error")

        test_path = Path("/test/file.py")
        # Should not raise, just log error
        handler.trigger_reindex(test_path)

        dispatcher_with_mock.remove_file.assert_called_once_with(test_path)
        dispatcher_with_mock.index_file.assert_called_once()

    def test_on_created_event(self, dispatcher_with_mock, tmp_path):
        """Test handling file creation events."""
        handler = _Handler(dispatcher_with_mock)

        # Create a Python file
        test_file = tmp_path / "new_file.py"
        test_file.write_text("print('hello')")

        event = FileCreatedEvent(str(test_file))
        handler.on_any_event(event)
        handler.flush()

        dispatcher_with_mock.index_file.assert_called_once()
        call_path = dispatcher_with_mock.index_file.call_args[0][0]
        assert str(call_path) == str(test_file)

    def test_on_modified_event(self, dispatcher_with_mock, tmp_path):
        """Test handling file modification events."""
        handler = _Handler(dispatcher_with_mock)

        test_file = tmp_path / "existing.py"
        test_file.write_text("# original content")

        event = FileModifiedEvent(str(test_file))
        handler.on_any_event(event)
        handler.flush()

        dispatcher_with_mock.index_file.assert_called_once()

    def test_on_moved_event(self, dispatcher_with_mock, tmp_path):
        """Test handling file move events."""
        handler = _Handler(dispatcher_with_mock)

        old_file = tmp_path / "old.py"
        new_file = tmp_path / "new.py"
        old_file.write_text("content")
        new_file.write_text("content")

        event = FileMovedEvent(str(old_file), str(new_file))
        handler.on_any_event(event)
        handler.flush()

        dispatcher_with_mock.move_file.assert_called_once()
        call_args = dispatcher_with_mock.move_file.call_args[0]
        assert str(call_args[0]) == str(old_file)
        assert str(call_args[1]) == str(new_file)

    def test_ignore_directory_events(self, dispatcher_with_mock, tmp_path):
        """Test that directory events are ignored."""
        handler = _Handler(dispatcher_with_mock)

        # Directory events should be ignored
        event = DirCreatedEvent(str(tmp_path / "new_dir"))
        handler.on_any_event(event)

        event = DirModifiedEvent(str(tmp_path / "new_dir"))
        handler.on_any_event(event)

        dispatcher_with_mock.index_file.assert_not_called()

    def test_ignore_non_code_files(self, dispatcher_with_mock, tmp_path):
        """Test that non-code files are ignored."""
        handler = _Handler(dispatcher_with_mock)

        # Test various non-code file types
        non_code_files = [
            "image.png",
            ".gitignore",
            "Makefile",
        ]

        for filename in non_code_files:
            test_file = tmp_path / filename
            test_file.write_text("content")

            event = FileCreatedEvent(str(test_file))
            handler.on_any_event(event)

        # None should trigger indexing
        dispatcher_with_mock.index_file.assert_not_called()

    def test_handle_all_supported_extensions(self, dispatcher_with_mock, tmp_path):
        """Test handling of all supported file extensions."""
        handler = _Handler(dispatcher_with_mock)

        supported_files = [
            ("test.py", "print('python')"),
            ("test.js", "console.log('javascript');"),
            ("test.c", "int main() { return 0; }"),
            ("test.cpp", "#include <iostream>"),
            ("test.dart", "void main() {}"),
            ("test.html", "<html></html>"),
            ("test.css", "body { margin: 0; }"),
        ]

        for filename, content in supported_files:
            test_file = tmp_path / filename
            test_file.write_text(content)

            event = FileCreatedEvent(str(test_file))
            handler.on_any_event(event)

        # Force drain before asserting
        handler.flush()

        # All should trigger indexing
        assert dispatcher_with_mock.index_file.call_count == len(supported_files)

    def test_file_not_exists_handling(self, dispatcher_with_mock):
        """Test handling events for non-existent files."""
        handler = _Handler(dispatcher_with_mock)

        # Event for non-existent file
        event = FileModifiedEvent("/nonexistent/file.py")
        handler.on_any_event(event)

        # Should not attempt to index
        dispatcher_with_mock.index_file.assert_not_called()


class TestFileWatcher:
    """Test the FileWatcher class."""

    def test_file_watcher_init(self, temp_code_directory, dispatcher_with_mock):
        """Test FileWatcher initialization."""
        watcher = FileWatcher(temp_code_directory, dispatcher_with_mock)

        assert watcher._observer is not None
        assert not watcher._observer.is_alive()  # Not started yet

    def test_file_watcher_start_stop(self, temp_code_directory, dispatcher_with_mock):
        """Test starting and stopping the watcher."""
        watcher = FileWatcher(temp_code_directory, dispatcher_with_mock)

        # Start watcher
        watcher.start()
        assert watcher._observer.is_alive()

        # Stop watcher
        watcher.stop()
        time.sleep(0.1)  # Give it time to stop
        assert not watcher._observer.is_alive()

    def test_file_watcher_recursive(self, temp_code_directory, dispatcher_with_mock):
        """Test that watcher monitors subdirectories recursively."""
        watcher = FileWatcher(temp_code_directory, dispatcher_with_mock)
        watcher.start()

        try:
            # Create file in subdirectory
            subdir = temp_code_directory / "subdir"
            subdir.mkdir()

            test_file = subdir / "test.py"
            test_file.write_text("print('test')")

            # Give watcher + debounce time to process (debounce=0.5s + drain tick overhead)
            time.sleep(1.5)

            # Should have indexed the file
            dispatcher_with_mock.index_file.assert_called()

            # Check the path matches
            indexed_paths = [call[0][0] for call in dispatcher_with_mock.index_file.call_args_list]
            assert any(str(test_file) in str(path) for path in indexed_paths)

        finally:
            watcher.stop()

    @pytest.mark.slow
    def test_file_watcher_real_events(self, tmp_path, dispatcher_with_mock):
        """Test watcher with real file system events."""
        # Create a clean directory for this test
        watch_dir = tmp_path / "watch_test"
        watch_dir.mkdir()

        watcher = FileWatcher(watch_dir, dispatcher_with_mock)
        watcher.start()

        try:
            # Test 1: Create a new Python file
            py_file = watch_dir / "test.py"
            py_file.write_text("def hello(): pass")
            time.sleep(0.5)

            # Test 2: Modify the file
            py_file.write_text("def hello(): print('world')")
            time.sleep(0.5)

            # Test 3: Create a JavaScript file
            js_file = watch_dir / "app.js"
            js_file.write_text("function test() {}")
            time.sleep(0.5)

            # Test 4: Create a non-code file (should be ignored)
            txt_file = watch_dir / "image.png"
            txt_file.write_text("This should not trigger indexing")
            time.sleep(0.5)

            # Test 5: Move/rename a file
            new_py_file = watch_dir / "renamed.py"
            py_file.rename(new_py_file)
            time.sleep(0.5)
            watcher._handler.flush()

            # Verify indexing was called for code files
            assert dispatcher_with_mock.index_file.call_count >= 2
            assert dispatcher_with_mock.move_file.call_count >= 1

            # Verify correct files were indexed
            indexed_paths = [
                str(call[0][0]) for call in dispatcher_with_mock.index_file.call_args_list
            ]

            # Should have indexed Python and JS files
            assert any("test.py" in path or "renamed.py" in path for path in indexed_paths)
            assert any("app.js" in path for path in indexed_paths)

            # Should not have indexed png file
            assert not any("image.png" in path for path in indexed_paths)

        finally:
            watcher.stop()

    def test_multiple_watchers(self, tmp_path, dispatcher_with_mock):
        """Test multiple watchers on different directories."""
        dir1 = tmp_path / "dir1"
        dir2 = tmp_path / "dir2"
        dir1.mkdir()
        dir2.mkdir()

        watcher1 = FileWatcher(dir1, dispatcher_with_mock)
        watcher2 = FileWatcher(dir2, dispatcher_with_mock)

        watcher1.start()
        watcher2.start()

        try:
            # Create files in both directories
            (dir1 / "file1.py").write_text("# file 1")
            (dir2 / "file2.py").write_text("# file 2")

            # Allow debounce (0.5s) + drain tick overhead
            time.sleep(1.5)

            # Both files should be indexed
            assert dispatcher_with_mock.index_file.call_count >= 2

        finally:
            watcher1.stop()
            watcher2.stop()


class TestEdgeCases:
    """Test edge cases and error conditions."""

    @pytest.mark.skipif(
        sys.platform == "darwin",
        reason="FSEventsObserver on macOS silently ignores nonexistent directories",
    )
    def test_watcher_on_nonexistent_directory(self, dispatcher_with_mock):
        """Test creating watcher on non-existent directory."""
        nonexistent = Path("/this/does/not/exist")

        # Should raise error when trying to start
        watcher = FileWatcher(nonexistent, dispatcher_with_mock)

        with pytest.raises(Exception):
            watcher.start()

    def test_watcher_permission_denied(self, tmp_path, dispatcher_with_mock):
        """Test watcher behavior with permission issues."""
        if not hasattr(os, "chmod"):
            pytest.skip("chmod not available on this platform")

        restricted_dir = tmp_path / "restricted"
        restricted_dir.mkdir()

        # Start watcher first
        watcher = FileWatcher(restricted_dir, dispatcher_with_mock)
        watcher.start()

        try:
            # Create a file
            test_file = restricted_dir / "test.py"
            test_file.write_text("print('test')")

            # Remove read permissions
            os.chmod(test_file, 0o000)

            # Trigger modification event
            time.sleep(0.1)

            # Try to trigger reindex by touching the file
            # This might fail due to permissions, which is expected
            try:
                test_file.touch()
            except Exception:
                pass

            time.sleep(0.5)

            # Restore permissions for cleanup
            os.chmod(test_file, 0o644)

        finally:
            watcher.stop()

    def test_rapid_file_changes(self, tmp_path, dispatcher_with_mock):
        """Test handling rapid successive file changes."""
        watcher = FileWatcher(tmp_path, dispatcher_with_mock)
        watcher.start()

        try:
            test_file = tmp_path / "rapid.py"

            # Rapidly create and modify file
            for i in range(10):
                test_file.write_text(f"# Version {i}")
                time.sleep(0.05)  # Very short delay

            # Give watcher time to process all events
            time.sleep(1)

            # Should have indexed the file multiple times
            assert dispatcher_with_mock.index_file.call_count >= 1

            # Last call should be with final content
            if dispatcher_with_mock.index_file.call_count > 0:
                last_call_path = dispatcher_with_mock.index_file.call_args_list[-1][0][0]
                assert str(last_call_path) == str(test_file)

        finally:
            watcher.stop()

    def test_symlink_handling(self, tmp_path, dispatcher_with_mock):
        """Test handling of symbolic links."""
        if not hasattr(os, "symlink"):
            pytest.skip("Symbolic links not supported on this platform")

        watcher = FileWatcher(tmp_path, dispatcher_with_mock)
        watcher.start()

        try:
            # Create actual file
            actual_file = tmp_path / "actual.py"
            actual_file.write_text("print('actual')")

            # Create symlink
            link_file = tmp_path / "link.py"
            link_file.symlink_to(actual_file)

            # Allow debounce + drain
            time.sleep(1.5)

            # Both actual file and symlink might trigger events
            assert dispatcher_with_mock.index_file.call_count >= 1

        finally:
            watcher.stop()


class TestConcurrency:
    """Test concurrent file operations."""

    def test_concurrent_file_creation(self, tmp_path, dispatcher_with_mock):
        """Test handling concurrent file creation."""
        watcher = FileWatcher(tmp_path, dispatcher_with_mock)
        watcher.start()

        try:

            def create_files(start_idx):
                for i in range(start_idx, start_idx + 5):
                    file_path = tmp_path / f"concurrent_{i}.py"
                    file_path.write_text(f"# File {i}")
                    time.sleep(0.01)

            # Create files from multiple threads
            threads = []
            for i in range(0, 20, 5):
                thread = threading.Thread(target=create_files, args=(i,))
                threads.append(thread)
                thread.start()

            # Wait for all threads
            for thread in threads:
                thread.join()

            # Give watcher time to process
            time.sleep(1)

            # Should have indexed all files
            assert dispatcher_with_mock.index_file.call_count >= 20

        finally:
            watcher.stop()

    def test_watch_during_bulk_operations(self, tmp_path, dispatcher_with_mock):
        """Test watcher during bulk file operations."""
        # Pre-create many files
        for i in range(50):
            (tmp_path / f"existing_{i}.py").write_text(f"# File {i}")

        # Start watcher after files exist
        watcher = FileWatcher(tmp_path, dispatcher_with_mock)
        watcher.start()

        try:
            # Modify all files
            for i in range(50):
                file_path = tmp_path / f"existing_{i}.py"
                file_path.write_text(f"# Modified file {i}")

            # Create new files
            for i in range(50, 60):
                (tmp_path / f"new_{i}.py").write_text(f"# New file {i}")

            # Delete some files
            for i in range(0, 10):
                (tmp_path / f"existing_{i}.py").unlink()

            # Give watcher time to process
            time.sleep(2)

            # Should have processed many events
            assert dispatcher_with_mock.index_file.call_count > 0

        finally:
            watcher.stop()


class TestPerformance:
    """Performance tests for file watcher."""

    @pytest.mark.benchmark
    @pytest.mark.slow
    def test_large_directory_performance(self, tmp_path, dispatcher_with_mock, benchmark_results):
        """Test watcher performance with large directory."""
        # Create a large directory structure
        for i in range(10):
            subdir = tmp_path / f"subdir_{i}"
            subdir.mkdir()
            for j in range(10):
                (subdir / f"file_{j}.py").write_text(f"# File {i}-{j}")

        with measure_time("watcher_startup_large_dir", benchmark_results):
            watcher = FileWatcher(tmp_path, dispatcher_with_mock)
            watcher.start()

        try:
            # Test event processing performance
            with measure_time("watcher_bulk_modify", benchmark_results):
                # Modify multiple files
                for i in range(5):
                    for j in range(5):
                        file_path = tmp_path / f"subdir_{i}" / f"file_{j}.py"
                        file_path.write_text(f"# Modified {i}-{j}")

                # Wait for processing
                time.sleep(1)

        finally:
            watcher.stop()

    @pytest.mark.benchmark
    def test_event_processing_speed(self, tmp_path, dispatcher_with_mock, benchmark_results):
        """Test speed of event processing."""
        # Mock fast indexing
        dispatcher_with_mock.index_file.return_value = None

        watcher = FileWatcher(tmp_path, dispatcher_with_mock)
        watcher.start()

        try:
            with measure_time("watcher_event_processing", benchmark_results):
                # Create many files rapidly
                for i in range(100):
                    (tmp_path / f"perf_test_{i}.py").write_text(f"# {i}")

                # Wait for all events to be processed
                time.sleep(2)

            # Verify all were processed
            assert dispatcher_with_mock.index_file.call_count >= 100

        finally:
            watcher.stop()


class TestReindexIntegration:
    """Committed watcher updates publish fresh SQLite generations."""

    @staticmethod
    def _commit(repo):
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-qm", "Synthetic watcher fixture"], cwd=repo, check=True)

    @pytest.mark.integration
    def test_file_modification_updates_line_numbers(self, runtime):
        repo, _registry, repo_id, _original, manager = runtime
        path = repo / "mod_test.py"
        path.write_text("def foo():\n    pass\n")
        self._commit(repo)
        assert manager.rebuild_repository_index(repo_id).action == "full_index"
        handler = _Handler(
            manager.dispatcher, ctx=manager._resolve_ctx(repo_id), index_manager=manager
        )
        try:
            store = handler.ctx.sqlite_store
            file_id = store.get_file_id_by_path(path.name)
            assert any(c["line_start"] == 1 for c in store.get_chunks_for_file(file_id))
            path.write_text("\n\ndef foo():\n    pass\n")
            self._commit(repo)
            handler.trigger_reindex(path)
            store = manager._resolve_ctx(repo_id).sqlite_store
            chunks = store.get_chunks_for_file(store.get_file_id_by_path(path.name))
            assert not any(c["line_start"] == 1 for c in chunks)
            assert any(c["line_start"] == 3 for c in chunks)
        finally:
            handler.stop()

    @pytest.mark.integration
    def test_file_deletion_clears_all_index_rows(self, runtime):
        repo, _registry, repo_id, _original, manager = runtime
        path = repo / "del_test.py"
        path.write_text("def bar():\n    return 1\n")
        self._commit(repo)
        assert manager.rebuild_repository_index(repo_id).action == "full_index"
        handler = _Handler(
            manager.dispatcher, ctx=manager._resolve_ctx(repo_id), index_manager=manager
        )
        try:
            store = handler.ctx.sqlite_store
            file_id = store.get_file_id_by_path(path.name)
            assert store.get_chunks_for_file(file_id)
            path.unlink()
            self._commit(repo)
            handler.remove_file_from_index(path)
            store = manager._resolve_ctx(repo_id).sqlite_store
            assert store.get_file_id_by_path(path.name) is None
            assert not any(
                (r.get("filepath") or r.get("relative_path") or "").endswith(path.name)
                for r in store.search_bm25("bar")
            )
        finally:
            handler.stop()

    @pytest.mark.integration
    def test_file_creation_populates_correct_line_numbers(self, runtime):
        repo, _registry, repo_id, _original, manager = runtime
        path = repo / "create_test.py"
        path.write_text("# header\n\ndef baz():\n    pass\n")
        self._commit(repo)
        assert manager.rebuild_repository_index(repo_id).action == "full_index"
        store = manager._resolve_ctx(repo_id).sqlite_store
        chunks = store.get_chunks_for_file(store.get_file_id_by_path(path.name))
        assert chunks and any(c["line_start"] <= 3 <= c["line_end"] for c in chunks)
        assert any(
            (r.get("filepath") or r.get("relative_path") or "").endswith(path.name)
            for r in store.search_bm25("baz")
        )


class TestIntegration:
    @pytest.mark.integration
    def test_watcher_with_real_dispatcher(self, runtime):
        repo, registry, repo_id, _original, manager = runtime
        assert manager.rebuild_repository_index(repo_id).action == "full_index"
        before = registry.get(repo_id).index_generation
        watcher = FileWatcher(
            repo,
            manager.dispatcher,
            ctx=manager._resolve_ctx(repo_id),
            index_manager=manager,
        )
        watcher.start()
        try:
            path = repo / "integration_test.py"
            path.write_text(
                "def integration_function():\n    return 1\n\nclass IntegrationClass:\n    pass\n"
            )
            TestReindexIntegration._commit(repo)
            deadline = time.monotonic() + 10
            while registry.get(repo_id).index_generation == before and time.monotonic() < deadline:
                time.sleep(0.05)
            assert registry.get(repo_id).index_generation != before
            ctx = manager._resolve_ctx(repo_id)
            assert manager.dispatcher.lookup(ctx, "integration_function")["kind"] == "function"
            assert manager.dispatcher.lookup(ctx, "IntegrationClass")["kind"] == "class"
        finally:
            watcher.stop()
