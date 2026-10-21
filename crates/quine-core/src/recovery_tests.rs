use super::*;
use crate::protocol::RecoveryParams;
use crate::recovery::RecoveryContext;
use serde_json::json;

fn fixture() -> (Store, PathBuf, ConfirmParams, Experiment) {
    let dir = std::env::temp_dir().join(format!("quine-recovery-{}", Uuid::new_v4()));
    std::fs::create_dir_all(&dir).unwrap();
    let path = dir.join("quine.db");
    let mut store = Store::open(&path, "test").unwrap();
    store.recovery_context = Some(RecoveryContext {
        machine: "machine-a".into(),
        boot: "boot-a".into(),
        workspace: "workspace-a".into(),
    });
    let bytes = br#"{"draft_revision":1,"initial_cash":"10000"}"#;
    let input = crate::protocol::FrozenInput {
        canonical_bytes: hex_encode(bytes),
        sha256: sha256_hex(bytes),
    };
    let confirm = ConfirmParams {
        strategy_id: "strategy".into(),
        intent_id: "intent".into(),
        draft_revision: 1,
        preview_identity: format!("sha256:{}", input.sha256),
        budget: Budget {
            max_model_calls: 7,
            max_backtests: 1,
        },
        frozen_input: input,
    };
    store.confirm(&confirm, 1).unwrap();
    let dispatched = store.dispatch("strategy", "intent", "test", 2).unwrap();
    let unknown = store
        .mark_unknown("strategy", "intent", dispatched.revision)
        .unwrap();
    (store, path, confirm, unknown)
}
fn request(experiment: &Experiment) -> RecoveryParams {
    RecoveryParams {
        strategy_id: experiment.strategy_id.clone(),
        intent_id: experiment.intent_id.clone(),
        attempt_id: experiment.attempt_id.clone(),
        expected_revision: experiment.revision,
        preparation_id: "preparation-1".into(),
    }
}
fn current(store: &Store) -> Experiment {
    store
        .query(&QueryParams {
            strategy_id: "strategy".into(),
            intent_id: "intent".into(),
        })
        .unwrap()
        .experiment
        .unwrap()
}
fn reboot(store: &mut Store) {
    store.recovery_context.as_mut().unwrap().boot = "boot-b".into();
}

#[test]
fn recovery_requires_origin_and_changed_boot_without_touching_history() {
    let (store, _, _, old) = fixture();
    let prep = store.prepare_recovery(&request(&old), 3).unwrap();
    let p = request(&prep);
    assert_eq!(
        store.release_unknown(&p, 4).unwrap_err().code,
        "restart_required"
    );
    assert!(current(&store).slot_held);
    assert_eq!(current(&store).frozen_canonical, old.frozen_canonical);
    store
        .lock()
        .unwrap()
        .execute("UPDATE attempt SET origin_json=NULL", [])
        .unwrap();
    assert_eq!(
        store.release_unknown(&p, 4).unwrap_err().code,
        "recovery_unavailable"
    );
}

#[test]
fn recovery_releases_only_slot_and_replay_never_dispatches_old_intent() {
    let (mut store, path, confirm, old) = fixture();
    let params = request(&old);
    let prepared = store.prepare_recovery(&params, 3).unwrap();
    assert_eq!(
        store.prepare_recovery(&params, 4).unwrap().revision,
        prepared.revision
    );
    reboot(&mut store);
    let release = request(&prepared);
    let freed = store.release_unknown(&release, 5).unwrap();
    assert!(!freed.slot_held);
    assert_eq!(freed.status, STATUS_UNKNOWN);
    assert_eq!(freed.budget, old.budget);
    assert_eq!(freed.frozen_canonical, old.frozen_canonical);
    assert_eq!(freed.dispatched_at, old.dispatched_at);
    assert_eq!(freed.job_id, old.job_id);
    assert_eq!(freed.attempt_id, old.attempt_id);
    assert_eq!(freed.result, None);
    assert_eq!(
        store.release_unknown(&release, 6).unwrap().revision,
        freed.revision
    );
    assert!(
        !store
            .confirm(&confirm, 7)
            .unwrap()
            .experiment
            .dispatch_eligible
    );
    assert!(store.dispatch("strategy", "intent", "test", 8).is_err());
    assert!(store
        .cancel(
            &CancelParams {
                strategy_id: "strategy".into(),
                intent_id: "intent".into()
            },
            8
        )
        .is_err());
    let late = ResultSubmission {
        strategy_id: "strategy".into(),
        intent_id: "intent".into(),
        request_id: old.request_id,
        job_id: old.job_id,
        attempt_id: old.attempt_id,
        draft_revision: 1,
        frozen_sha256: old.frozen_sha256,
        fence: freed.revision,
        outcome: STATUS_SUCCEEDED.into(),
        result: json!({}),
    };
    assert!(store.commit_result(&late, 9).is_err());
    assert_eq!(store.reconcile_uncertain(10).unwrap(), 0);
    let mut new = confirm.clone();
    new.intent_id = "new-intent".into();
    assert!(
        store
            .confirm(&new, 11)
            .unwrap()
            .experiment
            .dispatch_eligible
    );
    new.intent_id = "racing-intent".into();
    assert_eq!(
        store.confirm(&new, 12).unwrap_err().code,
        CODE_ELIGIBILITY_HELD
    );
    drop(store);
    let store = Store::open(&path, "reopened").unwrap();
    assert!(!current(&store).slot_held);
    assert_eq!(current(&store).recovery.unwrap().status, "released");
}

