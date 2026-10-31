#!/usr/bin/env python3
"""Debug semantic search configuration and connectivity."""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv

# Load environment variables
load_dotenv()

def check_environment():
    """Check environment variable configuration."""
    print("=== Environment Configuration ===")
    
    # Check Voyage AI configuration
    voyage_key = os.getenv("VOYAGE_API_KEY")
    print(f"Voyage API Key: {'✓ Present' if voyage_key else '✗ Missing'}")
    if voyage_key:
        print(f"  Key prefix: {voyage_key[:10]}...")
    
    # Check semantic search settings
    semantic_enabled = os.getenv("SEMANTIC_SEARCH_ENABLED", "false").lower()
    print(f"Semantic Search Enabled: {semantic_enabled}")
    
    embedding_model = os.getenv("SEMANTIC_EMBEDDING_MODEL", "voyage-code-3")
    print(f"Embedding Model: {embedding_model}")
    
    collection_name = os.getenv("SEMANTIC_COLLECTION_NAME", "code-embeddings")
    print(f"Collection Name: {collection_name}")
    
    # Check Qdrant configuration
    qdrant_host = os.getenv("QDRANT_HOST", "localhost")
    qdrant_port = os.getenv("QDRANT_PORT", "6333")
    print(f"Qdrant Host: {qdrant_host}:{qdrant_port}")
    
    return voyage_key is not None


def test_voyage_ai():
    """Test Voyage AI connectivity."""
    print("\n=== Testing Voyage AI ===")
    
    try:
        import voyageai
        
        # Test API connection
        client = voyageai.Client()
        
        # Test with a simple embedding
        test_text = "def hello_world(): print('Hello, World!')"
        
        print("Testing embedding generation...")
        result = client.embed([test_text], model="voyage-code-3")
        
        if result and result.embeddings:
            embedding = result.embeddings[0]
            print(f"✓ Voyage AI working - embedding size: {len(embedding)}")
            return True
        else:
            print("✗ Voyage AI returned no embeddings")
            return False
            
    except Exception as e:
        print(f"✗ Voyage AI error: {e}")
        return False


def test_qdrant():
    """Test Qdrant connectivity."""
    print("\n=== Testing Qdrant ===")
    
    try:
        from qdrant_client import QdrantClient, models
        
        # Try to connect to Qdrant
        qdrant_host = os.getenv("QDRANT_HOST", "localhost")
        qdrant_port = os.getenv("QDRANT_PORT", "6333")
        
        try:
            # Try HTTP connection first
            from mcp_server.config.env_vars import resolve_qdrant_server_api_key

            client = QdrantClient(
                url=f"http://{qdrant_host}:{qdrant_port}",
                api_key=resolve_qdrant_server_api_key(),
            )
            collections = client.get_collections()
            print(f"✓ Qdrant HTTP connection successful")
            print(f"  Collections: {len(collections.collections)}")
            return True, client
        except Exception as e:
            print(f"✗ Qdrant HTTP connection failed: {e}")
            
            try:
                # Try in-memory fallback
                client = QdrantClient(location=":memory:")
                print(f"✓ Qdrant memory fallback working")
                return True, client
            except Exception as e2:
                print(f"✗ Qdrant memory fallback failed: {e2}")
                return False, None
                
    except ImportError as e:
        print(f"✗ Qdrant client not available: {e}")
        return False, None


