//! A real-process CLI test: JSON in on stdin, JSON replies out on stdout, one
//! line each, with the lease held for the process lifetime.

use std::io::Write;
use std::path::PathBuf;
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

use quine_core::protocol::{
    Budget, ConfirmParams, Envelope, FrozenInput, QueryParams, ReplyEnvelope, Request,
};
use quine_core::util::{hex_encode, sha256_hex};
use serde_json::json;

fn temp_dir(tag: &str) -> PathBuf {
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    let dir = std::env::temp_dir().join(format!(
        "quine-core-cli-{tag}-{}-{nanos}",
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

fn confirm_params(strategy: &str, intent: &str, revision: i64) -> ConfirmParams {
    let input = frozen(&json!({"initial_cash": "10000.00"}));
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

fn run_cli(db: &std::path::Path, lines: &[String]) -> Vec<ReplyEnvelope> {
    let mut child = Command::new(env!("CARGO_BIN_EXE_quine-core"))
        .arg("--db")
        .arg(db)
        .arg("--owner")
        .arg("cli-test")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::inherit())
        .spawn()
        .unwrap();
    {
        let stdin = child.stdin.as_mut().unwrap();
        for line in lines {
            stdin.write_all(line.as_bytes()).unwrap();
            stdin.write_all(b"\n").unwrap();
        }
    }
    let output = child.wait_with_output().unwrap();
    assert!(output.status.success());
    String::from_utf8(output.stdout)
        .unwrap()
        .lines()
        .map(|line| serde_json::from_str::<ReplyEnvelope>(line).unwrap())
        .collect()
}

#[test]
fn cli_confirm_then_query_over_one_lifetime() {
    let dir = temp_dir("roundtrip");
    let db = dir.join("quine.db");
    let confirm = Envelope {
        protocol: quine_core::protocol::PROTOCOL_VERSION.to_string(),
        request: Request::Confirm {
            params: confirm_params("s1", "i1", 1),
        },
    };
    let query = Envelope {
        protocol: quine_core::protocol::PROTOCOL_VERSION.to_string(),
        request: Request::Query {
            params: QueryParams {
                strategy_id: "s1".to_string(),
                intent_id: "i1".to_string(),
            },
        },
    };
    let replies = run_cli(
        &db,
        &[
            serde_json::to_string(&confirm).unwrap(),
            serde_json::to_string(&query).unwrap(),
        ],
    );
    assert_eq!(replies.len(), 2);

    let confirmed = replies[0].clone();
    assert!(confirmed.ok);
    assert_eq!(confirmed.method, "confirm");
    let experiment = confirmed.result.unwrap()["experiment"].clone();
    assert_eq!(experiment["status"], "eligible");
    assert_eq!(experiment["strategy_id"], "s1");

    let queried = replies[1].clone();
    assert!(queried.ok);
    assert_eq!(queried.method, "query");
    assert_eq!(queried.result.unwrap()["experiment"]["status"], "eligible");
}

#[test]
fn cli_returns_conflict_error_envelope() {
    let dir = temp_dir("conflict");
    let db = dir.join("quine.db");
    let first = serde_json::to_string(&Envelope {
        protocol: quine_core::protocol::PROTOCOL_VERSION.to_string(),
        request: Request::Confirm {
            params: confirm_params("s1", "i1", 1),
        },
    })
    .unwrap();
    let second = serde_json::to_string(&Envelope {
        protocol: quine_core::protocol::PROTOCOL_VERSION.to_string(),
        request: Request::Confirm {
            params: confirm_params("s1", "i1", 2),
        },
    })
    .unwrap();
    let replies = run_cli(&db, &[first, second]);
    assert!(replies[0].ok);
    assert!(!replies[1].ok);
    assert_eq!(replies[1].error.as_ref().unwrap().code, "conflict");
}

fn spawn_holder(db: &std::path::Path, owner: &str) -> std::process::Child {
    Command::new(env!("CARGO_BIN_EXE_quine-core"))
        .arg("--db")
        .arg(db)
        .arg("--owner")
        .arg(owner)
        .stdin(Stdio::piped())
        .stdout(Stdio::null())
        .stderr(Stdio::inherit())
        .spawn()
        .unwrap()
}

fn wait_for_lock_file(lock_file: &std::path::Path, owner: &str) {
    let deadline = Instant::now() + Duration::from_secs(10);
    while Instant::now() < deadline {
        let held = std::fs::read_to_string(lock_file).unwrap_or_default();
        if held.trim() == owner {
            return;
        }
        std::thread::sleep(Duration::from_millis(10));
    }
    panic!("the lock file was never taken by {owner}");
}

fn wait_for_any_lock(paths: &[PathBuf], owner: &str) {
    let deadline = Instant::now() + Duration::from_secs(10);
    while Instant::now() < deadline {
        for path in paths {
            let held = std::fs::read_to_string(path).unwrap_or_default();
            if held.trim() == owner {
                return;
            }
        }
        std::thread::sleep(Duration::from_millis(10));
    }
    panic!("none of the expected lock files were taken by {owner}");
}

#[test]
fn second_process_rejected_while_first_holds_exclusive_lock() {
    let dir = temp_dir("cli-lock-held");
    let db = dir.join("quine.db");
    let mut first = spawn_holder(&db, "owner-a");
    wait_for_lock_file(&dir.join("quine.db.lock"), "owner-a");

    // The same owner is still a second writer and must be refused.
    let same = Command::new(env!("CARGO_BIN_EXE_quine-core"))
        .arg("--db")
        .arg(&db)
        .arg("--owner")
        .arg("owner-a")
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::inherit())
        .status()
        .unwrap();
    assert!(!same.success());

    // A different owner cannot steal the live lock.
    let different = Command::new(env!("CARGO_BIN_EXE_quine-core"))
        .arg("--db")
        .arg(&db)
        .arg("--owner")
        .arg("owner-b")
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::inherit())
        .status()
        .unwrap();
    assert!(!different.success());

    // Closing stdin lets the first process exit and release the lock.
    drop(first.stdin.take());
    assert!(first.wait().unwrap().success());

    // A fresh process can now take the lock.
    let fresh = Command::new(env!("CARGO_BIN_EXE_quine-core"))
        .arg("--db")
        .arg(&db)
        .arg("--owner")
        .arg("owner-b")
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::inherit())
        .status()
        .unwrap();
    assert!(fresh.success());
}