#[test]
fn recovery_binds_host_workspace_attempt_revision_and_preparation() {
    let (mut store, _, _, old) = fixture();
    let mut p = request(&old);
    p.attempt_id = "other-attempt".into();
    assert!(store.prepare_recovery(&p, 3).is_err());
    p = request(&old);
    p.expected_revision -= 1;
    assert!(store.prepare_recovery(&p, 3).is_err());
    let prepared = store.prepare_recovery(&request(&old), 3).unwrap();
    reboot(&mut store);
    let p = request(&prepared);
    let valid = store.recovery_context.clone();
    store.recovery_context.as_mut().unwrap().machine = "other-machine".into();
    assert!(store.release_unknown(&p, 4).is_err());
    store.recovery_context = valid.clone();
    store.recovery_context.as_mut().unwrap().workspace = "moved-workspace".into();
    assert!(store.release_unknown(&p, 4).is_err());
    store.recovery_context = None;
    assert!(store.release_unknown(&p, 4).is_err());
    store.recovery_context = valid;
    let mut bad = p.clone();
    bad.preparation_id = "another-preparation".into();
    assert!(store.release_unknown(&bad, 4).is_err());
    bad = p.clone();
    bad.expected_revision -= 1;
    assert!(store.release_unknown(&bad, 4).is_err());
    assert!(current(&store).slot_held);
    assert!(store.release_unknown(&p, 4).is_ok());
}

#[test]
fn failed_recovery_commit_rolls_back_and_can_retry_without_a_second_release() {
    let (mut store, _, _, old) = fixture();
    let prepared = store.prepare_recovery(&request(&old), 3).unwrap();
    reboot(&mut store);
    store.lock().unwrap().execute_batch("CREATE TRIGGER injected_failure AFTER UPDATE OF active ON attempt WHEN NEW.active=0 BEGIN SELECT RAISE(ABORT,'injected'); END;").unwrap();
    assert!(store.release_unknown(&request(&prepared), 4).is_err());
    let unchanged = current(&store);
    assert!(unchanged.slot_held);
    assert_eq!(unchanged.revision, prepared.revision);
    assert_eq!(unchanged.recovery.unwrap().status, "ready");
    store
        .lock()
        .unwrap()
        .execute_batch("DROP TRIGGER injected_failure;")
        .unwrap();
    let freed = store.release_unknown(&request(&prepared), 5).unwrap();
    assert!(!freed.slot_held);
    assert!(store
        .lock()
        .unwrap()
        .execute(
            "UPDATE attempt SET status='stopping',active=1,revision=revision+1",
            []
        )
        .is_err());
    assert!(!current(&store).slot_held);
}

