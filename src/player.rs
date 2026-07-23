//! Step playback engine — executes recorded steps via Win32 SendInput + shell APIs.

use std::{
    collections::HashMap,
    time::Duration,
    thread,
};
use anyhow::{anyhow, Result};
use tokio::sync::broadcast;
use tracing::warn;

use windows::Win32::{
    Foundation::{HWND, LPARAM, WPARAM},
    UI::{
        Input::KeyboardAndMouse::{
            SendInput, INPUT, INPUT_0, INPUT_KEYBOARD, INPUT_MOUSE,
            KEYBDINPUT, MOUSEINPUT, KEYBD_EVENT_FLAGS,
            KEYEVENTF_KEYUP, KEYEVENTF_UNICODE,
            MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP,
            MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP,
            MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP,
            MOUSEEVENTF_MOVE, MOUSEEVENTF_ABSOLUTE, MOUSEEVENTF_WHEEL,
            VIRTUAL_KEY,
        },
        WindowsAndMessaging::{
            FindWindowW, PostMessageW,
            GetSystemMetrics, SM_CXSCREEN, SM_CYSCREEN,
            WM_CLOSE,
        },
    },
    System::{
        Threading::{OpenProcess, TerminateProcess, PROCESS_TERMINATE},
        DataExchange::{OpenClipboard, CloseClipboard, EmptyClipboard, SetClipboardData},
        Memory::{GlobalAlloc, GlobalLock, GlobalUnlock, GMEM_MOVEABLE},
    },
};
use windows::core::PCWSTR;

use crate::{
    hud,
    state::{Step, SharedState, AppStatus},
    ws::{WsTx, emit, emit_empty},
};

/// Control signals sent to the running player task.
#[derive(Clone, Debug)]
pub enum PlayerCmd {
    Stop,
    Pause,
    Resume,
}

pub struct Player {
    state: SharedState,
    ws_tx: WsTx,
}

impl Player {
    pub fn new(state: SharedState, ws_tx: WsTx) -> Self {
        Self { state, ws_tx }
    }

    /// Play the full workflow steps list.
    pub async fn play_all(&self, cmd_rx: broadcast::Receiver<PlayerCmd>) -> Result<()> {
        let (steps, vars) = {
            let s = self.state.lock();
            (s.steps.clone(), s.variables.clone())
        };
        self.run_steps(&steps, &vars, cmd_rx).await
    }

    /// Play a single step by index.
    pub async fn play_step(&self, index: usize) -> Result<()> {
        let (steps, vars) = {
            let s = self.state.lock();
            (s.steps.clone(), s.variables.clone())
        };
        if index >= steps.len() {
            return Err(anyhow!("Step index {} out of range", index));
        }
        self.execute_step(&steps[index], &vars).await
    }

    async fn run_steps(
        &self,
        steps: &[Step],
        vars: &HashMap<String, serde_json::Value>,
        mut cmd_rx: broadcast::Receiver<PlayerCmd>,
    ) -> Result<()> {
        let total = steps.len();
        let mut paused = false;
        let mut i = 0usize;

        while i < steps.len() {
            // Poll control commands
            loop {
                match cmd_rx.try_recv() {
                    Ok(PlayerCmd::Stop) => {
                        emit_empty(&self.ws_tx, "play_done");
                        return Ok(());
                    }
                    Ok(PlayerCmd::Pause) => { paused = true; }
                    Ok(PlayerCmd::Resume) => { paused = false; }
                    _ => break,
                }
            }

            if paused {
                emit_empty(&self.ws_tx, "play_paused");
                loop {
                    match cmd_rx.recv().await {
                        Ok(PlayerCmd::Resume) => { paused = false; break; }
                        Ok(PlayerCmd::Stop) => {
                            emit_empty(&self.ws_tx, "play_done");
                            return Ok(());
                        }
                        _ => {}
                    }
                }
            }

            let step = &steps[i];

            emit(&self.ws_tx, "play_progress", serde_json::json!({ "index": i }));
            hud::show_playing(i as u32, total as u32);
            emit(&self.ws_tx, "step_update", serde_json::json!({
                "index": i,
                "step": step,
            }));

            match self.execute_step(step, vars).await {
                Ok(_) => {
                    // step done — no extra event needed
                }
                Err(e) => {
                    let msg = e.to_string();
                    warn!("Step {} failed: {}", i, msg);
                    emit(&self.ws_tx, "play_error", serde_json::json!({ "error": format!("Step {} ({}): {}", i+1, steps[i].kind, msg) }));
                    emit_empty(&self.ws_tx, "play_done");
                    return Err(e);
                }
            }

            i += 1;
        }

        hud::hide();
        emit_empty(&self.ws_tx, "play_done");
        Ok(())
    }

