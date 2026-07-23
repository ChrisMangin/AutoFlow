//! Recording engine — Win32 low-level hooks + screenshot capture + UIA detection.
//! Hook thread runs its own Win32 message pump.

use crate::{
    hud,
    state::{AppStatus, SharedState, Step},
    ws::{emit, emit_empty, WsTx},
};
use base64::{engine::general_purpose::STANDARD as B64, Engine};
use parking_lot::Mutex;
use serde_json::{json, Value};
use std::{
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc,
    },
    time::{Duration, Instant},
};
use windows::Win32::{
    Foundation::{LPARAM, LRESULT, RECT, WPARAM, HWND, POINT},
    Graphics::Gdi::{
        BitBlt, CreateCompatibleBitmap, CreateCompatibleDC, DeleteDC, DeleteObject,
        GetDC, ReleaseDC, SelectObject, SRCCOPY, BITMAPINFOHEADER, BITMAPINFO,
        GetDIBits, DIB_RGB_COLORS, BI_RGB,
    },
    UI::{
        Input::KeyboardAndMouse::GetKeyState,
        WindowsAndMessaging::{
            CallNextHookEx, GetMessageW, HHOOK, PostThreadMessageW,
            SetWindowsHookExW, UnhookWindowsHookEx, WH_KEYBOARD_LL,
            WH_MOUSE_LL, MSG, WM_QUIT, KBDLLHOOKSTRUCT, MSLLHOOKSTRUCT,
            WM_KEYDOWN, WM_SYSKEYDOWN, WM_LBUTTONDOWN, WM_RBUTTONDOWN,
            WM_MBUTTONDOWN, WM_MOUSEWHEEL, WM_MOUSEMOVE, HC_ACTION,
            GetCursorPos, GetForegroundWindow, GetWindowTextW,
            GetWindowRect, WINDOWPLACEMENT,
        },
    },
    System::Threading::GetCurrentThreadId,
};

// ── Thread-local hook state ───────────────────────────────────────────────────

thread_local! {
    static HOOK_CTX: std::cell::RefCell<Option<HookContext>> = std::cell::RefCell::new(None);
}

struct HookContext {
    ws_tx:      WsTx,
    state:      SharedState,
    recording:  Arc<AtomicBool>,
    paused:     Arc<AtomicBool>,
    steps:      Arc<Mutex<Vec<Step>>>,
    next_id:    u32,
    start_time: Instant,
    /// Ignore input events before this time so the user can switch to the
    /// target app without those transition clicks being recorded.
    startup_end: Instant,
    /// keys currently held (for modifier tracking)
    held_mods:  std::collections::HashSet<u32>,
    pending_type_buf: String,
    last_scroll_pos:  (i32, i32),
    last_scroll_dy:   i32,
    // Double-click detection
    last_lclick_pos:  (i32, i32),
    last_lclick_time: std::time::Instant,
    drag_start:       Option<(i32, i32)>,
    // Auto-wait: last recorded event time (seconds from recording start).
    // f64::INFINITY = no prior event yet (suppresses wait before first action).
    last_event_ts:    f64,
}

// ── Public recorder handle ────────────────────────────────────────────────────

#[derive(Clone)]
pub struct Recorder {
    ws_tx:     WsTx,
    state:     SharedState,
    recording: Arc<AtomicBool>,
    paused:    Arc<AtomicBool>,
    steps:     Arc<Mutex<Vec<Step>>>,
    hook_tid:  Arc<Mutex<Option<u32>>>,
}

impl Recorder {
    pub fn new(ws_tx: WsTx, state: SharedState) -> Self {
        Self {
            ws_tx,
            state,
            recording: Arc::new(AtomicBool::new(false)),
            paused:    Arc::new(AtomicBool::new(false)),
            steps:     Arc::new(Mutex::new(Vec::new())),
            hook_tid:  Arc::new(Mutex::new(None)),
        }
    }

    /// Spawn the hook thread. Returns immediately.
    pub fn start(&self) {
        if self.recording.swap(true, Ordering::SeqCst) {
            return; // already recording
        }
        self.paused.store(false, Ordering::SeqCst);
        self.steps.lock().clear();
        {
            let mut s = self.state.lock();
            s.status = AppStatus::Recording;
            s.steps.clear();
        }
        emit_empty(&self.ws_tx, "recording_started");
        hud::show_recording(0);

        // After 1.5 s the user will have switched to their working app.
        // Insert start context as step 0 if not already there.
        let ctx_ws   = self.ws_tx.clone();
        let ctx_steps = self.steps.clone();
        let ctx_recording = self.recording.clone();
        std::thread::spawn(move || {
            std::thread::sleep(std::time::Duration::from_millis(1500));
            if !ctx_recording.load(Ordering::SeqCst) { return; }
            if let Some(mut start_step) = detect_start_context() {
                let mut steps = ctx_steps.lock();
                // Only insert if we don't already have a navigate/open step at position 0
                let first_is_nav = steps.first()
                    .map(|s| matches!(s.kind.as_str(), "navigate" | "open_file"))
                    .unwrap_or(false);
                if !first_is_nav {
                    start_step.id = 0;
                    // Renumber existing steps to make room
                    for s in steps.iter_mut() { s.id += 1; }
                    steps.insert(0, start_step.clone());
                    // Emit full step list so UI rebuilds
                    let all = steps.clone();
                    drop(steps);
                    for (i, s) in all.iter().enumerate() {
                        if i == 0 { emit(&ctx_ws, "step", s); }
                        else { emit(&ctx_ws, "step_update", serde_json::json!({ "index": i, "step": s })); }
                    }
                }
            }
        });

        let ws_tx    = self.ws_tx.clone();
        let state    = self.state.clone();
        let recording = self.recording.clone();
        let paused   = self.paused.clone();
        let steps    = self.steps.clone();
        let hook_tid = self.hook_tid.clone();

        std::thread::spawn(move || {
            hook_thread_main(ws_tx, state, recording, paused, steps, hook_tid);
        });
    }

    pub fn stop(&self) -> Vec<Step> {
        if !self.recording.swap(false, Ordering::SeqCst) {
            return vec![];
        }
        self.paused.store(false, Ordering::SeqCst);
        // Signal hook thread to quit
        if let Some(tid) = *self.hook_tid.lock() {
            unsafe { PostThreadMessageW(tid, WM_QUIT, WPARAM(0), LPARAM(0)); }
        }
        // Wait for hook thread AND all background screenshot/UIA threads to finish.
        // Background threads sleep 120ms + capture time; 500ms gives comfortable margin.
        std::thread::sleep(Duration::from_millis(500));

        let raw = self.steps.lock().clone();
        let cleaned = cleanup_steps(raw);
        {
            let mut s = self.state.lock();
            s.status = AppStatus::Idle;
            s.steps = cleaned.clone();
        }
        hud::hide();
        emit(&self.ws_tx, "record_stopped", serde_json::json!({ "steps": cleaned }));
        cleaned
    }

