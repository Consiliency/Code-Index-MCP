"""
Real-World Semantic Search Testing for Dormant Features Validation

Tests semantic search capabilities with real codebases to validate dormant features.
Requires SEMANTIC_SEARCH_ENABLED=true and proper Voyage AI + Qdrant configuration.
"""

import asyncio
import json
import os
import tempfile
from pathlib import Path

import pytest

# Skip all tests if semantic search is not enabled
pytestmark = pytest.mark.skipif(
    not os.getenv("SEMANTIC_SEARCH_ENABLED", "false").lower() == "true",
    reason="Semantic search not enabled - set SEMANTIC_SEARCH_ENABLED=true",
)


@pytest.mark.semantic
class TestSemanticSearch:
    """Test semantic search capabilities with real codebases."""

    @pytest.fixture
    def setup_semantic_indexer(self):
        """Setup semantic indexer for testing."""
        if not os.getenv("VOYAGE_API_KEY"):
            pytest.skip("VOYAGE_API_KEY not set")

        try:
            from mcp_server.utils.semantic_indexer import SemanticIndexer
        except ImportError:
            pytest.skip("Semantic indexer dependencies not available")

        # Use in-memory Qdrant for testing
        indexer = SemanticIndexer(collection="test-semantic", qdrant_path=":memory:")
        yield indexer

        # Cleanup
        try:
            if hasattr(indexer, "qdrant") and indexer.qdrant:
                indexer.qdrant.delete_collection("test-semantic")
        except Exception:
            pass

    def test_semantic_code_similarity(self, setup_semantic_indexer):
        """Test semantic similarity search for code patterns."""
        semantic_indexer = setup_semantic_indexer

        # Sample Python code with different functionality
        test_files = {
            "auth.py": '''
def authenticate_user(username: str, password: str) -> bool:
    """Authenticate user with username and password."""
    return verify_credentials(username, password)

def login_user(user_id: int, session_token: str):
    """Log in user and create session."""
    create_user_session(user_id, session_token)
''',
            "http_client.py": '''
import requests

def send_http_request(url: str, method: str = "GET", data=None):
    """Send HTTP request to specified URL."""
    return requests.request(method, url, json=data)

def fetch_api_data(endpoint: str):
    """Fetch data from API endpoint."""
    response = send_http_request(endpoint)
    return response.json()
''',
            "data_processing.py": '''
def process_json_response(response_data: dict) -> dict:
    """Process and clean JSON response data."""
    cleaned_data = {}
    for key, value in response_data.items():
        if value is not None:
            cleaned_data[key.lower()] = value
    return cleaned_data

def transform_data(input_data: list) -> list:
    """Transform input data to required format."""
    return [process_item(item) for item in input_data]
''',
            "session_manager.py": '''
class SessionManager:
    """Manages user sessions and authentication tokens."""
    
    def create_session(self, user_id: int) -> str:
        """Create new user session and return token."""
        token = generate_session_token()
        store_session(user_id, token)
        return token
    
    def validate_session(self, token: str) -> bool:
        """Validate session token."""
        return check_token_validity(token)
''',
        }

        # Index the test files
        indexed_files = []
        for filename, content in test_files.items():
            with tempfile.NamedTemporaryFile(suffix=".py", mode="w", delete=False) as f:
                f.write(content)
                f.flush()
                temp_path = Path(f.name)

                # Index with semantic embeddings
                semantic_indexer.index_file(temp_path)
                indexed_files.append(temp_path)

        # Test natural language queries
        test_queries = [
            {
                "query": "function that sends HTTP requests",
                "expected_matches": ["send_http_request", "fetch_api_data"],
                "min_score": 0.7,
            },
            {
                "query": "class for handling authentication",
                "expected_matches": ["SessionManager", "authenticate_user"],
                "min_score": 0.6,
            },
            {
                "query": "method to process JSON responses",
                "expected_matches": ["process_json_response", "fetch_api_data"],
                "min_score": 0.7,
            },
            {
                "query": "code that manages sessions and cookies",
                "expected_matches": ["SessionManager", "create_session"],
                "min_score": 0.6,
            },
        ]

        for test_case in test_queries:
            query = test_case["query"]
            results = list(semantic_indexer.query(query, limit=5))

            assert len(results) > 0, f"Should find semantic matches for '{query}'"
            assert all(
                result.get("semantic_source") == "semantic" for result in results
            ), f"Semantic path metadata should be preserved for '{query}'"

            # Check that results have reasonable scores
            best_score = max(r.get("score", 0) for r in results)
            assert (
                best_score >= test_case["min_score"]
            ), f"Best semantic score {best_score:.3f} should be >= {test_case['min_score']} for '{query}'"

            print(f"Query: '{query}' -> {len(results)} results, best score: {best_score:.3f}")

            # Verify we found at least some expected matches
            result_content = " ".join(str(r.get("content", "")) for r in results)
            found_matches = [
                match for match in test_case["expected_matches"] if match in result_content
            ]

            assert (
                len(found_matches) > 0
            ), f"Should find at least one expected match {test_case['expected_matches']} for '{query}'"

        # Cleanup temp files
        for temp_path in indexed_files:
            try:
                temp_path.unlink()
            except Exception:
                pass

    @pytest.mark.performance
    def test_semantic_search_performance(self, setup_semantic_indexer, benchmark):
        """Benchmark semantic search performance."""
        semantic_indexer = setup_semantic_indexer

        # Pre-index substantial code sample
        test_code = '''
def authenticate_user(username, password):
    """Authenticate user with credentials."""
    return check_credentials(username, password)

class SessionManager:
    """Manages user sessions and tokens."""
    def create_session(self, user_id):
        return generate_token(user_id)
    
    def invalidate_session(self, token):
        return remove_token(token)

def send_http_request(url, method="GET", headers=None):
    """Send HTTP request to specified URL."""
    import requests
    return requests.request(method, url, headers=headers)

def process_api_response(response):
    """Process API response and extract data."""
    if response.status_code == 200:
        return response.json()
    return None

class DatabaseManager:
    """Handle database operations and connections."""
    def __init__(self, connection_string):
        self.connection = create_connection(connection_string)
    
    def execute_query(self, query, params=None):
        """Execute SQL query with optional parameters."""
        cursor = self.connection.cursor()
        cursor.execute(query, params or [])
        return cursor.fetchall()

def calculate_statistics(data_points):
    """Calculate basic statistics from data points."""
    if not data_points:
        return None
    
    total = sum(data_points)
    count = len(data_points)
    average = total / count
    return {"total": total, "count": count, "average": average}
'''

        # Index the code
        with tempfile.NamedTemporaryFile(suffix=".py", mode="w", delete=False) as f:
            f.write(test_code)
            f.flush()
            semantic_indexer.index_file(Path(f.name))

        def run_semantic_queries():
            queries = [
                "user authentication function",
                "session management class",
                "HTTP request method",
                "database query execution",
                "statistical calculation",
            ]
            results = []
            for query in queries:
                search_results = list(semantic_indexer.query(query, limit=3))
                results.extend(search_results)
            return results

        results = benchmark(run_semantic_queries)

        # Performance assertions
        assert len(results) > 0, "Should find semantic results"
        assert benchmark.stats.mean < 3.0, f"Semantic search too slow: {benchmark.stats.mean:.2f}s"

        # Verify result quality
        avg_score = sum(r.get("score", 0) for r in results) / len(results)
        assert avg_score > 0.5, f"Average semantic score {avg_score:.3f} should be > 0.5"

        print(f"Semantic search: {len(results)} results in {benchmark.stats.mean:.3f}s average")
        print(f"Average semantic score: {avg_score:.3f}")

    def test_semantic_vs_keyword_search_quality(self, setup_semantic_indexer):
        """Compare semantic search quality vs keyword search."""
        semantic_indexer = setup_semantic_indexer

        # Code with varied terminology for similar concepts
        code_samples = {
            "user_auth.py": '''
def verify_user_identity(login_name, secret_key):
    """Verify user identity using login credentials."""
    return validate_login(login_name, secret_key)
''',
            "request_handler.py": '''
def make_web_request(endpoint_url, http_method="GET"):
    """Make web request to API endpoint."""
    return fetch_from_url(endpoint_url, http_method)
''',
            "data_processor.py": '''
def parse_server_response(api_result):
    """Parse response from server API call."""
    return extract_json_data(api_result)
''',
        }

        # Index the samples
        for filename, content in code_samples.items():
            with tempfile.NamedTemporaryFile(suffix=".py", mode="w", delete=False) as f:
                f.write(content)
                f.flush()
                semantic_indexer.index_file(Path(f.name))

        # Test semantic understanding vs exact keyword matching
        semantic_queries = [
            {
                "query": "authenticate user with password",
                "should_find": "verify_user_identity",  # Different words, same concept
                "exact_keywords": ["authenticate", "password"],
            },
            {
                "query": "send HTTP request to API",
                "should_find": "make_web_request",  # Similar concept
                "exact_keywords": ["HTTP", "request"],
            },
        ]

        for test_case in semantic_queries:
            query = test_case["query"]
            results = list(semantic_indexer.query(query, limit=5))

            assert len(results) > 0, f"Should find results for '{query}'"

            # Check if semantic search found the conceptually similar function
            result_text = " ".join(str(r.get("content", "")) for r in results)
            found_target = test_case["should_find"] in result_text

            print(
                f"Semantic query '{query}' found target '{test_case['should_find']}': {found_target}"
            )
            print(f"Results contain: {[r.get('content', '')[:50] + '...' for r in results[:2]]}")

            # Semantic search should find conceptually similar code even with different keywords
            assert (
                found_target
            ), f"Semantic search should find '{test_case['should_find']}' for query '{query}'"

    def test_semantic_search_with_real_repository(self, setup_semantic_indexer):
        """Test semantic search on actual repository if available."""
        semantic_indexer = setup_semantic_indexer

        # Try to use requests repository if available
        repo_path = Path("test_workspace/real_repos/requests")
        if not repo_path.exists():
            pytest.skip("Requests repository not available for semantic testing")

        # Index a subset of Python files from requests
        python_files = list(repo_path.rglob("*.py"))[:10]  # Limit for testing
        indexed_count = 0

        for file_path in python_files:
            try:
                if file_path.stat().st_size < 50000:  # Skip very large files
                    semantic_indexer.index_file(file_path)
                    indexed_count += 1
            except Exception as e:
                print(f"Skipped {file_path}: {e}")

        assert indexed_count >= 3, f"Should index at least 3 files, got {indexed_count}"

        # Test domain-specific semantic queries
        domain_queries = [
            "HTTP session management",
            "SSL certificate verification",
            "request timeout handling",
            "JSON response parsing",
            "authentication headers",
        ]

        for query in domain_queries:
            results = list(semantic_indexer.query(query, limit=3))

            if len(results) > 0:
                best_score = max(r.get("score", 0) for r in results)
                print(
                    f"Domain query '{query}': {len(results)} results, best score: {best_score:.3f}"
                )

                # Should find contextually relevant results in HTTP library
                assert (
                    best_score > 0.5
                ), f"Domain query '{query}' should have reasonable semantic match"

    def test_repo_local_dogfood_queries_stay_on_semantic_path(self, monkeypatch):
        """Use the real semantic query path against the opt-in local dogfood repo."""
        dogfood_repo = os.getenv("CODE_INDEX_DOGFOOD_REPO")
        if not dogfood_repo:
            pytest.skip("CODE_INDEX_DOGFOOD_REPO not set")

        repo_path = Path(dogfood_repo).resolve()
        if not repo_path.exists():
            pytest.skip(f"Dogfood repo not found: {repo_path}")

        monkeypatch.setenv("MCP_ALLOWED_ROOTS", str(repo_path.parent))

        from mcp_server.cli.tool_handlers import handle_search_code
        from mcp_server.core.repo_resolver import RepoResolver
        from mcp_server.dispatcher.dispatcher_enhanced import EnhancedDispatcher
        from mcp_server.storage.repository_registry import RepositoryRegistry
        from mcp_server.storage.store_registry import StoreRegistry

        registry = RepositoryRegistry()
        repo_info = registry.get_repository_by_path(str(repo_path))
        if repo_info is None:
            pytest.skip("Dogfood repo is not registered")

        store_registry = StoreRegistry.for_registry(registry)
        resolver = RepoResolver(registry, store_registry)
        dispatcher = EnhancedDispatcher()

        dogfood_cases = [
            (
                "how does semantic setup validate qdrant and embedding readiness",
                {"mcp_server/setup/semantic_preflight.py", "mcp_server/cli/setup_commands.py"},
            ),
            (
                "where does repository status print semantic readiness evidence",
                {"mcp_server/cli/repository_commands.py"},
            ),
        ]

        for query, expected_hits in dogfood_cases:
            result = asyncio.run(
                handle_search_code(
                    arguments={
                        "query": query,
                        "repository": str(repo_path),
                        "semantic": True,
                        "limit": 5,
                    },
                    dispatcher=dispatcher,
                    repo_resolver=resolver,
                )
            )
            data = json.loads(result[0].text)
            if data.get("code") == "index_unavailable":
                readiness = data["readiness"]
                assert readiness["code"] in {
                    "stale_commit",
                    "missing_index",
                    "index_empty",
                    "index_unavailable",
                }
                evidence = (
                    repo_path / "docs" / "status" / "SEMANTIC_DOGFOOD_REBUILD.md"
                ).read_text(encoding="utf-8")
                assert "SEMAGENTS" in evidence
                assert "README.md" in evidence
                pytest.skip(
                    "Dogfood repo indexed semantic query is unavailable: "
                    f"{readiness['state']} ({readiness['code']})"
                )
            if data.get("code") == "semantic_not_ready":
                readiness = data["semantic_readiness"]
                assert readiness["code"] in {
                    "summaries_missing",
                    "blocked_missing_summaries",
                    "blocked_summary_plateau",
                    "blocked_semantic_batch",
                }
                pytest.skip(
                    "Dogfood repo semantic readiness is blocked: "
                    f"{readiness['state']} ({readiness['code']})"
                )
            assert data["semantic_requested"] is True
            assert data["semantic_source"] == "semantic"
            assert data["semantic_profile_id"] == "oss_high"
            assert data["semantic_collection_name"] == "code_index__oss_high__v1"
            assert data["results"], query
            result_files = {item["file"] for item in data["results"]}
            assert result_files & expected_hits, (query, result_files)
            assert all(item.get("semantic_source") == "semantic" for item in data["results"])

    @pytest.mark.integration
    def test_semantic_indexer_integration_with_plugin_system(self, setup_semantic_indexer):
        """Test semantic indexer integration with the plugin system."""
        semantic_indexer = setup_semantic_indexer

        try:
            from mcp_server.plugin_system import PluginManager
            from mcp_server.storage.sqlite_store import SQLiteStore
        except ImportError:
            pytest.skip("Plugin system components not available")

        # Create temporary database
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as db_file:
            store = SQLiteStore(db_file.name)
            plugin_manager = PluginManager(sqlite_store=store)

            # Create test Python file
            test_code = '''
class APIClient:
    """Client for making API requests."""
    
    def __init__(self, base_url: str):
        self.base_url = base_url
    
    def get_user_data(self, user_id: int) -> dict:
        """Retrieve user data from API."""
        return self.request(f"/users/{user_id}")
    
    def authenticate(self, username: str, password: str) -> str:
        """Authenticate user and return access token."""
        response = self.request("/auth/login", {
            "username": username,
            "password": password
        })
        return response.get("access_token")
'''

            with tempfile.NamedTemporaryFile(suffix=".py", mode="w", delete=False) as f:
                f.write(test_code)
                f.flush()
                temp_path = Path(f.name)

                # Test that semantic indexer can work alongside plugin indexing
                semantic_indexer.index_file(temp_path)

                # Get plugin for Python files
                python_plugin = None
                for (
                    plugin_name,
                    plugin_instance,
                ) in plugin_manager.get_active_plugins().items():
                    if hasattr(plugin_instance, "supports") and plugin_instance.supports(temp_path):
                        python_plugin = plugin_instance
                        break

                if python_plugin:
                    # Index with plugin system
                    plugin_result = python_plugin.indexFile(temp_path, test_code)
                    assert plugin_result, "Plugin should successfully index file"

                    # Test semantic search
                    semantic_results = list(
                        semantic_indexer.query("API authentication method", limit=3)
                    )
                    assert len(semantic_results) > 0, "Semantic search should find results"

                    # Both indexing methods should work together
                    print(f"Plugin indexed: {len(plugin_result.get('symbols', []))} symbols")
                    print(f"Semantic search found: {len(semantic_results)} results")

                # Cleanup
                temp_path.unlink()

    def test_semantic_search_error_handling(self, setup_semantic_indexer):
        """Test error handling in semantic search operations."""
        semantic_indexer = setup_semantic_indexer

        # Test with empty query
        results = list(semantic_indexer.query("", limit=5))
        assert len(results) == 0, "Empty query should return no results"

        # Test with very long query
        long_query = "very " * 1000 + "long query that might cause issues"
        results = list(semantic_indexer.query(long_query, limit=5))
        # Should not crash, may return empty results
        assert isinstance(results, list), "Long query should return list"

        # Test with special characters
        special_query = "query with !@#$%^&*() special characters"
        results = list(semantic_indexer.query(special_query, limit=5))
        assert isinstance(results, list), "Special characters query should return list"

        # Test with non-existent file
        non_existent = Path("/non/existent/file.py")
        try:
            semantic_indexer.index_file(non_existent)
        except FileNotFoundError, OSError:
            pass  # Expected error
        except Exception as e:
            pytest.fail(f"Unexpected error for non-existent file: {e}")


