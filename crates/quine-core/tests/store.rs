//! Focused store behavior tests: replay/conflict, cancel barriers, reopen
//! identity, the exclusive owner lock, the single active slot across the whole
//! admitted run, and the identity-bound result submission fence.

use std::path::PathBuf;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::time::{SystemTime, UNIX_EPOCH};

use quine_core::protocol::{
    Budget, CancelParams, ConfirmParams, Experiment, FrozenInput, QueryParams, ResultSubmission,
};
use quine_core::store::{
    Store, CODE_CANCELLED_BEFORE_CONFIRM, CODE_CONFLICT, CODE_DIGEST_MISMATCH,
    CODE_ELIGIBILITY_HELD, CODE_INVALID_STATE, CODE_LATE_CANCEL_REFUSED, CODE_LEASE_HELD,
    CODE_STALE_FENCE, STATUS_DISPATCHED, STATUS_ELIGIBLE, STATUS_RUNNING, STATUS_STOPPING,
    STATUS_SUCCEEDED, STATUS_UNKNOWN,
};
use quine_core::util::{hex_encode, sha256_hex};
use serde_json::json;

static COUNTER: AtomicUsize = AtomicUsize::new(0);

fn temp_dir(tag: &str) -> PathBuf {
    let count = COUNTER.fetch_add(1, Ordering::SeqCst);
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    let dir = std::env::temp_dir().join(format!(
        "quine-core-{tag}-{}-{count}-{nanos}",
        std::process::id()
    ));
    std::fs::create_dir_all(&dir).unwrap();
    dir
}

fn frozen(payload: &serde_json::Value) -> FrozenInput {
    let bytes = quine_core::canonical::canonical_json_bytes(payload).unwrap();
    FrozenInput {
        canonical_bytes: hex_encode(&bytes),
        sha256: sha256_hex(&bytes),
    }
}

fn confirm(
    strategy: &str,
    intent: &str,
    revision: i64,
    payload: &serde_json::Value,
) -> ConfirmParams {
    let input = frozen(payload);
    ConfirmParams {
        strategy_id: strategy.to_string(),
        intent_id: intent.to_string(),
        draft_revision: revision,
        preview_identity: format!("sha256:{}", input.sha256),
        budget: Budget {
            max_model_calls: 0,
            max_backtests: 1,
        },
        frozen_input: input,
    }
}

fn payload(initial_cash: &str) -> serde_json::Value {
    json!({
        "draft_revision": 1,
        "initial_cash": initial_cash,
        "parameters": {"fast": 3, "slow": 9},
    })
}

fn submission(
    experiment: &Experiment,
    outcome: &str,
    result: &serde_json::Value,
) -> ResultSubmission {
    ResultSubmission {
        strategy_id: experiment.strategy_id.clone(),
        intent_id: experiment.intent_id.clone(),
        request_id: experiment.request_id.clone(),
        job_id: experiment.job_id.clone(),
        attempt_id: experiment.attempt_id.clone(),
        draft_revision: experiment.draft_revision,
        frozen_sha256: experiment.frozen_sha256.clone(),
        fence: experiment.revision,
        outcome: outcome.to_string(),
        result: result.clone(),
    }
}

fn query_experiment(store: &Store, strategy: &str, intent: &str) -> Experiment {
    store
        .query(&QueryParams {
            strategy_id: strategy.to_string(),
            intent_id: intent.to_string(),
        })
        .unwrap()
        .experiment
        .unwrap()
}

fn cancel_params(strategy: &str, intent: &str) -> CancelParams {
    CancelParams {
        strategy_id: strategy.to_string(),
        intent_id: intent.to_string(),
    }
}

fn confirm_s1(store: &Store, now: i64) {
    store
        .confirm(&confirm("s1", "i1", 1, &payload("10000.00")), now)
        .unwrap();
}

fn confirm_s2(store: &Store, now: i64) {
    store
        .confirm(&confirm("s2", "i2", 1, &payload("20000.00")), now)
        .unwrap();
}