    pub fn pause(&self) {
        self.paused.store(true, Ordering::SeqCst);
        let mut s = self.state.lock();
        s.status = AppStatus::RecordingPaused;
        hud::show_rec_paused(self.steps.lock().len() as u32);
    }

    pub fn resume(&self) {
        self.paused.store(false, Ordering::SeqCst);
        let mut s = self.state.lock();
        s.status = AppStatus::Recording;
        hud::show_recording(self.steps.lock().len() as u32);
    }

    pub fn is_recording(&self) -> bool { self.recording.load(Ordering::SeqCst) }
}

// ── Hook thread ────────────────────────────────────────────────────────────────

fn hook_thread_main(
    ws_tx:    WsTx,
    state:    SharedState,
    recording: Arc<AtomicBool>,
    paused:   Arc<AtomicBool>,
    steps:    Arc<Mutex<Vec<Step>>>,
    hook_tid: Arc<Mutex<Option<u32>>>,
) {
    unsafe {
        let tid = GetCurrentThreadId();
        *hook_tid.lock() = Some(tid);

        let ctx = HookContext {
            ws_tx,
            state,
            recording,
            paused,
            steps,
            next_id:         0,
            start_time:      Instant::now(),
            // Give the user 1.5 s to switch to their target app.
            // Events during this window are silently ignored so transition
            // clicks (opening browser, clicking address bar, etc.) are not recorded.
            startup_end:     Instant::now() + Duration::from_millis(1500),
            held_mods:       Default::default(),
            pending_type_buf: String::new(),
            last_scroll_pos: (0, 0),
            last_scroll_dy:  0,
            last_lclick_pos: (-9999, -9999),
            last_lclick_time: Instant::now(),
            drag_start:      None,
            last_event_ts:   f64::INFINITY,
        };
        HOOK_CTX.with(|c| *c.borrow_mut() = Some(ctx));

        let mouse_hook = SetWindowsHookExW(WH_MOUSE_LL, Some(mouse_proc), None, 0)
            .expect("failed to set mouse hook");
        let kbd_hook = SetWindowsHookExW(WH_KEYBOARD_LL, Some(kbd_proc), None, 0)
            .expect("failed to set keyboard hook");

        let mut msg = MSG::default();
        while GetMessageW(&mut msg, None, 0, 0).as_bool() {
            // message pump keeps hooks alive
        }

        UnhookWindowsHookEx(mouse_hook).ok();
        UnhookWindowsHookEx(kbd_hook).ok();
        *hook_tid.lock() = None;
        HOOK_CTX.with(|c| *c.borrow_mut() = None);
    }
}

// ── Mouse hook ────────────────────────────────────────────────────────────────

unsafe extern "system" fn mouse_proc(code: i32, wparam: WPARAM, lparam: LPARAM) -> LRESULT {
    if code == HC_ACTION as i32 {
        let info = &*(lparam.0 as *const MSLLHOOKSTRUCT);
        let (x, y) = (info.pt.x, info.pt.y);

        HOOK_CTX.with(|c| {
            if let Some(ref mut ctx) = *c.borrow_mut() {
                if !ctx.recording.load(Ordering::SeqCst) || ctx.paused.load(Ordering::SeqCst) {
                    return;
                }
                // Ignore events during the startup window (user switching to target app)
                if Instant::now() < ctx.startup_end { return; }

                // Skip clicks on AutoFlow window itself
                let hwnd = WindowFromPoint(info.pt);
                if is_autoflow_window(hwnd) { return; }

                let ts = ctx.start_time.elapsed().as_secs_f64();

                match wparam.0 as u32 {
                    WM_LBUTTONDOWN | WM_RBUTTONDOWN | WM_MBUTTONDOWN => {
                        flush_type_step(ctx, ts);
                        let button = match wparam.0 as u32 {
                            WM_RBUTTONDOWN => "right",
                            WM_MBUTTONDOWN => "middle",
                            _              => "left",
                        };

                        // Double-click detection: two left clicks within 500ms at near-same pos
                        let is_double = wparam.0 as u32 == WM_LBUTTONDOWN && {
                            let dx = (ctx.last_lclick_pos.0 - x).abs();
                            let dy = (ctx.last_lclick_pos.1 - y).abs();
                            let dt = ctx.last_lclick_time.elapsed().as_millis();
                            dx < 8 && dy < 8 && dt < 500
                        };

                        if is_double {
                            // Upgrade last click step to double_click
                            let update = {
                                let mut steps = ctx.steps.lock();
                                let idx = steps.len().saturating_sub(1);
                                if let Some(last) = steps.last_mut() {
                                        last.kind = "double_click".into();
                                        last.kind = "double_click".into();
                                        Some((idx, last.clone()))
                                } else { None }
                            };
                            if let Some((idx, step)) = update {
                                emit(&ctx.ws_tx, "step_update", json!({ "index": idx, "step": step }));
                            }
                            ctx.last_lclick_pos = (-9999, -9999);
                            return;
                        }

                        if wparam.0 as u32 == WM_LBUTTONDOWN {
                            ctx.last_lclick_pos  = (x, y);
                            ctx.last_lclick_time = Instant::now();
                        }

                        let id = next_id(ctx);
                        let (win_title, win_rect) = get_foreground_info();
                        // NOTE: No GDI in hook callbacks — screenshots captured in background thread below.
                        let step = Step {
                            id,
                            kind: "click".into(),
                            data: json!({
                                "x": x, "y": y,
                                "button": button,
                                "window": win_title,
                                "window_rect": win_rect,
                                "screenshot": null,
                                "screenshot_region": null,
                                "screenshot_full": null,
                            }),
                            note: None,
                            disabled: false,
                            timestamp: Some(ts),
                        };
                        let step_idx = {
                            let mut sl = ctx.steps.lock();
                            sl.push(step.clone());
                            sl.len() - 1
                        };
                        hud::show_recording(ctx.steps.lock().len() as u32);
                        emit(&ctx.ws_tx, "step", &step);

                        // Background thread: screenshots + UIA element detection.
                        // Screenshots MUST be outside the hook callback (GDI forbidden in hooks).
                        let bg_ws    = ctx.ws_tx.clone();
                        let bg_steps = ctx.steps.clone();
                        // Read screenshot settings before leaving the hook callback
                        let (bg_ss_mode, bg_ss_mon) = {
                            let s = ctx.state.lock();
                            (s.settings.screenshot_mode.clone(), s.settings.screenshot_monitor)
                        };
                        std::thread::spawn(move || {
                            // Brief delay so the clicked UI fully renders before screenshot.
                            // 120ms gives the window time to repaint after the click lands.
                            std::thread::sleep(std::time::Duration::from_millis(120));

                            // Capture screenshots (safe here — outside hook callback)
                            let region = capture_region_b64(x, y);
                            let full   = capture_full_b64(&bg_ss_mode, bg_ss_mon, Some((x, y)));

                            // UIA element detection
                            let element = get_uia_element_at(x, y);

                            // Patch the step with everything we found
                            let mut steps = bg_steps.lock();
                            if step_idx < steps.len() {
                                if let Some(r) = region {
                                    steps[step_idx].data["screenshot"]        = serde_json::Value::String(r.clone());
                                    steps[step_idx].data["screenshot_region"] = serde_json::Value::String(r);
                                }
                                if let Some(f) = full {
                                    steps[step_idx].data["screenshot_full"] = serde_json::Value::String(f);
                                }
                                if let Some(el) = element {
                                    steps[step_idx].data["element"] = el;
                                }
                                let updated = steps[step_idx].clone();
                                drop(steps);
                                emit(&bg_ws, "step_update", serde_json::json!({
                                    "index": step_idx,
                                    "step":  updated,
                                }));
                            }
                        });
                    }
                    WM_MOUSEWHEEL => {
                        let delta = ((info.mouseData >> 16) as i16) as i32;
                        let dy = delta / 120; // notches
                        // Merge consecutive scrolls at same position
                        if (ctx.last_scroll_pos.0 - x).abs() < 5
                            && (ctx.last_scroll_pos.1 - y).abs() < 5
                        {
                            ctx.last_scroll_dy += dy;
                            // Update last scroll step in-place
                            let mut steps = ctx.steps.lock();
                            if let Some(last) = steps.last_mut() {
                                if last.kind == "scroll" {
                                    last.data["dy"] = json!(ctx.last_scroll_dy);
                                    return;
                                }
                            }
                        }
                        ctx.last_scroll_pos = (x, y);
                        ctx.last_scroll_dy  = dy;
                        let id = next_id(ctx);
                        let step = Step {
                            id, kind: "scroll".into(),
                            data: json!({ "x": x, "y": y, "dx": 0, "dy": dy }),
                            note: None, disabled: false, timestamp: Some(ts),
                        };
                        ctx.steps.lock().push(step.clone());
                        emit(&ctx.ws_tx, "step", &step);
                    }
                    _ => {}
                }
            }
        });
    }
    CallNextHookEx(HHOOK::default(), code, wparam, lparam)
}

