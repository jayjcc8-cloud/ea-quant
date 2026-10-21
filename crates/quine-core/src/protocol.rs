//! Versioned JSON request/reply protocol for the quine-core CLI and store.

use serde::{Deserialize, Serialize};

pub const PROTOCOL_VERSION: &str = "quine.v1";

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Envelope {
    pub protocol: String,
    pub request: Request,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "method", rename_all = "snake_case")]
pub enum Request {
    Confirm {
        params: ConfirmParams,
    },
    Cancel {
        params: CancelParams,
    },
    Query {
        params: QueryParams,
    },
    List {
        params: ListParams,
    },
    /// Close dispatch admission and drain only work owned by this core session.
    Quiesce {
        params: ListParams,
    },
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ConfirmParams {
    pub strategy_id: String,
    pub intent_id: String,
    pub draft_revision: i64,
    pub preview_identity: String,
    pub budget: Budget,
    pub frozen_input: FrozenInput,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct Budget {
    pub max_model_calls: i64,
    pub max_backtests: i64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct FrozenInput {
    /// The exact canonical bytes Python produced, hex-encoded. These bytes are
    /// the frozen payload and are stored untouched; Rust never parses them as
    /// economic numbers.
    pub canonical_bytes: String,
    /// Lowercase SHA-256 hex of `canonical_bytes`, the identity binding.
    pub sha256: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CancelParams {
    pub strategy_id: String,
    pub intent_id: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct QueryParams {
    pub strategy_id: String,
    pub intent_id: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ListParams {}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ConfirmResult {
    pub experiment: Experiment,
    pub replayed: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CancelResult {
    pub strategy_id: String,
    pub intent_id: String,
    pub cancel_receipt: String,
    pub experiment: Option<Experiment>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct QueryResult {
    pub experiment: Option<Experiment>,
    pub barrier: Option<Barrier>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ListResult {
    pub experiments: Vec<Experiment>,
    pub barriers: Vec<Barrier>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Experiment {
    pub strategy_id: String,
    pub intent_id: String,
    pub request_id: String,
    pub draft_revision: i64,
    pub preview_identity: String,
    pub budget: Budget,
    pub frozen_sha256: String,
    pub frozen_canonical: String,
    pub status: String,
    pub job_id: String,
    pub attempt_id: String,
    pub dispatch_eligible: bool,
    pub dispatch_owner: Option<String>,
    pub revision: i64,
    pub result: Option<serde_json::Value>,
    pub created_at: i64,
    pub dispatched_at: Option<i64>,
    pub committed_at: Option<i64>,
    pub cancelled_at: Option<i64>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Barrier {
    pub strategy_id: String,
    pub intent_id: String,
    pub cancel_receipt: String,
    pub created_at: i64,
}

/// A proven result the future Rust supervisor submits. Every identity and the
/// record fence must match the current attempt before the result is registered.
/// This is a library seam only: the CLI never accepts result registration from
/// a caller.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ResultSubmission {
    pub strategy_id: String,
    pub intent_id: String,
    pub request_id: String,
    pub job_id: String,
    pub attempt_id: String,
    pub draft_revision: i64,
    pub frozen_sha256: String,
    pub fence: i64,
    pub outcome: String,
    pub result: serde_json::Value,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ErrorBody {
    pub code: String,
    pub message: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ReplyEnvelope {
    pub protocol: String,
    pub method: String,
    pub ok: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub result: Option<serde_json::Value>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<ErrorBody>,
}

impl ReplyEnvelope {
    pub fn ok(method: &str, result: serde_json::Value) -> Self {
        ReplyEnvelope {
            protocol: PROTOCOL_VERSION.to_string(),
            method: method.to_string(),
            ok: true,
            result: Some(result),
            error: None,
        }
    }

    pub fn err(method: &str, code: &str, message: String) -> Self {
        ReplyEnvelope {
            protocol: PROTOCOL_VERSION.to_string(),
            method: method.to_string(),
            ok: false,
            result: None,
            error: Some(ErrorBody {
                code: code.to_string(),
                message,
            }),
        }
    }
}
