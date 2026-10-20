"""SQLite statements for the statistics store and read-only source views."""

STATS_SCHEMA = """
PRAGMA journal_mode = WAL;
CREATE TABLE roots (
    root_run_id TEXT PRIMARY KEY NOT NULL,
    finished_at INTEGER NOT NULL,
    archived_at INTEGER NOT NULL
) WITHOUT ROWID;
CREATE TABLE buckets (
    scope_kind TEXT NOT NULL CHECK (scope_kind IN ('agent','thread','root_run')),
    scope_id TEXT NOT NULL,
    period TEXT NOT NULL CHECK (period IN ('all','minute','hour','day')),
    bucket_start INTEGER NOT NULL,
    model_ref TEXT NOT NULL,
    input_tokens INTEGER CHECK (input_tokens >= 0),
    output_tokens INTEGER CHECK (output_tokens >= 0),
    cached_tokens INTEGER CHECK (cached_tokens >= 0),
    spend_micros INTEGER CHECK (spend_micros IS NULL OR
        (typeof(spend_micros) = 'integer' AND spend_micros >= 0)),
    model_calls INTEGER NOT NULL CHECK (model_calls >= 0),
    tool_calls INTEGER NOT NULL CHECK (tool_calls >= 0),
    root_runs INTEGER NOT NULL CHECK (root_runs >= 0),
    spend_approximate INTEGER NOT NULL CHECK (spend_approximate IN (0,1)),
    PRIMARY KEY (scope_kind,scope_id,period,bucket_start,model_ref),
    CHECK (model_calls = 0 OR model_ref <> ''),
    CHECK (tool_calls = 0 OR model_ref = ''),
    CHECK (root_runs = 0 OR model_ref = ''),
    CHECK (period <> 'all' OR bucket_start = 0)
) WITHOUT ROWID;
CREATE TABLE state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    initialized INTEGER NOT NULL CHECK (initialized IN (0,1)),
    updated_at INTEGER
);
INSERT INTO state VALUES (1,0,NULL);
"""

PIN_STATS = """
-- Execute BEGIN, this SELECT, then pin_source; keep the transaction open.
SELECT initialized,updated_at FROM main.state WHERE id = 1;
"""

PIN_SOURCE = """
SELECT id FROM src.runs LIMIT 1;
"""

RECENT_ROOT_COUNTS = """
SELECT status,COUNT(*) AS count FROM src.runs
WHERE parent IS NULL AND status IN ('succeeded','failed','canceled')
  AND finished_at IS NOT NULL AND finished_at >= :lo_text AND finished_at <= :hi_text
GROUP BY status;
"""

RECENT_ROOT_PAGE = """
SELECT id,thread,status,created_at,started_at,finished_at,report FROM src.runs
WHERE parent IS NULL AND status IN ('succeeded','failed','canceled')
  AND finished_at IS NOT NULL AND finished_at >= :lo_text AND finished_at <= :hi_text
  AND (:before_time IS NULL OR finished_at < :before_time
       OR (finished_at = :before_time AND id > :after_id))
ORDER BY finished_at DESC,id LIMIT :limit;
"""

RECENT_ROOTS_BY_IDS = """
SELECT id,thread,status,created_at,started_at,finished_at,report FROM src.runs
WHERE id IN (SELECT value FROM json_each(:ids_json)) AND parent IS NULL
  AND status IN ('succeeded','failed','canceled') AND finished_at IS NOT NULL
  AND finished_at >= :lo_text AND finished_at <= :hi_text;
"""

ARCHIVE_CANDIDATES = """
-- cutoff_text = canonical UTC text for pass_time - archive_after_seconds.
-- Exclude receipts before reading Steps; no started_at/lower finished_at cursor.
SELECT r.id,r.finished_at
FROM src.runs AS r
WHERE r.parent IS NULL AND r.status IN ('succeeded','failed','canceled')
  AND r.finished_at IS NOT NULL AND r.finished_at < :cutoff_text
  AND NOT EXISTS (SELECT 1 FROM main.roots AS a WHERE a.root_run_id = r.id)
ORDER BY r.finished_at,r.id;
"""

