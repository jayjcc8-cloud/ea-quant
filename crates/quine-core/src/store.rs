//! SQLite single-owner store for confirmed experiments.
//!
//! Ownership is an OS-held exclusive lock on a sidecar lock file, kept open for
//! the whole `Store` lifetime; the owner name is diagnostic only and a crashed
//! owner's lock is released by the OS, never inferred from a clock.
//!
//! Every write is one transaction on one connection. Confirm registers the
//! confirmation, the approved single-backtest allowance, a stable job/attempt
//! pair and the one active slot atomically. The slot stays held for the whole
//! admitted run until a proven terminal state; dispatch, running, stopping and
//! unknown exit states all keep it, so an unproven run can never be re-dispatched
//! and never frees room for a second run.

use std::fs::{File, OpenOptions};
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::Mutex;

use rusqlite::{params, Connection, OptionalExtension, TransactionBehavior};
use uuid::Uuid;

use crate::protocol::{
    Barrier, Budget, CancelParams, CancelResult, ConfirmParams, ConfirmResult, Experiment,
    ListParams, ListResult, QueryParams, QueryResult, ResultSubmission,
};
use crate::util::{hex_decode, hex_encode, sha256_hex};

pub const STATUS_ELIGIBLE: &str = "eligible";
pub const STATUS_DISPATCHED: &str = "dispatched";
pub const STATUS_RUNNING: &str = "running";
pub const STATUS_STOPPING: &str = "stopping";
pub const STATUS_UNKNOWN: &str = "unknown";
pub const STATUS_SUCCEEDED: &str = "succeeded";
pub const STATUS_FAILED: &str = "failed";
pub const STATUS_CANCELLED: &str = "cancelled";

pub const CODE_INVALID_REQUEST: &str = "invalid_request";
pub const CODE_CONFLICT: &str = "conflict";
pub const CODE_CANCELLED_BEFORE_CONFIRM: &str = "cancelled_before_confirm";
pub const CODE_LATE_CANCEL_REFUSED: &str = "late_cancel_refused";
pub const CODE_DIGEST_MISMATCH: &str = "digest_mismatch";
pub const CODE_LEASE_HELD: &str = "lease_held";
pub const CODE_NOT_FOUND: &str = "not_found";
pub const CODE_ELIGIBILITY_HELD: &str = "eligibility_held";
pub const CODE_ALREADY_DISPATCHED: &str = "already_dispatched";
pub const CODE_INVALID_STATE: &str = "invalid_state";
pub const CODE_STALE_FENCE: &str = "stale_fence";
pub const CODE_INTERNAL: &str = "internal";

#[derive(Debug, Clone)]
pub struct QuineError {
    pub code: String,
    pub message: String,
}

impl QuineError {
    pub fn new(code: &str, message: impl Into<String>) -> Self {
        QuineError {
            code: code.to_string(),
            message: message.into(),
        }
    }
}

impl std::fmt::Display for QuineError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}: {}", self.code, self.message)
    }
}

impl std::error::Error for QuineError {}

impl From<rusqlite::Error> for QuineError {
    fn from(error: rusqlite::Error) -> Self {
        QuineError::new(CODE_INTERNAL, format!("store operation failed: {error}"))
    }
}

impl From<std::io::Error> for QuineError {
    fn from(error: std::io::Error) -> Self {
        QuineError::new(CODE_INTERNAL, format!("store I/O failed: {error}"))
    }
}

fn is_safe_id(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || byte == b'-' || byte == b'_')
}

fn is_sha256(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

fn is_preview_identity(value: &str) -> bool {
    value
        .strip_prefix("sha256:")
        .map(is_sha256)
        .unwrap_or(false)
}

fn validate_ids(strategy_id: &str, intent_id: &str) -> Result<(), QuineError> {
    if !is_safe_id(strategy_id) {
        return Err(QuineError::new(
            CODE_INVALID_REQUEST,
            "strategy_id must be 1-64 characters of letters, digits, '-' or '_'",
        ));
    }
    if !is_safe_id(intent_id) {
        return Err(QuineError::new(
            CODE_INVALID_REQUEST,
            "intent_id must be 1-64 characters of letters, digits, '-' or '_'",
        ));
    }
    Ok(())
}

fn validate_budget(budget: &Budget) -> Result<(), QuineError> {
    if budget.max_model_calls < 0 {
        return Err(QuineError::new(
            CODE_INVALID_REQUEST,
            "budget.max_model_calls must be a non-negative integer",
        ));
    }
    if budget.max_backtests != 1 {
        return Err(QuineError::new(
            CODE_INVALID_REQUEST,
            "an experiment confirmation approves exactly one backtest",
        ));
    }
    Ok(())
}

fn request_id_for(strategy_id: &str, intent_id: &str) -> String {
    Uuid::new_v5(
        &Uuid::NAMESPACE_URL,
        format!("ea.user-strategy/{strategy_id}/{intent_id}").as_bytes(),
    )
    .to_string()
}

fn cancel_receipt_for(strategy_id: &str, intent_id: &str) -> String {
    Uuid::new_v5(
        &Uuid::NAMESPACE_URL,
        format!("ea.quine.cancel/{strategy_id}/{intent_id}").as_bytes(),
    )
    .to_string()
}

fn lock_path_for(path: &Path) -> PathBuf {
    // Derive the lock name from the file name only, so equivalent paths to the
    // same database (relative vs absolute) resolve to the same lock file.
    let mut name = path
        .file_name()
        .unwrap_or_else(|| std::ffi::OsStr::new("quine.db"))
        .to_os_string();
    name.push(".lock");
    path.with_file_name(name)
}

pub struct Store {
    conn: Mutex<Connection>,
    //: Held open for the whole Store lifetime: closing it releases the OS lock.
    _lock: File,
}

/// One string field of a holdout reference, or a refusal that names it.
fn holdout_field<'a>(holdout: &'a serde_json::Value, name: &str) -> Result<&'a str, QuineError> {
    holdout
        .get(name)
        .and_then(serde_json::Value::as_str)
        .ok_or_else(|| {
            QuineError::new(
                CODE_INVALID_REQUEST,
                format!("holdout.{name} must be a string"),
            )
        })
}

