"""
Query result caching with invalidation strategies and performance optimization.
"""

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Awaitable, Callable, Dict, Optional, Set

from .cache_manager import ICacheManager

logger = logging.getLogger(__name__)


class QueryType(Enum):
    """Types of queries that can be cached."""

    SYMBOL_LOOKUP = "symbol_lookup"
    SEARCH = "search"
    SEMANTIC_SEARCH = "semantic_search"
    FILE_SYMBOLS = "file_symbols"
    PROJECT_STATUS = "project_status"


class InvalidationStrategy(Enum):
    """Cache invalidation strategies."""

    TTL_ONLY = "ttl_only"  # Only time-based invalidation
    FILE_BASED = "file_based"  # Invalidate when files change
    TAG_BASED = "tag_based"  # Invalidate by tags
    HYBRID = "hybrid"  # Combine multiple strategies


@dataclass
class QueryCacheConfig:
    """Configuration for query caching."""

    enabled: bool = True
    default_ttl: int = 300  # 5 minutes
    symbol_lookup_ttl: int = 1800  # 30 minutes
    search_ttl: int = 600  # 10 minutes
    semantic_search_ttl: int = 3600  # 1 hour
    file_symbols_ttl: int = 900  # 15 minutes
    status_ttl: int = 60  # 1 minute
    max_query_size: int = 1024  # Max query size to cache
    invalidation_strategy: InvalidationStrategy = InvalidationStrategy.HYBRID
    enable_warming: bool = True
    popular_queries_threshold: int = 5  # Queries accessed N times become popular


@dataclass
class CachedQuery:
    """Represents a cached query with metadata."""

    query_type: QueryType
    query_params: Dict[str, Any]
    result: Any
    created_at: datetime
    expires_at: datetime
    access_count: int = 0
    last_accessed: datetime = None
    file_dependencies: Set[str] = None
    tags: Set[str] = None

    def __post_init__(self):
        if self.last_accessed is None:
            self.last_accessed = self.created_at
        if self.file_dependencies is None:
            self.file_dependencies = set()
        if self.tags is None:
            self.tags = set()

    def is_expired(self) -> bool:
        """Check if query result has expired."""
        return datetime.now() > self.expires_at

    def touch(self) -> None:
        """Update access metadata."""
        self.access_count += 1
        self.last_accessed = datetime.now()


