//! Axum HTTP server — REST API matches the existing app.js frontend exactly.

use std::{collections::HashMap, path::PathBuf, sync::{Arc, atomic::{AtomicUsize, Ordering}}};
use axum::{
    extract::{Path, State, WebSocketUpgrade},
    extract::ws::{WebSocket, Message},
    http::{StatusCode, header},
    response::{IntoResponse, Response},
    routing::{delete, get, post},
    Json, Router,
};
use futures_util::{SinkExt, StreamExt};
use serde::Deserialize;
use serde_json::{json, Value};
use tokio::sync::broadcast;
use crate::embedded::serve_embedded;
use tracing::{info, warn};

use crate::{
    export,
    player::{Player, PlayerCmd},
    recorder::Recorder,
    state::{AppState, AppStatus, SharedState, Step},
    ws::{WsEnvelope, WsTx, emit, emit_empty},
};

/// Tracks live WebSocket connections. When it hits 0, the app auto-exits.
static CLIENT_COUNT: AtomicUsize = AtomicUsize::new(0);

// ── Server state ──────────────────────────────────────────────────────────────

#[derive(Clone)]
pub struct ServerState {
    pub app:           SharedState,
    pub ws_tx:         WsTx,
    pub recorder:      Arc<Recorder>,
    pub player_cmd_tx: broadcast::Sender<PlayerCmd>,
}

// ── Router ────────────────────────────────────────────────────────────────────

pub fn build_router(ss: ServerState) -> Router {
    use tower_http::cors::{CorsLayer, Any};

    let cors = CorsLayer::new()
        .allow_origin(Any)
        .allow_methods(Any)
        .allow_headers(Any);

    Router::new()
        // WebSocket
        .route("/ws",                     get(ws_handler))
        // Record
        .route("/api/record/start",       post(record_start))
        .route("/api/record/stop",        post(record_stop))
        .route("/api/record/pause",       post(record_pause))
        .route("/api/record/resume",      post(record_resume))
        // Playback
        .route("/api/play",               post(play_start))
        .route("/api/play/stop",          post(play_stop))
        .route("/api/play/pause",         post(play_pause))
        .route("/api/play/resume",        post(play_resume))
        .route("/api/play/step",          post(play_step))
        // Workflows
        .route("/api/workflows",           get(list_workflows).post(save_workflow))
        .route("/api/workflows/load",      post(load_workflow_by_body))
        .route("/api/workflows/delete",    post(del_workflow_by_body))
        .route("/api/workflows/{file}",    get(load_workflow))
        // Settings
        .route("/api/settings",           get(get_settings).post(update_settings).patch(update_settings))
        // Export / Import
        .route("/api/export/report",      post(export_report))
        .route("/api/export/script",      post(export_script))
        .route("/api/export/zip",         post(export_zip))
        .route("/api/export/schedule",    post(export_schedule))
        .route("/api/import/script",      post(import_script))
        // AI
        .route("/api/ai/describe",        post(ai_describe))
        // Misc
        .route("/api/quit",               post(quit))
        .route("/api/overlay/hide",      post(|| async { axum::Json(serde_json::json!({"ok":true})) }))
        .route("/api/overlay/show",      post(|| async { axum::Json(serde_json::json!({"ok":true})) }))
        // Everything else: serve embedded static file (or index.html)
        .fallback(|uri: axum::http::Uri| async move { serve_embedded(uri.path()).await })
        .layer(cors)
        .with_state(ss)
}


// ── WebSocket ─────────────────────────────────────────────────────────────────

async fn ws_handler(ws: WebSocketUpgrade, State(ss): State<ServerState>) -> impl IntoResponse {
    ws.on_upgrade(move |socket| handle_socket(socket, ss))
}