def test_semantic_indexer():
    """Test the semantic indexer directly."""
    print("\n=== Testing Semantic Indexer ===")
    
    try:
        from mcp_server.utils.semantic_indexer import SemanticIndexer
        
        # Create indexer with memory backend
        indexer = SemanticIndexer(collection="test-collection", qdrant_path=":memory:")
        print("✓ Semantic indexer created")
        
        # Test indexing a symbol
        test_symbol = {
            'file': 'test.py',
            'name': 'test_function',
            'kind': 'function',
            'signature': 'def test_function(x: int) -> str',
            'line': 1,
            'span': (1, 3),
            'doc': 'A test function',
            'content': 'def test_function(x: int) -> str:\n    """A test function."""\n    return str(x)'
        }
        
        indexer.index_symbol(**test_symbol)
        print("✓ Symbol indexed successfully")
        
        # Test semantic search
        results = indexer.search("test function", limit=5)
        print(f"✓ Semantic search returned {len(results)} results")
        
        if results:
            for i, result in enumerate(results):
                # result is a dictionary, not an object
                symbol_name = result.get('symbol', 'unknown')
                score = result.get('score', 0.0)
                print(f"  {i+1}. {symbol_name} (score: {score:.3f})")
        
        return len(results) > 0
        
    except Exception as e:
        print(f"✗ Semantic indexer error: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_plugin_semantic_search():
    """Test semantic search through a plugin."""
    print("\n=== Testing Plugin Semantic Search ===")
    
    try:
        from mcp_server.plugins.plugin_factory import PluginFactory
        from mcp_server.storage.sqlite_store import SQLiteStore
        
        # Create a plugin with semantic search enabled
        store = SQLiteStore(":memory:")
        plugin = PluginFactory.create_plugin("python", store, enable_semantic=True)
        
        print(f"✓ Created plugin: {plugin.__class__.__name__}")
        print(f"✓ Semantic enabled: {getattr(plugin, '_enable_semantic', False)}")
        
        # Test indexing a file
        test_code = '''def calculate_fibonacci(n):
    """Calculate the nth Fibonacci number."""
    if n <= 1:
        return n
    return calculate_fibonacci(n-1) + calculate_fibonacci(n-2)

def validate_email(email):
    """Validate email address format."""
    return "@" in email and "." in email.split("@")[-1]
'''
        
        test_file = Path("test_semantic.py")
        test_file.write_text(test_code)
        
        try:
            # Index the file
            shard = plugin.indexFile(test_file, test_code)
            print(f"✓ Indexed file with {len(shard['symbols'])} symbols")
            
            # Test semantic search
            search_opts = {"semantic": True, "limit": 5}
            results = list(plugin.search("number calculation", search_opts))
            print(f"✓ Semantic search returned {len(results)} results")
            
            if results:
                for result in results:
                    snippet = result.get('snippet', '')[:50]
                    print(f"  → {snippet}...")
            
            return len(results) > 0
            
        finally:
            test_file.unlink(missing_ok=True)
            
    except Exception as e:
        print(f"✗ Plugin semantic search error: {e}")
        import traceback
        traceback.print_exc()
        return False


def main():
    """Run all semantic search diagnostics."""
    print("🔍 Semantic Search Diagnostic Tool\n")
    
    # Test environment
    env_ok = check_environment()
    
    # Test components
    voyage_ok = test_voyage_ai() if env_ok else False
    qdrant_ok, _ = test_qdrant()
    indexer_ok = test_semantic_indexer() if voyage_ok and qdrant_ok else False
    plugin_ok = test_plugin_semantic_search() if indexer_ok else False
    
    # Summary
    print("\n=== Diagnostic Summary ===")
    print(f"Environment: {'✅' if env_ok else '❌'}")
    print(f"Voyage AI: {'✅' if voyage_ok else '❌'}")
    print(f"Qdrant: {'✅' if qdrant_ok else '❌'}")
    print(f"Semantic Indexer: {'✅' if indexer_ok else '❌'}")
    print(f"Plugin Integration: {'✅' if plugin_ok else '❌'}")
    
    if all([env_ok, voyage_ok, qdrant_ok, indexer_ok, plugin_ok]):
        print("\n🎉 Semantic search is fully operational!")
    elif env_ok and voyage_ok:
        print("\n⚠️ Semantic search partially working - some components need attention")
    else:
        print("\n❌ Semantic search needs configuration")
        
        if not env_ok:
            print("  → Check .env file for VOYAGE_API_KEY")
        if not voyage_ok:
            print("  → Verify Voyage AI API key and connectivity")
        if not qdrant_ok:
            print("  → Install/start Qdrant or use memory backend")


if __name__ == "__main__":
    main()