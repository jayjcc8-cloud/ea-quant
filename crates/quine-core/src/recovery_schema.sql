CREATE TABLE attempt_recovery_v1 (
  strategy_id TEXT NOT NULL,
  intent_id TEXT NOT NULL,
  job_id TEXT NOT NULL UNIQUE,
  attempt_id TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL,
  active INTEGER NOT NULL,
  revision INTEGER NOT NULL,
  dispatch_owner TEXT,
  result_json TEXT,
  commit_fence INTEGER,
  created_at INTEGER NOT NULL,
  dispatched_at INTEGER,
  committed_at INTEGER,
  cancelled_at INTEGER,
  origin_json TEXT,
  preparation_json TEXT,
  release_json TEXT,
  PRIMARY KEY (strategy_id, intent_id),
  FOREIGN KEY (strategy_id, intent_id) REFERENCES experiment(strategy_id, intent_id),
  CHECK (
    (status IN ('eligible','dispatched','running','stopping','unknown') AND active=1 AND release_json IS NULL)
    OR (status IN ('succeeded','failed','cancelled') AND active=0 AND release_json IS NULL)
    OR (status='unknown' AND active=0 AND release_json IS NOT NULL AND preparation_json IS NOT NULL AND origin_json IS NOT NULL)
  )
);
INSERT INTO attempt_recovery_v1 (
  strategy_id,intent_id,job_id,attempt_id,status,active,revision,dispatch_owner,result_json,
  commit_fence,created_at,dispatched_at,committed_at,cancelled_at
) SELECT strategy_id,intent_id,job_id,attempt_id,status,active,revision,dispatch_owner,result_json,
  commit_fence,created_at,dispatched_at,committed_at,cancelled_at FROM attempt;
DROP TABLE attempt;
ALTER TABLE attempt_recovery_v1 RENAME TO attempt;
CREATE UNIQUE INDEX one_active_attempt ON attempt(active) WHERE active=1;
-- Old clients must never reactivate, settle or change a released unknown.
CREATE TRIGGER released_unknown_immutable BEFORE UPDATE ON attempt
WHEN OLD.release_json IS NOT NULL
BEGIN SELECT RAISE(ABORT,'released unknown is immutable'); END;
PRAGMA user_version=1;