class QueryResultCache:
    """Query result cache with intelligent invalidation."""

    def __init__(self, cache_manager: ICacheManager, config: QueryCacheConfig = None):
        self.cache_manager = cache_manager
        self.config = config or QueryCacheConfig()
        self._query_stats: Dict[str, int] = {}  # Track query frequency
        self._file_query_map: Dict[str, Set[str]] = {}  # File -> Query keys mapping
        self._popular_queries: Set[str] = set()

    def _generate_query_key(self, query_type: QueryType, **params) -> str:
        """Generate a unique cache key for a query."""
        # Create a consistent key from query type and parameters
        key_data = {"type": query_type.value, "params": dict(sorted(params.items()))}

        # Convert to string and hash if too long
        key_str = json.dumps(key_data, sort_keys=True, default=str)
        if len(key_str) > self.config.max_query_size:
            return f"query:{hashlib.sha256(key_str.encode()).hexdigest()}"

        return f"query:{hashlib.md5(key_str.encode(), usedforsecurity=False).hexdigest()}"

    def _get_ttl_for_query_type(self, query_type: QueryType) -> int:
        """Get TTL based on query type."""
        ttl_map = {
            QueryType.SYMBOL_LOOKUP: self.config.symbol_lookup_ttl,
            QueryType.SEARCH: self.config.search_ttl,
            QueryType.SEMANTIC_SEARCH: self.config.semantic_search_ttl,
            QueryType.FILE_SYMBOLS: self.config.file_symbols_ttl,
            QueryType.PROJECT_STATUS: self.config.status_ttl,
        }
        return ttl_map.get(query_type, self.config.default_ttl)

    def _extract_file_dependencies(self, query_type: QueryType, result: Any, **params) -> Set[str]:
        """Extract file dependencies from query result."""
        files = set()

        try:
            if query_type in {QueryType.SYMBOL_LOOKUP, QueryType.SEARCH, QueryType.SEMANTIC_SEARCH}:
                items = result if isinstance(result, list) else [result]
                for item in items:
                    if isinstance(item, dict):
                        path = item.get("defined_in") or item.get("file") or item.get("file_path")
                    else:
                        path = getattr(item, "file_path", None) or getattr(item, "defined_in", None)
                    if path:
                        files.add(str(path))

            elif query_type == QueryType.FILE_SYMBOLS:
                # File symbols query depends on the specific file
                if "file_path" in params:
                    files.add(params["file_path"])

        except Exception as e:
            logger.warning(f"Error extracting file dependencies: {e}")

        return files

    def _generate_cache_tags(self, query_type: QueryType, **params) -> Set[str]:
        """Generate cache tags for a query."""
        tags = {f"query_type:{query_type.value}"}

        # Add parameter-based tags
        if "semantic" in params:
            tags.add(f"semantic:{params['semantic']}")

        if "language" in params:
            tags.add(f"language:{params['language']}")

        if "file_path" in params:
            # Extract file extension for tagging
            file_path = params["file_path"]
            if "." in file_path:
                ext = file_path.split(".")[-1].lower()
                tags.add(f"file_ext:{ext}")

        return tags

    def _track_query_popularity(self, query_key: str):
        """Track query access frequency."""
        self._query_stats[query_key] = self._query_stats.get(query_key, 0) + 1

        # Mark as popular if threshold reached
        if (
            self._query_stats[query_key] >= self.config.popular_queries_threshold
            and query_key not in self._popular_queries
        ):
            self._popular_queries.add(query_key)
            logger.debug(f"Query marked as popular: {query_key}")

    async def get_cached_result(self, query_type: QueryType, **params) -> Optional[Any]:
        """Get cached query result if available."""
        if not self.config.enabled:
            return None

        query_key = self._generate_query_key(query_type, **params)

        try:
            result = await self.cache_manager.get(query_key)
            if result is not None:
                self._track_query_popularity(query_key)
                logger.debug(f"Cache hit for query: {query_type.value}")
                return result

            logger.debug(f"Cache miss for query: {query_type.value}")
            return None

        except Exception as e:
            logger.error(f"Error getting cached result: {e}")
            return None

    async def cache_result(self, query_type: QueryType, result: Any, **params) -> bool:
        """Cache a query result."""
        if not self.config.enabled or result is None:
            return False

        query_key = self._generate_query_key(query_type, **params)
        ttl = self._get_ttl_for_query_type(query_type)

        try:
            # Generate tags and file dependencies
            tags = self._generate_cache_tags(query_type, **params)
            file_deps = self._extract_file_dependencies(query_type, result, **params)

            # Add file dependencies to tags for invalidation
            for file_path in file_deps:
                tags.add(f"file:{file_path}")

            # Track file -> query mapping for invalidation
            for file_path in file_deps:
                if file_path not in self._file_query_map:
                    self._file_query_map[file_path] = set()
                self._file_query_map[file_path].add(query_key)

            # Cache the result
            success = await self.cache_manager.set(query_key, result, ttl, tags)

            if success:
                logger.debug(f"Cached result for query: {query_type.value}")

            return success

        except Exception as e:
            logger.error(f"Error caching result: {e}")
            return False

    async def invalidate_file_queries(self, file_path: str) -> int:
        """Invalidate all queries that depend on a file."""
        if not self.config.enabled:
            return 0

        count = 0

        try:
            # Invalidate by file tag
            file_tag = f"file:{file_path}"
            count += await self.cache_manager.invalidate_by_tags({file_tag})

            # Clean up file mapping
            if file_path in self._file_query_map:
                del self._file_query_map[file_path]

            if count > 0:
                logger.debug(f"Invalidated {count} queries for file: {file_path}")

        except Exception as e:
            logger.error(f"Error invalidating file queries: {e}")

        return count

    async def invalidate_by_query_type(self, query_type: QueryType) -> int:
        """Invalidate all queries of a specific type."""
        if not self.config.enabled:
            return 0

        try:
            tag = f"query_type:{query_type.value}"
            count = await self.cache_manager.invalidate_by_tags({tag})

            if count > 0:
                logger.debug(f"Invalidated {count} queries of type: {query_type.value}")

            return count

        except Exception as e:
            logger.error(f"Error invalidating query type: {e}")
            return 0

    async def invalidate_semantic_queries(self) -> int:
        """Invalidate all semantic search queries."""
        if not self.config.enabled:
            return 0

        try:
            tag = "semantic:True"
            count = await self.cache_manager.invalidate_by_tags({tag})

            if count > 0:
                logger.debug(f"Invalidated {count} semantic queries")

            return count

        except Exception as e:
            logger.error(f"Error invalidating semantic queries: {e}")
            return 0

    async def warm_popular_queries(
        self, query_factory: Callable[[str, QueryType], Awaitable[Any]]
    ) -> int:
        """Warm cache with popular queries."""
        if not self.config.enable_warming or not self._popular_queries:
            return 0

        count = 0

        for query_key in self._popular_queries:
            try:
                # Check if still cached
                if await self.cache_manager.exists(query_key):
                    continue

                # Would need to decode query_key to regenerate result
                # This is a simplified implementation
                logger.debug(f"Would warm popular query: {query_key}")
                count += 1

            except Exception as e:
                logger.warning(f"Error warming query {query_key}: {e}")

        return count

    async def get_cache_stats(self) -> Dict[str, Any]:
        """Get query cache statistics."""
        try:
            cache_metrics = await self.cache_manager.get_metrics()

            return {
                "enabled": self.config.enabled,
                "total_queries_tracked": len(self._query_stats),
                "popular_queries": len(self._popular_queries),
                "file_dependencies": len(self._file_query_map),
                "cache_metrics": {
                    "hits": cache_metrics.hits,
                    "misses": cache_metrics.misses,
                    "hit_rate": cache_metrics.hit_rate,
                    "entries": cache_metrics.entries_count,
                    "memory_usage_mb": cache_metrics.memory_usage_mb,
                },
                "query_frequency": dict(
                    sorted(self._query_stats.items(), key=lambda x: x[1], reverse=True)[:10]
                ),  # Top 10 most frequent queries
            }

        except Exception as e:
            logger.error(f"Error getting cache stats: {e}")
            return {"error": str(e)}

    async def clear_all(self) -> int:
        """Clear all cached queries."""
        try:
            count = await self.cache_manager.clear()
            self._query_stats.clear()
            self._file_query_map.clear()
            self._popular_queries.clear()
            return count
        except Exception as e:
            logger.error(f"Error clearing cache: {e}")
            return 0


