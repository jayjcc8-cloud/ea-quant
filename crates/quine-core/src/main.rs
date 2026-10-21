//! The quine-core entry point.
//!
//! ``serve`` runs the one long-running core for a workspace: it takes the
//! exclusive lock, binds the private socket and supervises every child itself.
//! ``request`` is a thin client that forwards JSON lines to that socket.
//! Without either, the process is the stage-1 sequential store CLI, which
//! answers the same confirmed-experiment methods over stdin.

use std::io::{BufRead, Write};
use std::path::PathBuf;
use std::sync::{
    atomic::{AtomicBool, Ordering},
    Arc,
};
use std::time::Duration;

use quine_core::core::{Core, CoreConfig};
use quine_core::ipc;
use quine_core::protocol::{
    ConfirmParams, Envelope, FrozenInput, ListParams, QueryParams, ReplyEnvelope, Request,
    PROTOCOL_VERSION,
};
use quine_core::store::{QuineError, Store};

fn main() {
    let arguments: Vec<String> = std::env::args().skip(1).collect();
    match arguments.first().map(String::as_str) {
        Some("serve") => serve(&arguments[1..]),
        Some("request") => request(&arguments[1..]),
        _ => offline(&arguments),
    }
}

fn serve(arguments: &[String]) {
    let flags = Flags::parse(arguments);
    let parent_closed = Arc::new(AtomicBool::new(false));
    if flags.present("parent-stdin") {
        // Desktop-owned liveness pipe. Owner loss uses the same recovery semantics
        // as a crashed core; no terminal/cancellation state is fabricated here.
        let closed = parent_closed.clone();
        std::thread::spawn(move || {
            let mut byte = [0u8; 1];
            loop {
                match std::io::Read::read(&mut std::io::stdin(), &mut byte) {
                    Ok(0) | Err(_) => {
                        closed.store(true, Ordering::SeqCst);
                        break;
                    }
                    Ok(_) => {}
                }
            }
        });
    }
    let workspace = match flags.value("workspace") {
        Some(value) => PathBuf::from(value),
        None => {
            eprintln!("serve requires --workspace");
            std::process::exit(2);
        }
    };
    let python = PathBuf::from(
        flags
            .value("python")
            .unwrap_or_else(|| "python3".to_string()),
    );
    let mut config = CoreConfig::new(&workspace, &python);
    if flags.present("parent-stdin") {
        config.parent_closed = Some(parent_closed);
    }
    if let Some(owner) = flags.value("owner") {
        config.owner = owner;
    }
    if let Some(socket) = flags.value("socket") {
        config.socket = PathBuf::from(socket);
    }
    config.test_hook = flags.value("test-hook");
    if let Some(milliseconds) = flags
        .value("test-commit-delay-ms")
        .and_then(|value| value.parse().ok())
    {
        config.commit_delay = Duration::from_millis(milliseconds);
    }
    config.crash_before_commit = flags.present("test-crash-before-commit");
    config.tamper_before_read = flags.present("test-tamper-before-read");
    config.faults.fail_running_state = flags.present("test-fail-running-state");
    config.faults.lose_running_state_receipt = flags.present("test-lose-running-state-receipt");
    config.faults.fail_abort_reread = flags.present("test-fail-abort-reread");
    config.faults.hold_after_spawn = flags.present("test-hold-after-spawn");

    let mut core = match Core::open(config) {
        Ok(core) => core,
        Err(error) => {
            eprintln!("{error}");
            std::process::exit(1);
        }
    };
    if let Err(error) = core.serve() {
        eprintln!("{error}");
        std::process::exit(if error.kind() == std::io::ErrorKind::BrokenPipe {
            70
        } else {
            1
        });
    }
}

fn request(arguments: &[String]) {
    let flags = Flags::parse(arguments);
    let socket = match flags.value("socket") {
        Some(value) => PathBuf::from(value),
        None => {
            eprintln!("request requires --socket");
            std::process::exit(2);
        }
    };
    let mut stream = match ipc::connect(&socket) {
        Ok(stream) => stream,
        Err(error) => {
            eprintln!("{error}");
            std::process::exit(1);
        }
    };
    let stdin = std::io::stdin();
    let stdout = std::io::stdout();
    let mut output = stdout.lock();
    let mut reader = std::io::BufReader::new(match stream.try_clone() {
        Ok(clone) => clone,
        Err(error) => {
            eprintln!("{error}");
            std::process::exit(1);
        }
    });
    for line in stdin.lock().lines() {
        let line = match line {
            Ok(line) => line,
            Err(_) => break,
        };
        if line.trim().is_empty() {
            continue;
        }
        if stream.write_all(line.as_bytes()).is_err() || stream.write_all(b"\n").is_err() {
            break;
        }
        let mut reply = String::new();
        if reader.read_line(&mut reply).is_err() || reply.is_empty() {
            break;
        }
        if output.write_all(reply.as_bytes()).is_err() || output.flush().is_err() {
            break;
        }
    }
}