/// Admit a holdout only over the source run its own metadata names.
///
/// The holdout metadata travels inside the frozen input, so the frozen-input
/// digest already covers it. What this proves is the other half: that the
/// referenced source is a record *this* core registered, that it reached a
/// terminal success, and that the frozen input and official result the metadata
/// names are still the ones this core holds. The official result's digest is
/// recomputed with the project's canonical JSON rule, which Python and Rust
/// share, so neither side can claim a digest of its own.
fn guard_holdout_source(
    transaction: &rusqlite::Transaction<'_>,
    holdout: &serde_json::Value,
) -> Result<(), QuineError> {
    let validation_id = holdout_field(holdout, "validation_id")?;
    if !is_safe_id(validation_id) {
        return Err(QuineError::new(
            CODE_INVALID_REQUEST,
            "holdout.validation_id must be 1-64 characters of letters, digits, '-' or '_'",
        ));
    }
    let source_job_id = holdout_field(holdout, "source_job_id")?;
    if !is_safe_id(source_job_id) {
        return Err(QuineError::new(
            CODE_INVALID_REQUEST,
            "holdout.source_job_id must be 1-64 characters of letters, digits, '-' or '_'",
        ));
    }
    let source_frozen_sha256 = holdout_field(holdout, "source_frozen_sha256")?;
    let source_result_sha256 = holdout_field(holdout, "source_result_sha256")?;
    if !is_sha256(source_frozen_sha256) || !is_sha256(source_result_sha256) {
        return Err(QuineError::new(
            CODE_INVALID_REQUEST,
            "holdout source digests must be lowercase 64-character SHA-256 hex",
        ));
    }
    let source: Option<(String, String, Option<String>)> = transaction
        .query_row(
            "SELECT e.frozen_sha256, a.status, a.result_json
             FROM attempt a JOIN experiment e
               ON e.strategy_id = a.strategy_id AND e.intent_id = a.intent_id
             WHERE a.job_id = ?1",
            params![source_job_id],
            |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
        )
        .optional()?;
    let (frozen_sha256, status, result_json) = source.ok_or_else(|| {
        QuineError::new(
            CODE_NOT_FOUND,
            "this holdout names a source run this core never registered",
        )
    })?;
    if status != STATUS_SUCCEEDED {
        return Err(QuineError::new(
            CODE_INVALID_STATE,
            format!("this holdout's source run is {status}, not a registered success"),
        ));
    }
    if frozen_sha256 != source_frozen_sha256 {
        return Err(QuineError::new(
            CODE_DIGEST_MISMATCH,
            "this holdout's source frozen input is not the one this core registered",
        ));
    }
    let stored = result_json.ok_or_else(|| {
        QuineError::new(
            CODE_INVALID_STATE,
            "this holdout's source run registered no official result",
        )
    })?;
    let result: serde_json::Value = serde_json::from_str(&stored).map_err(|error| {
        QuineError::new(
            CODE_INTERNAL,
            format!("the source run's official result is unreadable: {error}"),
        )
    })?;
    let canonical = crate::canonical::canonical_json_bytes(&result).map_err(|error| {
        QuineError::new(
            CODE_INTERNAL,
            format!("the source run's official result is not canonical: {error}"),
        )
    })?;
    if sha256_hex(&canonical) != source_result_sha256 {
        return Err(QuineError::new(
            CODE_DIGEST_MISMATCH,
            "this holdout's source result is not the one this core registered",
        ));
    }
    Ok(())
}

/// The current persisted identity, fence and frozen binding of one attempt.
#[derive(Debug, Clone)]
pub struct AttemptContext {
    pub status: String,
    pub revision: i64,
    pub job_id: String,
    pub attempt_id: String,
    pub request_id: String,
    pub draft_revision: i64,
    pub frozen_sha256: String,
}