#[test]
fn confirm_registers_eligible_attempt_atomically() {
    let dir = temp_dir("confirm-atomically");
    let store = Store::open(&dir.join("quine.db"), "owner").unwrap();
    let reply = store
        .confirm(&confirm("s1", "i1", 1, &payload("10000.00")), 1_001)
        .unwrap();
    assert!(!reply.replayed);
    assert_eq!(reply.experiment.status, STATUS_ELIGIBLE);
    assert_eq!(reply.experiment.strategy_id, "s1");
    assert_eq!(reply.experiment.intent_id, "i1");
    assert!(reply.experiment.dispatch_eligible);
    assert_eq!(reply.experiment.revision, 1);
    assert!(!reply.experiment.job_id.is_empty());
    assert!(!reply.experiment.attempt_id.is_empty());
    assert!(!reply.experiment.request_id.is_empty());
    assert_eq!(reply.experiment.created_at, 1_001);
    store.release().unwrap();
}

#[test]
fn same_id_same_content_replays_original() {
    let dir = temp_dir("replay");
    let store = Store::open(&dir.join("quine.db"), "owner").unwrap();
    let first = store
        .confirm(&confirm("s1", "i1", 1, &payload("10000.00")), 1_001)
        .unwrap();
    let second = store
        .confirm(&confirm("s1", "i1", 1, &payload("10000.00")), 1_002)
        .unwrap();
    assert!(second.replayed);
    assert_eq!(first.experiment.request_id, second.experiment.request_id);
    assert_eq!(first.experiment.job_id, second.experiment.job_id);
    assert_eq!(first.experiment.attempt_id, second.experiment.attempt_id);
    store.release().unwrap();
}

#[test]
fn same_id_different_content_conflicts() {
    let dir = temp_dir("conflict");
    let store = Store::open(&dir.join("quine.db"), "owner").unwrap();
    store
        .confirm(&confirm("s1", "i1", 1, &payload("10000.00")), 1_001)
        .unwrap();
    let error = store
        .confirm(&confirm("s1", "i1", 2, &payload("10000.00")), 1_002)
        .err()
        .unwrap();
    assert_eq!(error.code, CODE_CONFLICT);
    store.release().unwrap();
}

#[test]
fn cancel_before_confirm_records_durable_barrier() {
    let dir = temp_dir("cancel-before");
    let store = Store::open(&dir.join("quine.db"), "owner").unwrap();
    let cancelled = store.cancel(&cancel_params("s1", "i1"), 1_001).unwrap();
    let receipt = cancelled.cancel_receipt.clone();
    assert!(!receipt.is_empty());
    assert!(cancelled.experiment.is_none());

    let query = store
        .query(&QueryParams {
            strategy_id: "s1".into(),
            intent_id: "i1".into(),
        })
        .unwrap();
    assert!(query.experiment.is_none());
    assert_eq!(query.barrier.unwrap().cancel_receipt, receipt);

    let error = store
        .confirm(&confirm("s1", "i1", 1, &payload("10000.00")), 1_002)
        .err()
        .unwrap();
    assert_eq!(error.code, CODE_CANCELLED_BEFORE_CONFIRM);
    store.release().unwrap();
}

#[test]
fn reopen_after_lost_receipt_keeps_identity() {
    let dir = temp_dir("reopen");
    let db = dir.join("quine.db");
    let store = Store::open(&db, "owner").unwrap();
    let first = store
        .confirm(&confirm("s1", "i1", 1, &payload("10000.00")), 1_001)
        .unwrap();
    let request_id = first.experiment.request_id.clone();
    let job_id = first.experiment.job_id.clone();
    let attempt_id = first.experiment.attempt_id.clone();
    store.release().unwrap();

    // The reply above was "lost": a fresh process reopens the same workspace.
    let reopened = Store::open(&db, "owner").unwrap();
    let experiment = query_experiment(&reopened, "s1", "i1");
    assert_eq!(experiment.request_id, request_id);
    assert_eq!(experiment.job_id, job_id);
    assert_eq!(experiment.attempt_id, attempt_id);
    assert_eq!(experiment.status, STATUS_ELIGIBLE);
    reopened.release().unwrap();
}

#[test]
fn frozen_identity_binds_digest() {
    let dir = temp_dir("digest");
    let store = Store::open(&dir.join("quine.db"), "owner").unwrap();
    let mut params = confirm("s1", "i1", 1, &payload("10000.00"));
    params.frozen_input.sha256 = "0".repeat(64);
    let error = store.confirm(&params, 1_001).err().unwrap();
    assert_eq!(error.code, CODE_DIGEST_MISMATCH);
    store.release().unwrap();
}