fn downgrade_fixture(path: &Path) {
    let conn = Connection::open(path).unwrap();
    conn.execute_batch("DROP TRIGGER released_unknown_immutable; ALTER TABLE attempt RENAME TO new_attempt;
        DROP INDEX one_active_attempt;
        CREATE TABLE attempt (
          strategy_id TEXT NOT NULL,intent_id TEXT NOT NULL,job_id TEXT NOT NULL UNIQUE,
          attempt_id TEXT NOT NULL UNIQUE,status TEXT NOT NULL,active INTEGER NOT NULL,
          revision INTEGER NOT NULL,dispatch_owner TEXT,result_json TEXT,commit_fence INTEGER,
          created_at INTEGER NOT NULL,dispatched_at INTEGER,committed_at INTEGER,cancelled_at INTEGER,
          PRIMARY KEY(strategy_id,intent_id),FOREIGN KEY(strategy_id,intent_id) REFERENCES experiment(strategy_id,intent_id),
          CHECK ((status IN ('eligible','dispatched','running','stopping','unknown') AND active=1)
            OR (status IN ('succeeded','failed','cancelled') AND active=0)));
        INSERT INTO attempt SELECT strategy_id,intent_id,job_id,attempt_id,status,active,revision,
          dispatch_owner,result_json,commit_fence,created_at,dispatched_at,committed_at,cancelled_at FROM new_attempt;
        DROP TABLE new_attempt;
        CREATE UNIQUE INDEX one_active_attempt ON attempt(active) WHERE active=1;
        PRAGMA user_version=0;").unwrap();
}

#[test]
fn legacy_migration_retains_history_and_never_invents_origin() {
    let (store, path, _, old) = fixture();
    drop(store);
    downgrade_fixture(&path);
    let store = Store::open(&path, "migrated").unwrap();
    let retained = current(&store);
    assert!(retained.slot_held);
    assert_eq!(retained.status, old.status);
    assert_eq!(retained.revision, old.revision);
    assert_eq!(retained.frozen_canonical, old.frozen_canonical);
    assert_eq!(retained.budget, old.budget);
    assert_eq!(retained.dispatched_at, old.dispatched_at);
    assert_eq!(retained.recovery.unwrap().status, "unavailable");
    assert!(store.prepare_recovery(&request(&old), 4).is_err());
    assert_eq!(
        store
            .lock()
            .unwrap()
            .query_row("PRAGMA user_version", [], |r| r.get::<_, i64>(0))
            .unwrap(),
        1
    );
}

#[test]
fn failed_schema_migration_rolls_back_the_original_table_and_version() {
    let (store, path, _, _) = fixture();
    drop(store);
    downgrade_fixture(&path);
    {
        let conn = Connection::open(&path).unwrap();
        conn.execute_batch("PRAGMA ignore_check_constraints=ON; UPDATE attempt SET active=7;")
            .unwrap();
    }
    assert!(Store::open(&path, "migration-fails").is_err());
    let conn = Connection::open(&path).unwrap();
    assert_eq!(
        conn.query_row("PRAGMA user_version", [], |r| r.get::<_, i64>(0))
            .unwrap(),
        0
    );
    assert_eq!(
        conn.query_row("SELECT active FROM attempt", [], |r| r.get::<_, i64>(0))
            .unwrap(),
        7
    );
    assert!(conn.prepare("SELECT origin_json FROM attempt").is_err());
    assert_eq!(
        conn.query_row(
            "SELECT count(*) FROM sqlite_master WHERE name='attempt_recovery_v1'",
            [],
            |r| r.get::<_, i64>(0)
        )
        .unwrap(),
        0
    );
}

#[test]
fn clients_cannot_supply_boot_or_machine_proof() {
    let request = json!({"protocol":"quine.v1","request":{"method":"release_unknown","params":{
        "strategy_id":"strategy","intent_id":"intent","attempt_id":"attempt","expected_revision":3,
        "preparation_id":"prep","boot":"fake-boot","machine":"fake-machine"}}});
    assert!(serde_json::from_value::<crate::protocol::Envelope>(request).is_err());
}

#[test]
fn copied_database_does_not_inherit_execution_origin() {
    let (store, path, _, old) = fixture();
    drop(store);
    // Real file identity, not a synthetic path label, must reject a copied database.
    let mut original = Store::open(&path, "origin").unwrap();
    let context = RecoveryContext::read(&path).unwrap();
    original.recovery_context = Some(context.clone());
    original
        .lock()
        .unwrap()
        .execute(
            "UPDATE attempt SET origin_json=?1",
            params![serde_json::to_string(&context).unwrap()],
        )
        .unwrap();
    drop(original);
    let copy = path.with_file_name("copy.db");
    std::fs::copy(&path, &copy).unwrap();
    let store = Store::open(&copy, "copy").unwrap();
    assert_eq!(current(&store).recovery.unwrap().status, "unavailable");
    assert!(store.prepare_recovery(&request(&old), 4).is_err());
}

#[test]
fn a_changed_attempt_needs_a_fresh_preparation_and_another_reboot() {
    let (mut store, _, _, old) = fixture();
    let prepared = store.prepare_recovery(&request(&old), 3).unwrap();
    store
        .cancel(
            &CancelParams {
                strategy_id: "strategy".into(),
                intent_id: "intent".into(),
            },
            4,
        )
        .unwrap();
    store.reconcile_uncertain(5).unwrap();
    reboot(&mut store);
    let changed = current(&store);
    assert_eq!(
        changed.recovery.as_ref().unwrap().status,
        "prepare_required"
    );
    assert!(store.release_unknown(&request(&prepared), 6).is_err());
    let mut new = request(&changed);
    new.preparation_id = "new-preparation".into();
    let next = store.prepare_recovery(&new, 7).unwrap();
    new.expected_revision = next.revision;
    assert_eq!(
        store.release_unknown(&new, 8).unwrap_err().code,
        "restart_required"
    );
    store.recovery_context.as_mut().unwrap().boot = "boot-c".into();
    assert!(!store.release_unknown(&new, 9).unwrap().slot_held);
}