// ── Keyboard hook ─────────────────────────────────────────────────────────────

// Virtual key codes for modifiers
const VK_SHIFT:   u16 = 0x10;
const VK_CONTROL: u16 = 0x11;
const VK_MENU:    u16 = 0x12;  // Alt
const VK_LWIN:    u16 = 0x5B;
const VK_RWIN:    u16 = 0x5C;

fn is_modifier(vk: u32) -> bool {
    matches!(vk, 0x10 | 0x11 | 0x12 | 0x5B | 0x5C | 0xA0..=0xA5)
}

fn vk_name(vk: u32) -> Option<&'static str> {
    Some(match vk {
        0x08 => "backspace", 0x09 => "tab",    0x0D => "enter",
        0x1B => "esc",       0x20 => "space",   0x2E => "delete",
        0x25 => "left",      0x26 => "up",      0x27 => "right", 0x28 => "down",
        0x21 => "pageup",    0x22 => "pagedown",0x23 => "end",   0x24 => "home",
        0x70 => "f1",  0x71 => "f2",  0x72 => "f3",  0x73 => "f4",
        0x74 => "f5",  0x75 => "f6",  0x76 => "f7",  0x77 => "f8",
        0x78 => "f9",  0x79 => "f10", 0x7A => "f11", 0x7B => "f12",
        0x2C => "printscreen", 0x91 => "scrolllock", 0x13 => "pause",
        0x11 => "ctrl", 0x10 => "shift", 0x12 => "alt",
        0x5B | 0x5C => "win",
        _ => return None,
    })
}

unsafe extern "system" fn kbd_proc(code: i32, wparam: WPARAM, lparam: LPARAM) -> LRESULT {
    if code == HC_ACTION as i32 {
        let info = &*(lparam.0 as *const KBDLLHOOKSTRUCT);
        let vk = info.vkCode;

        HOOK_CTX.with(|c| {
            if let Some(ref mut ctx) = *c.borrow_mut() {
                if !ctx.recording.load(Ordering::SeqCst) || ctx.paused.load(Ordering::SeqCst) {
                    return;
                }
                if Instant::now() < ctx.startup_end { return; }
                let ts = ctx.start_time.elapsed().as_secs_f64();
                let is_down = wparam.0 as u32 == WM_KEYDOWN || wparam.0 as u32 == WM_SYSKEYDOWN;
                if !is_down { return; }

                let ctrl_held  = GetKeyState(VK_CONTROL as i32) as u16 & 0x8000 != 0;
                let alt_held   = GetKeyState(VK_MENU as i32)    as u16 & 0x8000 != 0;
                let shift_held = GetKeyState(VK_SHIFT as i32)   as u16 & 0x8000 != 0;
                let win_held   = (GetKeyState(VK_LWIN as i32) | GetKeyState(VK_RWIN as i32)) as u16 & 0x8000 != 0;

                let has_modifier = ctrl_held || alt_held || win_held;

                if is_modifier(vk) { return; } // don't record standalone modifier press

                if has_modifier || vk_name(vk).is_some() {
                    // Flush any accumulated typing first
                    flush_type_step(ctx, ts);

                    // Build combo string
                    let mut parts = Vec::new();
                    if win_held   { parts.push("win"); }
                    if ctrl_held  { parts.push("ctrl"); }
                    if alt_held   { parts.push("alt"); }
                    if shift_held && has_modifier { parts.push("shift"); }
                    if let Some(name) = vk_name(vk) {
                        parts.push(name);
                    } else {
                        // printable key with modifier
                        if let Some(ch) = vk_to_char(vk) {
                            let s = ch.to_lowercase().to_string();
                            // leak OK for short-lived static-ish strings; use a thread-local buffer instead
                            parts.push(Box::leak(s.into_boxed_str()));
                        }
                    }
                    if parts.len() >= 2 || (parts.len() == 1 && vk_name(vk).is_some() && !is_printable_vk(vk)) {
                        let combo = parts.join("+");
                        let id = next_id(ctx);
                        let step = Step {
                            id, kind: "hotkey".into(),
                            data: json!({ "combo": combo }),
                            note: None, disabled: false, timestamp: Some(ts),
                        };
                        ctx.steps.lock().push(step.clone());
                        emit(&ctx.ws_tx, "step", &step);
                    }
                } else if is_printable_vk(vk) {
                    // Accumulate printable chars into a type step
                    if let Some(ch) = vk_to_char(vk) {
                        let upper = GetKeyState(VK_SHIFT as i32) as u16 & 0x8000 != 0;
                        let c = if upper { ch.to_uppercase().next().unwrap_or(ch) } else { ch };
                        ctx.pending_type_buf.push(c);
                    }
                } else {
                    // Special key without modifier (enter, backspace, etc.)
                    flush_type_step(ctx, ts);
                    if let Some(name) = vk_name(vk) {
                        let id = next_id(ctx);
                        let step = Step {
                            id, kind: "hotkey".into(),
                            data: json!({ "combo": name }),
                            note: None, disabled: false, timestamp: Some(ts),
                        };
                        ctx.steps.lock().push(step.clone());
                        emit(&ctx.ws_tx, "step", &step);
                    }
                }
            }
        });
    }
    CallNextHookEx(HHOOK::default(), code, wparam, lparam)
}

