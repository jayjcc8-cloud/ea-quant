//! Stage 2 end-to-end: the Rust core supervises a real engine run end to end.
//!
//! Every test here starts a real `quine-core serve` process, talks to it over a
//! real private Unix socket, and drives the unchanged Python worker and the
//! stateless domain adapter as children the core owns.

use std::io::{BufRead, BufReader, Write};
use std::os::unix::net::UnixStream;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::time::{Duration, Instant};

use quine_core::protocol::{
    Budget, CancelParams, ConfirmParams, Envelope, Experiment, FrozenInput, QueryParams,
    ReplyEnvelope, Request, PROTOCOL_VERSION,
};
use quine_core::result::{expected_relative, reference_agrees, ARTIFACTS};
use quine_core::util::{hex_encode, sha256_hex};
use serde_json::{json, Value};

const SCENARIO: &str = r#"schema_version: 1
data:
  path: prices.csv
  start_utc: '2026-01-02T09:31:00.000000Z'
  end_utc: '2026-01-02T09:34:00.000000Z'
  fingerprint:
    record_count: 4
    sha256: c693dd2db0a06f2736b8b4a54967cb91398dee11e72aae11c7bfba98a1e9f6a4
instrument:
  venue: XNAS
  symbol: AAPL
  specification_id: xnas.aapl.v1
  specification_set_id: scenario.xnas.aapl.v1
  settlement_currency: USD
  price_quantum: '0.01'
  quantity_quantum: '1'
  currency_quantum: '0.01'
  contract_multiplier: '1'
strategy:
  id: bounded-long-v1
  target_quantity: '2'
funding:
  currency: USD
  initial_cash: '10000'
risk:
  max_order_quantity: '5'
  max_position_quantity: '5'
  max_notional: '1000'
execution:
  policy: phase1.next-bar-close.v1
randomness_profile: none
"#;

const PRICES: &str = "schema_version,venue,symbol,interval_start,interval_end,adjustment,open,high,low,close,volume,source,source_sequence,revision,available_at
1,XNAS,AAPL,2026-01-02T09:31:00.000000Z,2026-01-02T09:32:00.000000Z,raw,100.5,102.0,100.0,101.5,12.0,fixture.raw,2,0,2026-01-02T09:32:00.000000Z
1,XNAS,AAPL,2026-01-02T09:30:00.000000Z,2026-01-02T09:31:00.000000Z,raw,100.0,101.0,99.0,100.5,10.0,fixture.raw,0,0,2026-01-02T09:31:00.000000Z
1,XNAS,AAPL,2026-01-02T09:30:00.000000Z,2026-01-02T09:31:00.000000Z,raw,100.0,101.5,99.0,101.0,11.0,fixture.raw,1,1,2026-01-02T09:31:30.000000Z
1,XNAS,AAPL,2026-01-02T09:32:00.000000Z,2026-01-02T09:33:00.000000Z,raw,108.0,111.0,107.0,110.0,9.0,fixture.raw,3,0,2026-01-02T09:33:00.000000Z
";

/// A worker-side hook that keeps computing until something stops it.
const SLOW_HOOK: &str =
    "import time\n\n\ndef hook(*, scenario, control, request, worker):\n    time.sleep(120)\n";

fn python() -> PathBuf {
    // The venv path itself must be used, not its resolved base interpreter: the
    // isolated interpreter is the one whose own site-packages holds `ea`.
    let path = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../venv/bin/python");
    assert!(path.is_file(), "the repository venv python is required");
    path
}

/// One fresh workspace inside the supplied temp root.
///
/// The directory name is deliberately short: the core's private socket lives at
/// the workspace root and a Unix socket path is bounded by `SUN_LEN`.
fn temp_dir(tag: &str) -> PathBuf {
    static COUNTER: std::sync::atomic::AtomicUsize = std::sync::atomic::AtomicUsize::new(0);
    let _ = tag;
    let count = COUNTER.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
    let name = format!("q{}{count}", std::process::id() % 1000);
    let dir = std::env::temp_dir().join(name);
    std::fs::create_dir_all(&dir).unwrap();
    // The strict scenario loader refuses a path that is not already resolved, so
    // the workspace is handed to every child in its resolved form.
    std::fs::canonicalize(&dir).unwrap()
}

