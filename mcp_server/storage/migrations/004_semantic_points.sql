-- Migration 004: Add semantic point mappings for stale vector cleanup

CREATE TABLE IF NOT EXISTS semantic_points (
    profile_id TEXT NOT NULL,
    chunk_id TEXT NOT NULL,
    point_id INTEGER NOT NULL,
    collection TEXT NOT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (profile_id, chunk_id)
);

CREATE INDEX IF NOT EXISTS idx_semantic_points_profile
    ON semantic_points(profile_id);
CREATE INDEX IF NOT EXISTS idx_semantic_points_point
    ON semantic_points(profile_id, point_id);
CREATE INDEX IF NOT EXISTS idx_semantic_points_collection
    ON semantic_points(collection);

INSERT OR REPLACE INTO schema_version (version, description)
VALUES (4, 'Semantic point mappings for profile-scoped stale vector cleanup');

INSERT INTO migrations (version_from, version_to, status)
SELECT 3, 4, 'completed'
WHERE NOT EXISTS (SELECT 1 FROM migrations WHERE version_to = 4 AND status = 'completed');
