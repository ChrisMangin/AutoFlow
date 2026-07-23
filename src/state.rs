//! Shared application state — thread-safe via Arc<Mutex<>>.

use std::{collections::HashMap, sync::Arc};
use parking_lot::Mutex;
use serde::{Deserialize, Serialize};
use serde_json::Value;

// ── Step ─────────────────────────────────────────────────────────────────────

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Step {
    pub id: u32,
    #[serde(rename = "type")]
    pub kind: String,
    pub data: Value,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub note: Option<String>,
    #[serde(default)]
    pub disabled: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub timestamp: Option<f64>,
}

// ── Settings ─────────────────────────────────────────────────────────────────

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Settings {
    pub screenshot_mode:    String, // "auto" | "all" | "manual"
    pub screenshot_monitor: i32,    // 0-based monitor index (manual mode only)
}

impl Default for Settings {
    fn default() -> Self {
        Self { screenshot_mode: "auto".into(), screenshot_monitor: 0 }
    }
}

// ── App state ─────────────────────────────────────────────────────────────────

#[derive(Debug, Default, Clone, PartialEq)]
pub enum AppStatus {
    #[default]
    Idle,
    Recording,
    RecordingPaused,
    Playing,
    PlayingPaused,
    Stepping,
}

#[derive(Debug, Default)]
pub struct AppState {
    pub status:    AppStatus,
    pub settings:  Settings,
    pub steps:     Vec<Step>,
    pub variables: HashMap<String, Value>,
}

pub type SharedState = Arc<Mutex<AppState>>;

pub fn new_state() -> SharedState {
    Arc::new(Mutex::new(AppState::default()))
}