fn is_printable_vk(vk: u32) -> bool {
    (0x30..=0x5A).contains(&vk) // 0-9, A-Z
    || (0x60..=0x6F).contains(&vk) // numpad
    || (0xBA..=0xC0).contains(&vk) // ; = , - . / `
    || (0xDB..=0xDE).contains(&vk) // [ \ ] '
}

fn vk_to_char(vk: u32) -> Option<char> {
    if (0x30..=0x39).contains(&vk) { // '0'-'9'
        return char::from_u32(vk);
    }
    if (0x41..=0x5A).contains(&vk) { // 'A'-'Z' -> lowercase
        return char::from_u32(vk + 32);
    }
    let c = match vk {
        0xBA => ';', 0xBB => '=', 0xBC => ',', 0xBD => '-',
        0xBE => '.', 0xBF => '/', 0xC0 => '`', 0xDB => '[',
        0xDC => '\\', 0xDD => ']', 0xDE => '\'',
        _ => return None,
    };
    Some(c)
}

fn flush_type_step(ctx: &mut HookContext, ts: f64) {
    let text = std::mem::take(&mut ctx.pending_type_buf);
    if text.is_empty() { return; }
    let id = next_id(ctx);
    let step = Step {
        id, kind: "type".into(),
        data: json!({ "text": text }),
        note: None, disabled: false, timestamp: Some(ts),
    };
    ctx.steps.lock().push(step.clone());
    emit(&ctx.ws_tx, "step", &step);
}

fn next_id(ctx: &mut HookContext) -> u32 {
    let id = ctx.next_id;
    ctx.next_id += 1;
    id
}

/// Insert an auto-wait step when there's a significant gap between recorded events.
/// Gaps under 1.5 s are normal UI interaction speed; gaps over 45 s are probably
/// the user walking away — we cap at 45 000 ms to avoid absurd wait steps.
fn auto_wait_step(ctx: &mut HookContext, current_ts: f64) {
    const MIN_MS: u64 = 1500;
    const MAX_MS: u64 = 45_000;
    if ctx.last_event_ts.is_infinite() { return; } // first event, no wait
    let gap_ms = ((current_ts - ctx.last_event_ts) * 1000.0).round() as u64;
    if gap_ms < MIN_MS || gap_ms > MAX_MS { return; }
    let id = next_id(ctx);
    let step = crate::state::Step {
        id,
        kind: "wait".into(),
        data: serde_json::json!({ "ms": gap_ms }),
        note: Some(format!("Auto-detected {}s pause", gap_ms / 1000)),
        disabled: false,
        timestamp: Some(current_ts - (gap_ms as f64 / 1000.0)),
    };
    ctx.steps.lock().push(step.clone());
    emit(&ctx.ws_tx, "step", &step);
}

// ── Win32 helpers ─────────────────────────────────────────────────────────────

fn is_autoflow_window(hwnd: HWND) -> bool {
    if hwnd.is_invalid() { return false; }
    // Class name is safe from a LL hook (kernel atom table, no cross-thread message).
    // GetWindowTextW is NOT safe here — it sends WM_GETTEXT which blocks if the
    // target thread is slow, causing Windows to forcibly remove the hook.
    unsafe {
        let mut buf = [0u16; 64];
        let len = windows::Win32::UI::WindowsAndMessaging::GetClassNameW(hwnd, &mut buf);
        len > 0 && String::from_utf16_lossy(&buf[..len as usize])
                       .eq_ignore_ascii_case("AutoFlowHUD")
    }
}

fn get_window_title(hwnd: HWND) -> String {
    unsafe {
        let mut buf = [0u16; 512];
        let len = GetWindowTextW(hwnd, &mut buf);
        String::from_utf16_lossy(&buf[..len as usize])
    }
}

fn get_foreground_info() -> (String, Value) {
    unsafe {
        let hwnd = GetForegroundWindow();
        if hwnd.is_invalid() { return (String::new(), Value::Null); }
        let title = get_window_title(hwnd);
        let mut rect = windows::Win32::Foundation::RECT::default();
        windows::Win32::UI::WindowsAndMessaging::GetWindowRect(hwnd, &mut rect).ok();
        let r = json!({ "left": rect.left, "top": rect.top, "width": rect.right - rect.left, "height": rect.bottom - rect.top });
        (title, r)
    }
}

unsafe fn WindowFromPoint(pt: POINT) -> HWND {
    windows::Win32::UI::WindowsAndMessaging::WindowFromPoint(pt)
}

// ── Screenshot capture ────────────────────────────────────────────────────────