UNARCHIVED_ROOTS = """
SELECT r.id,r.parent,r.thread,r.status,r.started_at,r.finished_at
FROM src.runs AS r
WHERE r.parent IS NULL
  AND NOT EXISTS (SELECT 1 FROM main.roots AS a WHERE a.root_run_id = r.id)
ORDER BY r.id;
"""

HAS_ARCHIVE_WORK = """
SELECT 1 FROM src.runs AS r
WHERE r.parent IS NULL AND r.status IN ('succeeded','failed','canceled')
  AND r.finished_at IS NOT NULL AND r.finished_at < :cutoff_text
  AND NOT EXISTS (SELECT 1 FROM main.roots AS a WHERE a.root_run_id = r.id)
LIMIT 1;
"""

ROOT_BY_ID = """
SELECT r.id,r.parent,r.thread,r.status,r.started_at,r.finished_at
FROM src.runs AS r WHERE r.id = :root_id AND r.parent IS NULL;
"""

OWNED_RUNS = """
-- parent is a launching StepRef: run_id + '.' + local_step_path.
WITH RECURSIVE tree(id,parent,thread,status,started_at,finished_at) AS (
    SELECT id,parent,thread,status,started_at,finished_at
    FROM src.runs WHERE id = :root_id AND parent IS NULL
    UNION ALL
    SELECT c.id,c.parent,c.thread,c.status,c.started_at,c.finished_at
    FROM tree AS p JOIN src.runs AS c
      ON c.parent >= p.id || '.' AND c.parent < p.id || '/'
)
SELECT * FROM tree;
"""

HAS_OPEN_STEPS = """
-- Check each owned Run before publishing an archive, including non-call Steps.
SELECT 1 FROM src.steps
WHERE run = :run_id
  AND (status NOT IN ('succeeded','failed','canceled') OR finished_at IS NULL)
LIMIT 1;
"""

SOURCE_STEP_PAGE = """
-- after_id='' for the first page. lo_text/hi_text are both NULL for all-time.
SELECT id,run,kind,spec,report,started_at,finished_at
FROM src.steps
WHERE run = :run_id AND id > :after_id
  AND kind IN ('model','tool') AND status IN ('succeeded','failed','canceled')
  AND finished_at IS NOT NULL
  AND (:lo_text IS NULL
       OR (started_at >= :lo_text AND started_at < :hi_text)
       OR (finished_at >= :lo_text AND finished_at < :hi_text))
ORDER BY id LIMIT :page_size;
"""

TIME_STEP_IDS = """
-- For recent-only startup reads or archived partial-minute edges.
-- UNION deduplicates a Step selected by both of its timestamps.
SELECT id,run FROM src.steps
WHERE kind IN ('model','tool') AND status IN ('succeeded','failed','canceled')
  AND started_at IS NOT NULL
  AND started_at >= :lo_text AND started_at < :hi_text
UNION
SELECT id,run FROM src.steps
WHERE kind IN ('model','tool') AND status IN ('succeeded','failed','canceled')
  AND finished_at IS NOT NULL
  AND finished_at >= :lo_text AND finished_at < :hi_text;
"""

STEPS_BY_IDS = """
SELECT id,run,kind,spec,report,started_at,finished_at
FROM src.steps WHERE id IN (SELECT value FROM json_each(:ids_json));
"""

RUNS_BY_IDS = """
-- Resolve roots by source-owned StepRef.parse(parent).run_id, memoizing Run IDs.
SELECT id,parent,thread,status,started_at,finished_at
FROM src.runs WHERE id IN (SELECT value FROM json_each(:ids_json));
"""