    async fn execute_step(&self, step: &Step, vars: &HashMap<String, serde_json::Value>) -> Result<()> {
        if step.disabled { return Ok(()); }

        let delay_ms = step.data.get("delay_ms")
            .and_then(|v| v.as_u64())
            .unwrap_or(0);
        if delay_ms > 0 {
            tokio::time::sleep(Duration::from_millis(delay_ms)).await;
        }

        match step.kind.as_str() {
            "click"        => self.do_click(step, false),
            "double_click" => self.do_click(step, true),
            "right_click"  => self.do_click(step, false),
            "scroll"       => self.do_scroll(step),
            "type"         => self.do_type(step, vars).await,
            "hotkey"       => self.do_hotkey(step),
            "key_press"    => self.do_key_press(step),
            "open_file" | "navigate" => self.do_open(step),
            "copy_file"    => self.do_copy_file(step),
            "move_file"    => self.do_move_file(step),
            "delete_file"  => self.do_delete_file(step),
            "create_folder"=> self.do_create_folder(step),
            "write_file"   => self.do_write_file(step, vars),
            "read_file"    => self.do_read_file(step),
            "kill_process" => self.do_kill_process(step),
            "close_window" => self.do_close_window(step),
            "clipboard_copy"  => self.do_clipboard_set(step, vars),
            "clipboard_paste" => self.do_clipboard_paste(),
            "wait"           => self.do_wait(step).await,
            "wait_for_window" => self.do_wait_for_window(step).await,
            "wait_for_url"    => self.do_wait_for_url(step).await,
            "screenshot"   => self.do_screenshot(step),
            "http_request" => self.do_http_request(step, vars).await,
            "set_variable" => self.do_set_variable(step),
            "run_workflow" => Box::pin(self.do_run_workflow(step, vars)).await,
            // Structural steps handled by run_steps logic
            "if" | "else" | "end_if" | "loop" | "end_loop" | "loop_end" | "comment" => Ok(()),
            _ => {
                warn!("Unknown step type: {}", step.kind);
                Ok(())
            }
        }
    }

    // ── Mouse ─────────────────────────────────────────────────────────────────

    fn do_click(&self, step: &Step, double: bool) -> Result<()> {
        let x = step.data.get("x").and_then(|v| v.as_i64()).unwrap_or(0) as i32;
        let y = step.data.get("y").and_then(|v| v.as_i64()).unwrap_or(0) as i32;
        let button = step.data.get("button").and_then(|v| v.as_str()).unwrap_or("left");
        let right = step.kind == "right_click" || button == "right";
        let middle = button == "middle";

        // Smart element-ready check: if we know the target window, wait for it
        // to be in the foreground (or at least visible) before clicking.
        // This replaces manual wait steps for most dynamic-content scenarios.
        if let Some(win) = step.data.get("window").and_then(|v| v.as_str()) {
            if !win.is_empty() && !win.to_lowercase().contains("autoflow") {
                wait_for_window_ready(win, 3000);
            }
        }

        move_mouse_abs(x, y)?;
        thread::sleep(Duration::from_millis(30));

        let clicks = if double { 2 } else { 1 };
        for _ in 0..clicks {
            if right {
                mouse_click(MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP)?;
            } else if middle {
                mouse_click(MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP)?;
            } else {
                mouse_click(MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP)?;
            }
            if double { thread::sleep(Duration::from_millis(50)); }
        }
        Ok(())
    }

