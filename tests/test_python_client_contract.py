from unittest.mock import MagicMock

import pytest

from mcp_server import (
    ClientReindexResult,
    ClientSearchOptions,
    ClientSearchResult,
    ClientStatusResult,
    ClientSymbolResult,
    IndexItClient,
    IndexUnavailable,
    open_client,
)
from mcp_server.client import execute_search_service
from mcp_server.core.repo_resolver import RepoResolver
from mcp_server.health.repository_readiness import (
    RepositoryReadiness,
    RepositoryReadinessState,
)


def test_public_client_exports_are_available_from_mcp_server():
    assert open_client is not None
    assert IndexItClient is not None
    assert ClientSearchOptions is not None
    assert ClientSearchResult is not None
    assert ClientSymbolResult is not None
    assert ClientReindexResult is not None
    assert ClientStatusResult is not None
    assert IndexUnavailable is not None


def test_client_close_retires_only_services_it_bootstrapped(monkeypatch):
    stores, resolver, dispatcher = MagicMock(), MagicMock(), MagicMock()
    monkeypatch.setattr(
        "mcp_server.client.initialize_stateless_services",
        lambda **kwargs: (stores, resolver, dispatcher, MagicMock(), MagicMock()),
    )
    client = IndexItClient()
    assert client.dispatcher is dispatcher
    client.close()
    client.close()
    dispatcher.shutdown.assert_called_once()
    stores.shutdown.assert_called_once()
    borrowed = IndexItClient()
    borrowed._dispatcher, borrowed._repo_resolver = dispatcher, resolver
    borrowed.close()
    dispatcher.shutdown.assert_called_once()


def test_failed_client_close_retains_owner_for_retry(monkeypatch):
    stores, resolver, dispatcher = MagicMock(), MagicMock(), MagicMock()
    monkeypatch.setattr(
        "mcp_server.client.initialize_stateless_services",
        lambda **kwargs: (stores, resolver, dispatcher, MagicMock(), MagicMock()),
    )
    client = IndexItClient()
    assert client.dispatcher is dispatcher
    dispatcher.shutdown.side_effect = RuntimeError("borrower retirement failed")
    with pytest.raises(RuntimeError):
        client.close()
    assert client._dispatcher is dispatcher
    dispatcher.shutdown.side_effect = None
    client.close()
    assert client._dispatcher is None


def test_client_search_options_freeze_source_filters():
    options = ClientSearchOptions(
        query="todo",
        repository="/repo",
        source_type="friction",  # type: ignore[arg-type]
        friction_categories=("todo",),
        history_labels=("reflection", "reflection"),
        history_repos=("owner/repo", "owner/repo"),
        include_source_metadata=True,
    )

    assert options.source_type is not None and options.source_type.value == "friction"
    assert options.friction_categories == ("todo",)
    assert options.history_labels == ("reflection",)
    assert options.history_repos == ("owner/repo",)
    assert options.include_source_metadata is True


def test_invalid_source_type_fails_before_dispatch():
    try:
        ClientSearchOptions(query="demo", source_type="unknown")  # type: ignore[arg-type]
    except ValueError as exc:
        assert "'unknown'" in str(exc)
    else:
        raise AssertionError("expected invalid source type to fail")


def test_distribution_identity_and_client_import_path_stay_separate():
    import importlib.metadata

    assert importlib.metadata.metadata("index-it-mcp")["Name"] == "index-it-mcp"
    assert IndexItClient.__module__ == "mcp_server.client"


def test_search_ready_transition_does_not_open_store_or_dispatch():
    resolver = object.__new__(RepoResolver)
    resolver._registry = None
    resolver._store_registry = MagicMock()
    resolver.classify = MagicMock(
        side_effect=[
            RepositoryReadiness(
                state=RepositoryReadinessState.READY,
                repository_id="repo",
            ),
            RepositoryReadiness(
                state=RepositoryReadinessState.STALE_COMMIT,
                repository_id="repo",
            ),
        ]
    )
    dispatcher = MagicMock()

    result = execute_search_service(
        dispatcher=dispatcher,
        repo_resolver=resolver,
        options=ClientSearchOptions(query="demo", repository="repo"),
    )
    assert result.index_unavailable is not None
    assert result.safe_fallback == "native_search"

    resolver._store_registry.get.assert_not_called()
    dispatcher.search.assert_not_called()