ROOT_STARTS_BY_TIME = """
-- archived=1 for cold edges; archived=0 for recent source projections.
SELECT r.id,r.parent,r.thread,r.status,r.started_at,r.finished_at
FROM src.runs AS r
WHERE r.parent IS NULL AND r.started_at IS NOT NULL
  AND r.started_at >= :lo_text AND r.started_at < :hi_text
  AND (EXISTS (SELECT 1 FROM main.roots AS a WHERE a.root_run_id = r.id))
      = :archived;
"""

ARCHIVED_ROOT_IDS = """
SELECT root_run_id FROM main.roots
WHERE root_run_id IN (SELECT value FROM json_each(:ids_json));
"""

BUCKET_TOTALS = """
-- Complete, disjoint bucket ranges only. all uses period=all, lo=0, hi=1.
-- An empty result set is an empty metric vector, not an unknown quantity.
SELECT model_ref,
    CASE WHEN COUNT(input_tokens) = COUNT(*) THEN SUM(input_tokens) END AS input_tokens,
    CASE WHEN COUNT(output_tokens) = COUNT(*) THEN SUM(output_tokens) END AS output_tokens,
    CASE WHEN COUNT(cached_tokens) = COUNT(*) THEN SUM(cached_tokens) END AS cached_tokens,
    CASE WHEN COUNT(spend_micros) = COUNT(*) THEN SUM(spend_micros) END AS spend_micros,
    SUM(model_calls) AS model_calls,SUM(tool_calls) AS tool_calls,
    SUM(root_runs) AS root_runs,MAX(spend_approximate) AS spend_approximate
FROM main.buckets
WHERE scope_kind = :scope_kind AND scope_id = :scope_id AND period = :period
  AND bucket_start >= :lo AND bucket_start < :hi
  AND (:model_ref IS NULL OR model_ref = :model_ref)
GROUP BY model_ref;
"""

RECEIPT_EXISTS = """
-- Skip before Step collection; recheck inside writer BEGIN IMMEDIATE.
-- If present in the writer transaction, ROLLBACK and skip all bucket writes.
SELECT 1 FROM main.roots WHERE root_run_id = :root_id;
"""

UPSERT_BUCKET = """
-- Writer: execute once per grouped key, inside the same root transaction.
INSERT INTO main.buckets (
    scope_kind,scope_id,period,bucket_start,model_ref,
    input_tokens,output_tokens,cached_tokens,spend_micros,
    model_calls,tool_calls,root_runs,spend_approximate
) VALUES (
    :scope_kind,:scope_id,:period,:bucket_start,:model_ref,
    :input_tokens,:output_tokens,:cached_tokens,:spend_micros,
    :model_calls,:tool_calls,:root_runs,:spend_approximate
)
ON CONFLICT (scope_kind,scope_id,period,bucket_start,model_ref) DO UPDATE SET
    input_tokens = buckets.input_tokens + excluded.input_tokens,
    output_tokens = buckets.output_tokens + excluded.output_tokens,
    cached_tokens = buckets.cached_tokens + excluded.cached_tokens,
    spend_micros = buckets.spend_micros + excluded.spend_micros,
    model_calls = buckets.model_calls + excluded.model_calls,
    tool_calls = buckets.tool_calls + excluded.tool_calls,
    root_runs = buckets.root_runs + excluded.root_runs,
    spend_approximate = MAX(buckets.spend_approximate,excluded.spend_approximate);
"""

INSERT_RECEIPT = """
INSERT INTO main.roots(root_run_id,finished_at,archived_at)
VALUES (:root_id,:finished_epoch,:now_epoch);
"""

TOUCH_STATE = """
-- Commit only after upsert_bucket, insert_receipt, and this statement succeed.
UPDATE main.state SET updated_at = :now_epoch WHERE id = 1;
"""

FINISH_PASS = """
UPDATE main.state SET initialized = 1,updated_at = :now_epoch WHERE id = 1;
"""