    fn do_scroll(&self, step: &Step) -> Result<()> {
        let x = step.data.get("x").and_then(|v| v.as_i64()).unwrap_or(0) as i32;
        let y = step.data.get("y").and_then(|v| v.as_i64()).unwrap_or(0) as i32;
        let dy = step.data.get("dy").and_then(|v| v.as_i64()).unwrap_or(0) as i32;

        move_mouse_abs(x, y)?;
        thread::sleep(Duration::from_millis(20));

        if dy != 0 {
            let delta = (dy * 120) as u32;
            send_mouse_input(MOUSEEVENTF_WHEEL, 0, 0, delta)?;
        }
        Ok(())
    }

    // ── Keyboard ─────────────────────────────────────────────────────────────

    async fn do_type(&self, step: &Step, vars: &HashMap<String, serde_json::Value>) -> Result<()> {
        let text = step.data.get("text")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string();
        let text = substitute_vars(&text, vars);

        for ch in text.chars() {
            send_unicode_char(ch)?;
            tokio::time::sleep(Duration::from_millis(15)).await;
        }
        Ok(())
    }

    fn do_hotkey(&self, step: &Step) -> Result<()> {
        let keys: Vec<String> = step.data.get("keys")
            .and_then(|v| v.as_array())
            .map(|a| a.iter().filter_map(|v| v.as_str().map(String::from)).collect())
            .unwrap_or_default();

        let vkeys: Vec<VIRTUAL_KEY> = keys.iter().filter_map(|k| str_to_vkey(k)).collect();
        for &vk in &vkeys { key_event(vk, false)?; thread::sleep(Duration::from_millis(10)); }
        thread::sleep(Duration::from_millis(30));
        for &vk in vkeys.iter().rev() { key_event(vk, true)?; thread::sleep(Duration::from_millis(10)); }
        Ok(())
    }

    fn do_key_press(&self, step: &Step) -> Result<()> {
        let key = step.data.get("key").and_then(|v| v.as_str()).unwrap_or("");
        if let Some(vk) = str_to_vkey(key) {
            key_event(vk, false)?;
            thread::sleep(Duration::from_millis(30));
            key_event(vk, true)?;
        }
        Ok(())
    }

    // ── Shell / Files ─────────────────────────────────────────────────────────

    fn do_open(&self, step: &Step) -> Result<()> {
        let path = step.data.get("path")
            .or_else(|| step.data.get("url"))
            .and_then(|v| v.as_str())
            .unwrap_or("").to_string();
        if path.is_empty() { return Ok(()); }
        open::that(&path).map_err(|e| anyhow!("open: {e}"))?;
        Ok(())
    }

    fn do_copy_file(&self, step: &Step) -> Result<()> {
        let src = step.data.get("src").and_then(|v| v.as_str()).unwrap_or("");
        let dst = step.data.get("dst").and_then(|v| v.as_str()).unwrap_or("");
        std::fs::copy(src, dst).map_err(|e| anyhow!("copy_file: {e}"))?;
        Ok(())
    }

    fn do_move_file(&self, step: &Step) -> Result<()> {
        let src = step.data.get("src").and_then(|v| v.as_str()).unwrap_or("");
        let dst = step.data.get("dst").and_then(|v| v.as_str()).unwrap_or("");
        std::fs::rename(src, dst).map_err(|e| anyhow!("move_file: {e}"))?;
        Ok(())
    }

    fn do_delete_file(&self, step: &Step) -> Result<()> {
        let path = step.data.get("path").and_then(|v| v.as_str()).unwrap_or("");
        if std::path::Path::new(path).is_dir() {
            std::fs::remove_dir_all(path)
        } else {
            std::fs::remove_file(path)
        }.map_err(|e| anyhow!("delete: {e}"))?;
        Ok(())
    }

    fn do_create_folder(&self, step: &Step) -> Result<()> {
        let path = step.data.get("path").and_then(|v| v.as_str()).unwrap_or("");
        std::fs::create_dir_all(path).map_err(|e| anyhow!("mkdir: {e}"))?;
        Ok(())
    }

