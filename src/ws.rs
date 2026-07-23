//! WebSocket event types and broadcast channel helpers.
//! Replaces Socket.IO with a thin JSON-envelope protocol:
//!   { "type": "event_name", "data": <json value> }

use serde::{Deserialize, Serialize};
use serde_json::Value;
use tokio::sync::broadcast;

pub type WsTx = broadcast::Sender<WsEnvelope>;
pub type WsRx = broadcast::Receiver<WsEnvelope>;

/// Envelope sent over the WebSocket wire in both directions.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct WsEnvelope {
    #[serde(rename = "type")]
    pub kind: String,
    #[serde(default)]
    pub data: Value,
}

impl WsEnvelope {
    pub fn new(kind: impl Into<String>, data: impl Serialize) -> Self {
        Self {
            kind: kind.into(),
            data: serde_json::to_value(data).unwrap_or(Value::Null),
        }
    }
    pub fn empty(kind: impl Into<String>) -> Self {
        Self { kind: kind.into(), data: Value::Null }
    }
    pub fn to_text(&self) -> String {
        serde_json::to_string(self).unwrap()
    }
}

pub fn make_channel() -> WsTx {
    let (tx, _) = broadcast::channel(512);
    tx
}

/// Emit a typed event to all connected WebSocket clients.
pub fn emit(tx: &WsTx, kind: impl Into<String>, data: impl Serialize) {
    let _ = tx.send(WsEnvelope::new(kind, data));
}

pub fn emit_empty(tx: &WsTx, kind: impl Into<String>) {
    let _ = tx.send(WsEnvelope::empty(kind));
}