async fn handle_socket(socket: WebSocket, ss: ServerState) {
    CLIENT_COUNT.fetch_add(1, Ordering::SeqCst);
    info!("WS client connected (total: {})", CLIENT_COUNT.load(Ordering::SeqCst));

    let (mut sender, mut receiver) = socket.split();
    let mut ws_rx = ss.ws_tx.subscribe();

    // Send initial state on connect (drop lock before .await)
    let init_msg = {
        let s = ss.app.lock();
        WsEnvelope::new("app_state", app_state_json(&s))
    };
    let _ = sender.send(Message::Text(init_msg.to_text().into())).await;

    let out = tokio::spawn(async move {
        while let Ok(env) = ws_rx.recv().await {
            if sender.send(Message::Text(env.to_text().into())).await.is_err() { break; }
        }
    });

    // Read until disconnect
    while let Some(Ok(msg)) = receiver.next().await {
        if let Message::Close(_) = msg { break; }
    }
    out.abort();

    let remaining = CLIENT_COUNT.fetch_sub(1, Ordering::SeqCst) - 1;
    info!("WS client disconnected (remaining: {})", remaining);

    // When the last browser tab closes: hide the HUD overlay immediately so it
    // doesn't linger, then exit the process after a short grace window that lets
    // page refreshes reconnect without restarting the whole app.
    if remaining == 0 {
        crate::hud::hide(); // instant — just flips an atomic and repaints
        tokio::spawn(async {
            tokio::time::sleep(std::time::Duration::from_millis(300)).await;
            if CLIENT_COUNT.load(Ordering::SeqCst) == 0 {
                info!("No clients — exiting");
                std::process::exit(0);
            }
        });
    }
}

/// Build the app_state JSON that onAppState() in app.js expects.
pub fn app_state_json(s: &AppState) -> Value {
    json!({
        "record": match s.status {
            AppStatus::Recording       => "recording",
            AppStatus::RecordingPaused => "paused",
            _                          => "idle",
        },
        "play": match s.status {
            AppStatus::Playing       => "playing",
            AppStatus::PlayingPaused => "paused",
            _                        => "idle",
        },
    })
}

fn broadcast_state(ws_tx: &WsTx, app: &SharedState) {
    let s = app.lock();
    emit(ws_tx, "app_state", app_state_json(&s));
}

// ── Record endpoints ──────────────────────────────────────────────────────────

async fn record_start(State(ss): State<ServerState>) -> impl IntoResponse {
    crate::hud::move_to_cursor_monitor();
    ss.recorder.start();
    broadcast_state(&ss.ws_tx, &ss.app);
    Json(json!({"ok": true}))
}

async fn record_stop(State(ss): State<ServerState>) -> impl IntoResponse {
    // recorder.stop() sleeps 100 ms — run it on a blocking thread so Tokio isn't stalled
    let recorder = ss.recorder.clone();
    tokio::task::spawn_blocking(move || recorder.stop()).await.ok();
    broadcast_state(&ss.ws_tx, &ss.app);
    // Steps are delivered via WS record_stopped event — no need to repeat them here
    Json(json!({"ok": true}))
}

async fn record_pause(State(ss): State<ServerState>) -> impl IntoResponse {
    ss.recorder.pause();
    broadcast_state(&ss.ws_tx, &ss.app);
    Json(json!({"ok": true}))
}

async fn record_resume(State(ss): State<ServerState>) -> impl IntoResponse {
    ss.recorder.resume();
    broadcast_state(&ss.ws_tx, &ss.app);
    Json(json!({"ok": true}))
}

// ── Play endpoints ────────────────────────────────────────────────────────────

#[derive(Deserialize)]
struct PlayBody {
    steps:               Option<Vec<Step>>,
    variables:           Option<HashMap<String, Value>>,
    #[serde(rename = "startIndex", default)]
    start_index:         usize,
    // speed and useElementTargeting ignored for now
}