    fn do_write_file(&self, step: &Step, vars: &HashMap<String, serde_json::Value>) -> Result<()> {
        let path    = step.data.get("path").and_then(|v| v.as_str()).unwrap_or("");
        let content = step.data.get("content").and_then(|v| v.as_str()).unwrap_or("");
        let content = substitute_vars(content, vars);
        std::fs::write(path, content).map_err(|e| anyhow!("write_file: {e}"))?;
        Ok(())
    }

    fn do_read_file(&self, step: &Step) -> Result<()> {
        let path     = step.data.get("path").and_then(|v| v.as_str()).unwrap_or("");
        let var_name = step.data.get("variable").and_then(|v| v.as_str()).unwrap_or("file_content");
        let content  = std::fs::read_to_string(path).map_err(|e| anyhow!("read_file: {e}"))?;
        self.state.lock().variables.insert(var_name.to_string(), serde_json::Value::String(content));
        Ok(())
    }

    // ── Process / Window ─────────────────────────────────────────────────────

    fn do_kill_process(&self, step: &Step) -> Result<()> {
        let name = step.data.get("name").and_then(|v| v.as_str()).unwrap_or("");
        let pid  = step.data.get("pid").and_then(|v| v.as_u64());

        if let Some(pid) = pid {
            unsafe {
                let h = OpenProcess(PROCESS_TERMINATE, false, pid as u32)
                    .map_err(|e| anyhow!("OpenProcess: {e}"))?;
                TerminateProcess(h, 1).map_err(|e| anyhow!("TerminateProcess: {e}"))?;
            }
        } else if !name.is_empty() {
            std::process::Command::new("taskkill")
                .args(["/F", "/IM", name])
                .output()
                .map_err(|e| anyhow!("taskkill: {e}"))?;
        }
        Ok(())
    }

    fn do_close_window(&self, step: &Step) -> Result<()> {
        let title = step.data.get("title").and_then(|v| v.as_str()).unwrap_or("");
        if title.is_empty() { return Ok(()); }
        let wide: Vec<u16> = title.encode_utf16().chain(std::iter::once(0)).collect();
        unsafe {
            if let Ok(hwnd) = FindWindowW(PCWSTR::null(), PCWSTR(wide.as_ptr())) {
                let _ = PostMessageW(hwnd, WM_CLOSE, WPARAM(0), LPARAM(0));
            }
        }
        Ok(())
    }

    // ── Clipboard ─────────────────────────────────────────────────────────────

    fn do_clipboard_set(&self, step: &Step, vars: &HashMap<String, serde_json::Value>) -> Result<()> {
        let text = step.data.get("text").and_then(|v| v.as_str()).unwrap_or("");
        let text = substitute_vars(text, vars);
        set_clipboard_text(&text)
    }

    fn do_clipboard_paste(&self) -> Result<()> {
        use windows::Win32::UI::Input::KeyboardAndMouse::{VK_CONTROL, VK_V};
        key_event(VK_CONTROL, false)?;
        thread::sleep(Duration::from_millis(20));
        key_event(VK_V, false)?;
        thread::sleep(Duration::from_millis(30));
        key_event(VK_V, true)?;
        thread::sleep(Duration::from_millis(20));
        key_event(VK_CONTROL, true)?;
        Ok(())
    }

    // ── Misc ──────────────────────────────────────────────────────────────────

    async fn do_wait(&self, step: &Step) -> Result<()> {
        let ms = step.data.get("ms").and_then(|v| v.as_u64()).unwrap_or(1000);
        tokio::time::sleep(Duration::from_millis(ms)).await;
        Ok(())
    }

