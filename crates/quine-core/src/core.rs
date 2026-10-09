//! The one long-running core: it owns the workspace, the socket and every child.
//!
//! A single event owner accepts clients, answers confirmed-experiment methods
//! from SQLite, and advances each owned attempt through its phases without ever
//! blocking on a worker, on the stateless validator, or on a slow client. A
//! cancel or a query is answered in bounded time while compute is still running.
//!
//! Nothing here is a scheduling framework: there is one map of live attempts,
//! each holding at most one supervised child at a time.

use std::collections::HashMap;
use std::io::{self, Write};
use std::os::unix::fs::OpenOptionsExt;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use serde_json::{json, Value};

use crate::domain::{self, DomainReply};
use crate::ipc::{self, Conn};
use crate::protocol::{
    CancelParams, ConfirmParams, Envelope, ListParams, QueryParams, ReplyEnvelope, Request,
    PROTOCOL_VERSION,
};
use crate::result;
use crate::store::{
    QuineError, Store, STATUS_ELIGIBLE, STATUS_STOPPING, STATUS_SUCCEEDED, STATUS_UNKNOWN,
};
use crate::supervise::{ChildGuard, GuardState};
use crate::util::hex_decode;

pub const REQUEST_SCHEMA: &str = "ea.research-worker.request.v1";
pub const WORKER_RESULT_SCHEMA: &str = "ea.research-worker.result.v1";
pub const ARTIFACT_MISMATCH: &str = "artifact_mismatch";
pub const INPUT_CHANGED: &str = "input_changed";

/// How long one bounded supervision step may sleep while idle.
const IDLE_TICK: Duration = Duration::from_millis(3);

#[derive(Debug, Clone)]
pub struct CoreConfig {
    pub workspace: PathBuf,
    pub owner: String,
    pub python: PathBuf,
    pub socket: PathBuf,
    /// Test-only: the worker's own ``--test-hook`` seam. Never set by the IPC
    /// surface and never accepted from a client.
    pub test_hook: Option<String>,
    /// Test-only: hold the validated result before it is published, so a
    /// cancellation can be reproduced inside that exact window.
    pub commit_delay: Duration,
    /// Test-only: leave the process right after publication and before the
    /// durable registration, so the file/database crash gap is reproducible.
    pub crash_before_commit: bool,
    /// Test-only: alter the staged bytes after validation proved them, so the
    /// published-bytes check is reproducible.
    pub tamper_before_read: bool,
}

impl CoreConfig {
    pub fn new(workspace: &Path, python: &Path) -> CoreConfig {
        CoreConfig {
            workspace: workspace.to_path_buf(),
            owner: "quine-core".to_string(),
            python: python.to_path_buf(),
            socket: ipc::socket_path(workspace),
            test_hook: None,
            commit_delay: Duration::ZERO,
            crash_before_commit: false,
            tamper_before_read: false,
        }
    }
}

enum Phase {
    /// Nothing live: the attempt is terminal, or its compute is unproven.
    Idle,
    Freeze {
        guard: ChildGuard,
        dir: PathBuf,
    },
    Worker {
        guard: ChildGuard,
    },
    Validate {
        guard: ChildGuard,
        dir: PathBuf,
    },
    /// Validated and proven, waiting out the (test-only) publication window.
    Committing {
        ready_at: Instant,
        artifacts: Vec<(String, Vec<u8>)>,
        official: Value,
    },
}

struct Run {
    job_id: String,
    attempt_id: String,
    frozen: Value,
    control: PathBuf,
    /// A cancellation this core has accepted for this attempt.
    stop: bool,
    phase: Phase,
}

/// What one bounded supervision step proved, decided before anything is acted on.
#[derive(Clone, Copy, PartialEq, Eq)]
enum Action {
    None,
    Settle,
    Unknown,
    Commit,
}

pub struct Core {
    store: Store,
    config: CoreConfig,
    listener: std::os::unix::net::UnixListener,
    conns: Vec<Conn>,
    runs: HashMap<String, Run>,
}