#[test]
fn exclusive_owner_lock_blocks_same_and_different_owner() {
    let dir = temp_dir("lock-exclusive");
    let db = dir.join("quine.db");
    let first = Store::open(&db, "owner-a").unwrap();
    let same = Store::open(&db, "owner-a").err().unwrap();
    assert_eq!(same.code, CODE_LEASE_HELD);
    let different = Store::open(&db, "owner-b").err().unwrap();
    assert_eq!(different.code, CODE_LEASE_HELD);
    first.release().unwrap();
    let reopened = Store::open(&db, "owner-b").unwrap();
    reopened.release().unwrap();
}

#[test]
fn confirm_requires_the_single_active_slot() {
    let dir = temp_dir("single-eligible");
    let store = Store::open(&dir.join("quine.db"), "owner").unwrap();
    confirm_s1(&store, 1_001);
    let error = store
        .confirm(&confirm("s2", "i2", 1, &payload("20000.00")), 1_002)
        .err()
        .unwrap();
    assert_eq!(error.code, CODE_ELIGIBILITY_HELD);
    store.release().unwrap();
}

#[test]
fn dispatch_does_not_free_slot_for_second_confirmation() {
    let dir = temp_dir("slot-dispatch-holds");
    let store = Store::open(&dir.join("quine.db"), "owner").unwrap();
    confirm_s1(&store, 1_001);
    store.dispatch("s1", "i1", "supervisor", 1_002).unwrap();
    let error = store
        .confirm(&confirm("s2", "i2", 1, &payload("20000.00")), 1_003)
        .err()
        .unwrap();
    assert_eq!(error.code, CODE_ELIGIBILITY_HELD);
    store.release().unwrap();
}

#[test]
fn cancel_after_dispatch_is_pending_and_keeps_slot() {
    let dir = temp_dir("slot-cancel-after-dispatch");
    let store = Store::open(&dir.join("quine.db"), "owner").unwrap();
    confirm_s1(&store, 1_001);
    store.dispatch("s1", "i1", "supervisor", 1_002).unwrap();
    let cancelled = store.cancel(&cancel_params("s1", "i1"), 1_003).unwrap();
    assert_eq!(cancelled.experiment.unwrap().status, STATUS_STOPPING);
    let error = store
        .confirm(&confirm("s2", "i2", 1, &payload("20000.00")), 1_004)
        .err()
        .unwrap();
    assert_eq!(error.code, CODE_ELIGIBILITY_HELD);
    store.release().unwrap();
}

#[test]
fn uncertain_reopen_keeps_slot() {
    let dir = temp_dir("slot-uncertain-reopen");
    let db = dir.join("quine.db");
    let store = Store::open(&db, "owner").unwrap();
    confirm_s1(&store, 1_001);
    store.dispatch("s1", "i1", "supervisor", 1_002).unwrap();
    drop(store);

    let reopened = Store::open(&db, "owner").unwrap();
    let error = reopened
        .confirm(&confirm("s2", "i2", 1, &payload("20000.00")), 2_001)
        .err()
        .unwrap();
    assert_eq!(error.code, CODE_ELIGIBILITY_HELD);
    reopened.release().unwrap();
}

#[test]
fn interruption_after_dispatch_never_redispatches() {
    let dir = temp_dir("no-redispatch");
    let db = dir.join("quine.db");
    let store = Store::open(&db, "owner").unwrap();
    let first = store
        .confirm(&confirm("s1", "i1", 1, &payload("10000.00")), 1_001)
        .unwrap();
    store.dispatch("s1", "i1", "supervisor", 1_002).unwrap();
    let attempt_id = first.experiment.attempt_id.clone();
    let job_id = first.experiment.job_id.clone();
    drop(store);

    let reopened = Store::open(&db, "owner").unwrap();
    let experiment = query_experiment(&reopened, "s1", "i1");
    assert_eq!(experiment.status, STATUS_DISPATCHED);
    assert_eq!(experiment.attempt_id, attempt_id);
    assert_eq!(experiment.job_id, job_id);
    assert!(!experiment.dispatch_eligible);

    // Replaying the confirmation returns the original identity and starts nothing.
    let replay = reopened
        .confirm(&confirm("s1", "i1", 1, &payload("10000.00")), 2_001)
        .unwrap();
    assert!(replay.replayed);
    assert_eq!(replay.experiment.attempt_id, attempt_id);
    assert_eq!(replay.experiment.status, STATUS_DISPATCHED);
    reopened.release().unwrap();
}