    async fn do_wait_for_window(&self, step: &Step) -> Result<()> {
        let title   = step.data.get("title").and_then(|v| v.as_str()).unwrap_or("").to_string();
        let timeout = step.data.get("timeout_ms").and_then(|v| v.as_u64()).unwrap_or(30_000);
        let partial = step.data.get("partial").and_then(|v| v.as_bool()).unwrap_or(true);

        let deadline = tokio::time::Instant::now() + std::time::Duration::from_millis(timeout);
        loop {
            if tokio::time::Instant::now() >= deadline {
                return Err(anyhow!("wait_for_window: {} not found after {}ms", title, timeout));
            }
            if window_exists(&title, partial) { return Ok(()); }
            tokio::time::sleep(std::time::Duration::from_millis(500)).await;
        }
    }

    async fn do_wait_for_url(&self, step: &Step) -> Result<()> {
        // For browsers, the window title usually contains the page title + browser name.
        // We check the foreground window title for the expected URL fragment.
        let url_fragment = step.data.get("url").and_then(|v| v.as_str()).unwrap_or("").to_string();
        let timeout = step.data.get("timeout_ms").and_then(|v| v.as_u64()).unwrap_or(30_000);

        let deadline = tokio::time::Instant::now() + std::time::Duration::from_millis(timeout);
        loop {
            if tokio::time::Instant::now() >= deadline {
                return Err(anyhow!("wait_for_url: {} not found after {}ms", url_fragment, timeout));
            }
            if browser_title_contains(&url_fragment) { return Ok(()); }
            tokio::time::sleep(std::time::Duration::from_millis(500)).await;
        }
    }


    fn do_screenshot(&self, step: &Step) -> Result<()> {
        let path = step.data.get("path").and_then(|v| v.as_str()).unwrap_or("screenshot.jpg");
        crate::recorder::capture_screenshot_to_file(path)
    }

    async fn do_http_request(&self, step: &Step, vars: &HashMap<String, serde_json::Value>) -> Result<()> {
        use std::io::{Read, Write};

        let url    = step.data.get("url").and_then(|v| v.as_str()).unwrap_or("").to_string();
        let method = step.data.get("method").and_then(|v| v.as_str()).unwrap_or("GET").to_string();
        let body   = step.data.get("body").and_then(|v| v.as_str()).unwrap_or("").to_string();
        let var_nm = step.data.get("variable").and_then(|v| v.as_str()).map(String::from);

        let response = tokio::task::spawn_blocking(move || -> Result<String> {
            let stripped = url.trim_start_matches("http://").trim_start_matches("https://");
            let slash_pos = stripped.find('/').unwrap_or(stripped.len());
            let host_port = &stripped[..slash_pos];
            let path = if slash_pos < stripped.len() { &stripped[slash_pos..] } else { "/" };
            let (host, port_str) = host_port.split_once(':').unwrap_or((host_port, "80"));
            let port: u16 = port_str.parse().unwrap_or(80);

            let mut stream = std::net::TcpStream::connect(format!("{}:{}", host, port))
                .map_err(|e| anyhow!("connect: {e}"))?;
            let req = if body.is_empty() {
                format!("{} {} HTTP/1.0\r\nHost: {}\r\nConnection: close\r\n\r\n", method, path, host)
            } else {
                format!("{} {} HTTP/1.0\r\nHost: {}\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}", method, path, host, body.len(), body)
            };
            stream.write_all(req.as_bytes())?;
            let mut resp = String::new();
            stream.read_to_string(&mut resp)?;
            Ok(if let Some(pos) = resp.find("\r\n\r\n") { resp[pos+4..].to_string() } else { resp })
        }).await??;

        if let Some(name) = var_nm {
            self.state.lock().variables.insert(name, serde_json::Value::String(response));
        }
        Ok(())
    }