/// Capture a rectangular region of the virtual desktop as RGB pixels.
/// ox/oy are screen-space coordinates (can be negative for left/above monitors).
fn grab_region_rgb(ox: i32, oy: i32, w: i32, h: i32) -> Option<Vec<u8>> {
    unsafe {
        if w <= 0 || h <= 0 { return None; }
        let screen_dc = GetDC(None);
        let mem_dc    = CreateCompatibleDC(screen_dc);
        let bmp       = CreateCompatibleBitmap(screen_dc, w, h);
        let old       = SelectObject(mem_dc, bmp);
        BitBlt(mem_dc, 0, 0, w, h, screen_dc, ox, oy, SRCCOPY).ok();
        SelectObject(mem_dc, old);

        let mut bi = BITMAPINFO {
            bmiHeader: BITMAPINFOHEADER {
                biSize:        std::mem::size_of::<BITMAPINFOHEADER>() as u32,
                biWidth:       w,
                biHeight:      -h,
                biPlanes:      1,
                biBitCount:    32,
                biCompression: BI_RGB.0,
                ..Default::default()
            },
            ..Default::default()
        };
        let mut pixels = vec![0u8; (w * h * 4) as usize];
        GetDIBits(mem_dc, bmp, 0, h as u32, Some(pixels.as_mut_ptr() as _), &mut bi, DIB_RGB_COLORS);
        let _ = DeleteObject(bmp);
        let _ = DeleteDC(mem_dc);
        ReleaseDC(None, screen_dc);

        // BGRA → RGB
        let mut rgb = vec![0u8; (w * h * 3) as usize];
        for i in 0..(w * h) as usize {
            rgb[i*3]   = pixels[i*4+2];
            rgb[i*3+1] = pixels[i*4+1];
            rgb[i*3+2] = pixels[i*4];
        }
        Some(rgb)
    }
}

/// Virtual desktop bounding rect (covers all monitors, origin can be negative).
fn virtual_screen_rect() -> RECT {
    use windows::Win32::UI::WindowsAndMessaging::{
        GetSystemMetrics, SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN,
        SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN,
    };
    unsafe {
        let x = GetSystemMetrics(SM_XVIRTUALSCREEN);
        let y = GetSystemMetrics(SM_YVIRTUALSCREEN);
        let w = GetSystemMetrics(SM_CXVIRTUALSCREEN);
        let h = GetSystemMetrics(SM_CYVIRTUALSCREEN);
        RECT { left: x, top: y, right: x + w, bottom: y + h }
    }
}

/// Monitor rect (screen-space) for the monitor containing point (px, py).
fn monitor_rect_at(px: i32, py: i32) -> RECT {
    use windows::Win32::{
        Foundation::POINT as WIN_POINT,
        Graphics::Gdi::{GetMonitorInfoW, MONITORINFO, MONITOR_DEFAULTTONEAREST, MonitorFromPoint},
    };
    unsafe {
        let pt = WIN_POINT { x: px, y: py };
        let hmon = MonitorFromPoint(pt, MONITOR_DEFAULTTONEAREST);
        let mut info = MONITORINFO {
            cbSize: std::mem::size_of::<MONITORINFO>() as u32,
            ..Default::default()
        };
        GetMonitorInfoW(hmon, &mut info);
        if info.rcMonitor.right > info.rcMonitor.left {
            info.rcMonitor
        } else {
            virtual_screen_rect() // fallback
        }
    }
}

/// Enumerate all monitor rects (screen-space), in EnumDisplayMonitors order.
fn all_monitor_rects() -> Vec<RECT> {
    use windows::Win32::{
        Foundation::{BOOL, LPARAM, TRUE},
        Graphics::Gdi::{
            EnumDisplayMonitors, GetMonitorInfoW, HDC, HMONITOR, MONITORINFO,
        },
    };
    let mut monitors: Vec<RECT> = Vec::new();
    unsafe {
        unsafe extern "system" fn cb(
            hmon: HMONITOR, _: HDC, _: *mut RECT, data: LPARAM,
        ) -> BOOL {
            let list = &mut *(data.0 as *mut Vec<RECT>);
            let mut info = MONITORINFO {
                cbSize: std::mem::size_of::<MONITORINFO>() as u32,
                ..Default::default()
            };
            if GetMonitorInfoW(hmon, &mut info).as_bool() {
                list.push(info.rcMonitor);
            }
            TRUE
        }
        EnumDisplayMonitors(HDC::default(), None, Some(cb),
            LPARAM(&mut monitors as *mut Vec<RECT> as isize));
    }
    monitors
}

/// Capture the full screenshot according to mode/monitor settings.
/// - "auto"   → the monitor containing (cx, cy)
/// - "all"    → full virtual desktop
/// - "manual" → monitor at index `monitor_idx`
pub fn capture_full_b64(mode: &str, monitor_idx: i32, highlight: Option<(i32, i32)>) -> Option<String> {
    let rect = match mode {
        "all" => virtual_screen_rect(),
        "manual" => {
            let rects = all_monitor_rects();
            let idx = (monitor_idx.max(0) as usize).min(rects.len().saturating_sub(1));
            rects.into_iter().nth(idx).unwrap_or_else(virtual_screen_rect)
        }
        _ => { // "auto" — monitor where the action happened
            if let Some((cx, cy)) = highlight {
                monitor_rect_at(cx, cy)
            } else {
                virtual_screen_rect()
            }
        }
    };

    let (mx, my) = (rect.left, rect.top);
    let (mw, mh) = (rect.right - rect.left, rect.bottom - rect.top);
    let mut rgb = grab_region_rgb(mx, my, mw, mh)?;

    // Draw crosshair at position relative to captured rect
    if let Some((cx, cy)) = highlight {
        draw_crosshair(&mut rgb, mw, mh, cx - mx, cy - my);
    }

    let img = image::ImageBuffer::<image::Rgb<u8>, _>::from_raw(mw as u32, mh as u32, rgb)?;
    let thumb = image::imageops::resize(
        &img, (mw / 2) as u32, (mh / 2) as u32,
        image::imageops::FilterType::Nearest,
    );
    encode_jpeg(&thumb)
}

/// Capture a 420x260 region centred on (cx, cy) with a target circle annotation.
pub fn capture_region_b64(cx: i32, cy: i32) -> Option<String> {
    let rw = 420i32;
    let rh = 260i32;
    let vr = virtual_screen_rect();
    let l = (cx - rw / 2).max(vr.left);
    let t = (cy - rh / 2).max(vr.top);
    let r = (cx + rw / 2).min(vr.right);
    let b = (cy + rh / 2).min(vr.bottom);
    let aw = r - l;
    let ah = b - t;
    if aw <= 0 || ah <= 0 { return None; }
    let rgb = grab_region_rgb(l, t, aw, ah)?;
    let mut img = image::ImageBuffer::<image::Rgb<u8>, _>::from_raw(aw as u32, ah as u32, rgb)?;
    // Click point in region coordinates
    let rx = (cx - l).clamp(0, aw - 1) as u32;
    let ry = (cy - t).clamp(0, ah - 1) as u32;
    draw_click_target(&mut img, rx, ry);
    encode_jpeg(&img)
}