impl Core {
    /// Take the workspace: acquire the exclusive lock, reconcile what a previous
    /// owner left unproven, then bind the private socket.
    pub fn open(config: CoreConfig) -> Result<Core, QuineError> {
        for directory in ["inputs", "control", "official", "tmp/domain"] {
            std::fs::create_dir_all(config.workspace.join(directory))?;
        }
        let store = Store::open(&config.workspace.join("quine.db"), &config.owner)?;
        store.reconcile_uncertain(now())?;
        let listener = ipc::bind(&config.socket)?;
        Ok(Core {
            store,
            config,
            listener,
            conns: Vec::new(),
            runs: HashMap::new(),
        })
    }

    pub fn store(&self) -> &Store {
        &self.store
    }

    /// One event owner: accept, answer, advance, sleep. It never waits on a
    /// child or on a client.
    pub fn serve(&mut self) -> io::Result<()> {
        loop {
            self.accept();
            self.pump();
            self.tick();
            std::thread::sleep(IDLE_TICK);
        }
    }

    fn accept(&mut self) {
        loop {
            match self.listener.accept() {
                Ok((stream, _)) => {
                    if let Ok(conn) = Conn::new(stream) {
                        self.conns.push(conn);
                    }
                }
                Err(error) if error.kind() == io::ErrorKind::WouldBlock => break,
                Err(_) => break,
            }
        }
    }

    fn pump(&mut self) {
        let mut pending: Vec<(usize, String)> = Vec::new();
        for (index, conn) in self.conns.iter_mut().enumerate() {
            for line in conn.poll_lines() {
                pending.push((index, line));
            }
        }
        for (index, line) in pending {
            let reply = self.handle_line(&line);
            let payload = serde_json::to_vec(&reply).unwrap_or_else(|_| b"{}".to_vec());
            if let Some(conn) = self.conns.get_mut(index) {
                conn.queue(payload);
            }
        }
        for conn in self.conns.iter_mut() {
            conn.flush();
        }
        self.conns.retain(|conn| !conn.closed);
    }

    fn handle_line(&mut self, line: &str) -> ReplyEnvelope {
        let envelope: Envelope = match serde_json::from_str(line) {
            Ok(envelope) => envelope,
            Err(error) => {
                return ReplyEnvelope::err(
                    "unknown",
                    crate::store::CODE_INVALID_REQUEST,
                    format!("request is not readable JSON: {error}"),
                )
            }
        };
        if envelope.protocol != PROTOCOL_VERSION {
            return ReplyEnvelope::err(
                "unknown",
                crate::store::CODE_INVALID_REQUEST,
                format!("unsupported protocol {}", envelope.protocol),
            );
        }
        match envelope.request {
            Request::Confirm { params } => self.confirm(&params),
            Request::Cancel { params } => self.cancel(&params),
            Request::Query { params } => self.query(&params),
            Request::List { params } => self.list(&params),
        }
    }

    // -- confirmed experiment methods ---------------------------------------

    fn confirm(&mut self, params: &ConfirmParams) -> ReplyEnvelope {
        match self.store.confirm(params, now()) {
            Ok(confirmed) => {
                let reply = serde_json::to_value(&confirmed).unwrap_or(Value::Null);
                // Dispatch is wired to the confirmation exactly once. A replayed
                // confirmation is not a fresh spawn grant: only an attempt that
                // is still `eligible` has never been dispatched, and only that
                // state may begin a run here.
                if confirmed.experiment.status == STATUS_ELIGIBLE {
                    let key = run_key(&params.strategy_id, &params.intent_id);
                    if !self.runs.contains_key(&key) {
                        if let Err(error) = self.begin_run(&params.strategy_id, &params.intent_id) {
                            self.record_failure(
                                &params.strategy_id,
                                &params.intent_id,
                                INPUT_CHANGED,
                                &error.message,
                            );
                        }
                    }
                }
                ReplyEnvelope::ok("confirm", reply)
            }
            Err(error) => ReplyEnvelope::err("confirm", &error.code, error.message),
        }
    }