async fn play_start(
    State(ss): State<ServerState>,
    body: Option<Json<PlayBody>>,
) -> impl IntoResponse {
    crate::hud::move_to_cursor_monitor();
    // Optionally load fresh steps/variables from the request
    if let Some(Json(b)) = body {
        let mut s = ss.app.lock();
        if let Some(steps) = b.steps    { s.steps     = steps; }
        if let Some(vars)  = b.variables { s.variables = vars; }
        s.status = AppStatus::Playing;
    } else {
        ss.app.lock().status = AppStatus::Playing;
    }

    let app    = ss.app.clone();
    let ws_tx  = ss.ws_tx.clone();
    let cmd_rx = ss.player_cmd_tx.subscribe();

    tokio::spawn(async move {
        let player = Player::new(app.clone(), ws_tx.clone());
        if let Err(e) = player.play_all(cmd_rx).await {
            warn!("play error: {e}");
        }
        app.lock().status = AppStatus::Idle;
        broadcast_state(&ws_tx, &app);
    });

    Json(json!({"ok": true}))
}

async fn play_stop(State(ss): State<ServerState>) -> impl IntoResponse {
    let _ = ss.player_cmd_tx.send(PlayerCmd::Stop);
    ss.app.lock().status = AppStatus::Idle;
    Json(json!({"ok": true}))
}

async fn play_pause(State(ss): State<ServerState>) -> impl IntoResponse {
    let _ = ss.player_cmd_tx.send(PlayerCmd::Pause);
    ss.app.lock().status = AppStatus::PlayingPaused;
    Json(json!({"ok": true}))
}

async fn play_resume(State(ss): State<ServerState>) -> impl IntoResponse {
    let _ = ss.player_cmd_tx.send(PlayerCmd::Resume);
    ss.app.lock().status = AppStatus::Playing;
    Json(json!({"ok": true}))
}

async fn play_step(
    State(ss): State<ServerState>,
    body: Option<Json<PlayBody>>,
) -> impl IntoResponse {
    if let Some(Json(b)) = body {
        // "Start player in single-step mode" — load steps, play one, pause
        let mut s = ss.app.lock();
        if let Some(steps) = b.steps     { s.steps     = steps; }
        if let Some(vars)  = b.variables  { s.variables = vars; }
        let idx = b.start_index;
        s.status = AppStatus::Playing;
        drop(s);

        let app   = ss.app.clone();
        let ws_tx = ss.ws_tx.clone();
        let tx    = ss.player_cmd_tx.clone();
        tokio::spawn(async move {
            let player = Player::new(app.clone(), ws_tx.clone());
            if let Err(e) = player.play_step(idx).await {
                warn!("step error: {e}");
                emit(&ws_tx, "play_error", json!({ "error": e.to_string() }));
            }
            app.lock().status = AppStatus::PlayingPaused;
        });
    } else {
        // Player is already running paused — advance one step
        let _ = ss.player_cmd_tx.send(PlayerCmd::Resume);
    }
    Json(json!({"ok": true}))
}

// ── Workflows ─────────────────────────────────────────────────────────────────

fn app_data_dir() -> PathBuf {
    // Use %APPDATA%\AutoFlow — consistent regardless of where the exe lives
    if let Ok(appdata) = std::env::var("APPDATA") {
        let dir = PathBuf::from(appdata).join("AutoFlow");
        let _ = std::fs::create_dir_all(&dir);
        return dir;
    }
    // Fallback: next to exe
    std::env::current_exe()
        .ok()
        .and_then(|p| p.parent().map(|d| d.to_path_buf()))
        .unwrap_or_else(|| PathBuf::from("."))
}

fn workflows_dir() -> PathBuf {
    let dir = app_data_dir().join("workflows");
    let _ = std::fs::create_dir_all(&dir);
    dir
}