#[test]
fn terminal_completion_frees_slot() {
    let dir = temp_dir("slot-terminal-frees");
    let store = Store::open(&dir.join("quine.db"), "owner").unwrap();
    confirm_s1(&store, 1_001);
    let dispatched = store.dispatch("s1", "i1", "supervisor", 1_002).unwrap();
    store
        .commit_result(
            &submission(
                &dispatched,
                STATUS_SUCCEEDED,
                &json!({"schema": "ea.report"}),
            ),
            1_003,
        )
        .unwrap();
    confirm_s2(&store, 1_004);
    assert_eq!(query_experiment(&store, "s2", "i2").status, STATUS_ELIGIBLE);
    store.release().unwrap();
}

#[test]
fn undispatched_cancel_frees_slot() {
    let dir = temp_dir("slot-undispatched-cancel-frees");
    let store = Store::open(&dir.join("quine.db"), "owner").unwrap();
    confirm_s1(&store, 1_001);
    let cancelled = store.cancel(&cancel_params("s1", "i1"), 1_002).unwrap();
    assert_eq!(cancelled.experiment.unwrap().status, "cancelled");
    confirm_s2(&store, 1_003);
    assert_eq!(query_experiment(&store, "s2", "i2").status, STATUS_ELIGIBLE);
    store.release().unwrap();
}

#[test]
fn lifecycle_advance_makes_old_fence_stale() {
    let dir = temp_dir("fence-lifecycle");
    let store = Store::open(&dir.join("quine.db"), "owner").unwrap();
    confirm_s1(&store, 1_001);
    let dispatched = store.dispatch("s1", "i1", "supervisor", 1_002).unwrap();
    let old_fence = dispatched.revision;
    let running = store.mark_running("s1", "i1", old_fence).unwrap();
    assert_eq!(running.status, STATUS_RUNNING);

    // Running still holds the single slot.
    let error = store
        .confirm(&confirm("s2", "i2", 1, &payload("20000.00")), 1_003)
        .err()
        .unwrap();
    assert_eq!(error.code, CODE_ELIGIBILITY_HELD);

    // A submission with the pre-advance fence is stale and must not register.
    let stale = submission(
        &dispatched,
        STATUS_SUCCEEDED,
        &json!({"schema": "ea.report"}),
    );
    let error = store.commit_result(&stale, 1_004).err().unwrap();
    assert_eq!(error.code, CODE_STALE_FENCE);

    // The current fence commits.
    let current = query_experiment(&store, "s1", "i1");
    store
        .commit_result(
            &submission(&current, STATUS_SUCCEEDED, &json!({"schema": "ea.report"})),
            1_005,
        )
        .unwrap();
    store.release().unwrap();
}

#[test]
fn unknown_termination_keeps_slot_and_rejects_commit() {
    let dir = temp_dir("fence-unknown");
    let store = Store::open(&dir.join("quine.db"), "owner").unwrap();
    confirm_s1(&store, 1_001);
    let dispatched = store.dispatch("s1", "i1", "supervisor", 1_002).unwrap();
    let unknown = store.mark_unknown("s1", "i1", dispatched.revision).unwrap();
    assert_eq!(unknown.status, STATUS_UNKNOWN);

    let error = store
        .confirm(&confirm("s2", "i2", 1, &payload("20000.00")), 1_003)
        .err()
        .unwrap();
    assert_eq!(error.code, CODE_ELIGIBILITY_HELD);

    let submission = submission(&unknown, STATUS_SUCCEEDED, &json!({"schema": "ea.report"}));
    let error = store.commit_result(&submission, 1_004).err().unwrap();
    assert_eq!(error.code, CODE_INVALID_STATE);
    store.release().unwrap();
}

#[test]
fn late_cancel_cannot_overwrite_committed_success() {
    let dir = temp_dir("late-cancel");
    let store = Store::open(&dir.join("quine.db"), "owner").unwrap();
    confirm_s1(&store, 1_001);
    let dispatched = store.dispatch("s1", "i1", "supervisor", 1_002).unwrap();
    store
        .commit_result(
            &submission(
                &dispatched,
                STATUS_SUCCEEDED,
                &json!({"schema": "ea.report"}),
            ),
            1_003,
        )
        .unwrap();

    let error = store
        .cancel(&cancel_params("s1", "i1"), 1_004)
        .err()
        .unwrap();
    assert_eq!(error.code, CODE_LATE_CANCEL_REFUSED);
    assert_eq!(
        query_experiment(&store, "s1", "i1").status,
        STATUS_SUCCEEDED
    );
    store.release().unwrap();
}