    fn cancel(&mut self, params: &CancelParams) -> ReplyEnvelope {
        match self.store.cancel(params, now()) {
            Ok(cancelled) => {
                let key = run_key(&params.strategy_id, &params.intent_id);
                if let Some(run) = self.runs.get_mut(&key) {
                    // Only an accepted cancellation reaches a live child, and it
                    // is never itself proof that the child has stopped.
                    run.stop = true;
                }
                let reply = serde_json::to_value(&cancelled).unwrap_or(Value::Null);
                ReplyEnvelope::ok("cancel", reply)
            }
            Err(error) => ReplyEnvelope::err("cancel", &error.code, error.message),
        }
    }

    fn query(&mut self, params: &QueryParams) -> ReplyEnvelope {
        match self.store.query(params) {
            Ok(queried) => ReplyEnvelope::ok(
                "query",
                serde_json::to_value(&queried).unwrap_or(Value::Null),
            ),
            Err(error) => ReplyEnvelope::err("query", &error.code, error.message),
        }
    }

    fn list(&mut self, params: &ListParams) -> ReplyEnvelope {
        match self.store.list(params) {
            Ok(listed) => {
                ReplyEnvelope::ok("list", serde_json::to_value(&listed).unwrap_or(Value::Null))
            }
            Err(error) => ReplyEnvelope::err("list", &error.code, error.message),
        }
    }

    // -- run lifecycle -------------------------------------------------------

    /// Dispatch one eligible attempt durably, then start its frozen-input check.
    fn begin_run(&mut self, strategy_id: &str, intent_id: &str) -> Result<(), QuineError> {
        let bytes = self
            .store
            .frozen_input(strategy_id, intent_id)?
            .ok_or_else(|| {
                QuineError::new(crate::store::CODE_NOT_FOUND, "no confirmed experiment")
            })?;
        let frozen: Value = serde_json::from_slice(&bytes).map_err(|error| {
            QuineError::new(
                INPUT_CHANGED,
                format!("frozen input is unreadable: {error}"),
            )
        })?;
        // The durable dispatch is the first step: nothing is spawned, and no
        // file is written, before the store says this attempt was dispatched.
        let dispatched = self
            .store
            .dispatch(strategy_id, intent_id, &self.config.owner, now())?;
        let control = self
            .config
            .workspace
            .join("control")
            .join(&dispatched.job_id);
        std::fs::create_dir_all(&control)?;
        let run = Run {
            job_id: dispatched.job_id.clone(),
            attempt_id: dispatched.attempt_id.clone(),
            frozen,
            control,
            stop: false,
            phase: Phase::Idle,
        };
        let key = run_key(strategy_id, intent_id);
        self.runs.insert(key.clone(), run);
        self.start_freeze(&key)
    }

    /// Re-verify the frozen input as a supervised child before anything is run.
    fn start_freeze(&mut self, key: &str) -> Result<(), QuineError> {
        let (dir, argv) = {
            let run = match self.runs.get(key) {
                Some(run) => run,
                None => return Ok(()),
            };
            let dir = domain::staging_dir(&self.config.workspace, &run.job_id);
            let _ = std::fs::remove_dir_all(&dir);
            let request = domain::write_request(&dir, &json!({"frozen": run.frozen}))?;
            let reply = domain::reply_path(&dir);
            let argv = domain::argv(&self.config.python, "freeze", &request, &reply);
            (dir, argv)
        };
        let guard = ChildGuard::spawn(&argv, None, true).map_err(|error| {
            QuineError::new(
                INPUT_CHANGED,
                format!("the input check did not start: {error}"),
            )
        })?;
        if let Some(run) = self.runs.get_mut(key) {
            run.phase = Phase::Freeze { guard, dir };
        }
        Ok(())
    }