async fn list_workflows() -> impl IntoResponse {
    let dir = workflows_dir();
    let _ = std::fs::create_dir_all(&dir);

    let workflows: Vec<Value> = std::fs::read_dir(&dir)
        .into_iter()
        .flatten()
        .flatten()
        .filter_map(|e| {
            let p = e.path();
            if p.extension().map(|x| x == "json").unwrap_or(false) {
                let file = p.file_stem()?.to_str()?.to_string();
                let raw  = std::fs::read_to_string(&p).ok()?;
                let wf: Value = serde_json::from_str(&raw).ok()?;
                let name = wf.get("name").and_then(|v| v.as_str())
                    .unwrap_or(&file).to_string();
                let steps = wf.get("steps").and_then(|v| v.as_array())
                    .map(|a| a.len()).unwrap_or(0);
                Some(json!({ "file": file, "name": name, "steps": steps }))
            } else {
                None
            }
        })
        .collect();

    Json(json!({"ok": true, "workflows": workflows}))
}

#[derive(Deserialize)]
struct SaveBody {
    name:      String,
    steps:     Vec<Step>,
    #[serde(default)]
    variables: HashMap<String, Value>,
}

async fn save_workflow(Json(body): Json<SaveBody>) -> impl IntoResponse {
    let dir = workflows_dir();
    let _ = std::fs::create_dir_all(&dir);
    // Sanitise filename
    let file: String = body.name.chars().map(|c| if c.is_alphanumeric()||c=='-'||c=='_'||c==' ' { c } else { '_' }).collect();
    let path = dir.join(format!("{}.json", file));
    let payload = json!({
        "name": body.name, "steps": body.steps, "variables": body.variables
    });
    match std::fs::write(&path, serde_json::to_string_pretty(&payload).unwrap()) {
        Ok(_)  => Json(json!({"ok": true, "file": file})).into_response(),
        Err(e) => Json(json!({"ok": false, "error": e.to_string()})).into_response(),
    }
}

async fn load_workflow(
    State(ss): State<ServerState>,
    Path(file): Path<String>,
) -> impl IntoResponse {
    let path = workflows_dir().join(format!("{}.json", file));
    match std::fs::read_to_string(&path) {
        Ok(raw) => {
            if let Ok(wf) = serde_json::from_str::<Value>(&raw) {
                let mut s = ss.app.lock();
                if let Some(steps) = wf.get("steps").and_then(|v| serde_json::from_value(v.clone()).ok()) {
                    s.steps = steps;
                }
                if let Some(vars) = wf.get("variables").and_then(|v| serde_json::from_value(v.clone()).ok()) {
                    s.variables = vars;
                }
            }
            Json(json!({"ok": true, "workflow": serde_json::from_str::<Value>(&raw).unwrap_or(Value::Null)}))
                .into_response()
        }
        Err(_) => Json(json!({"ok": false, "error": "not found"})).into_response(),
    }
}

#[derive(Deserialize)]
struct FileBody { file: String }

async fn del_workflow_by_body(Json(body): Json<FileBody>) -> impl IntoResponse {
    let file = body.file.trim().to_string();
    if file.is_empty() {
        return Json(json!({"ok": false, "error": "no file specified"})).into_response();
    }
    let path = workflows_dir().join(format!("{}.json", file));
    match std::fs::remove_file(&path) {
        Ok(_)  => Json(json!({"ok": true})).into_response(),
        Err(e) => Json(json!({"ok": false, "error": e.to_string()})).into_response(),
    }
}

// Legacy path-param handler kept for backwards compat (GET /api/workflows/{file} still works)
async fn del_workflow(Path(file): Path<String>) -> impl IntoResponse {
    let path = workflows_dir().join(format!("{}.json", file));
    match std::fs::remove_file(&path) {
        Ok(_)  => Json(json!({"ok": true})).into_response(),
        Err(e) => Json(json!({"ok": false, "error": e.to_string()})).into_response(),
    }
}