/// Draw a target/crosshair annotation at (px, py) in an RGB image.
/// White outer ring + red inner ring + short crosshair arms.
fn draw_click_target(
    img: &mut image::ImageBuffer<image::Rgb<u8>, Vec<u8>>,
    px: u32, py: u32,
) {
    let (w, h) = img.dimensions();
    let cx = px as i32;
    let cy = py as i32;

    // Helper to set a pixel with bounds check
    let set = |img: &mut image::ImageBuffer<image::Rgb<u8>, Vec<u8>>, x: i32, y: i32, r: u8, g: u8, b: u8| {
        if x >= 0 && y >= 0 && (x as u32) < w && (y as u32) < h {
            img.put_pixel(x as u32, y as u32, image::Rgb([r, g, b]));
        }
    };

    // Draw concentric rings: white (r=18) then red (r=14)
    for (radius, r, g, b) in &[(18i32, 255u8, 255u8, 255u8), (14i32, 230u8, 50u8, 50u8)] {
        let mut x = *radius;
        let mut y = 0i32;
        let mut err = 0i32;
        while x >= y {
            for &(dx, dy) in &[(x,y),(y,x),(-y,x),(-x,y),(-x,-y),(-y,-x),(y,-x),(x,-y)] {
                set(img, cx+dx, cy+dy, *r, *g, *b);
                // 2px thick
                set(img, cx+dx+1, cy+dy, *r, *g, *b);
                set(img, cx+dx, cy+dy+1, *r, *g, *b);
            }
            y += 1;
            err += 1 + 2*y;
            if 2*(err-x)+1 > 0 { x -= 1; err += 1-2*x; }
        }
    }

    // Crosshair arms (gap=4 around center, length=18)
    let gap = 5i32;
    let arm = 20i32;
    for t in -1i32..=1 {
        for i in gap..arm {
            set(img, cx+i,  cy+t, 255, 255, 255); // right white
            set(img, cx-i,  cy+t, 255, 255, 255); // left white
            set(img, cx+t,  cy+i, 255, 255, 255); // down white
            set(img, cx+t,  cy-i, 255, 255, 255); // up white
        }
    }
    for i in (gap+1)..(arm-1) {
        set(img, cx+i, cy, 230, 50, 50); // right red center
        set(img, cx-i, cy, 230, 50, 50);
        set(img, cx, cy+i, 230, 50, 50);
        set(img, cx, cy-i, 230, 50, 50);
    }
}

/// Legacy wrapper (used by playback screenshot steps).
pub fn capture_screenshot_b64(_region_only: bool) -> Option<String> {
    capture_full_b64("auto", 0, None)
}

fn encode_jpeg<C: std::ops::Deref<Target=[u8]>>(
    img: &image::ImageBuffer<image::Rgb<u8>, C>
) -> Option<String> {
    let mut buf = std::io::Cursor::new(Vec::new());
    img.write_to(&mut buf, image::ImageFormat::Jpeg).ok()?;
    Some(B64.encode(buf.into_inner()))
}

/// Draw a red crosshair + circle at (cx, cy) in an RGB pixel buffer.
fn draw_crosshair(rgb: &mut Vec<u8>, w: i32, h: i32, cx: i32, cy: i32) {
    let r = 22i32;          // circle radius
    let arm = 38i32;        // crosshair arm length
    let thickness = 2i32;   // line/arc thickness

    let set_px = |rgb: &mut Vec<u8>, x: i32, y: i32| {
        if x >= 0 && x < w && y >= 0 && y < h {
            let idx = (y * w + x) as usize * 3;
            rgb[idx] = 255; rgb[idx+1] = 59; rgb[idx+2] = 48; // #ff3b30
        }
    };

    // Circle arc (using midpoint algorithm)
    for t_deg in 0..360 {
        let angle = t_deg as f32 * std::f32::consts::PI / 180.0;
        for dr in -thickness..=thickness {
            let rad = (r + dr) as f32;
            let px = cx + (rad * angle.cos()).round() as i32;
            let py = cy + (rad * angle.sin()).round() as i32;
            set_px(rgb, px, py);
        }
    }

    // Crosshair arms (outside the circle, with gap)
    let gap = 5i32;
    for t in -thickness..=thickness {
        for i in r+gap..r+arm {
            set_px(rgb, cx + i, cy + t);  // right
            set_px(rgb, cx - i, cy + t);  // left
            set_px(rgb, cx + t, cy + i);  // down
            set_px(rgb, cx + t, cy - i);  // up
        }
    }
}

// ── Post-recording cleanup ────────────────────────────────────────────────────

fn cleanup_steps(steps: Vec<Step>) -> Vec<Step> {
    if steps.is_empty() { return steps; }

    let mut out: Vec<Step> = Vec::with_capacity(steps.len());
    for step in steps {
        // Merge consecutive scroll steps at same position
        if step.kind == "scroll" {
            if let Some(last) = out.last_mut() {
                if last.kind == "scroll"
                    && last.data["x"] == step.data["x"]
                    && last.data["y"] == step.data["y"]
                {
                    let new_dy = last.data["dy"].as_i64().unwrap_or(0)
                        + step.data["dy"].as_i64().unwrap_or(0);
                    last.data["dy"] = json!(new_dy);
                    continue;
                }
            }
        }
        // Merge consecutive type steps
        if step.kind == "type" {
            if let Some(last) = out.last_mut() {
                if last.kind == "type" {
                    let combined = format!("{}{}",
                        last.data["text"].as_str().unwrap_or(""),
                        step.data["text"].as_str().unwrap_or(""));
                    last.data["text"] = json!(combined);
                    continue;
                }
            }
        }
        out.push(step);
    }
    out
}