    async fn do_run_workflow(
        &self,
        step: &crate::state::Step,
        vars: &HashMap<String, serde_json::Value>,
    ) -> anyhow::Result<()> {
        let file = step.data.get("file")
            .and_then(|v| v.as_str())
            .unwrap_or("");
        if file.is_empty() { return Ok(()); }

        let wf_path = {
            let dir = std::env::var("APPDATA")
                .map(std::path::PathBuf::from)
                .unwrap_or_else(|_| std::path::PathBuf::from("."))
                .join("AutoFlow").join("workflows");
            dir.join(format!("{}.json", file))
        };

        let raw = std::fs::read_to_string(&wf_path)
            .map_err(|e| anyhow::anyhow!("run_workflow '{}': {}", file, e))?;
        let wf: serde_json::Value = serde_json::from_str(&raw)
            .map_err(|e| anyhow::anyhow!("run_workflow parse: {}", e))?;

        let sub_steps: Vec<crate::state::Step> = wf
            .get("steps")
            .and_then(|v| serde_json::from_value(v.clone()).ok())
            .unwrap_or_default();

        // Merge sub-workflow variables into a new scope (sub vars override)
        let mut sub_vars = vars.clone();
        if let Some(obj) = wf.get("variables").and_then(|v| v.as_object()) {
            for (k, v) in obj { sub_vars.entry(k.clone()).or_insert_with(|| v.clone()); }
        }

        // Use a fresh broadcast channel — sub-workflow cannot be paused independently
        let (_, sub_rx) = tokio::sync::broadcast::channel::<PlayerCmd>(4);
        Box::pin(self.run_steps(&sub_steps, &sub_vars, sub_rx)).await
    }


    fn do_set_variable(&self, step: &Step) -> Result<()> {
        let name  = step.data.get("name").and_then(|v| v.as_str()).unwrap_or("");
        let value = step.data.get("value").cloned().unwrap_or(serde_json::Value::Null);
        if !name.is_empty() {
            self.state.lock().variables.insert(name.to_string(), value);
        }
        Ok(())
    }
}

// ── Win32 helpers ─────────────────────────────────────────────────────────────

fn move_mouse_abs(x: i32, y: i32) -> Result<()> {
    unsafe {
        let sw = GetSystemMetrics(SM_CXSCREEN);
        let sh = GetSystemMetrics(SM_CYSCREEN);
        let nx = ((x as i64 * 65535) / sw as i64) as i32;
        let ny = ((y as i64 * 65535) / sh as i64) as i32;

        let input = INPUT {
            r#type: INPUT_MOUSE,
            Anonymous: INPUT_0 {
                mi: MOUSEINPUT { dx: nx, dy: ny, mouseData: 0,
                    dwFlags: MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE,
                    time: 0, dwExtraInfo: 0 },
            },
        };
        SendInput(&[input], std::mem::size_of::<INPUT>() as i32);
    }
    Ok(())
}

fn mouse_click(
    down: windows::Win32::UI::Input::KeyboardAndMouse::MOUSE_EVENT_FLAGS,
    up:   windows::Win32::UI::Input::KeyboardAndMouse::MOUSE_EVENT_FLAGS,
) -> Result<()> {
    send_mouse_input(down, 0, 0, 0)?;
    thread::sleep(Duration::from_millis(20));
    send_mouse_input(up, 0, 0, 0)?;
    Ok(())
}

fn send_mouse_input(
    flags: windows::Win32::UI::Input::KeyboardAndMouse::MOUSE_EVENT_FLAGS,
    dx: i32,
    dy: i32,
    data: u32,
) -> Result<()> {
    unsafe {
        let input = INPUT {
            r#type: INPUT_MOUSE,
            Anonymous: INPUT_0 {
                mi: MOUSEINPUT { dx, dy, mouseData: data, dwFlags: flags, time: 0, dwExtraInfo: 0 },
            },
        };
        SendInput(&[input], std::mem::size_of::<INPUT>() as i32);
    }
    Ok(())
}

fn key_event(vk: VIRTUAL_KEY, key_up: bool) -> Result<()> {
    unsafe {
        let flags = if key_up { KEYEVENTF_KEYUP } else { KEYBD_EVENT_FLAGS(0) };
        let input = INPUT {
            r#type: INPUT_KEYBOARD,
            Anonymous: INPUT_0 {
                ki: KEYBDINPUT { wVk: vk, wScan: 0, dwFlags: flags, time: 0, dwExtraInfo: 0 },
            },
        };
        SendInput(&[input], std::mem::size_of::<INPUT>() as i32);
    }
    Ok(())
}

