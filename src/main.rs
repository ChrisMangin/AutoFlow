//! AutoFlow -- native Rust backend.
//! Starts Axum HTTP/WS server on :7878 and a Win32 tray icon on the main thread.

#![windows_subsystem = "windows"]

mod embedded;
mod export;
mod hud;
mod player;
mod recorder;
mod server;
mod state;
mod tray;
mod ws;

use std::sync::Arc;
use anyhow::Result;
use tracing::info;
use tracing_subscriber::EnvFilter;

use recorder::Recorder;
use server::{ServerState, build_router};
use state::new_state;
use tray::Tray;
use ws::make_channel;

fn main() -> Result<()> {
    // Duplicate instance detection: if port 7878 is already bound, another
    // AutoFlow is running. Open the browser to it and exit.
    if std::net::TcpListener::bind("127.0.0.1:7878").is_err() {
        let _ = open::that("http://localhost:7878");
        return Ok(());
    }

    // Set per-monitor DPI awareness so all coordinates are physical pixels.
    // Without this, SM_CXSCREEN returns logical pixels but WH_MOUSE_LL gives
    // physical -- causing SendInput to map clicks to the wrong position.
    unsafe {
        let _ = windows::Win32::UI::HiDpi::SetProcessDpiAwarenessContext(
            windows::Win32::UI::HiDpi::DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2,
        );
    }

    tracing_subscriber::fmt()
        .with_env_filter(
            EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| EnvFilter::new("info")),
        )
        .init();

    let app    = new_state();
    let ws_tx  = make_channel();
    let (player_cmd_tx, _) = tokio::sync::broadcast::channel::<player::PlayerCmd>(8);
    let recorder = Arc::new(Recorder::new(ws_tx.clone(), app.clone()));

    // ── HUD command channel ───────────────────────────────────────────────────
    // Bypass HTTP for record stop/pause/resume so HUD overlay buttons work
    // reliably even if the hook thread is busy or the Tokio runtime is loaded.
    let (hud_tx, hud_rx) = std::sync::mpsc::sync_channel::<hud::HudCmd>(16);
    hud::set_cmd_sender(hud_tx);

    let recorder_hud = recorder.clone();
    let app_hud      = app.clone();
    let ws_tx_hud    = ws_tx.clone();
    std::thread::spawn(move || {
        use hud::HudCmd;
        use state::AppStatus;
        while let Ok(cmd) = hud_rx.recv() {
            match cmd {
                HudCmd::RecordStop => {
                    // stop() sleeps 100 ms internally; fine on a dedicated thread
                    recorder_hud.stop();
                    // record_stopped WS event is already emitted inside stop()
                }
                HudCmd::RecordPause => {
                    recorder_hud.pause();
                    // Notify browser of the new paused state
                    let s = app_hud.lock();
                    let record_st = match s.status {
                        AppStatus::RecordingPaused => "paused",
                        AppStatus::Recording       => "recording",
                        _                          => "idle",
                    };
                    drop(s);
                    ws::emit(&ws_tx_hud, "app_state",
                        serde_json::json!({ "record": record_st, "play": "idle" }));
                }
                HudCmd::RecordResume => {
                    recorder_hud.resume();
                    let s = app_hud.lock();
                    let record_st = match s.status {
                        AppStatus::Recording       => "recording",
                        AppStatus::RecordingPaused => "paused",
                        _                          => "idle",
                    };
                    drop(s);
                    ws::emit(&ws_tx_hud, "app_state",
                        serde_json::json!({ "record": record_st, "play": "idle" }));
                }
                HudCmd::RecordToggle => {
                    // F9 hotkey: start if idle, stop if recording/paused
                    let is_rec = recorder_hud.is_recording();
                    if is_rec {
                        recorder_hud.stop();
                    } else {
                        recorder_hud.start();
                        ws::emit(&ws_tx_hud, "app_state",
                            serde_json::json!({ "record": "recording", "play": "idle" }));
                    }
                }
            }
        }
    });

    let ss = ServerState {
        app:           app.clone(),
        ws_tx:         ws_tx.clone(),
        recorder:      recorder.clone(),
        player_cmd_tx: player_cmd_tx.clone(),
    };

    // Tokio runtime on background threads
    let rt = tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .build()?;

    rt.spawn(async move {
        let router = build_router(ss);
        let listener = tokio::net::TcpListener::bind("0.0.0.0:7878").await
            .expect("Failed to bind :7878 -- is another instance running?");
        info!("AutoFlow listening on http://localhost:7878");
        axum::serve(listener, router).await.expect("Server error");
    });

    // Open browser after server starts
    std::thread::spawn(|| {
        std::thread::sleep(std::time::Duration::from_millis(600));
        let _ = open::that("http://localhost:7878");
    });

    // HUD overlay windows (created before message loop)
    if let Err(e) = hud::create_windows() {
        tracing::warn!("HUD creation failed: {e}");
    }

    // Tray icon (main thread, Win32 message loop)
    let _tray = Tray::new(ws_tx, app)?;
    run_message_loop();

    Ok(())
}

fn run_message_loop() {
    use windows::Win32::UI::WindowsAndMessaging::{GetMessageW, TranslateMessage, DispatchMessageW, MSG};
    unsafe {
        let mut msg = MSG::default();
        while GetMessageW(&mut msg, None, 0, 0).as_bool() {
            let _ = TranslateMessage(&msg);
            DispatchMessageW(&msg);
        }
    }
}
