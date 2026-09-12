CREATE TABLE IF NOT EXISTS chunk_summaries (
    chunk_hash TEXT PRIMARY KEY,
    file_id INTEGER NOT NULL,
    chunk_start INTEGER NOT NULL,
    chunk_end INTEGER NOT NULL,
    symbol TEXT,
    summary_text TEXT NOT NULL,
    is_authoritative BOOLEAN DEFAULT 0,
    llm_model TEXT,
    provider_name TEXT,
    profile_id TEXT,
    prompt_fingerprint TEXT,
    audit_metadata TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(file_id) REFERENCES files(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_chunk_summaries_file_bounds ON chunk_summaries(file_id, chunk_start, chunk_end);

INSERT OR REPLACE INTO schema_version (version, description)
VALUES (5, 'Chunk summaries');
INSERT INTO migrations (version_from, version_to, status)
SELECT 4, 5, 'completed'
WHERE NOT EXISTS (SELECT 1 FROM migrations WHERE version_to = 5 AND status = 'completed');