fn send_unicode_char(ch: char) -> Result<()> {
    let mut buf = [0u16; 2];
    let encoded = ch.encode_utf16(&mut buf);
    for &code_unit in encoded.iter() {
        unsafe {
            let down = INPUT {
                r#type: INPUT_KEYBOARD,
                Anonymous: INPUT_0 {
                    ki: KEYBDINPUT { wVk: VIRTUAL_KEY(0), wScan: code_unit,
                        dwFlags: KEYEVENTF_UNICODE, time: 0, dwExtraInfo: 0 },
                },
            };
            let up = INPUT {
                r#type: INPUT_KEYBOARD,
                Anonymous: INPUT_0 {
                    ki: KEYBDINPUT { wVk: VIRTUAL_KEY(0), wScan: code_unit,
                        dwFlags: KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, time: 0, dwExtraInfo: 0 },
                },
            };
            SendInput(&[down, up], std::mem::size_of::<INPUT>() as i32);
        }
    }
    Ok(())
}

fn set_clipboard_text(text: &str) -> Result<()> {
    let wide: Vec<u16> = text.encode_utf16().chain(std::iter::once(0)).collect();
    let byte_len = wide.len() * 2;
    unsafe {
        OpenClipboard(HWND::default()).map_err(|e| anyhow!("OpenClipboard: {e}"))?;
        let _ = EmptyClipboard();
        let hmem = GlobalAlloc(GMEM_MOVEABLE, byte_len)
            .map_err(|e| { let _ = CloseClipboard(); anyhow!("GlobalAlloc: {e}") })?;
        let ptr = GlobalLock(hmem) as *mut u16;
        if ptr.is_null() {
            let _ = CloseClipboard();
            return Err(anyhow!("GlobalLock null"));
        }
        std::ptr::copy_nonoverlapping(wide.as_ptr(), ptr, wide.len());
        let _ = GlobalUnlock(hmem);
        // CF_UNICODETEXT = 13
        SetClipboardData(13, windows::Win32::Foundation::HANDLE(hmem.0))
            .map_err(|e| { let _ = CloseClipboard(); anyhow!("SetClipboardData: {e}") })?;
        let _ = CloseClipboard();
    }
    Ok(())
}

fn substitute_vars(text: &str, vars: &HashMap<String, serde_json::Value>) -> String {
    let mut result = text.to_string();
    for (k, v) in vars {
        let placeholder = format!("{{{{{}}}}}", k);
        let replacement = match v {
            serde_json::Value::String(s) => s.clone(),
            other => other.to_string(),
        };
        result = result.replace(&placeholder, &replacement);
    }
    result
}


/// Wait up to `timeout_ms` for a window matching the given title substring to
/// appear in the foreground. If the window is already there this returns
/// immediately. This is called automatically before every recorded click so
/// that playback naturally waits for slow-loading pages without requiring
/// manual Wait steps.
fn wait_for_window_ready(title: &str, timeout_ms: u64) {
    use windows::Win32::UI::WindowsAndMessaging::{GetForegroundWindow, GetWindowTextW};
    let title_lower = title.to_lowercase();
    let deadline = std::time::Instant::now() + std::time::Duration::from_millis(timeout_ms);
    loop {
        // Check if the foreground window matches
        let fg_title = unsafe {
            let hwnd = GetForegroundWindow();
            if hwnd.is_invalid() { String::new() } else {
                let mut buf = [0u16; 512];
                let n = GetWindowTextW(hwnd, &mut buf);
                String::from_utf16_lossy(&buf[..n as usize]).to_lowercase()
            }
        };
        if fg_title.contains(&title_lower) { return; }
        if std::time::Instant::now() >= deadline { return; } // timeout — proceed anyway
        thread::sleep(std::time::Duration::from_millis(100));
    }
}