fn scenario_root(root: &Path) -> PathBuf {
    let scenarios = root.join("scenarios");
    std::fs::create_dir_all(&scenarios).unwrap();
    std::fs::write(scenarios.join("bounded-long.yaml"), SCENARIO).unwrap();
    std::fs::write(scenarios.join("prices.csv"), PRICES).unwrap();
    scenarios
}

/// Freeze one admitted input by asking the stateless adapter for it, exactly as
/// the Python Web service would render it.
fn frozen_input(scenarios: &Path, parameters: Option<Value>) -> Value {
    let adapter = scenarios.join("adapter");
    std::fs::create_dir_all(&adapter).unwrap();
    let request_path = adapter.join("request.json");
    let reply_path = adapter.join("reply.json");
    let scenario_path = scenarios.join("bounded-long.yaml");
    assert!(scenario_path.is_file(), "the fixture scenario is missing");
    let mut request = json!({"scenario_path": scenario_path.display().to_string()});
    if let Some(parameters) = parameters {
        request["parameters"] = parameters;
    }
    std::fs::write(&request_path, serde_json::to_vec(&request).unwrap()).unwrap();
    let status = Command::new(python())
        .args(["-I", "-B", "-m", "ea.web.rust_domain", "snapshot"])
        .arg(&request_path)
        .arg(&reply_path)
        .status()
        .expect("the domain adapter must run");
    assert!(status.success());
    let reply: Value =
        serde_json::from_slice(&std::fs::read(&reply_path).unwrap()).expect("adapter reply");
    assert_eq!(reply["status"], "ok", "{reply}");
    reply["result"]["frozen"].clone()
}

struct Client {
    stream: UnixStream,
    reader: BufReader<UnixStream>,
}

impl Client {
    fn connect(workspace: &Path) -> Client {
        let deadline = Instant::now() + Duration::from_secs(20);
        loop {
            if let Ok(stream) = UnixStream::connect(workspace.join("core.sock")) {
                let reader = BufReader::new(stream.try_clone().unwrap());
                return Client { stream, reader };
            }
            assert!(Instant::now() < deadline, "the core never bound its socket");
            std::thread::sleep(Duration::from_millis(20));
        }
    }

    fn call(&mut self, request: Request) -> ReplyEnvelope {
        let envelope = Envelope {
            protocol: PROTOCOL_VERSION.to_string(),
            request,
        };
        let mut line = serde_json::to_vec(&envelope).unwrap();
        line.push(b'\n');
        self.stream.write_all(&line).unwrap();
        self.stream.flush().unwrap();
        let mut reply = String::new();
        self.reader.read_line(&mut reply).unwrap();
        serde_json::from_str(&reply).expect("a reply line")
    }

    fn confirm(&mut self, params: ConfirmParams) -> ReplyEnvelope {
        self.call(Request::Confirm { params })
    }

    fn query(&mut self, strategy_id: &str, intent_id: &str) -> Value {
        let reply = self.call(Request::Query {
            params: QueryParams {
                strategy_id: strategy_id.to_string(),
                intent_id: intent_id.to_string(),
            },
        });
        assert!(reply.ok, "{:?}", reply.error);
        reply.result.unwrap()
    }
}

struct CoreProcess {
    child: Child,
    client: Client,
}