/// Detect the current foreground context when recording begins.
/// Called from a background thread ~1.5s after record starts so the user
/// has had time to switch back to their working app.
/// Retries up to 4 times (400 ms apart) to skip "New Tab" / blank pages.
pub fn detect_start_context() -> Option<Step> {
    for attempt in 0..4u32 {
        if attempt > 0 {
            std::thread::sleep(std::time::Duration::from_millis(400));
        }

        let (title, _rect) = get_foreground_info();
        if title.is_empty() { continue; }
        let lower = title.to_lowercase();

        // Skip AutoFlow itself
        if lower.contains("autoflow") { continue; }

        let browser_suffixes = [
            " - google chrome", " - microsoft edge", " - mozilla firefox",
            " - brave", " - opera", " - internet explorer",
        ];
        let is_browser = browser_suffixes.iter().any(|s| lower.ends_with(s));

        // Skip blank browser tabs before the user has navigated anywhere
        let is_blank_tab =
            lower == "new tab"
            || lower.starts_with("new tab - ")
            || lower.ends_with("- new tab")
            || lower == "about:blank"
            || lower == "about:newtab";

        if is_browser {
            // Get the current URL from the address bar via UIA
            let url = unsafe {
                use windows::Win32::UI::WindowsAndMessaging::GetForegroundWindow;
                let hwnd = GetForegroundWindow();
                get_browser_url(hwnd).unwrap_or_default()
            };

            // Skip internal pages (new tab, settings, etc.) — retry for real URL
            let is_internal =
                url.is_empty()
                || url.starts_with("chrome://")
                || url.starts_with("edge://")
                || url.starts_with("about:")
                || url.starts_with("chrome-search://");

            if is_blank_tab || is_internal { continue; }

            let display_url = if url.is_empty() {
                let parts: Vec<&str> = title.rsplitn(2, " - ").collect();
                if parts.len() == 2 { parts[1].to_string() } else { String::new() }
            } else {
                url.clone()
            };

            return Some(Step {
                id: 0,
                kind: "navigate".into(),
                data: serde_json::json!({
                    "url": url,
                    "window": title,
                }),
                note: Some(if display_url.is_empty() {
                    format!("Start: {} (browser)", title)
                } else {
                    format!("Start: {}", display_url)
                }),
                disabled: false,
                timestamp: Some(0.0),
            });
        }

        if is_blank_tab { continue; }

        // Regular application
        let exe = get_foreground_exe().unwrap_or_default();
        return Some(Step {
            id: 0,
            kind: "open_file".into(),
            data: serde_json::json!({
                "path": exe,
                "window": title,
            }),
            note: Some(format!("Start: {}", title)),
            disabled: false,
            timestamp: Some(0.0),
        });
    }

    None // all retries exhausted
}
/// Get the executable path of the foreground window's process.

/// Capture a screenshot and save it to disk (for the screenshot step type).
pub fn capture_screenshot_to_file(path: &str) -> anyhow::Result<()> {
    use std::io::Cursor;
    let b64 = capture_screenshot_b64(false)
        .ok_or_else(|| anyhow::anyhow!("screenshot capture failed"))?;
    // b64 is already the JPEG — decode and write raw bytes
    use base64::{engine::general_purpose::STANDARD as B64, Engine};
    let bytes = B64.decode(&b64)?;
    std::fs::write(path, bytes)?;
    Ok(())
}

// ── UIA element + browser URL detection ──────────────────────────────────────

/// Get the UI element at screen coords using IUIAutomation (runs in a background thread).
/// STA required for UIA — COINIT_APARTMENTTHREADED is used.
pub fn get_uia_element_at(x: i32, y: i32) -> Option<serde_json::Value> {
    use windows::Win32::{
        System::Com::{CoCreateInstance, CoInitializeEx, CLSCTX_INPROC_SERVER, COINIT_APARTMENTTHREADED},
        UI::Accessibility::{CUIAutomation, IUIAutomation, IUIAutomationElement},
        Foundation::POINT,
    };
    unsafe {
        let _ = CoInitializeEx(None, COINIT_APARTMENTTHREADED);
        let uia: IUIAutomation = CoCreateInstance(&CUIAutomation, None, CLSCTX_INPROC_SERVER).ok()?;
        let elem: IUIAutomationElement = uia.ElementFromPoint(POINT { x, y }).ok()?;

        let name  = elem.CurrentName().ok().map(|s| s.to_string()).unwrap_or_default();
        let class = elem.CurrentClassName().ok().map(|s| s.to_string()).unwrap_or_default();
        let ctrl  = elem.CurrentControlType().ok().map(|v| v.0).unwrap_or(0);
        let aid   = elem.CurrentAutomationId().ok().map(|s| s.to_string()).unwrap_or_default();
        let help  = elem.CurrentHelpText().ok().map(|s| s.to_string()).unwrap_or_default();

        // Resolution order: accessible name → help text → automationId fallback → child walk
        let best_name = if !name.is_empty() {
            name
        } else if !help.is_empty() {
            help.clone()
        } else if !aid.is_empty() && aid.len() < 80 {
            // automationId is often a developer string like "omnibox" or "submit-btn"
            aid.clone()
        } else {
            // Walk children (depth ≤ 4) to find a named descendant that contains the point
            walk_for_name(&uia, &elem, x, y, 0).unwrap_or_default()
        };

        // Also walk UP to find the nearest named ancestor (gives window/panel context)
        let window = uia_ancestor_window_name(&uia, &elem).unwrap_or_default();

        let mut obj = serde_json::json!({
            "name":   best_name,
            "class":  class,
            "type":   ctrl_type_name(ctrl),
            "source": "uia",
        });
        // Include automationId if it provides info beyond the name
        if !aid.is_empty() {
            obj["aid"] = serde_json::Value::String(aid);
        }
        if !window.is_empty() {
            obj["window"] = serde_json::Value::String(window);
        }
        Some(obj)
    }
}

/// Walk up the element tree to find the nearest ancestor that is a Window or Pane
/// with a non-empty name.  Returns the window title / pane label for context.
unsafe fn uia_ancestor_window_name(
    uia: &windows::Win32::UI::Accessibility::IUIAutomation,
    elem: &windows::Win32::UI::Accessibility::IUIAutomationElement,
) -> Option<String> {
    use windows::Win32::UI::Accessibility::TreeScope_Parent;
    // Walk up to 6 levels looking for a named Window/Pane/Document
    let walker = uia.ControlViewWalker().ok()?;
    let mut cur = elem.clone();
    for _ in 0..6 {
        let parent = walker.GetParentElement(&cur).ok()?;
        let ctrl = parent.CurrentControlType().ok().map(|v| v.0).unwrap_or(0);
        // 50033=Window, 50034=Pane, 50031=Document
        if matches!(ctrl, 50033 | 50034 | 50031) {
            let n = parent.CurrentName().ok().map(|s| s.to_string()).unwrap_or_default();
            if !n.is_empty() { return Some(n); }
        }
        cur = parent;
    }
    None
}