impl Store {
    /// Open a workspace database and take the exclusive OS lock before any
    /// SQLite schema or write. The lock file is never unlinked or replaced while
    /// live; owner name is diagnostic only.
    pub fn open(path: &Path, owner: &str) -> Result<Store, QuineError> {
        if owner.is_empty() || owner.len() > 64 {
            return Err(QuineError::new(
                CODE_INVALID_REQUEST,
                "owner must be 1-64 characters",
            ));
        }
        if let Some(parent) = path.parent() {
            if !parent.as_os_str().is_empty() {
                std::fs::create_dir_all(parent)?;
            }
        }
        let lock_path = lock_path_for(path);
        let lock_file = OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .truncate(false)
            .open(&lock_path)?;
        if lock_file.try_lock().is_err() {
            let held = std::fs::read_to_string(&lock_path).unwrap_or_default();
            let held = held.trim();
            let detail = if held.is_empty() {
                String::new()
            } else {
                format!(" by '{held}'")
            };
            return Err(QuineError::new(
                CODE_LEASE_HELD,
                format!("the workspace lock is already held{detail}"),
            ));
        }
        // The lock is ours now: write the diagnostic owner, never unlink.
        lock_file.set_len(0)?;
        let mut writer = &lock_file;
        writer.write_all(owner.as_bytes())?;
        writer.flush()?;

        let conn = Connection::open(path)?;
        conn.execute_batch(
            "PRAGMA journal_mode=WAL;
             PRAGMA foreign_keys=ON;
             PRAGMA busy_timeout=5000;",
        )?;
        conn.execute_batch(
            "CREATE TABLE IF NOT EXISTS experiment (
               strategy_id TEXT NOT NULL,
               intent_id TEXT NOT NULL,
               request_id TEXT NOT NULL UNIQUE,
               draft_revision INTEGER NOT NULL,
               preview_identity TEXT NOT NULL,
               max_model_calls INTEGER NOT NULL,
               max_backtests INTEGER NOT NULL,
               frozen_sha256 TEXT NOT NULL,
               frozen_canonical BLOB NOT NULL,
               created_at INTEGER NOT NULL,
               PRIMARY KEY (strategy_id, intent_id)
             );
             CREATE TABLE IF NOT EXISTS attempt (
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
               PRIMARY KEY (strategy_id, intent_id),
               FOREIGN KEY (strategy_id, intent_id)
                 REFERENCES experiment(strategy_id, intent_id),
               CHECK (
                 (status IN ('eligible', 'dispatched', 'running', 'stopping', 'unknown')
                    AND active = 1)
                 OR
                 (status IN ('succeeded', 'failed', 'cancelled') AND active = 0)
               )
             );
             CREATE UNIQUE INDEX IF NOT EXISTS one_active_attempt
               ON attempt(active) WHERE active = 1;
             CREATE TABLE IF NOT EXISTS barrier (
               strategy_id TEXT NOT NULL,
               intent_id TEXT NOT NULL,
               cancel_receipt TEXT NOT NULL,
               created_at INTEGER NOT NULL,
               PRIMARY KEY (strategy_id, intent_id)
             );",
        )?;
        Ok(Store {
            conn: Mutex::new(conn),
            _lock: lock_file,
        })
    }

    /// Release the OS lock by dropping this Store and its lock file handle.
    pub fn release(self) -> Result<(), QuineError> {
        drop(self);
        Ok(())
    }

    fn lock(&self) -> Result<std::sync::MutexGuard<'_, Connection>, QuineError> {
        self.conn
            .lock()
            .map_err(|_| QuineError::new(CODE_INTERNAL, "store lock is poisoned"))
    }

    pub fn confirm(&self, params: &ConfirmParams, now: i64) -> Result<ConfirmResult, QuineError> {
        validate_ids(&params.strategy_id, &params.intent_id)?;
        validate_budget(&params.budget)?;
        if params.draft_revision < 1 {
            return Err(QuineError::new(
                CODE_INVALID_REQUEST,
                "draft_revision must be a positive integer",
            ));
        }
        if !is_preview_identity(&params.preview_identity) {
            return Err(QuineError::new(
                CODE_INVALID_REQUEST,
                "preview_identity must be the identity of an experiment preview",
            ));
        }
        if !is_sha256(&params.frozen_input.sha256) {
            return Err(QuineError::new(
                CODE_INVALID_REQUEST,
                "frozen_input.sha256 must be a lowercase 64-character SHA-256 hex digest",
            ));
        }
        let frozen = hex_decode(&params.frozen_input.canonical_bytes).ok_or_else(|| {
            QuineError::new(
                CODE_INVALID_REQUEST,
                "frozen_input.canonical_bytes must be lowercase hex",
            )
        })?;
        // The frozen payload must be readable JSON, but it is never parsed into
        // economic numbers: the exact bytes are stored and their digest is the
        // identity binding.
        let frozen_value: serde_json::Value = serde_json::from_slice(&frozen).map_err(|error| {
            QuineError::new(
                CODE_INVALID_REQUEST,
                format!("frozen_input.canonical_bytes is not readable JSON: {error}"),
            )
        })?;
        let digest = sha256_hex(&frozen);
        if digest != params.frozen_input.sha256 {
            return Err(QuineError::new(
                CODE_DIGEST_MISMATCH,
                "frozen_input.canonical_bytes does not match frozen_input.sha256",
            ));
        }

        let mut conn = self.lock()?;
        let transaction = conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
        let existing: Option<(i64, String, i64, i64, String)> = transaction
            .query_row(
                "SELECT draft_revision, preview_identity, max_model_calls, max_backtests,
                        frozen_sha256
                 FROM experiment WHERE strategy_id = ?1 AND intent_id = ?2",
                params![params.strategy_id, params.intent_id],
                |row| {
                    Ok((
                        row.get(0)?,
                        row.get(1)?,
                        row.get(2)?,
                        row.get(3)?,
                        row.get(4)?,
                    ))
                },
            )
            .optional()?;
        if let Some((revision, preview, model_calls, backtests, frozen_sha256)) = existing {
            let same = revision == params.draft_revision
                && preview == params.preview_identity
                && model_calls == params.budget.max_model_calls
                && backtests == params.budget.max_backtests
                && frozen_sha256 == params.frozen_input.sha256;
            if !same {
                transaction.rollback().ok();
                return Err(QuineError::new(
                    CODE_CONFLICT,
                    "this launch intent is already bound to a different confirmation",
                ));
            }
            let experiment =
                Self::experiment_for(&transaction, &params.strategy_id, &params.intent_id)?
                    .ok_or_else(|| {
                        QuineError::new(CODE_INTERNAL, "confirmed experiment lost its attempt")
                    })?;
            transaction.commit()?;
            return Ok(ConfirmResult {
                experiment,
                replayed: true,
            });
        }

        let barrier: Option<String> = transaction
            .query_row(
                "SELECT cancel_receipt FROM barrier WHERE strategy_id = ?1 AND intent_id = ?2",
                params![params.strategy_id, params.intent_id],
                |row| row.get(0),
            )
            .optional()?;
        if barrier.is_some() {
            transaction.rollback().ok();
            return Err(QuineError::new(
                CODE_CANCELLED_BEFORE_CONFIRM,
                "this launch intent was cancelled before it was confirmed",
            ));
        }

        // A confirmation whose frozen input names a chronological holdout is
        // admitted only when the source run it references is one this core really
        // registered: present, terminal-successful, and still the exact frozen
        // input and official result the holdout metadata names. This is a
        // reference guard over the core's own records — no economic value is read
        // or computed — and it runs in the same transaction that admits the
        // holdout, so a holdout can never be stored against a source that did not
        // exist at admission or is not the one it names.
        if let Some(holdout) = frozen_value.get("holdout") {
            guard_holdout_source(&transaction, holdout)?;
        }

        let request_id = request_id_for(&params.strategy_id, &params.intent_id);
        let job_id = Uuid::new_v4().to_string();
        let attempt_id = Uuid::new_v4().to_string();
        transaction.execute(
            "INSERT INTO experiment (
               strategy_id, intent_id, request_id, draft_revision, preview_identity,
               max_model_calls, max_backtests, frozen_sha256, frozen_canonical, created_at
             ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10)",
            params![
                params.strategy_id,
                params.intent_id,
                request_id,
                params.draft_revision,
                params.preview_identity,
                params.budget.max_model_calls,
                params.budget.max_backtests,
                params.frozen_input.sha256,
                frozen,
                now,
            ],
        )?;
        if let Err(error) = transaction.execute(
            "INSERT INTO attempt (
               strategy_id, intent_id, job_id, attempt_id, status, active, revision,
               dispatch_owner, result_json, created_at
             ) VALUES (?1, ?2, ?3, ?4, ?5, 1, 1, NULL, NULL, ?6)",
            params![
                params.strategy_id,
                params.intent_id,
                job_id,
                attempt_id,
                STATUS_ELIGIBLE,
                now,
            ],
        ) {
            transaction.rollback().ok();
            if error.sqlite_error_code() == Some(rusqlite::ErrorCode::ConstraintViolation) {
                return Err(QuineError::new(
                    CODE_ELIGIBILITY_HELD,
                    "another run already holds the single active slot",
                ));
            }
            return Err(error.into());
        }
        let experiment =
            Self::experiment_for(&transaction, &params.strategy_id, &params.intent_id)?
                .ok_or_else(|| {
                    QuineError::new(CODE_INTERNAL, "confirmed experiment lost its attempt")
                })?;
        transaction.commit()?;
        Ok(ConfirmResult {
            experiment,
            replayed: false,
        })
    }

    pub fn cancel(&self, params: &CancelParams, now: i64) -> Result<CancelResult, QuineError> {
        validate_ids(&params.strategy_id, &params.intent_id)?;
        let receipt = cancel_receipt_for(&params.strategy_id, &params.intent_id);
        let mut conn = self.lock()?;
        let transaction = conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
        let status: Option<String> = transaction
            .query_row(
                "SELECT a.status FROM attempt a
                 WHERE a.strategy_id = ?1 AND a.intent_id = ?2",
                params![params.strategy_id, params.intent_id],
                |row| row.get(0),
            )
            .optional()?;
        if let Some(ref status) = status {
            if status.as_str() == STATUS_SUCCEEDED || status.as_str() == STATUS_FAILED {
                transaction.rollback().ok();
                return Err(QuineError::new(
                    CODE_LATE_CANCEL_REFUSED,
                    "the experiment already committed a result; a late cancel cannot overwrite it",
                ));
            }
        }
        let existing: Option<String> = transaction
            .query_row(
                "SELECT cancel_receipt FROM barrier WHERE strategy_id = ?1 AND intent_id = ?2",
                params![params.strategy_id, params.intent_id],
                |row| row.get(0),
            )
            .optional()?;
        if existing.is_none() {
            transaction.execute(
                "INSERT INTO barrier (strategy_id, intent_id, cancel_receipt, created_at)
                 VALUES (?1, ?2, ?3, ?4)",
                params![params.strategy_id, params.intent_id, receipt, now],
            )?;
            if let Some(ref status) = status {
                match status.as_str() {
                    // Nothing was spawned, so the cancellation is a proven
                    // terminal state and the slot is released.
                    STATUS_ELIGIBLE => {
                        transaction.execute(
                            "UPDATE attempt SET status = ?1, active = 0, revision = revision + 1,
                               cancelled_at = ?2
                             WHERE strategy_id = ?3 AND intent_id = ?4",
                            params![STATUS_CANCELLED, now, params.strategy_id, params.intent_id],
                        )?;
                    }
                    // Compute may exist: the cancellation is accepted/pending,
                    // not a proven stop, and the slot stays held.
                    STATUS_DISPATCHED | STATUS_RUNNING | STATUS_UNKNOWN | STATUS_STOPPING => {
                        transaction.execute(
                            "UPDATE attempt SET status = ?1, active = 1, revision = revision + 1,
                               cancelled_at = ?2
                             WHERE strategy_id = ?3 AND intent_id = ?4
                               AND status != ?1",
                            params![STATUS_STOPPING, now, params.strategy_id, params.intent_id],
                        )?;
                    }
                    _ => {}
                }
            }
        }
        let experiment =
            Self::experiment_for(&transaction, &params.strategy_id, &params.intent_id)?;
        transaction.commit()?;
        Ok(CancelResult {
            strategy_id: params.strategy_id.clone(),
            intent_id: params.intent_id.clone(),
            cancel_receipt: receipt,
            experiment,
        })
    }

    pub fn query(&self, params: &QueryParams) -> Result<QueryResult, QuineError> {
        validate_ids(&params.strategy_id, &params.intent_id)?;
        let conn = self.lock()?;
        let experiment = Self::experiment_for(&conn, &params.strategy_id, &params.intent_id)?;
        let barrier: Option<Barrier> = conn
            .query_row(
                "SELECT strategy_id, intent_id, cancel_receipt, created_at
                 FROM barrier WHERE strategy_id = ?1 AND intent_id = ?2",
                params![params.strategy_id, params.intent_id],
                |row| {
                    Ok(Barrier {
                        strategy_id: row.get(0)?,
                        intent_id: row.get(1)?,
                        cancel_receipt: row.get(2)?,
                        created_at: row.get(3)?,
                    })
                },
            )
            .optional()?;
        Ok(QueryResult {
            experiment,
            barrier,
        })
    }

    pub fn list(&self, _params: &ListParams) -> Result<ListResult, QuineError> {
        let conn = self.lock()?;
        let mut statement = conn.prepare(
            "SELECT e.strategy_id, e.intent_id FROM experiment e ORDER BY e.created_at, e.strategy_id",
        )?;
        let rows = statement.query_map([], |row| {
            Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))
        })?;
        let mut experiments = Vec::new();
        for row in rows {
            let (strategy_id, intent_id) = row?;
            if let Some(experiment) = Self::experiment_for(&conn, &strategy_id, &intent_id)? {
                experiments.push(experiment);
            }
        }
        drop(statement);
        let mut statement = conn.prepare(
            "SELECT strategy_id, intent_id, cancel_receipt, created_at
             FROM barrier ORDER BY created_at, strategy_id",
        )?;
        let barriers = statement
            .query_map([], |row| {
                Ok(Barrier {
                    strategy_id: row.get(0)?,
                    intent_id: row.get(1)?,
                    cancel_receipt: row.get(2)?,
                    created_at: row.get(3)?,
                })
            })?
            .collect::<Result<Vec<_>, _>>()?;
        Ok(ListResult {
            experiments,
            barriers,
        })
    }

    /// Stage 2 seam: mark the single eligible attempt dispatched. This consumes
    /// the one allowance and records the dispatch owner; it starts no worker and
    /// never re-dispatches an already dispatched attempt. The returned record's
    /// `revision` is the fence the supervisor must carry into later seams.
    pub fn dispatch(
        &self,
        strategy_id: &str,
        intent_id: &str,
        dispatch_owner: &str,
        now: i64,
    ) -> Result<Experiment, QuineError> {
        validate_ids(strategy_id, intent_id)?;
        let mut conn = self.lock()?;
        let transaction = conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
        let current: Option<(String, Option<String>)> = transaction
            .query_row(
                "SELECT status, dispatch_owner FROM attempt WHERE strategy_id = ?1 AND intent_id = ?2",
                params![strategy_id, intent_id],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .optional()?;
        let (status, held_owner) = current.ok_or_else(|| {
            QuineError::new(
                CODE_NOT_FOUND,
                "no confirmed experiment matches this identity",
            )
        })?;
        match status.as_str() {
            STATUS_ELIGIBLE => {
                transaction.execute(
                    "UPDATE attempt SET status = ?1, active = 1, revision = revision + 1,
                       dispatch_owner = ?2, dispatched_at = ?3
                     WHERE strategy_id = ?4 AND intent_id = ?5",
                    params![
                        STATUS_DISPATCHED,
                        dispatch_owner,
                        now,
                        strategy_id,
                        intent_id
                    ],
                )?;
            }
            STATUS_DISPATCHED | STATUS_RUNNING | STATUS_STOPPING | STATUS_UNKNOWN => {
                if held_owner.as_deref() != Some(dispatch_owner) {
                    transaction.rollback().ok();
                    return Err(QuineError::new(
                        CODE_ALREADY_DISPATCHED,
                        "this experiment was already dispatched to another owner",
                    ));
                }
            }
            _ => {
                transaction.rollback().ok();
                return Err(QuineError::new(
                    CODE_INVALID_STATE,
                    format!("this experiment is {status} and cannot be dispatched"),
                ));
            }
        }
        let experiment =
            Self::experiment_for(&transaction, strategy_id, intent_id)?.ok_or_else(|| {
                QuineError::new(CODE_INTERNAL, "dispatched experiment lost its attempt")
            })?;
        transaction.commit()?;
        Ok(experiment)
    }

    /// Stage 2 seam: record that the worker actually started. Only the dispatch
    /// owner may advance it, and only from the exact dispatch fence it holds.
    pub fn mark_running(
        &self,
        strategy_id: &str,
        intent_id: &str,
        fence: i64,
    ) -> Result<Experiment, QuineError> {
        self.advance_lifecycle(
            strategy_id,
            intent_id,
            fence,
            &[STATUS_DISPATCHED],
            STATUS_RUNNING,
        )
    }

    /// Stage 2 seam: record that the worker exited without a proven outcome.
    /// The slot stays held; unknown termination never frees it.
    pub fn mark_unknown(
        &self,
        strategy_id: &str,
        intent_id: &str,
        fence: i64,
    ) -> Result<Experiment, QuineError> {
        self.advance_lifecycle(
            strategy_id,
            intent_id,
            fence,
            &[STATUS_DISPATCHED, STATUS_RUNNING],
            STATUS_UNKNOWN,
        )
    }

    fn advance_lifecycle(
        &self,
        strategy_id: &str,
        intent_id: &str,
        fence: i64,
        from: &[&str],
        to: &str,
    ) -> Result<Experiment, QuineError> {
        validate_ids(strategy_id, intent_id)?;
        let mut conn = self.lock()?;
        let transaction = conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
        let current: Option<(String, i64)> = transaction
            .query_row(
                "SELECT status, revision FROM attempt WHERE strategy_id = ?1 AND intent_id = ?2",
                params![strategy_id, intent_id],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .optional()?;
        let (status, revision) = current.ok_or_else(|| {
            QuineError::new(
                CODE_NOT_FOUND,
                "no confirmed experiment matches this identity",
            )
        })?;
        if !from.contains(&status.as_str()) {
            transaction.rollback().ok();
            return Err(QuineError::new(
                CODE_INVALID_STATE,
                format!("this experiment is {status} and cannot advance to {to}"),
            ));
        }
        if revision != fence {
            transaction.rollback().ok();
            return Err(QuineError::new(
                CODE_STALE_FENCE,
                format!("the record moved on from fence {fence}; re-read it before acting"),
            ));
        }
        transaction.execute(
            "UPDATE attempt SET status = ?1, active = 1, revision = revision + 1
             WHERE strategy_id = ?2 AND intent_id = ?3",
            params![to, strategy_id, intent_id],
        )?;
        let experiment =
            Self::experiment_for(&transaction, strategy_id, intent_id)?.ok_or_else(|| {
                QuineError::new(CODE_INTERNAL, "advanced experiment lost its attempt")
            })?;
        transaction.commit()?;
        Ok(experiment)
    }

    /// Stage 2 seam: commit one official, proven result. Every identity and the
    /// record fence must match the current attempt in the same transaction that
    /// arbitrates the cancellation barrier. A wrong attempt, job, request,
    /// frozen revision/digest, stale fence or accepted cancel never registers a
    /// success and never releases the slot.
    pub fn commit_result(
        &self,
        submission: &ResultSubmission,
        now: i64,
    ) -> Result<Experiment, QuineError> {
        let strategy_id = &submission.strategy_id;
        let intent_id = &submission.intent_id;
        let outcome = &submission.outcome;
        validate_ids(strategy_id, intent_id)?;
        if submission.draft_revision < 1 {
            return Err(QuineError::new(
                CODE_INVALID_REQUEST,
                "draft_revision must be a positive integer",
            ));
        }
        if !is_sha256(&submission.frozen_sha256) {
            return Err(QuineError::new(
                CODE_INVALID_REQUEST,
                "frozen_sha256 must be a lowercase 64-character SHA-256 hex digest",
            ));
        }
        if outcome != STATUS_SUCCEEDED && outcome != STATUS_FAILED {
            return Err(QuineError::new(
                CODE_INVALID_REQUEST,
                "outcome must be 'succeeded' or 'failed'",
            ));
        }
        let result_json = serde_json::to_string(&submission.result)
            .map_err(|error| QuineError::new(CODE_INVALID_REQUEST, error.to_string()))?;
        let mut conn = self.lock()?;
        let transaction = conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
        let current = transaction
            .query_row(
                "SELECT e.request_id, e.draft_revision, e.frozen_sha256, e.frozen_canonical,
                        a.status, a.revision, a.job_id, a.attempt_id, a.result_json,
                        a.commit_fence
                 FROM experiment e JOIN attempt a
                   ON a.strategy_id = e.strategy_id AND a.intent_id = e.intent_id
                 WHERE e.strategy_id = ?1 AND e.intent_id = ?2",
                params![strategy_id, intent_id],
                |row| {
                    Ok((
                        row.get::<_, String>(0)?,
                        row.get::<_, i64>(1)?,
                        row.get::<_, String>(2)?,
                        row.get::<_, Vec<u8>>(3)?,
                        row.get::<_, String>(4)?,
                        row.get::<_, i64>(5)?,
                        row.get::<_, String>(6)?,
                        row.get::<_, String>(7)?,
                        row.get::<_, Option<String>>(8)?,
                        row.get::<_, Option<i64>>(9)?,
                    ))
                },
            )
            .optional()?;
        let (
            request_id,
            draft_revision,
            frozen_sha256,
            frozen_canonical,
            status,
            revision,
            job_id,
            attempt_id,
            stored_result,
            commit_fence,
        ) = current.ok_or_else(|| {
            QuineError::new(
                CODE_NOT_FOUND,
                "no confirmed experiment matches this identity",
            )
        })?;
        if request_id != submission.request_id
            || job_id != submission.job_id
            || attempt_id != submission.attempt_id
        {
            transaction.rollback().ok();
            return Err(QuineError::new(
                CODE_CONFLICT,
                "the result does not match this experiment's request, job or attempt identity",
            ));
        }
        if draft_revision != submission.draft_revision {
            transaction.rollback().ok();
            return Err(QuineError::new(
                CODE_CONFLICT,
                "the result does not match this experiment's frozen draft revision",
            ));
        }
        // The retained frozen bytes stay bound to their recorded digest, and the
        // submission must name exactly that digest.
        if sha256_hex(&frozen_canonical) != frozen_sha256 {
            transaction.rollback().ok();
            return Err(QuineError::new(
                CODE_INTERNAL,
                "stored frozen input does not match its recorded digest",
            ));
        }
        if frozen_sha256 != submission.frozen_sha256 {
            transaction.rollback().ok();
            return Err(QuineError::new(
                CODE_DIGEST_MISMATCH,
                "the result does not match this experiment's frozen input digest",
            ));
        }
        if status == STATUS_SUCCEEDED || status == STATUS_FAILED {
            if status.as_str() != outcome.as_str() || stored_result.as_deref() != Some(&result_json)
            {
                transaction.rollback().ok();
                return Err(QuineError::new(
                    CODE_CONFLICT,
                    "this experiment already committed a different result",
                ));
            }
            if commit_fence != Some(submission.fence) {
                transaction.rollback().ok();
                return Err(QuineError::new(
                    CODE_STALE_FENCE,
                    "the result was committed under a different fence",
                ));
            }
            let experiment = Self::experiment_for(&transaction, strategy_id, intent_id)?
                .ok_or_else(|| {
                    QuineError::new(CODE_INTERNAL, "committed experiment lost its attempt")
                })?;
            transaction.commit()?;
            return Ok(experiment);
        }
        let barrier: Option<String> = transaction
            .query_row(
                "SELECT cancel_receipt FROM barrier WHERE strategy_id = ?1 AND intent_id = ?2",
                params![strategy_id, intent_id],
                |row| row.get(0),
            )
            .optional()?;
        if barrier.is_some() {
            transaction.rollback().ok();
            return Err(QuineError::new(
                CODE_INVALID_STATE,
                "a cancellation barrier was accepted; no result can be committed",
            ));
        }
        if revision != submission.fence {
            transaction.rollback().ok();
            return Err(QuineError::new(
                CODE_STALE_FENCE,
                format!(
                    "the record moved on from fence {}; re-read it before submitting",
                    submission.fence
                ),
            ));
        }
        if status != STATUS_DISPATCHED && status != STATUS_RUNNING {
            transaction.rollback().ok();
            return Err(QuineError::new(
                CODE_INVALID_STATE,
                format!("this experiment is {status} and cannot commit a result"),
            ));
        }
        transaction.execute(
            "UPDATE attempt SET status = ?1, result_json = ?2, committed_at = ?3,
               active = 0, revision = revision + 1, commit_fence = ?4
             WHERE strategy_id = ?5 AND intent_id = ?6",
            params![
                outcome,
                result_json,
                now,
                submission.fence,
                strategy_id,
                intent_id
            ],
        )?;
        let experiment =
            Self::experiment_for(&transaction, strategy_id, intent_id)?.ok_or_else(|| {
                QuineError::new(CODE_INTERNAL, "committed experiment lost its attempt")
            })?;
        transaction.commit()?;
        Ok(experiment)
    }

    /// Advance every attempt whose compute this process did not start into the
    /// honest `unknown` state. Called once when a core takes the workspace:
    /// a dispatched, running, stopping or already-unknown attempt may still own
    /// compute that no restart can prove ended, so it stays unknown and keeps
    /// the single active slot. An `eligible` attempt is untouched: dispatch is
    /// the first durable step before anything is spawned, so nothing ran.
    pub fn reconcile_uncertain(&self, now: i64) -> Result<usize, QuineError> {
        let mut conn = self.lock()?;
        let transaction = conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
        let changed = transaction.execute(
            "UPDATE attempt SET status = ?1, active = 1, revision = revision + 1
             WHERE status IN (?2, ?3, ?4)",
            params![
                STATUS_UNKNOWN,
                STATUS_DISPATCHED,
                STATUS_RUNNING,
                STATUS_STOPPING
            ],
        )?;
        transaction.commit()?;
        let _ = now;
        Ok(changed)
    }

    /// Record a stopped attempt whose whole group is proven gone after an
    /// accepted cancellation. Only a proven-absent group reaches here, and only
    /// from `stopping`: this is the one place a cancellation releases the slot.
    pub fn settle_cancelled(
        &self,
        strategy_id: &str,
        intent_id: &str,
        fence: i64,
        now: i64,
    ) -> Result<Experiment, QuineError> {
        validate_ids(strategy_id, intent_id)?;
        let mut conn = self.lock()?;
        let transaction = conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
        let current: Option<(String, i64)> = transaction
            .query_row(
                "SELECT status, revision FROM attempt WHERE strategy_id = ?1 AND intent_id = ?2",
                params![strategy_id, intent_id],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .optional()?;
        let (status, revision) = current.ok_or_else(|| {
            QuineError::new(
                CODE_NOT_FOUND,
                "no confirmed experiment matches this identity",
            )
        })?;
        if status != STATUS_STOPPING {
            transaction.rollback().ok();
            return Err(QuineError::new(
                CODE_INVALID_STATE,
                format!("this experiment is {status} and is not a cancelled stop"),
            ));
        }
        if revision != fence {
            transaction.rollback().ok();
            return Err(QuineError::new(
                CODE_STALE_FENCE,
                format!("the record moved on from fence {fence}; re-read it before acting"),
            ));
        }
        transaction.execute(
            "UPDATE attempt SET status = ?1, active = 0, revision = revision + 1,
               cancelled_at = COALESCE(cancelled_at, ?2)
             WHERE strategy_id = ?3 AND intent_id = ?4",
            params![STATUS_CANCELLED, now, strategy_id, intent_id],
        )?;
        let experiment = Self::experiment_for(&transaction, strategy_id, intent_id)?
            .ok_or_else(|| QuineError::new(CODE_INTERNAL, "settled experiment lost its attempt"))?;
        transaction.commit()?;
        Ok(experiment)
    }

    /// The exact frozen input bytes one confirmation retained, unparsed.
    pub fn frozen_input(
        &self,
        strategy_id: &str,
        intent_id: &str,
    ) -> Result<Option<Vec<u8>>, QuineError> {
        validate_ids(strategy_id, intent_id)?;
        let conn = self.lock()?;
        let stored: Option<(Vec<u8>, String)> = conn
            .query_row(
                "SELECT frozen_canonical, frozen_sha256 FROM experiment
                 WHERE strategy_id = ?1 AND intent_id = ?2",
                params![strategy_id, intent_id],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .optional()?;
        let Some((bytes, claimed)) = stored else {
            return Ok(None);
        };
        if sha256_hex(&bytes) != claimed {
            return Err(QuineError::new(
                CODE_INTERNAL,
                "stored frozen input does not match its recorded digest",
            ));
        }
        Ok(Some(bytes))
    }

    /// The complete current identity and fence of one attempt, so a supervisor
    /// never has to reconstruct it from several reads.
    pub fn attempt_context(
        &self,
        strategy_id: &str,
        intent_id: &str,
    ) -> Result<Option<AttemptContext>, QuineError> {
        validate_ids(strategy_id, intent_id)?;
        let conn = self.lock()?;
        let row: Option<AttemptContext> = conn
            .query_row(
                "SELECT a.status, a.revision, a.job_id, a.attempt_id, e.request_id,
                        e.draft_revision, e.frozen_sha256
                 FROM attempt a JOIN experiment e
                   ON e.strategy_id = a.strategy_id AND e.intent_id = a.intent_id
                 WHERE a.strategy_id = ?1 AND a.intent_id = ?2",
                params![strategy_id, intent_id],
                |row| {
                    Ok(AttemptContext {
                        status: row.get(0)?,
                        revision: row.get(1)?,
                        job_id: row.get(2)?,
                        attempt_id: row.get(3)?,
                        request_id: row.get(4)?,
                        draft_revision: row.get(5)?,
                        frozen_sha256: row.get(6)?,
                    })
                },
            )
            .optional()?;
        Ok(row)
    }

    /// The complete current identity and fence of one attempt.
    pub fn attempt_current(
        &self,
        strategy_id: &str,
        intent_id: &str,
    ) -> Result<AttemptContext, QuineError> {
        self.attempt_context(strategy_id, intent_id)?
            .ok_or_else(|| {
                QuineError::new(
                    CODE_NOT_FOUND,
                    "no confirmed experiment matches this identity",
                )
            })
    }

    fn experiment_for(
        conn: &Connection,
        strategy_id: &str,
        intent_id: &str,
    ) -> Result<Option<Experiment>, QuineError> {
        let row = conn
            .query_row(
                "SELECT e.request_id, e.draft_revision, e.preview_identity,
                        e.max_model_calls, e.max_backtests, e.frozen_sha256,
                        e.frozen_canonical, e.created_at,
                        a.status, a.job_id, a.attempt_id, a.active, a.revision,
                        a.dispatch_owner, a.result_json, a.dispatched_at,
                        a.committed_at, a.cancelled_at
                 FROM experiment e JOIN attempt a
                   ON a.strategy_id = e.strategy_id AND a.intent_id = e.intent_id
                 WHERE e.strategy_id = ?1 AND e.intent_id = ?2",
                params![strategy_id, intent_id],
                |row| {
                    Ok((
                        row.get::<_, String>(0)?,
                        row.get::<_, i64>(1)?,
                        row.get::<_, String>(2)?,
                        row.get::<_, i64>(3)?,
                        row.get::<_, i64>(4)?,
                        row.get::<_, String>(5)?,
                        row.get::<_, Vec<u8>>(6)?,
                        row.get::<_, i64>(7)?,
                        row.get::<_, String>(8)?,
                        row.get::<_, String>(9)?,
                        row.get::<_, String>(10)?,
                        row.get::<_, i64>(11)?,
                        row.get::<_, i64>(12)?,
                        row.get::<_, Option<String>>(13)?,
                        row.get::<_, Option<String>>(14)?,
                        row.get::<_, Option<i64>>(15)?,
                        row.get::<_, Option<i64>>(16)?,
                        row.get::<_, Option<i64>>(17)?,
                    ))
                },
            )
            .optional()?;
        let Some((
            request_id,
            draft_revision,
            preview_identity,
            model_calls,
            backtests,
            frozen_sha256,
            frozen_canonical,
            created_at,
            status,
            job_id,
            attempt_id,
            active,
            revision,
            dispatch_owner,
            result_json,
            dispatched_at,
            committed_at,
            cancelled_at,
        )) = row
        else {
            return Ok(None);
        };
        // The stored frozen bytes stay bound to the digest they were recorded
        // with; an edited authoritative record is refused rather than served.
        if sha256_hex(&frozen_canonical) != frozen_sha256 {
            return Err(QuineError::new(
                CODE_INTERNAL,
                "stored frozen input does not match its recorded digest",
            ));
        }
        let result =
            result_json.and_then(|text| serde_json::from_str::<serde_json::Value>(&text).ok());
        let dispatch_eligible = active == 1 && status == STATUS_ELIGIBLE;
        Ok(Some(Experiment {
            strategy_id: strategy_id.to_string(),
            intent_id: intent_id.to_string(),
            request_id,
            draft_revision,
            preview_identity,
            budget: Budget {
                max_model_calls: model_calls,
                max_backtests: backtests,
            },
            frozen_sha256,
            frozen_canonical: hex_encode(&frozen_canonical),
            status,
            job_id,
            attempt_id,
            dispatch_eligible,
            dispatch_owner,
            revision,
            result,
            created_at,
            dispatched_at,
            committed_at,
            cancelled_at,
        }))
    }
}
