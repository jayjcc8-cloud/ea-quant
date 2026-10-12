//! Deriving staged identities and publishing the verified official bytes.
//!
//! Every staging path is derived from the identities the core itself wrote, and
//! a worker's own relative reference is only ever accepted when it names exactly
//! that derived path. What is published is the byte set this core read after the
//! independent validation proved it, so the published artifact is the verified
//! one rather than a second read of the same name.

use std::io::{self, Write};
use std::os::unix::fs::OpenOptionsExt;
use std::path::{Path, PathBuf};

use serde_json::{json, Value};

use crate::util::sha256_hex;

pub const OFFICIAL_SCHEMA: &str = "ea.quine-official-result.v1";
pub const FAILURE_SCHEMA: &str = "ea.quine-result-failure.v1";
pub const ARTIFACTS: [&str; 3] = ["report.json", "summary.txt", "equity-path.json"];

pub fn staged_attempt(control: &Path, attempt_id: &str) -> PathBuf {
    control.join("runs").join(attempt_id)
}

pub fn staged_report(control: &Path, job_id: &str) -> PathBuf {
    control.join("reports").join(job_id)
}

/// The relative reference a worker must use for one derived staging path.
pub fn expected_relative(kind: &str, identity: &str) -> String {
    format!("{kind}/{identity}")
}

/// The worker's own staged references, held to the derived identities.
///
/// An absent reference is accepted only when the worker did not claim to have
/// staged that artifact at all; a reference that names anything but the derived
/// path is refused rather than followed.
pub fn reference_agrees(claimed: Option<&Value>, expected: &str, must_exist: bool) -> bool {
    match claimed {
        None => !must_exist,
        Some(value) => value.as_str() == Some(expected),
    }
}

/// One worker result document, held to this attempt's own identities.
pub fn worker_result(document: &Value, job_id: &str, attempt_id: &str) -> Result<String, String> {
    if document.get("schema").and_then(Value::as_str) != Some("ea.research-worker.result.v1") {
        return Err("the worker result version is unsupported".to_string());
    }
    if document.get("job_id").and_then(Value::as_str) != Some(job_id)
        || document.get("attempt_id").and_then(Value::as_str) != Some(attempt_id)
    {
        return Err("the worker result is not this attempt".to_string());
    }
    match document.get("status").and_then(Value::as_str) {
        Some("succeeded") => Ok("succeeded".to_string()),
        Some("failed") => Ok("failed".to_string()),
        _ => Err("the worker result has no readable status".to_string()),
    }
}

/// Read exactly the three verified artifact names out of one staged report
/// directory, refusing anything that is not one regular file.
pub fn read_artifacts(report_dir: &Path) -> io::Result<Vec<(String, Vec<u8>)>> {
    let mut artifacts = Vec::new();
    for name in ARTIFACTS {
        let path = report_dir.join(name);
        let metadata = std::fs::symlink_metadata(&path)?;
        if !metadata.is_file() || metadata.file_type().is_symlink() {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                format!("staged {name} is not one regular file"),
            ));
        }
        artifacts.push((name.to_string(), std::fs::read(&path)?));
    }
    Ok(artifacts)
}

/// The immutable official record registered under the current attempt fence.
pub fn official_document(
    job_id: &str,
    attempt_id: &str,
    adapter: &Value,
    artifacts: &[(String, Vec<u8>)],
) -> Value {
    let mut digests = serde_json::Map::new();
    for (name, payload) in artifacts {
        digests.insert(
            name.clone(),
            json!({"sha256": sha256_hex(payload), "size": payload.len()}),
        );
    }
    json!({
        "schema": OFFICIAL_SCHEMA,
        "job_id": job_id,
        "attempt_id": attempt_id,
        "run_id": attempt_id,
        "report_schema": adapter.get("report_schema").cloned().unwrap_or(Value::Null),
        "path_schema": adapter.get("path_schema").cloned().unwrap_or(Value::Null),
        "artifacts": Value::Object(digests),
        // Host-verified economic readings, opaque exact strings and integers.
        "facts": adapter.get("facts").cloned().unwrap_or(Value::Null),
    })
}

pub fn failure_document(code: &str, message: &str) -> Value {
    json!({"schema": FAILURE_SCHEMA, "code": code, "message": message})
}

/// Publish the verified byte set durably, then make it visible as one directory.
pub fn publish(workspace: &Path, job_id: &str, artifacts: &[(String, Vec<u8>)]) -> io::Result<()> {
    let root = workspace.join("official");
    std::fs::create_dir_all(&root)?;
    let target = root.join(job_id);
    match std::fs::create_dir(&target) {
        Ok(()) => {}
        Err(error) if error.kind() == io::ErrorKind::AlreadyExists => {
            return Err(io::Error::new(
                io::ErrorKind::AlreadyExists,
                "an official result already exists for this job",
            ))
        }
        Err(error) => return Err(error),
    }
    for (name, payload) in artifacts {
        let path = target.join(name);
        let mut file = std::fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(&path)?;
        file.write_all(payload)?;
        file.sync_all()?;
    }
    sync_dir(&target)?;
    sync_dir(&root)
}

fn sync_dir(path: &Path) -> io::Result<()> {
    let directory = std::fs::File::open(path)?;
    directory.sync_all()
}

/// The official result directory one job would publish into.
pub fn official_dir(workspace: &Path, job_id: &str) -> PathBuf {
    workspace.join("official").join(job_id)
}
