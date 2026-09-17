-- Complete the source/installed upgrade contract after replaying partial v2-v6 scripts.
CREATE TABLE IF NOT EXISTS index_config (
    id INTEGER PRIMARY KEY,
    config_key TEXT UNIQUE NOT NULL,
    config_value TEXT NOT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    description TEXT
);

INSERT OR IGNORE INTO index_config (config_key, config_value, description) VALUES
    ('embedding_model', 'voyage-code-3', 'Current embedding model used for vector search'),
    ('model_dimension', '1024', 'Embedding vector dimension'),
    ('distance_metric', 'cosine', 'Distance metric for vector similarity'),
    ('index_version', '1.0', 'Index schema version');

CREATE INDEX IF NOT EXISTS idx_trigrams_symbol_id ON symbol_trigrams(symbol_id);

INSERT OR REPLACE INTO schema_version (version, description)
VALUES (7, 'Transactional reconciliation of partial migrations and installed schema');
INSERT INTO migrations (version_from, version_to, status)
SELECT 6, 7, 'completed'
WHERE NOT EXISTS (SELECT 1 FROM migrations WHERE version_to = 7 AND status = 'completed');