#[test]
fn cancel_after_confirm_before_dispatch_cancels_and_blocks_dispatch() {
    let dir = temp_dir("cancel-confirmed");
    let store = Store::open(&dir.join("quine.db"), "owner").unwrap();
    confirm_s1(&store, 1_001);
    let cancelled = store.cancel(&cancel_params("s1", "i1"), 1_002).unwrap();
    assert_eq!(cancelled.experiment.unwrap().status, "cancelled");

    let error = store
        .dispatch("s1", "i1", "supervisor", 1_003)
        .err()
        .unwrap();
    assert_eq!(error.code, CODE_INVALID_STATE);
    store.release().unwrap();
}

#[test]
fn result_commit_is_idempotent_and_conflict_safe() {
    let dir = temp_dir("commit");
    let store = Store::open(&dir.join("quine.db"), "owner").unwrap();
    confirm_s1(&store, 1_001);
    let dispatched = store.dispatch("s1", "i1", "supervisor", 1_002).unwrap();
    let result = json!({"schema": "ea.report", "run_id": "run-1"});
    let first = store
        .commit_result(&submission(&dispatched, STATUS_SUCCEEDED, &result), 1_003)
        .unwrap();
    let second = store
        .commit_result(&submission(&dispatched, STATUS_SUCCEEDED, &result), 1_004)
        .unwrap();
    assert_eq!(first.status, STATUS_SUCCEEDED);
    assert_eq!(first.committed_at, second.committed_at);

    let conflict = store
        .commit_result(
            &submission(&dispatched, STATUS_SUCCEEDED, &json!({"different": true})),
            1_005,
        )
        .err()
        .unwrap();
    assert_eq!(conflict.code, CODE_CONFLICT);
    store.release().unwrap();
}

#[test]
fn commit_rejects_wrong_attempt_identity() {
    let dir = temp_dir("commit-wrong-attempt");
    let store = Store::open(&dir.join("quine.db"), "owner").unwrap();
    confirm_s1(&store, 1_001);
    let dispatched = store.dispatch("s1", "i1", "supervisor", 1_002).unwrap();
    let mut sub = submission(
        &dispatched,
        STATUS_SUCCEEDED,
        &json!({"schema": "ea.report"}),
    );
    sub.attempt_id = "wrong-attempt".to_string();
    let error = store.commit_result(&sub, 1_003).err().unwrap();
    assert_eq!(error.code, CODE_CONFLICT);
    assert_eq!(
        query_experiment(&store, "s1", "i1").status,
        STATUS_DISPATCHED
    );
    store.release().unwrap();
}

#[test]
fn commit_rejects_wrong_job_identity() {
    let dir = temp_dir("commit-wrong-job");
    let store = Store::open(&dir.join("quine.db"), "owner").unwrap();
    confirm_s1(&store, 1_001);
    let dispatched = store.dispatch("s1", "i1", "supervisor", 1_002).unwrap();
    let mut sub = submission(
        &dispatched,
        STATUS_SUCCEEDED,
        &json!({"schema": "ea.report"}),
    );
    sub.job_id = "wrong-job".to_string();
    let error = store.commit_result(&sub, 1_003).err().unwrap();
    assert_eq!(error.code, CODE_CONFLICT);
    store.release().unwrap();
}

#[test]
fn commit_rejects_wrong_request_identity() {
    let dir = temp_dir("commit-wrong-request");
    let store = Store::open(&dir.join("quine.db"), "owner").unwrap();
    confirm_s1(&store, 1_001);
    let dispatched = store.dispatch("s1", "i1", "supervisor", 1_002).unwrap();
    let mut sub = submission(
        &dispatched,
        STATUS_SUCCEEDED,
        &json!({"schema": "ea.report"}),
    );
    sub.request_id = "wrong-request".to_string();
    let error = store.commit_result(&sub, 1_003).err().unwrap();
    assert_eq!(error.code, CODE_CONFLICT);
    store.release().unwrap();
}

