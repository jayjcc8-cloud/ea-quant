//! Sequential JSON-lines CLI for the quine-core store.
//!
//! One line in, one line out. The process holds the single owner lease from
//! startup until EOF, then releases it. Stage 2 adds IPC serving and direct
//! worker supervision on top of the same store.

use std::io::{BufRead, Write};
use std::path::PathBuf;

use quine_core::protocol::{
    CancelParams, ConfirmParams, Envelope, FrozenInput, ListParams, QueryParams, ReplyEnvelope,
    Request, PROTOCOL_VERSION,
};
use quine_core::store::{QuineError, Store};

fn main() {
    let args = Args::parse();
    let store = match Store::open(&args.db, &args.owner) {
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
    }
}

fn unix_now() -> i64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|duration| duration.as_secs() as i64)
        .unwrap_or(0)
}

struct Args {
    db: PathBuf,
    owner: String,
}

impl Args {
    fn parse() -> Args {
        let mut db = None;
        let mut owner = "cli".to_string();
        let mut argv = std::env::args().skip(1);
        while let Some(argument) = argv.next() {
            match argument.as_str() {
                "--db" => {
                    db = argv.next();
                }
                "--owner" => {
                    owner = argv.next().unwrap_or_else(|| "cli".to_string());
                }
                _ => {}
            }
        }
        Args {
            db: PathBuf::from(db.unwrap_or_else(|| "quine.db".to_string())),
            owner,
        }
    }
}

// Referenced only so the protocol types are linked into the binary for stage 2
// callers; the CLI itself is the tested entry point today.
#[allow(dead_code)]
fn _protocol_types(_: ConfirmParams, _: CancelParams, _: QueryParams, _: ListParams, _: FrozenInput) {}
