-- Migration 002: Convert to relative paths and add content tracking
-- This migration updates the schema to use relative paths as primary identifiers

-- Phase 1: Add new columns (non-breaking)
ALTER TABLE files ADD COLUMN content_hash TEXT;
ALTER TABLE files ADD COLUMN is_deleted BOOLEAN DEFAULT FALSE;
ALTER TABLE files ADD COLUMN deleted_at TIMESTAMP;

-- Phase 2: Add indexes for new columns
CREATE INDEX IF NOT EXISTS idx_files_content_hash ON files(content_hash);
CREATE INDEX IF NOT EXISTS idx_files_deleted ON files(is_deleted);
CREATE INDEX IF NOT EXISTS idx_files_relative_path ON files(relative_path);

-- Phase 3: Add file moves tracking table
CREATE TABLE IF NOT EXISTS file_moves (
    id INTEGER PRIMARY KEY,
    repository_id INTEGER NOT NULL,
    old_relative_path TEXT NOT NULL,
    new_relative_path TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    moved_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    move_type TEXT CHECK(move_type IN ('rename', 'relocate', 'restructure')),
    FOREIGN KEY (repository_id) REFERENCES repositories(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_moves_content_hash ON file_moves(content_hash);
CREATE INDEX IF NOT EXISTS idx_moves_timestamp ON file_moves(moved_at);
CREATE INDEX IF NOT EXISTS idx_moves_old_path ON file_moves(old_relative_path);
CREATE INDEX IF NOT EXISTS idx_moves_new_path ON file_moves(new_relative_path);

-- Phase 4: Update unique constraint to use relative_path
-- First drop the old constraint if it exists
DROP INDEX IF EXISTS files_repository_id_path_key;

-- Create new unique constraint on relative_path
CREATE UNIQUE INDEX IF NOT EXISTS files_repository_id_relative_path_key 
  ON files(repository_id, relative_path);

-- Phase 5: Add triggers for soft delete cascade
CREATE TRIGGER IF NOT EXISTS soft_delete_symbols
AFTER UPDATE OF is_deleted ON files
WHEN NEW.is_deleted = TRUE
BEGIN
    -- Mark all symbols as deleted (we'll add is_deleted to symbols table if needed)
    UPDATE symbols SET documentation = '[DELETED]' || documentation 
    WHERE file_id = NEW.id AND documentation NOT LIKE '[DELETED]%';
END;

-- Phase 6: Update schema version
INSERT OR REPLACE INTO schema_version (version, description) 
VALUES (2, 'Relative paths and content tracking');

-- Phase 7: Add migration record
INSERT INTO migrations (version_from, version_to, status) 
SELECT 1, 2, 'completed'
WHERE NOT EXISTS (SELECT 1 FROM migrations WHERE version_to = 2 AND status = 'completed');
