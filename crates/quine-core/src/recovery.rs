//! Local stop evidence. A process cannot survive its original machine rebooting.
//! Neither client claims, PIDs, wall clocks nor free locks are evidence here.
use crate::protocol::RecoveryInfo;
use crate::util::sha256_hex;
use serde::{Deserialize, Serialize};
use std::os::unix::fs::MetadataExt;
use std::path::Path;

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub(crate) struct RecoveryContext {
    pub machine: String,
    pub boot: String,
    pub workspace: String,
}

impl RecoveryContext {
    pub fn read(database: &Path) -> Option<Self> {
        let (machine, boot) = platform_identity()?;
        let path = database.canonicalize().ok()?;
        let metadata = path.metadata().ok()?;
        // Copies, restores and moves do not inherit the original execution's
        // workspace identity. Symlink aliases of the same file are equivalent.
        let workspace = sha256_hex(
            format!("{}\0{}\0{}", path.display(), metadata.dev(), metadata.ino()).as_bytes(),
        );
        Some(Self {
            machine: sha256_hex(machine.as_bytes()),
            boot,
            workspace,
        })
    }
    pub fn same_origin(&self, origin: &Self) -> bool {
        self.machine == origin.machine && self.workspace == origin.workspace
    }
}

#[cfg(target_os = "macos")]
fn platform_identity() -> Option<(String, String)> {
    fn output(program: &str, args: &[&str]) -> Option<String> {
        let result = std::process::Command::new(program)
            .args(args)
            .env_clear()
            .output()
            .ok()?;
        result
            .status
            .success()
            .then(|| String::from_utf8(result.stdout).ok())
            .flatten()
    }
    let boot = output("/usr/sbin/sysctl", &["-n", "kern.bootsessionuuid"])?;
    let platform = output("/usr/sbin/ioreg", &["-rd1", "-c", "IOPlatformExpertDevice"])?;
    let machine = platform.lines().find_map(|line| {
        let (key, value) = line.split_once('=')?;
        key.trim()
            .ends_with("\"IOPlatformUUID\"")
            .then(|| value.trim().trim_matches('"').to_owned())
    })?;
    Some((
        uuid::Uuid::parse_str(&machine).ok()?.to_string(),
        uuid::Uuid::parse_str(boot.trim()).ok()?.to_string(),
    ))
}
#[cfg(target_os = "linux")]
fn platform_identity() -> Option<(String, String)> {
    let machine = std::fs::read_to_string("/etc/machine-id").ok()?;
    let boot = std::fs::read_to_string("/proc/sys/kernel/random/boot_id").ok()?;
    Some((
        uuid::Uuid::parse_str(machine.trim()).ok()?.to_string(),
        uuid::Uuid::parse_str(boot.trim()).ok()?.to_string(),
    ))
}
#[cfg(not(any(target_os = "macos", target_os = "linux")))]
fn platform_identity() -> Option<(String, String)> {
    None
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub(crate) struct Preparation {
    pub id: String,
    pub context: RecoveryContext,
    pub base_revision: i64,
    pub prepared_at: i64,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
pub(crate) struct Release {
    pub preparation_id: String,
    pub context: RecoveryContext,
    pub base_revision: i64,
    pub released_at: i64,
}

pub(crate) fn projection(
    context: Option<&RecoveryContext>,
    origin: Option<&RecoveryContext>,
    preparation: Option<&Preparation>,
    release: Option<&Release>,
) -> RecoveryInfo {
    let status = if release.is_some() {
        "released"
    } else if !context.zip(origin).is_some_and(|(c, o)| c.same_origin(o)) {
        "unavailable"
    } else if let Some(prep) = preparation {
        if !context.is_some_and(|c| c.same_origin(&prep.context)) {
            "unavailable"
        } else if context.is_some_and(|c| c.boot != prep.context.boot) {
            "ready"
        } else {
            "restart_required"
        }
    } else {
        "prepare_required"
    };
    RecoveryInfo {
        status: status.into(),
        preparation_id: preparation.map(|p| p.id.clone()),
        prepared_at: preparation.map(|p| p.prepared_at),
        released_at: release.map(|r| r.released_at),
    }
}