    /// Advance every owned attempt by one bounded step.
    fn tick(&mut self) {
        let keys: Vec<String> = self.runs.keys().cloned().collect();
        for key in keys {
            let stop = self.runs.get(&key).map(|run| run.stop).unwrap_or(false);
            let action = match self.runs.get_mut(&key) {
                Some(run) => match &mut run.phase {
                    Phase::Idle => Action::None,
                    Phase::Freeze { guard, .. }
                    | Phase::Worker { guard }
                    | Phase::Validate { guard, .. } => match guard.poll(stop) {
                        GuardState::Running => Action::None,
                        GuardState::Stopped => Action::Settle,
                        GuardState::Unconfirmed | GuardState::Abandoned => Action::Unknown,
                    },
                    Phase::Committing { ready_at, .. } => {
                        if stop {
                            Action::Settle
                        } else if Instant::now() >= *ready_at {
                            Action::Commit
                        } else {
                            Action::None
                        }
                    }
                },
                None => Action::None,
            };
            match action {
                Action::None => {}
                Action::Settle => self.settle(&key),
                Action::Unknown => self.mark_unknown(&key),
                Action::Commit => self.commit_validated(&key),
            }
        }
    }

    fn settle(&mut self, key: &str) {
        let stop = self.runs.get(key).map(|run| run.stop).unwrap_or(false);
        if stop {
            self.settle_cancelled(key);
            return;
        }
        let phase = match self.runs.get_mut(key) {
            Some(run) => std::mem::replace(&mut run.phase, Phase::Idle),
            None => return,
        };
        match phase {
            Phase::Freeze { dir, .. } => self.finish_freeze(key, &dir),
            Phase::Worker { .. } => self.finish_worker(key),
            Phase::Validate { dir, .. } => self.finish_validate(key, &dir),
            Phase::Idle | Phase::Committing { .. } => {}
        }
    }

    fn finish_freeze(&mut self, key: &str, dir: &Path) {
        let reply = match domain::read_reply(dir)
            .ok()
            .and_then(|value| DomainReply::decode(&value))
        {
            Some(reply) if reply.ok() => reply,
            Some(reply) => {
                let code = reply.refusal_code();
                let message = reply
                    .message
                    .unwrap_or_else(|| "the frozen input was refused".to_string());
                self.record_failure_for(key, &code, &message);
                return;
            }
            None => {
                self.record_failure_for(key, INPUT_CHANGED, "the input check produced no reply");
                return;
            }
        };
        if let Err(error) = self.materialize_and_spawn(key, reply.result.unwrap_or(Value::Null)) {
            self.record_failure_for(key, INPUT_CHANGED, &error.message);
        }
    }

    /// Write the frozen files, then spawn the existing worker directly.
    fn materialize_and_spawn(&mut self, key: &str, frozen_result: Value) -> Result<(), QuineError> {
        let (run_dir, argv) = {
            let run = match self.runs.get(key) {
                Some(run) => run,
                None => return Ok(()),
            };
            let inputs = self.config.workspace.join("inputs");
            let scenario_path = inputs.join(format!("{}.json", run.job_id));
            write_once(
                &scenario_path,
                &decode_hex_field(&frozen_result, "scenario_file")?,
            )?;
            let package_path = match frozen_result.get("package_file") {
                Some(Value::String(encoded)) => {
                    let path = inputs.join(format!("{}.eastrategy", run.job_id));
                    write_once(&path, &decode_hex(encoded)?)?;
                    Some(path)
                }
                _ => None,
            };
            let request = json!({
                "schema": REQUEST_SCHEMA,
                "job_id": run.job_id,
                "attempt_id": run.attempt_id,
                "scenario": {
                    "path": scenario_path.display().to_string(),
                    "sha256": frozen_result.get("scenario_file_sha256").cloned().unwrap_or(Value::Null),
                    "identity_sha256": frozen_result
                        .get("scenario_identity_sha256")
                        .cloned()
                        .unwrap_or(Value::Null),
                },
                "package": package_path.as_ref().map(|path| json!({
                    "path": path.display().to_string(),
                    "sha256": frozen_result.get("package_sha256").cloned().unwrap_or(Value::Null),
                })),
                "staging": {"runs": "runs", "reports": "reports"},
            });
            let mut argv = vec![
                self.config.python.display().to_string(),
                "-I".to_string(),
                "-B".to_string(),
                "-m".to_string(),
                "ea.web.worker".to_string(),
                run.control.display().to_string(),
            ];
            if let Some(hook) = &self.config.test_hook {
                argv.push("--test-hook".to_string());
                argv.push(hook.clone());
            }
            write_once(
                &run.control.join("request.json"),
                &serde_json::to_vec(&request).map_err(io::Error::other)?,
            )?;
            (run.control.clone(), argv)
        };
        let _ = run_dir;
        let guard = ChildGuard::spawn(&argv, None, true).map_err(|error| {
            QuineError::new(INPUT_CHANGED, format!("the worker did not start: {error}"))
        })?;
        // Record that this attempt's compute actually started, at the fence the
        // dispatch produced.
        let (strategy_id, intent_id) = split_key(key);
        let context = self.store.attempt_current(&strategy_id, &intent_id)?;
        self.store
            .mark_running(&strategy_id, &intent_id, context.revision)?;
        if let Some(run) = self.runs.get_mut(key) {
            run.phase = Phase::Worker { guard };
        }
        Ok(())
    }

