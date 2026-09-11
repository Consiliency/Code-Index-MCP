-- Migration 003: Add stable IDs and token counting support
-- Extends schema for TreeSitter Chunker integration with 5 stable ID types and token counting

-- Phase 1: Create code_chunks table with stable IDs
CREATE TABLE IF NOT EXISTS code_chunks (
    id INTEGER PRIMARY KEY,
    file_id INTEGER NOT NULL,
    symbol_id INTEGER,
    content TEXT NOT NULL,
    content_start INTEGER NOT NULL,
    content_end INTEGER NOT NULL,
    line_start INTEGER NOT NULL,
    line_end INTEGER NOT NULL,

    -- 5 Stable IDs from TreeSitter Chunker
    chunk_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    treesitter_file_id TEXT NOT NULL,
    symbol_hash TEXT,
    definition_id TEXT,

    -- Token counting
    token_count INTEGER,
    token_model TEXT DEFAULT 'cl100k_base',

    -- Metadata
    chunk_type TEXT NOT NULL DEFAULT 'code',
    language TEXT,
    node_type TEXT,
    parent_chunk_id TEXT,
    depth INTEGER DEFAULT 0,
    chunk_index INTEGER NOT NULL DEFAULT 0,
    metadata JSON,

    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    FOREIGN KEY (file_id) REFERENCES files(id) ON DELETE CASCADE,
    FOREIGN KEY (symbol_id) REFERENCES symbols(id) ON DELETE SET NULL,
    UNIQUE(file_id, chunk_id)
);

-- Phase 2: Add indexes for code_chunks
CREATE INDEX IF NOT EXISTS idx_chunks_file_id ON code_chunks(file_id);
CREATE INDEX IF NOT EXISTS idx_chunks_symbol_id ON code_chunks(symbol_id);
CREATE INDEX IF NOT EXISTS idx_chunks_chunk_id ON code_chunks(chunk_id);
CREATE INDEX IF NOT EXISTS idx_chunks_node_id ON code_chunks(node_id);
CREATE INDEX IF NOT EXISTS idx_chunks_treesitter_file_id ON code_chunks(treesitter_file_id);
CREATE INDEX IF NOT EXISTS idx_chunks_symbol_hash ON code_chunks(symbol_hash);
CREATE INDEX IF NOT EXISTS idx_chunks_definition_id ON code_chunks(definition_id);
CREATE INDEX IF NOT EXISTS idx_chunks_chunk_type ON code_chunks(chunk_type);
CREATE INDEX IF NOT EXISTS idx_chunks_language ON code_chunks(language);
CREATE INDEX IF NOT EXISTS idx_chunks_parent_chunk_id ON code_chunks(parent_chunk_id);

-- Phase 3: Add token_count columns to symbols table
-- Note: IF NOT EXISTS syntax may not be available in all SQLite builds
-- The migration runner should handle duplicate column errors gracefully
ALTER TABLE symbols ADD COLUMN token_count INTEGER;
ALTER TABLE symbols ADD COLUMN token_model TEXT DEFAULT 'cl100k_base';

-- Phase 4: Add indexes for token counting
CREATE INDEX IF NOT EXISTS idx_symbols_token_count ON symbols(token_count);
CREATE INDEX IF NOT EXISTS idx_chunks_token_count ON code_chunks(token_count);

-- Phase 5: Create trigger to update chunk timestamps
CREATE TRIGGER IF NOT EXISTS update_chunk_timestamp
AFTER UPDATE ON code_chunks
FOR EACH ROW
BEGIN
    UPDATE code_chunks SET updated_at = CURRENT_TIMESTAMP WHERE id = NEW.id;
END;

-- Phase 6: Update schema version
INSERT OR REPLACE INTO schema_version (version, description)
VALUES (3, 'Stable IDs and token counting for TreeSitter Chunker integration');

-- Phase 7: Add migration record
INSERT INTO migrations (version_from, version_to, status)
SELECT 2, 3, 'completed'
WHERE NOT EXISTS (SELECT 1 FROM migrations WHERE version_to = 3 AND status = 'completed');