#[test]
fn fresh_process_can_take_lock_after_abrupt_death() {
    let dir = temp_dir("cli-lock-kill");
    let db = dir.join("quine.db");
    let mut first = spawn_holder(&db, "owner-a");
    wait_for_lock_file(&dir.join("quine.db.lock"), "owner-a");

    // Abrupt death: the OS releases the lock even though the file remains.
    first.kill().unwrap();
    first.wait().unwrap();

    let fresh = Command::new(env!("CARGO_BIN_EXE_quine-core"))
        .arg("--db")
        .arg(&db)
        .arg("--owner")
        .arg("owner-b")
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::inherit())
        .status()
        .unwrap();
    assert!(fresh.success());
}

#[test]
fn relative_and_absolute_db_paths_share_one_lock() {
    let dir = temp_dir("cli-lock-paths");
    let root = dir.clone();
    let sub = root.join("sub");
    std::fs::create_dir_all(&sub).unwrap();
    std::fs::create_dir_all(sub.join("sub")).unwrap();
    let db_abs = sub.join("quine.db");

    let mut first = Command::new(env!("CARGO_BIN_EXE_quine-core"))
        .current_dir(&root)
        .arg("--db")
        .arg("sub/quine.db")
        .arg("--owner")
        .arg("owner-a")
        .stdin(Stdio::piped())
        .stdout(Stdio::null())
        .stderr(Stdio::inherit())
        .spawn()
        .unwrap();

    // Pre-fix the holder used sub/sub/quine.db.lock; post-fix it uses
    // sub/quine.db.lock. Wait for whichever one this build produced.
    wait_for_any_lock(
        &[
            root.join("sub/quine.db.lock"),
            root.join("sub/sub/quine.db.lock"),
        ],
        "owner-a",
    );

    let second = Command::new(env!("CARGO_BIN_EXE_quine-core"))
        .arg("--db")
        .arg(&db_abs)
        .arg("--owner")
        .arg("owner-b")
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::inherit())
        .status()
        .unwrap();

    // Reap the holder before asserting, even when the second process was let in.
    drop(first.stdin.take());
    let first_status = first.wait().unwrap();
    assert!(
        !second.success(),
        "a second process opened the same database through an equivalent path"
    );
    assert!(first_status.success());
}