    fn finish_worker(&mut self, key: &str) {
        let control = match self.runs.get(key) {
            Some(run) => run.control.clone(),
            None => return,
        };
        let document = match std::fs::read(control.join("result.json"))
            .ok()
            .and_then(|payload| serde_json::from_slice::<Value>(&payload).ok())
        {
            Some(document) => document,
            None => {
                self.record_failure_for(
                    key,
                    "worker_result_missing",
                    "the worker exited without a readable result",
                );
                return;
            }
        };
        let (job_id, attempt_id) = match self.runs.get(key) {
            Some(run) => (run.job_id.clone(), run.attempt_id.clone()),
            None => return,
        };
        let status = match result::worker_result(&document, &job_id, &attempt_id) {
            Ok(status) => status,
            Err(message) => {
                self.record_failure_for(key, "worker_result_invalid", &message);
                return;
            }
        };
        if status == "failed" {
            let code = document
                .get("code")
                .and_then(Value::as_str)
                .unwrap_or("worker_failed")
                .to_string();
            let message = document
                .get("message")
                .and_then(Value::as_str)
                .unwrap_or("the worker reported a failed run")
                .to_string();
            self.record_failure_for(key, &code, &message);
            return;
        }
        // The staged paths are derived from this core's own identities; the
        // worker's own references may only agree with them.
        let attempt_relative = result::expected_relative("runs", &attempt_id);
        let report_relative = result::expected_relative("reports", &job_id);
        if !result::reference_agrees(document.get("attempt_relative"), &attempt_relative, true)
            || !result::reference_agrees(document.get("report_relative"), &report_relative, true)
        {
            self.record_failure_for(
                key,
                ARTIFACT_MISMATCH,
                "the worker named a staged artifact that is not this attempt's",
            );
            return;
        }
        let frozen = match self.runs.get(key) {
            Some(run) => run.frozen.clone(),
            None => return,
        };
        let dir = domain::staging_dir(&self.config.workspace, &format!("{job_id}-validate"));
        let _ = std::fs::remove_dir_all(&dir);
        let request = json!({
            "frozen": frozen,
            "attempt_id": attempt_id,
            "job_id": job_id,
            "attempt_dir": result::staged_attempt(&control, &attempt_id).display().to_string(),
            "report_dir": result::staged_report(&control, &job_id).display().to_string(),
        });
        let started = domain::write_request(&dir, &request).and_then(|request_path| {
            let argv = domain::argv(
                &self.config.python,
                "validate",
                &request_path,
                &domain::reply_path(&dir),
            );
            ChildGuard::spawn(&argv, None, true)
        });
        match started {
            Ok(guard) => {
                if let Some(run) = self.runs.get_mut(key) {
                    run.phase = Phase::Validate { guard, dir };
                }
            }
            Err(error) => self.record_failure_for(
                key,
                "validation_unavailable",
                &format!("the validator did not start: {error}"),
            ),
        }
    }