async fn load_workflow_by_body(
    State(ss): State<ServerState>,
    Json(body): Json<FileBody>,
) -> impl IntoResponse {
    let file = body.file.trim().to_string();
    let path = workflows_dir().join(format!("{}.json", file));
    match std::fs::read_to_string(&path) {
        Ok(raw) => {
            if let Ok(wf) = serde_json::from_str::<Value>(&raw) {
                let mut s = ss.app.lock();
                if let Some(steps) = wf.get("steps").and_then(|v| serde_json::from_value(v.clone()).ok()) {
                    s.steps = steps;
                }
                if let Some(vars) = wf.get("variables").and_then(|v| serde_json::from_value(v.clone()).ok()) {
                    s.variables = vars;
                }
            }
            Json(json!({"ok": true, "workflow": serde_json::from_str::<Value>(&raw).unwrap_or(Value::Null)}))
                .into_response()
        }
        Err(_) => Json(json!({"ok": false, "error": "not found"})).into_response(),
    }
}

// ── Settings ──────────────────────────────────────────────────────────────────

async fn get_settings(State(ss): State<ServerState>) -> impl IntoResponse {
    Json(ss.app.lock().settings.clone())
}

async fn update_settings(State(ss): State<ServerState>, Json(patch): Json<Value>) -> impl IntoResponse {
    let mut s = ss.app.lock();
    if let Some(v) = patch.get("screenshot_mode").and_then(|v| v.as_str()) {
        s.settings.screenshot_mode = v.to_string();
    }
    if let Some(v) = patch.get("screenshot_monitor").and_then(|v| v.as_i64()) {
        s.settings.screenshot_monitor = v as i32;
    }
    Json(s.settings.clone())
}

// ── Export / Import ───────────────────────────────────────────────────────────

async fn export_report(Json(body): Json<export::ExportRequest>) -> impl IntoResponse {
    let html = export::generate_pdf_html(&body);
    (StatusCode::OK, [(header::CONTENT_TYPE, "text/html; charset=utf-8")], html).into_response()
}

async fn export_script(Json(body): Json<export::ExportRequest>) -> impl IntoResponse {
    let script = export::generate_script(&body);
    (StatusCode::OK, [(header::CONTENT_TYPE, "text/plain; charset=utf-8")], script).into_response()
}

async fn export_zip(Json(body): Json<export::ExportRequest>) -> impl IntoResponse {
    match export::generate_zip(&body) {
        Ok(bytes) => (StatusCode::OK, [(header::CONTENT_TYPE, "application/zip")], bytes).into_response(),
        Err(e)    => Json(json!({"ok": false, "error": e.to_string()})).into_response(),
    }
}

async fn import_script(
    State(_ss): State<ServerState>,
    Json(body): Json<Value>,
) -> impl IntoResponse {
    // Basic stub: return empty workflow (full Python→steps parsing is complex)
    let _content = body.get("content").and_then(|v| v.as_str()).unwrap_or("");
    Json(json!({"ok": true, "workflow": {"steps": [], "variables": {}}}))
}

// ── Quit ──────────────────────────────────────────────────────────────────────

async fn quit() -> impl IntoResponse {
    // Spawn a thread to exit after responding so the client gets the 200
    std::thread::spawn(|| {
        std::thread::sleep(std::time::Duration::from_millis(100));
        std::process::exit(0);
    });
    Json(json!({"ok": true}))
}

// ── AI step description (calls local Ollama if available) ────────────────────

#[derive(Deserialize)]
struct AiDescribeReq {
    #[serde(rename = "type", default)]
    kind: String,
    data: Option<serde_json::Value>,
}

