CREATE TRIGGER activity_runs_delete
                AFTER DELETE ON runs BEGIN
                INSERT OR IGNORE INTO activity_dirty VALUES ('run',OLD.id,0);
                END;
CREATE TRIGGER activity_runs_insert
                AFTER INSERT ON runs BEGIN
                INSERT OR IGNORE INTO activity_dirty VALUES ('run',NEW.id,0);
                END;
CREATE TRIGGER activity_runs_update
                AFTER UPDATE OF status,started_at,finished_at,output,error,control ON runs BEGIN
                INSERT OR IGNORE INTO activity_dirty VALUES ('run',NEW.id,0);
                END;
CREATE TRIGGER activity_steps_delete
                AFTER DELETE ON steps BEGIN
                INSERT OR IGNORE INTO activity_dirty VALUES ('step',OLD.id,0);
                END;
CREATE TRIGGER activity_steps_insert
                AFTER INSERT ON steps BEGIN
                INSERT OR IGNORE INTO activity_dirty VALUES ('step',NEW.id,0);
                END;
CREATE TRIGGER activity_steps_update
                AFTER UPDATE OF status,finished_at,noted,output,error ON steps BEGIN
                INSERT OR IGNORE INTO activity_dirty VALUES ('step',NEW.id,0);
                END;
CREATE TRIGGER activity_threads_delete
                AFTER DELETE ON threads BEGIN
                INSERT OR IGNORE INTO activity_dirty VALUES ('thread',OLD.id,0);
                END;
CREATE TRIGGER activity_threads_insert
                AFTER INSERT ON threads BEGIN
                INSERT OR IGNORE INTO activity_dirty VALUES ('thread',NEW.id,0);
                END;
CREATE TRIGGER activity_threads_update
                AFTER UPDATE OF updated_at,horizon,peer ON threads BEGIN
                INSERT OR IGNORE INTO activity_dirty VALUES ('thread',NEW.id,0);
                END;
CREATE TABLE activity_attempts (
        id INTEGER PRIMARY KEY, ref TEXT NOT NULL, session TEXT NOT NULL,
        kind TEXT NOT NULL, started REAL NOT NULL, finished REAL,
        cost REAL, estimated INTEGER NOT NULL DEFAULT 0,
        partial INTEGER NOT NULL DEFAULT 0, complete INTEGER NOT NULL DEFAULT 1,
        interrupted INTEGER NOT NULL DEFAULT 0, input_tokens INTEGER, cached_tokens INTEGER, output_tokens INTEGER, tokens_migrated INTEGER NOT NULL DEFAULT 0);
CREATE TABLE activity_buckets (
        scope TEXT NOT NULL, session TEXT NOT NULL, minute INTEGER NOT NULL,
        model INTEGER NOT NULL, tool INTEGER NOT NULL, cost REAL NOT NULL,
        known INTEGER NOT NULL, unknown INTEGER NOT NULL, estimated INTEGER NOT NULL,
        partial INTEGER NOT NULL, input_tokens INTEGER NOT NULL DEFAULT 0, input_tokens_known INTEGER NOT NULL DEFAULT 0, input_tokens_unknown INTEGER NOT NULL DEFAULT 0, cached_tokens INTEGER NOT NULL DEFAULT 0, cached_tokens_known INTEGER NOT NULL DEFAULT 0, cached_tokens_unknown INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0, output_tokens_known INTEGER NOT NULL DEFAULT 0, output_tokens_unknown INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(scope,session,minute));
CREATE TABLE activity_dirty (
        kind TEXT NOT NULL, id TEXT NOT NULL, legacy INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(kind,id));
CREATE TABLE activity_durations (
        scope TEXT NOT NULL, session TEXT NOT NULL, total REAL NOT NULL,
        incomplete INTEGER NOT NULL, PRIMARY KEY(scope,session));
CREATE TABLE activity_meta (
        singleton INTEGER PRIMARY KEY CHECK(singleton=1), revision INTEGER NOT NULL,
        session TEXT, legacy INTEGER NOT NULL, migrated INTEGER NOT NULL);
CREATE TABLE activity_nodes (
        id TEXT PRIMARY KEY, kind TEXT NOT NULL, thread TEXT NOT NULL,
        root TEXT NOT NULL, parent TEXT, title TEXT NOT NULL, summary TEXT NOT NULL,
        status TEXT NOT NULL, created REAL NOT NULL, changed REAL NOT NULL,
        started REAL, finished REAL, attempt INTEGER, current INTEGER NOT NULL,
        revision INTEGER NOT NULL, position TEXT NOT NULL, expected INTEGER NOT NULL);
CREATE TABLE activity_open (
        scope TEXT NOT NULL, attempt INTEGER NOT NULL, session TEXT NOT NULL,
        started REAL NOT NULL, model INTEGER NOT NULL, duration INTEGER NOT NULL,
        PRIMARY KEY(scope,attempt));
CREATE TABLE activity_owners (
        scope TEXT NOT NULL, attempt INTEGER NOT NULL, duration INTEGER NOT NULL,
        PRIMARY KEY(scope,attempt));
CREATE TABLE activity_search (
        root TEXT NOT NULL, ref TEXT NOT NULL, attempt INTEGER NOT NULL,
        title TEXT NOT NULL, summary TEXT NOT NULL, status TEXT NOT NULL,
        PRIMARY KEY(root,ref,attempt));
CREATE TABLE activity_sessions (
        id TEXT PRIMARY KEY, started REAL NOT NULL, checkpoint REAL NOT NULL,
        ended REAL, complete INTEGER NOT NULL DEFAULT 1);
CREATE INDEX activity_token_migration ON activity_attempts(tokens_migrated,id);
CREATE INDEX idx_activity_attempt_ref ON activity_attempts(ref,started);
CREATE INDEX idx_activity_changed ON activity_nodes(kind,current,changed);
CREATE INDEX idx_activity_duration_owner ON activity_owners(scope,duration,attempt);
CREATE INDEX idx_activity_finished ON activity_attempts(finished);
CREATE INDEX idx_activity_owner_attempt ON activity_owners(attempt);
CREATE INDEX idx_activity_parent ON activity_nodes(parent,current);
CREATE INDEX idx_activity_root ON activity_nodes(root,current,status);
CREATE INDEX idx_activity_started ON activity_attempts(started);