    fn finish_validate(&mut self, key: &str, dir: &Path) {
        let reply = match domain::read_reply(dir)
            .ok()
            .and_then(|value| DomainReply::decode(&value))
        {
            Some(reply) if reply.ok() => reply,
            Some(reply) => {
                let code = reply.refusal_code();
                let message = reply
                    .message
                    .unwrap_or_else(|| "the run's evidence was refused".to_string());
                self.record_failure_for(key, &code, &message);
                return;
            }
            None => {
                self.record_failure_for(
                    key,
                    "validation_unavailable",
                    "the validator produced no reply",
                );
                return;
            }
        };
        let adapter = reply.result.unwrap_or(Value::Null);
        let (control, job_id, attempt_id) = match self.runs.get(key) {
            Some(run) => (
                run.control.clone(),
                run.job_id.clone(),
                run.attempt_id.clone(),
            ),
            None => return,
        };
        let report_dir = result::staged_report(&control, &job_id);
        if self.config.tamper_before_read {
            let summary = report_dir.join("summary.txt");
            if let Ok(mut file) = std::fs::OpenOptions::new().append(true).open(&summary) {
                let _ = file.write_all(b"tampered\n");
            }
        }
        let artifacts = match result::read_artifacts(&report_dir) {
            Ok(artifacts) => artifacts,
            Err(error) => {
                self.record_failure_for(
                    key,
                    ARTIFACT_MISMATCH,
                    &format!("the staged report is not readable: {error}"),
                );
                return;
            }
        };
        // What is published is the byte set read here, and it must be exactly
        // the set the independent validation proved.
        for (name, payload) in &artifacts {
            let expected = adapter
                .get("artifacts")
                .and_then(|value| value.get(name))
                .and_then(Value::as_str);
            let actual = crate::util::sha256_hex(payload);
            if expected != Some(actual.as_str()) {
                self.record_failure_for(
                    key,
                    ARTIFACT_MISMATCH,
                    &format!("the staged {name} is not the artifact that was verified"),
                );
                return;
            }
        }
        let official = result::official_document(&job_id, &attempt_id, &adapter, &artifacts);
        let ready_at = Instant::now() + self.config.commit_delay;
        if let Some(run) = self.runs.get_mut(key) {
            run.phase = Phase::Committing {
                ready_at,
                artifacts,
                official,
            };
        }
    }

    /// Publish the verified bytes durably, then register them under the current
    /// fence in one arbitrated write. A cancellation accepted first wins.
    fn commit_validated(&mut self, key: &str) {
        let (artifacts, official, job_id) = match self.runs.get_mut(key) {
            Some(run) => match std::mem::replace(&mut run.phase, Phase::Idle) {
                Phase::Committing {
                    artifacts,
                    official,
                    ..
                } => (artifacts, official, run.job_id.clone()),
                other => {
                    run.phase = other;
                    return;
                }
            },
            None => return,
        };
        if let Err(error) = result::publish(&self.config.workspace, &job_id, &artifacts) {
            self.record_failure_for(
                key,
                "publication_failed",
                &format!("the verified result could not be published: {error}"),
            );
            return;
        }
        if self.config.crash_before_commit {
            // Test-only: the exact window between a durable publication and the
            // durable registration of it.
            std::process::exit(70);
        }
        let (strategy_id, intent_id) = split_key(key);
        let context = match self.store.attempt_context(&strategy_id, &intent_id) {
            Ok(Some(context)) => context,
            _ => return,
        };
        let submission = crate::protocol::ResultSubmission {
            strategy_id: strategy_id.clone(),
            intent_id: intent_id.clone(),
            request_id: context.request_id,
            job_id: context.job_id,
            attempt_id: context.attempt_id,
            draft_revision: context.draft_revision,
            frozen_sha256: context.frozen_sha256,
            fence: context.revision,
            outcome: STATUS_SUCCEEDED.to_string(),
            result: official,
        };
        match self.store.commit_result(&submission, now()) {
            Ok(_) => {}
            Err(error) => {
                // A cancellation accepted while this result was being validated
                // and published must win: nothing was registered, and the slot is
                // released only once the whole group is proven gone.
                if error.code == crate::store::CODE_INVALID_STATE {
                    self.settle_cancelled(key);
                }
            }
        }
    }