async fn ai_describe(Json(body): Json<AiDescribeReq>) -> impl IntoResponse {
    let data = body.data.unwrap_or_default();
    let element = data.get("element")
        .and_then(|e| e.get("name")).and_then(|n| n.as_str()).unwrap_or("");
    let el_type = data.get("element")
        .and_then(|e| e.get("type")).and_then(|t| t.as_str()).unwrap_or("");
    let window  = data.get("window").and_then(|w| w.as_str()).unwrap_or("");
    let url     = data.get("url").and_then(|u| u.as_str()).unwrap_or("");
    let text    = data.get("text").and_then(|t| t.as_str()).unwrap_or("");
    let combo   = data.get("combo").and_then(|c| c.as_str()).unwrap_or("");

    let step_info = match body.kind.as_str() {
        "click" if !element.is_empty() =>
            format!("click {} \"{}\" in \"{}\"", el_type, element, window),
        "click" =>
            format!("click at ({},{}) in \"{}\"",
                data.get("x").and_then(|v|v.as_i64()).unwrap_or(0),
                data.get("y").and_then(|v|v.as_i64()).unwrap_or(0),
                window),
        "type"     => format!("type text \"{}\"", &text.chars().take(40).collect::<String>()),
        "hotkey"   => format!("press hotkey {}", combo),
        "navigate" => format!("navigate to {}", url),
        "wait"     => format!("wait {} ms", data.get("ms").and_then(|v|v.as_u64()).unwrap_or(1000)),
        "open_file"=> format!("open {}",
            data.get("path").and_then(|v|v.as_str()).unwrap_or("")),
        other      => other.to_string(),
    };

    let prompt = format!(
        "Describe this UI automation step in ONE short plain-English phrase (no quotes, no punctuation at end, max 12 words): {step_info}"
    );

    match call_ollama_raw(&prompt).await {
        Ok(desc) if !desc.is_empty() =>
            Json(json!({"ok": true, "description": desc})).into_response(),
        Ok(_) =>
            Json(json!({"ok": false, "error": "empty response"})).into_response(),
        Err(e) =>
            Json(json!({"ok": false, "error": e.to_string()})).into_response(),
    }
}

async fn call_ollama_raw(prompt: &str) -> anyhow::Result<String> {
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    let mut stream = tokio::net::TcpStream::connect("127.0.0.1:11434")
        .await
        .map_err(|_| anyhow::anyhow!("Ollama not running on port 11434"))?;

    // Try qwen3:8b first (smallest capable model)
    let body = serde_json::json!({
        "model": "qwen3:8b",
        "prompt": prompt,
        "stream": false,
        "think": false,
        "options": {"temperature": 0.2, "num_predict": 50}
    }).to_string();

    let req = format!(
        "POST /api/generate HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
        body.len(), body
    );
    stream.write_all(req.as_bytes()).await?;
    let _ = stream.shutdown().await;

    let mut raw = Vec::new();
    stream.read_to_end(&mut raw).await?;
    let resp_str = String::from_utf8_lossy(&raw);

    // HTTP response: skip headers
    let body_start = resp_str.find("\r\n\r\n").map(|i| i + 4).unwrap_or(0);
    let json_str = resp_str[body_start..].trim();
    let v: serde_json::Value = serde_json::from_str(json_str)?;
    let text = v.get("response").and_then(|r| r.as_str()).unwrap_or("").trim().to_string();
    Ok(text)
}

// ── Schedule export ────────────────────────────────────────────────────────────

async fn export_schedule(
    Json(body): Json<crate::export::ExportRequest>,
) -> impl IntoResponse {
    match crate::export::generate_schedule_zip(&body) {
        Ok(data) => (
            axum::http::StatusCode::OK,
            [(header::CONTENT_TYPE, "application/zip"),
             (header::CONTENT_DISPOSITION, "attachment; filename=\"autoflow_schedule.zip\"")],
            data,
        ).into_response(),
        Err(e) => (
            axum::http::StatusCode::INTERNAL_SERVER_ERROR,
            [(header::CONTENT_TYPE, "application/json"),
             (header::CONTENT_DISPOSITION, "inline")],
            format!("{{\"ok\":false,\"error\":\"{}\"}}", e).into_bytes(),
        ).into_response(),
    }
}