class QueryCacheDecorator:
    """Decorator for automatic query caching."""

    def __init__(self, query_cache: QueryResultCache, query_type: QueryType):
        self.query_cache = query_cache
        self.query_type = query_type

    def __call__(self, func: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        async def wrapper(*args, **kwargs):
            # Try to get from cache first
            cached_result = await self.query_cache.get_cached_result(self.query_type, **kwargs)

            if cached_result is not None:
                return cached_result

            # Execute original function
            result = await func(*args, **kwargs)

            # Cache the result
            await self.query_cache.cache_result(self.query_type, result, **kwargs)

            return result

        return wrapper


def cache_symbol_lookup(query_cache: QueryResultCache):
    """Decorator for caching symbol lookup results."""
    return QueryCacheDecorator(query_cache, QueryType.SYMBOL_LOOKUP)


def cache_search(query_cache: QueryResultCache):
    """Decorator for caching search results."""
    return QueryCacheDecorator(query_cache, QueryType.SEARCH)


def cache_semantic_search(query_cache: QueryResultCache):
    """Decorator for caching semantic search results."""
    return QueryCacheDecorator(query_cache, QueryType.SEMANTIC_SEARCH)


def cache_file_symbols(query_cache: QueryResultCache):
    """Decorator for caching file symbols results."""
    return QueryCacheDecorator(query_cache, QueryType.FILE_SYMBOLS)


def cache_project_status(query_cache: QueryResultCache):
    """Decorator for caching project status results."""
    return QueryCacheDecorator(query_cache, QueryType.PROJECT_STATUS)