unsafe fn walk_for_name(
    uia: &windows::Win32::UI::Accessibility::IUIAutomation,
    elem: &windows::Win32::UI::Accessibility::IUIAutomationElement,
    x: i32, y: i32, depth: u32
) -> Option<String> {
    use windows::Win32::UI::Accessibility::{TreeScope_Children, IUIAutomationElementArray};
    if depth > 4 { return None; }
    let true_cond = uia.CreateTrueCondition().ok()?;
    let arr: IUIAutomationElementArray = elem.FindAll(TreeScope_Children, &true_cond).ok()?;
    let n = arr.Length().ok()? as i32;
    for i in 0..n {
        if let Ok(child) = arr.GetElement(i) {
            if let Ok(r) = child.CurrentBoundingRectangle() {
                if x >= r.left && x <= r.right && y >= r.top && y <= r.bottom {
                    let name = child.CurrentName().ok().map(|s| s.to_string()).unwrap_or_default();
                    if !name.is_empty() { return Some(name); }
                    return walk_for_name(uia, &child, x, y, depth + 1);
                }
            }
        }
    }
    None
}

fn ctrl_type_name(id: i32) -> &'static str {
    match id {
        50000 => "Button",    50001 => "Calendar",  50002 => "CheckBox",
        50003 => "ComboBox",  50004 => "Edit",       50005 => "Hyperlink",
        50006 => "Image",     50007 => "ListItem",   50008 => "List",
        50009 => "Menu",      50010 => "MenuBar",    50011 => "MenuItem",
        50012 => "ProgressBar",50013=> "RadioButton",50014 => "ScrollBar",
        50015 => "Slider",    50016 => "Spinner",    50017 => "StatusBar",
        50018 => "Tab",       50019 => "TabItem",    50020 => "Text",
        50021 => "ToolBar",   50022 => "ToolTip",    50023 => "Tree",
        50024 => "TreeItem",  50025 => "Custom",     50026 => "Group",
        50028 => "Thumb",     50029 => "DataGrid",   50030 => "DataItem",
        50031 => "Document",  50032 => "SplitButton",50033 => "Window",
        50034 => "Pane",      50035 => "Header",     50036 => "HeaderItem",
        50037 => "Table",     50038 => "TitleBar",   50039 => "Separator",
        _ => "Unknown",
    }
}

/// Get browser URL by finding the address bar via Win32 child window enumeration.
/// Works for Chrome, Edge, Firefox, Brave without COM patterns.
/// Get browser URL from the address bar — tries Win32 child window then UIA.
pub fn get_browser_url(hwnd: windows::Win32::Foundation::HWND) -> Option<String> {
    use windows::Win32::{
        Foundation::{HWND, LPARAM, BOOL},
        UI::WindowsAndMessaging::{EnumChildWindows, GetClassNameW, GetWindowTextW, IsWindowVisible},
    };

    struct Search { url: Option<String> }

    unsafe extern "system" fn cb(hwnd: HWND, lp: LPARAM) -> BOOL {
        let s = &mut *(lp.0 as *mut Search);
        if s.url.is_some() { return BOOL(0); }
        if !IsWindowVisible(hwnd).as_bool() { return BOOL(1); }
        let mut cls = [0u16; 256];
        let cl = GetClassNameW(hwnd, &mut cls);
        let class = String::from_utf16_lossy(&cls[..cl as usize]);
        let clc = class.to_lowercase();
        if clc.contains("omnibox") || clc.contains("autocomplete") || clc.contains("urlbar") {
            let mut buf = [0u16; 2048];
            let n = GetWindowTextW(hwnd, &mut buf);
            if n > 0 {
                let t = String::from_utf16_lossy(&buf[..n as usize]).trim().to_string();
                if t.starts_with("http") || t.starts_with("localhost") || t.starts_with("file://")
                    || (t.contains('.') && !t.contains(' ') && t.len() > 4)
                {
                    s.url = Some(t);
                    return BOOL(0);
                }
            }
        }
        BOOL(1)
    }

    let mut s = Search { url: None };
    unsafe { let _ = EnumChildWindows(hwnd, Some(cb), LPARAM(&mut s as *mut Search as isize)); }

    // UIA fallback
    if s.url.is_none() { s.url = get_browser_url_uia(hwnd); }
    s.url
}

fn get_browser_url_uia(hwnd: windows::Win32::Foundation::HWND) -> Option<String> {
    use windows::Win32::System::Com::{CoCreateInstance, CoInitializeEx, CLSCTX_INPROC_SERVER, COINIT_APARTMENTTHREADED};
    use windows::Win32::UI::Accessibility::{CUIAutomation, IUIAutomation, IUIAutomationElement, TreeScope_Descendants, UIA_EditControlTypeId};
    unsafe {
        let _ = CoInitializeEx(None, COINIT_APARTMENTTHREADED);
        let uia: IUIAutomation = CoCreateInstance(&CUIAutomation, None, CLSCTX_INPROC_SERVER).ok()?;
        let root: IUIAutomationElement = uia.ElementFromHandle(hwnd).ok()?;
        let cond = uia.CreateTrueCondition().ok()?;
        let all = root.FindAll(TreeScope_Descendants, &cond).ok()?;
        let n = all.Length().ok()? as i32;
        for i in 0..n {
            if let Ok(e) = all.GetElement(i) {
                let ctrl = e.CurrentControlType().ok().map(|v| v.0).unwrap_or(0);
                if ctrl != UIA_EditControlTypeId.0 { continue; }
                let aid = e.CurrentAutomationId().ok().map(|s| s.to_string()).unwrap_or_default();
                if aid.to_lowercase().contains("omnibox") || aid.to_lowercase().contains("urlbar") {
                    let name = e.CurrentName().ok().map(|s| s.to_string()).unwrap_or_default();
                    if name.starts_with("http") || name.starts_with("localhost") {
                        return Some(name);
                    }
                }
            }
        }
        None
    }
}


/// Get the executable path of the foreground window's process.
fn get_foreground_exe() -> Option<String> {
    use windows::Win32::{
        UI::WindowsAndMessaging::{GetForegroundWindow, GetWindowThreadProcessId},
        System::Threading::{
            OpenProcess, QueryFullProcessImageNameW,
            PROCESS_QUERY_LIMITED_INFORMATION, PROCESS_NAME_FORMAT,
        },
    };
    unsafe {
        let hwnd = GetForegroundWindow();
        let mut pid = 0u32;
        GetWindowThreadProcessId(hwnd, Some(&mut pid));
        if pid == 0 { return None; }
        let h = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, false, pid).ok()?;
        let mut buf = [0u16; 1024];
        let mut len = buf.len() as u32;
        QueryFullProcessImageNameW(h, PROCESS_NAME_FORMAT(0), windows::core::PWSTR(buf.as_mut_ptr()), &mut len).ok()?;
        Some(String::from_utf16_lossy(&buf[..len as usize]))
    }
}