fn str_to_vkey(key: &str) -> Option<VIRTUAL_KEY> {
    use windows::Win32::UI::Input::KeyboardAndMouse::*;
    Some(match key.to_lowercase().as_str() {
        "ctrl" | "control" => VK_CONTROL,
        "shift"            => VK_SHIFT,
        "alt"              => VK_MENU,
        "win" | "super"    => VK_LWIN,
        "enter" | "return" => VK_RETURN,
        "escape" | "esc"   => VK_ESCAPE,
        "tab"              => VK_TAB,
        "backspace"        => VK_BACK,
        "delete" | "del"   => VK_DELETE,
        "insert" | "ins"   => VK_INSERT,
        "home"             => VK_HOME,
        "end"              => VK_END,
        "pageup"  | "pgup" => VK_PRIOR,
        "pagedown"| "pgdn" => VK_NEXT,
        "left"             => VK_LEFT,
        "right"            => VK_RIGHT,
        "up"               => VK_UP,
        "down"             => VK_DOWN,
        "space"            => VK_SPACE,
        "f1"  => VK_F1,  "f2"  => VK_F2,  "f3"  => VK_F3,  "f4"  => VK_F4,
        "f5"  => VK_F5,  "f6"  => VK_F6,  "f7"  => VK_F7,  "f8"  => VK_F8,
        "f9"  => VK_F9,  "f10" => VK_F10, "f11" => VK_F11, "f12" => VK_F12,
        "a" => VK_A, "b" => VK_B, "c" => VK_C, "d" => VK_D, "e" => VK_E,
        "f" => VK_F, "g" => VK_G, "h" => VK_H, "i" => VK_I, "j" => VK_J,
        "k" => VK_K, "l" => VK_L, "m" => VK_M, "n" => VK_N, "o" => VK_O,
        "p" => VK_P, "q" => VK_Q, "r" => VK_R, "s" => VK_S, "t" => VK_T,
        "u" => VK_U, "v" => VK_V, "w" => VK_W, "x" => VK_X, "y" => VK_Y,
        "z" => VK_Z,
        "0" => VK_0, "1" => VK_1, "2" => VK_2, "3" => VK_3, "4" => VK_4,
        "5" => VK_5, "6" => VK_6, "7" => VK_7, "8" => VK_8, "9" => VK_9,
        _ => return None,
    })
}

// ── Wait condition helpers ────────────────────────────────────────────────────

struct WinSearch { title: String, partial: bool, found: bool }

unsafe extern "system" fn enum_windows_cb(hwnd: windows::Win32::Foundation::HWND, lparam: windows::Win32::Foundation::LPARAM) -> windows::Win32::Foundation::BOOL {
    use windows::Win32::UI::WindowsAndMessaging::{GetWindowTextW, IsWindowVisible};
    if !IsWindowVisible(hwnd).as_bool() { return windows::Win32::Foundation::BOOL(1); }
    let data = &mut *(lparam.0 as *mut WinSearch);
    let mut buf = [0u16; 512];
    let len = GetWindowTextW(hwnd, &mut buf);
    if len > 0 {
        let wt = String::from_utf16_lossy(&buf[..len as usize]).to_lowercase();
        if (data.partial && wt.contains(&data.title)) || (!data.partial && wt == data.title) {
            data.found = true;
            return windows::Win32::Foundation::BOOL(0);
        }
    }
    windows::Win32::Foundation::BOOL(1)
}

fn window_exists(title: &str, partial: bool) -> bool {
    use windows::Win32::UI::WindowsAndMessaging::EnumWindows;
    let mut data = WinSearch { title: title.to_lowercase(), partial, found: false };
    unsafe {
        let _ = EnumWindows(Some(enum_windows_cb), windows::Win32::Foundation::LPARAM(&mut data as *mut WinSearch as isize));
    }
    data.found
}

fn browser_title_contains(fragment: &str) -> bool {
    use windows::Win32::UI::WindowsAndMessaging::{GetForegroundWindow, GetWindowTextW};
    unsafe {
        let hwnd = GetForegroundWindow();
        let mut buf = [0u16; 512];
        let len = GetWindowTextW(hwnd, &mut buf);
        if len > 0 {
            let title = String::from_utf16_lossy(&buf[..len as usize]).to_lowercase();
            return title.contains(&fragment.to_lowercase());
        }
        false
    }
}