/// The stage-1 sequential store CLI: the same methods, one process, no socket.
fn offline(arguments: &[String]) {
    let flags = Flags::parse(arguments);
    let db = PathBuf::from(flags.value("db").unwrap_or_else(|| "quine.db".to_string()));
    let owner = flags.value("owner").unwrap_or_else(|| "cli".to_string());
    let store = match Store::open(&db, &owner) {
        Ok(store) => store,
        Err(error) => {
            eprintln!("{error}");
            std::process::exit(1);
        }
    };
    let stdin = std::io::stdin();
    let stdout = std::io::stdout();
    let mut output = stdout.lock();
    for line in stdin.lock().lines() {
        let line = match line {
            Ok(line) => line,
            Err(_) => break,
        };
        if line.trim().is_empty() {
            continue;
        }
        let reply = handle(&store, &line);
        let mut bytes = match serde_json::to_vec(&reply) {
            Ok(bytes) => bytes,
            Err(_) => break,
        };
        bytes.push(b'\n');
        if output.write_all(&bytes).is_err() || output.flush().is_err() {
            break;
        }
    }
    let _ = store.release();
}

fn handle(store: &Store, line: &str) -> ReplyEnvelope {
    let envelope: Envelope = match serde_json::from_str(line) {
        Ok(envelope) => envelope,
        Err(error) => {
            return ReplyEnvelope::err(
                "unknown",
                "invalid_request",
                format!("request is not readable JSON: {error}"),
            )
        }
    };
    if envelope.protocol != PROTOCOL_VERSION {
        return ReplyEnvelope::err(
            "unknown",
            "invalid_request",
            format!("unsupported protocol {}", envelope.protocol),
        );
    }
    let method = request_method(&envelope.request);
    let result: Result<serde_json::Value, QuineError> = match envelope.request {
        Request::Confirm { params } => store
            .confirm(&params, unix_now())
            .map(|value| serde_json::to_value(value).expect("confirm result serializes")),
        Request::Cancel { params } => store
            .cancel(&params, unix_now())
            .map(|value| serde_json::to_value(value).expect("cancel result serializes")),
        Request::Query { params } => store
            .query(&params)
            .map(|value| serde_json::to_value(value).expect("query result serializes")),
        Request::List { params } => store
            .list(&params)
            .map(|value| serde_json::to_value(value).expect("list result serializes")),
        Request::Quiesce { .. } => Err(QuineError::new(
            "invalid_request",
            "quiesce requires the supervising core",
        )),
    };
    match result {
        Ok(value) => ReplyEnvelope::ok(method, value),
        Err(error) => ReplyEnvelope::err(method, &error.code, error.message),
    }
}

fn request_method(request: &Request) -> &'static str {
    match request {
        Request::Confirm { .. } => "confirm",
        Request::Cancel { .. } => "cancel",
        Request::Query { .. } => "query",
        Request::List { .. } => "list",
        Request::Quiesce { .. } => "quiesce",
    }
}

fn unix_now() -> i64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|duration| duration.as_secs() as i64)
        .unwrap_or(0)
}

struct Flags {
    values: Vec<(String, String)>,
}

impl Flags {
    fn parse(arguments: &[String]) -> Flags {
        let mut values = Vec::new();
        let mut index = 0;
        while index < arguments.len() {
            let argument = &arguments[index];
            if let Some(name) = argument.strip_prefix("--") {
                let value =
                    if index + 1 < arguments.len() && !arguments[index + 1].starts_with("--") {
                        index += 1;
                        arguments[index].clone()
                    } else {
                        String::new()
                    };
                values.push((name.to_string(), value));
            }
            index += 1;
        }
        Flags { values }
    }

    fn value(&self, name: &str) -> Option<String> {
        self.values
            .iter()
            .find(|(key, _)| key == name)
            .map(|(_, value)| value.clone())
            .filter(|value| !value.is_empty())
    }

    fn present(&self, name: &str) -> bool {
        self.values.iter().any(|(key, _)| key == name)
    }
}

// Referenced only so the protocol types are linked into the binary for stage 2
// callers; the CLI itself is the tested entry point today.
#[allow(dead_code)]
fn _protocol_types(_: ConfirmParams, _: QueryParams, _: ListParams, _: FrozenInput) {}