impl CoreProcess {
    fn start(workspace: &Path, python: &Path, extras: &[&str]) -> CoreProcess {
        let mut command = Command::new(env!("CARGO_BIN_EXE_quine-core"));
        command
            .arg("serve")
            .arg("--workspace")
            .arg(workspace)
            .arg("--python")
            .arg(python)
            .args(extras)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::inherit());
        let child = command.spawn().expect("the core must start");
        let client = Client::connect(workspace);
        CoreProcess { child, client }
    }

    fn kill(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

impl Drop for CoreProcess {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

fn params(intent_id: &str, revision: i64, frozen: &Value) -> ConfirmParams {
    let bytes = serde_json::to_vec(frozen).unwrap();
    ConfirmParams {
        strategy_id: "strategy-a".to_string(),
        intent_id: intent_id.to_string(),
        draft_revision: revision,
        preview_identity: format!("sha256:{}", sha256_hex(&bytes)),
        budget: Budget {
            max_model_calls: 0,
            max_backtests: 1,
        },
        frozen_input: FrozenInput {
            canonical_bytes: hex_encode(&bytes),
            sha256: sha256_hex(&bytes),
        },
    }
}

fn confirmed(reply: &ReplyEnvelope) -> Experiment {
    assert!(reply.ok, "{:?}", reply.error);
    let result = reply.result.clone().unwrap();
    serde_json::from_value(result["experiment"].clone()).expect("an experiment record")
}

fn wait_for_status(client: &mut Client, intent_id: &str, wanted: &[&str]) -> Value {
    let deadline = Instant::now() + Duration::from_secs(90);
    loop {
        let queried = client.query("strategy-a", intent_id);
        let status = queried["experiment"]["status"].as_str().unwrap_or("");
        if wanted.contains(&status) {
            return queried;
        }
        assert!(
            Instant::now() < deadline,
            "status never became {wanted:?}; it is {status}"
        );
        std::thread::sleep(Duration::from_millis(20));
    }
}

fn wait_for_path(path: &Path) {
    let deadline = Instant::now() + Duration::from_secs(90);
    while !path.exists() {
        assert!(
            Instant::now() < deadline,
            "{} never appeared",
            path.display()
        );
        std::thread::sleep(Duration::from_millis(20));
    }
}

#[test]
fn staged_paths_are_derived_and_worker_references_must_agree() {
    let attempt = "11111111-1111-4111-8111-111111111111";
    assert_eq!(
        expected_relative("runs", attempt),
        format!("runs/{attempt}")
    );
    assert!(reference_agrees(
        Some(&json!(format!("runs/{attempt}"))),
        &expected_relative("runs", attempt),
        true
    ));
    // A worker-selected escape or alternate attempt is never followed.
    for forged in [
        json!("../../escape"),
        json!("/etc/passwd"),
        json!("runs/22222222-2222-4222-8222-222222222222"),
        json!(""),
    ] {
        assert!(!reference_agrees(
            Some(&forged),
            &expected_relative("runs", attempt),
            true
        ));
    }
    assert!(!reference_agrees(
        None,
        &expected_relative("runs", attempt),
        true
    ));
}

#[test]
fn real_run_is_registered_as_one_official_result_and_replays_its_identity() {
    let root = temp_dir("run");
    let frozen = frozen_input(&scenario_root(&root), None);
    let python = python();
    let mut core = CoreProcess::start(&root, &python, &[]);

    let first = confirmed(&core.client.confirm(params("intent-1", 1, &frozen)));
    let queried = wait_for_status(&mut core.client, "intent-1", &["succeeded"]);
    let experiment: Experiment = serde_json::from_value(queried["experiment"].clone()).unwrap();
    assert_eq!(experiment.job_id, first.job_id);
    assert_eq!(experiment.attempt_id, first.attempt_id);

    // The registered result is a durable official record over the verified bytes.
    let official = experiment.result.clone().expect("an official result");
    assert_eq!(official["schema"], "ea.quine-official-result.v1");
    assert_eq!(official["run_id"], first.attempt_id);
    for name in ARTIFACTS {
        let published = root.join("official").join(&first.job_id).join(name);
        assert!(published.is_file(), "the official {name} was not published");
        let digest = sha256_hex(&std::fs::read(&published).unwrap());
        assert_eq!(official["artifacts"][name]["sha256"], json!(digest));
    }
    let facts: Vec<Value> = serde_json::from_value(official["facts"].clone()).unwrap();
    let mut readings = std::collections::BTreeMap::new();
    for fact in &facts {
        readings.insert(
            fact["path"].as_str().unwrap().to_string(),
            fact["value"].clone(),
        );
    }
    // The engine's own readings of this frozen fixture — one entry and one exit
    // at 101.5 and 110.0 for two units — with no commission policy declared:
    // counts stay JSON integers and every decimal stays the report's own
    // canonical `ea-decimal-v1` string.
    assert_eq!(
        readings["economics.counts.orders"],
        json!(1),
        "{readings:?}"
    );
    assert_eq!(readings["economics.counts.fills"], json!(1), "{readings:?}");
    assert_eq!(
        readings["economics.net_pnl.amount"],
        json!("17"),
        "{readings:?}"
    );
    assert_eq!(
        readings["economics.fees.amount"],
        json!("0"),
        "{readings:?}"
    );
    assert_eq!(
        readings["economics.total_return.value"],
        json!("0.0017"),
        "{readings:?}"
    );

    // A repeated confirmation with lost response resolves to the same identity
    // and never starts a second run.
    let replay = confirmed(&core.client.confirm(params("intent-1", 1, &frozen)));
    assert_eq!(replay.job_id, first.job_id);
    assert_eq!(replay.attempt_id, first.attempt_id);
    let controls: Vec<_> = std::fs::read_dir(root.join("control")).unwrap().collect();
    assert_eq!(controls.len(), 1, "a replay spawned a second attempt");
    let inputs: Vec<_> = std::fs::read_dir(root.join("inputs")).unwrap().collect();
    assert_eq!(inputs.len(), 1, "a replay materialized a second input");

    core.kill();
}

#[test]
fn cancellation_during_a_slow_run_is_honest_and_keeps_ipc_responsive() {
    let root = temp_dir("cancel");
    let frozen = frozen_input(&scenario_root(&root), None);
    let hook = root.join("slow-hook.py");
    std::fs::write(&hook, SLOW_HOOK).unwrap();
    let hook_reference = format!("{}:hook", hook.display());
    let python = python();
    let mut core = CoreProcess::start(&root, &python, &["--test-hook", hook_reference.as_str()]);

    let first = confirmed(&core.client.confirm(params("intent-1", 1, &frozen)));
    wait_for_status(&mut core.client, "intent-1", &["running"]);

    // A query stays bounded while the worker is still computing.
    let started = Instant::now();
    let _ = core.client.query("strategy-a", "intent-1");
    assert!(
        started.elapsed() < Duration::from_secs(5),
        "a query blocked on the running worker"
    );

    let cancel = core.client.call(Request::Cancel {
        params: CancelParams {
            strategy_id: "strategy-a".to_string(),
            intent_id: "intent-1".to_string(),
        },
    });
    assert!(cancel.ok, "{:?}", cancel.error);

    // The cancellation is accepted, and only the proven-absent group settles it.
    let queried = wait_for_status(&mut core.client, "intent-1", &["cancelled"]);
    let experiment: Experiment = serde_json::from_value(queried["experiment"].clone()).unwrap();
    assert!(experiment.result.is_none(), "a cancelled run has no result");
    assert!(!root.join("official").join(&first.job_id).exists());

    core.kill();
}

#[test]
fn cancellation_inside_the_publication_window_blocks_the_late_result() {
    let root = temp_dir("late-cancel");
    let frozen = frozen_input(&scenario_root(&root), None);
    let python = python();
    let mut core = CoreProcess::start(&root, &python, &["--test-commit-delay-ms", "4000"]);

    let first = confirmed(&core.client.confirm(params("intent-1", 1, &frozen)));
    wait_for_status(&mut core.client, "intent-1", &["running"]);
    // The validator's reply is the deterministic start of the window between a
    // verified result and its durable registration.
    let reply = root
        .join("tmp/domain")
        .join(format!("{}-validate", first.job_id))
        .join("reply.json");
    wait_for_path(&reply);

    let cancel = core.client.call(Request::Cancel {
        params: CancelParams {
            strategy_id: "strategy-a".to_string(),
            intent_id: "intent-1".to_string(),
        },
    });
    assert!(cancel.ok, "{:?}", cancel.error);

    let queried = wait_for_status(&mut core.client, "intent-1", &["cancelled"]);
    let experiment: Experiment = serde_json::from_value(queried["experiment"].clone()).unwrap();
    assert!(
        experiment.result.is_none(),
        "a cancellation accepted before registration must not be overwritten by the late result"
    );
    assert!(!root.join("official").join(&first.job_id).exists());

    core.kill();
}

#[test]
fn altered_staged_bytes_are_never_published() {
    let root = temp_dir("tamper");
    let frozen = frozen_input(&scenario_root(&root), None);
    let python = python();
    let mut core = CoreProcess::start(&root, &python, &["--test-tamper-before-read"]);

    let first = confirmed(&core.client.confirm(params("intent-1", 1, &frozen)));
    let queried = wait_for_status(&mut core.client, "intent-1", &["failed"]);
    let experiment: Experiment = serde_json::from_value(queried["experiment"].clone()).unwrap();
    assert_eq!(experiment.result.unwrap()["code"], "artifact_mismatch");
    assert!(!root.join("official").join(&first.job_id).exists());

    core.kill();
}

#[test]
fn crash_between_publication_and_registration_recovers_fail_closed() {
    let root = temp_dir("crash");
    let frozen = frozen_input(&scenario_root(&root), None);
    let python = python();
    let mut core = CoreProcess::start(&root, &python, &["--test-crash-before-commit"]);
    let first = confirmed(&core.client.confirm(params("intent-1", 1, &frozen)));
    wait_for_status(&mut core.client, "intent-1", &["running"]);
    let status = core.child.wait().expect("the core must exit at the seam");
    assert_eq!(status.code(), Some(70), "the crash seam did not fire");

    // The published bytes exist, but no durable registration ever happened.
    wait_for_path(&root.join("official").join(&first.job_id));
    let mut restarted = CoreProcess::start(&root, &python, &[]);
    let queried = restarted.client.query("strategy-a", "intent-1");
    let experiment: Experiment = serde_json::from_value(queried["experiment"].clone()).unwrap();
    assert_eq!(experiment.status, "unknown");
    assert!(
        experiment.result.is_none(),
        "an unregistered artifact became official"
    );
    assert!(
        !experiment.dispatch_eligible,
        "an unproven run freed the slot"
    );

    // No automatic re-run: a second experiment is still refused the one slot.
    let second = restarted.client.confirm(params("intent-2", 1, &frozen));
    assert!(!second.ok);
    assert_eq!(second.error.unwrap().code, "eligibility_held");

    restarted.kill();
}

#[test]
fn killing_the_core_takes_the_worker_group_with_it() {
    let root = temp_dir("parent-death");
    let frozen = frozen_input(&scenario_root(&root), None);
    let hook = root.join("slow-hook.py");
    std::fs::write(&hook, SLOW_HOOK).unwrap();
    let hook_reference = format!("{}:hook", hook.display());
    let python = python();
    let mut core = CoreProcess::start(&root, &python, &["--test-hook", hook_reference.as_str()]);
    let first = confirmed(&core.client.confirm(params("intent-1", 1, &frozen)));
    wait_for_status(&mut core.client, "intent-1", &["running"]);

    // The worker's own group is recorded through the inherited liveness pipe, so
    // an abrupt core death ends the compute rather than orphaning it.
    let events = root
        .join("control")
        .join(&first.job_id)
        .join("events.jsonl");
    wait_for_path(&events);
    core.child.kill().unwrap();
    core.child.wait().unwrap();

    let mut restarted = CoreProcess::start(&root, &python, &[]);
    let queried = wait_for_status(&mut restarted.client, "intent-1", &["unknown"]);
    let experiment: Experiment = serde_json::from_value(queried["experiment"].clone()).unwrap();
    assert!(experiment.result.is_none());
    assert!(!experiment.dispatch_eligible);

    let second = restarted.client.confirm(params("intent-2", 1, &frozen));
    assert!(!second.ok);
    assert_eq!(second.error.unwrap().code, "eligibility_held");

    restarted.kill();
}

#[test]
fn a_frozen_input_that_no_longer_verifies_never_starts_a_worker() {
    let root = temp_dir("altered-freeze");
    let mut frozen = frozen_input(&scenario_root(&root), None);
    // Alter the retained snapshot without touching either self-hash: only the
    // agreement between the snapshot and the frozen file bytes catches this.
    frozen["snapshot"]["scenario"]["risk"]["max_notional"] = json!("9999");
    let python = python();
    let mut core = CoreProcess::start(&root, &python, &[]);
    let reply = core.client.confirm(params("intent-1", 1, &frozen));
    assert!(reply.ok, "{:?}", reply.error);
    let queried = wait_for_status(&mut core.client, "intent-1", &["failed"]);
    let experiment: Experiment = serde_json::from_value(queried["experiment"].clone()).unwrap();
    assert_eq!(experiment.result.unwrap()["code"], "digest_mismatch");
    assert!(
        !root
            .join("inputs")
            .join(format!("{}.json", experiment.job_id))
            .exists(),
        "a refused freeze materialized a worker input"
    );
    core.kill();
}