@pytest.mark.semantic
@pytest.mark.slow
class TestSemanticSearchScaling:
    """Test semantic search performance and scaling characteristics."""

    @pytest.fixture
    def setup_large_semantic_index(self):
        """Setup semantic indexer with larger dataset."""
        if not os.getenv("VOYAGE_API_KEY"):
            pytest.skip("VOYAGE_API_KEY not set")

        try:
            from mcp_server.utils.semantic_indexer import SemanticIndexer
        except ImportError:
            pytest.skip("Semantic indexer dependencies not available")

        indexer = SemanticIndexer(collection="test-large-semantic", qdrant_path=":memory:")

        # Index multiple code samples
        code_samples = [
            (
                "auth.py",
                "def login_user(username, password): return authenticate(username, password)",
            ),
            (
                "api.py",
                "def fetch_data(endpoint): return requests.get(endpoint).json()",
            ),
            (
                "db.py",
                "def save_record(table, data): return database.insert(table, data)",
            ),
            (
                "utils.py",
                "def format_date(date_obj): return date_obj.strftime('%Y-%m-%d')",
            ),
            (
                "crypto.py",
                "def hash_password(password): return bcrypt.hashpw(password, salt)",
            ),
            (
                "validation.py",
                "def validate_email(email): return re.match(r'[^@]+@[^@]+', email)",
            ),
            (
                "session.py",
                "def create_session(user_id): return generate_session_token(user_id)",
            ),
            (
                "middleware.py",
                "def check_permissions(user, resource): return user.can_access(resource)",
            ),
            ("cache.py", "def get_cached_value(key): return redis_client.get(key)"),
            (
                "logging.py",
                "def log_error(message, exception): logger.error(message, exc_info=exception)",
            ),
        ]

        for filename, code in code_samples:
            with tempfile.NamedTemporaryFile(suffix=".py", mode="w", delete=False) as f:
                f.write(code)
                f.flush()
                indexer.index_file(Path(f.name))

        yield indexer

        # Cleanup
        try:
            indexer.qdrant.delete_collection("test-large-semantic")
        except Exception:
            pass

    def test_semantic_search_accuracy_with_scale(self, setup_large_semantic_index):
        """Test search accuracy as index size increases."""
        semantic_indexer = setup_large_semantic_index

        # Test various query types
        query_tests = [
            ("user authentication", ["login_user", "authenticate"]),
            ("data retrieval", ["fetch_data", "get"]),
            ("password security", ["hash_password", "bcrypt"]),
            ("session management", ["create_session", "session_token"]),
            ("data validation", ["validate_email", "match"]),
        ]

        for query, expected_terms in query_tests:
            results = list(semantic_indexer.query(query, limit=5))

            assert len(results) > 0, f"Should find results for '{query}'"

            # Check if results contain expected terms
            result_text = " ".join(str(r.get("content", "")) for r in results)
            found_terms = [term for term in expected_terms if term in result_text]

            assert (
                len(found_terms) > 0
            ), f"Query '{query}' should find at least one term from {expected_terms}"

            # Verify result quality
            scores = [r.get("score", 0) for r in results]
            avg_score = sum(scores) / len(scores)
            assert (
                avg_score > 0.4
            ), f"Average score {avg_score:.3f} should be reasonable for '{query}'"

    def test_semantic_search_performance_scaling(self, setup_large_semantic_index, benchmark):
        """Test how semantic search performance scales with queries."""
        semantic_indexer = setup_large_semantic_index

        def run_multiple_semantic_queries():
            queries = [
                "user authentication method",
                "API data fetching",
                "database record saving",
                "password hashing function",
                "email validation logic",
            ]
            all_results = []
            for query in queries:
                results = list(semantic_indexer.query(query, limit=3))
                all_results.extend(results)
            return all_results

        results = benchmark(run_multiple_semantic_queries)

        # Performance requirements
        assert (
            benchmark.stats.mean < 5.0
        ), f"Multiple semantic queries too slow: {benchmark.stats.mean:.2f}s"

        assert len(results) >= 10, f"Should find multiple results across queries: {len(results)}"

        print(f"Semantic scaling test: {len(results)} total results in {benchmark.stats.mean:.3f}s")
