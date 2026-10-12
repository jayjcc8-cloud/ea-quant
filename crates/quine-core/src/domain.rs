//! Client for the stateless Python domain adapter.
//!
//! The adapter owns scenario, economic and report semantics; this module only
//! moves one request document in and one reply document out, as a supervised
//! child process that inherits the core's liveness channel. Nothing here parses
//! economics: a decimal stays an opaque string.

use std::io::{self, Write};
use std::os::unix::fs::OpenOptionsExt;
use std::path::{Path, PathBuf};

use serde_json::Value;

pub const DOMAIN_SCHEMA: &str = "ea.quine-domain.v1";

/// One bounded adapter invocation directory under the core's own workspace.
pub fn staging_dir(workspace: &Path, tag: &str) -> PathBuf {
    workspace.join("tmp").join("domain").join(tag)
}

pub fn write_request(dir: &Path, document: &Value) -> io::Result<PathBuf> {
    std::fs::create_dir_all(dir)?;
    let path = dir.join("request.json");
    let mut file = std::fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(&path)?;
    file.write_all(&serde_json::to_vec(document).map_err(io::Error::other)?)?;
    file.sync_all()?;
    Ok(path)
}

pub fn reply_path(dir: &Path) -> PathBuf {
    dir.join("reply.json")
}

pub fn read_reply(dir: &Path) -> io::Result<Value> {
    let payload = std::fs::read(reply_path(dir))?;
    serde_json::from_slice(&payload).map_err(io::Error::other)
}

/// The exact argv for one adapter mode.
pub fn argv(python: &Path, mode: &str, request: &Path, reply: &Path) -> Vec<String> {
    vec![
        python.display().to_string(),
        "-I".to_string(),
        "-B".to_string(),
        "-m".to_string(),
        "ea.web.rust_domain".to_string(),
        mode.to_string(),
        request.display().to_string(),
        reply.display().to_string(),
    ]
}

/// One parsed adapter reply.
#[derive(Debug, Clone)]
pub struct DomainReply {
    pub status: String,
    pub code: Option<String>,
    pub message: Option<String>,
    pub result: Option<Value>,
}

impl DomainReply {
    pub fn decode(document: &Value) -> Option<DomainReply> {
        if document.get("schema").and_then(Value::as_str) != Some(DOMAIN_SCHEMA) {
            return None;
        }
        Some(DomainReply {
            status: document.get("status")?.as_str()?.to_string(),
            code: document
                .get("code")
                .and_then(Value::as_str)
                .map(str::to_string),
            message: document
                .get("message")
                .and_then(Value::as_str)
                .map(str::to_string),
            result: document.get("result").cloned(),
        })
    }

    pub fn ok(&self) -> bool {
        self.status == "ok"
    }

    /// The refusal code, or a stable placeholder for an unreadable refusal.
    pub fn refusal_code(&self) -> String {
        self.code
            .clone()
            .unwrap_or_else(|| "domain_refused".to_string())
    }
}