#[test]
fn commit_rejects_wrong_frozen_revision() {
    let dir = temp_dir("commit-wrong-revision");
    let store = Store::open(&dir.join("quine.db"), "owner").unwrap();
    confirm_s1(&store, 1_001);
    let dispatched = store.dispatch("s1", "i1", "supervisor", 1_002).unwrap();
    let mut sub = submission(
        &dispatched,
        STATUS_SUCCEEDED,
        &json!({"schema": "ea.report"}),
    );
    sub.draft_revision = 99;
    let error = store.commit_result(&sub, 1_003).err().unwrap();
    assert_eq!(error.code, CODE_CONFLICT);
    store.release().unwrap();
}

#[test]
fn commit_rejects_wrong_frozen_digest() {
    let dir = temp_dir("commit-wrong-digest");
    let store = Store::open(&dir.join("quine.db"), "owner").unwrap();
    confirm_s1(&store, 1_001);
    let dispatched = store.dispatch("s1", "i1", "supervisor", 1_002).unwrap();
    let mut sub = submission(
        &dispatched,
        STATUS_SUCCEEDED,
        &json!({"schema": "ea.report"}),
    );
    sub.frozen_sha256 = "0".repeat(64);
    let error = store.commit_result(&sub, 1_003).err().unwrap();
    assert_eq!(error.code, CODE_DIGEST_MISMATCH);
    store.release().unwrap();
}

#[test]
fn commit_rejects_stale_fence() {
    let dir = temp_dir("commit-stale-fence");
    let store = Store::open(&dir.join("quine.db"), "owner").unwrap();
    confirm_s1(&store, 1_001);
    let dispatched = store.dispatch("s1", "i1", "supervisor", 1_002).unwrap();
    let mut sub = submission(
        &dispatched,
        STATUS_SUCCEEDED,
        &json!({"schema": "ea.report"}),
    );
    sub.fence = 999;
    let error = store.commit_result(&sub, 1_003).err().unwrap();
    assert_eq!(error.code, CODE_STALE_FENCE);
    store.release().unwrap();
}

#[test]
fn commit_rejects_after_accepted_cancel_and_leaves_state_unchanged() {
    let dir = temp_dir("commit-after-cancel");
    let store = Store::open(&dir.join("quine.db"), "owner").unwrap();
    confirm_s1(&store, 1_001);
    let dispatched = store.dispatch("s1", "i1", "supervisor", 1_002).unwrap();
    store.cancel(&cancel_params("s1", "i1"), 1_003).unwrap();
    let error = store
        .commit_result(
            &submission(
                &dispatched,
                STATUS_SUCCEEDED,
                &json!({"schema": "ea.report"}),
            ),
            1_004,
        )
        .err()
        .unwrap();
    assert_eq!(error.code, CODE_INVALID_STATE);

    let experiment = query_experiment(&store, "s1", "i1");
    assert_eq!(experiment.status, STATUS_STOPPING);
    assert!(experiment.result.is_none());
    // The slot is still held: a second confirmation is refused.
    let held = store
        .confirm(&confirm("s2", "i2", 1, &payload("20000.00")), 1_005)
        .err()
        .unwrap();
    assert_eq!(held.code, CODE_ELIGIBILITY_HELD);
    store.release().unwrap();
}

#[test]
fn terminal_replay_with_forged_fence_is_refused_and_state_unchanged() {
    let dir = temp_dir("commit-forged-fence");
    let store = Store::open(&dir.join("quine.db"), "owner").unwrap();
    confirm_s1(&store, 1_001);
    let dispatched = store.dispatch("s1", "i1", "supervisor", 1_002).unwrap();
    let result = json!({"schema": "ea.report", "run_id": "run-1"});
    let committed = store
        .commit_result(&submission(&dispatched, STATUS_SUCCEEDED, &result), 1_003)
        .unwrap();
    let committed_at = committed.committed_at.unwrap();
    let revision = committed.revision;

    // Same body and identities, but a forged fence.
    let mut forged = submission(&dispatched, STATUS_SUCCEEDED, &result);
    forged.fence = 999;
    let error = store.commit_result(&forged, 1_004).err().unwrap();
    assert_eq!(error.code, CODE_STALE_FENCE);

    let unchanged = query_experiment(&store, "s1", "i1");
    assert_eq!(unchanged.status, STATUS_SUCCEEDED);
    assert_eq!(unchanged.result, Some(result));
    assert_eq!(unchanged.committed_at, Some(committed_at));
    assert_eq!(unchanged.revision, revision);
    store.release().unwrap();
}