    // -- terminal helpers ----------------------------------------------------

    /// A cancellation this core accepted, with the whole group proven gone.
    fn settle_cancelled(&mut self, key: &str) {
        let (strategy_id, intent_id) = split_key(key);
        let context = match self.store.attempt_context(&strategy_id, &intent_id) {
            Ok(Some(context)) => context,
            _ => return,
        };
        if context.status != STATUS_STOPPING {
            return;
        }
        let _ = self
            .store
            .settle_cancelled(&strategy_id, &intent_id, context.revision, now());
        if let Some(run) = self.runs.get_mut(key) {
            run.phase = Phase::Idle;
        }
    }

    /// A group that could not be shown gone keeps the slot and stays unknown.
    fn mark_unknown(&mut self, key: &str) {
        let (strategy_id, intent_id) = split_key(key);
        if let Ok(Some(context)) = self.store.attempt_context(&strategy_id, &intent_id) {
            let _ = self
                .store
                .mark_unknown(&strategy_id, &intent_id, context.revision);
        }
        if let Some(run) = self.runs.get_mut(key) {
            run.phase = Phase::Idle;
        }
    }

    fn record_failure_for(&mut self, key: &str, code: &str, message: &str) {
        let (strategy_id, intent_id) = split_key(key);
        self.record_failure(&strategy_id, &intent_id, code, message);
        if let Some(run) = self.runs.get_mut(key) {
            run.phase = Phase::Idle;
        }
    }

    /// Register one failed attempt, or settle an accepted cancellation instead.
    fn record_failure(&mut self, strategy_id: &str, intent_id: &str, code: &str, message: &str) {
        let context = match self.store.attempt_context(strategy_id, intent_id) {
            Ok(Some(context)) => context,
            _ => return,
        };
        if context.status == STATUS_STOPPING {
            let _ = self
                .store
                .settle_cancelled(strategy_id, intent_id, context.revision, now());
            return;
        }
        if context.status == STATUS_UNKNOWN {
            return;
        }
        let submission = crate::protocol::ResultSubmission {
            strategy_id: strategy_id.to_string(),
            intent_id: intent_id.to_string(),
            request_id: context.request_id,
            job_id: context.job_id,
            attempt_id: context.attempt_id,
            draft_revision: context.draft_revision,
            frozen_sha256: context.frozen_sha256,
            fence: context.revision,
            outcome: "failed".to_string(),
            result: result::failure_document(code, message),
        };
        let _ = self.store.commit_result(&submission, now());
    }
}

fn run_key(strategy_id: &str, intent_id: &str) -> String {
    format!("{strategy_id}\u{0}{intent_id}")
}

fn split_key(key: &str) -> (String, String) {
    match key.split_once('\u{0}') {
        Some((strategy_id, intent_id)) => (strategy_id.to_string(), intent_id.to_string()),
        None => (key.to_string(), String::new()),
    }
}

fn decode_hex(encoded: &str) -> Result<Vec<u8>, QuineError> {
    hex_decode(encoded).ok_or_else(|| QuineError::new(INPUT_CHANGED, "frozen bytes are not hex"))
}

fn decode_hex_field(document: &Value, field: &str) -> Result<Vec<u8>, QuineError> {
    let encoded = document
        .get(field)
        .and_then(Value::as_str)
        .ok_or_else(|| QuineError::new(INPUT_CHANGED, format!("frozen {field} is missing")))?;
    decode_hex(encoded)
}

/// Write one file durably, and never over an existing one.
fn write_once(path: &Path, payload: &[u8]) -> Result<(), QuineError> {
    let mut file = std::fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(path)?;
    file.write_all(payload)?;
    file.sync_all()?;
    if let Some(parent) = path.parent() {
        let directory = std::fs::File::open(parent)?;
        directory.sync_all()?;
    }
    Ok(())
}

pub fn now() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_secs() as i64)
        .unwrap_or(0)
}
