//! Frameless Win32 HUD overlay.
//!
//! Key fixes:
//!   • WM_MOUSEACTIVATE → MA_NOACTIVATE prevents DefWindowProcW from eating clicks.
//!   • Drag via WM_SYSCOMMAND SC_MOVE in WM_LBUTTONDOWN — no WM_NCHITTEST override.
//!   • WM_MOUSEMOVE/WM_MOUSELEAVE for hover highlighting and IDC_HAND cursor.
//!   • Default position: primary monitor bottom-right (SPI_GETWORKAREA).
//!   • Saved position validated against real monitor bounds on load.

use once_cell::sync::OnceCell;
use std::{mem::size_of, sync::atomic::{AtomicI32, AtomicU32, Ordering}};
use windows::{
    core::w,
    Win32::{
        Foundation::{COLORREF, HWND, LPARAM, LRESULT, RECT, WPARAM},
        Graphics::Gdi::{
            BeginPaint, CreateFontW, CreateSolidBrush, DeleteObject, DrawTextW,
            EndPaint, FillRect, HGDIOBJ, InvalidateRect, PAINTSTRUCT,
            SelectObject, SetBkMode, SetTextColor,
            BACKGROUND_MODE, DT_CENTER, DT_LEFT, DT_SINGLELINE, DT_VCENTER,
        },
        UI::HiDpi::{GetDpiForSystem},
        UI::Input::KeyboardAndMouse::{RegisterHotKey, HOT_KEY_MODIFIERS},
        UI::WindowsAndMessaging::{
            CreateWindowExW, DefWindowProcW, GetClientRect,
            GetWindowRect, LoadCursorW, PostMessageW,
            RegisterClassExW, SetLayeredWindowAttributes,
            SetCursor, ShowWindow, SystemParametersInfoW,
            CS_HREDRAW, CS_VREDRAW,
            IDC_ARROW, IDC_HAND, LWA_ALPHA,
            SPI_GETWORKAREA, SYSTEM_PARAMETERS_INFO_UPDATE_FLAGS,
            SW_SHOWNOACTIVATE, WM_HOTKEY,
            WM_DESTROY, WM_EXITSIZEMOVE, WM_LBUTTONDOWN,
            WM_MOUSEMOVE, WM_MOUSEACTIVATE, WM_PAINT, WM_SETCURSOR,
            WM_SYSCOMMAND, WM_USER,
            WNDCLASSEXW, WS_EX_LAYERED, WS_EX_NOACTIVATE,
            WS_EX_TOOLWINDOW, WS_EX_TOPMOST, WS_POPUP,
        },
    },
};

// WM_MOUSELEAVE and TrackMouseEvent are not exported from Win32_UI_WindowsAndMessaging
// in windows-rs 0.58 — define them manually.
const WM_MOUSELEAVE_MSG: u32 = 0x02A3;

#[repr(C)]
struct TrackMouseEventData {
    cb_size:    u32,
    dw_flags:   u32,  // TME_LEAVE = 2
    hwnd_track: HWND,
    dw_hover:   u32,
}
#[link(name = "user32")]
extern "system" { fn TrackMouseEvent(p: *mut TrackMouseEventData) -> i32; }

unsafe fn request_mouse_leave(hwnd: HWND) {
    let mut tme = TrackMouseEventData { cb_size: 16, dw_flags: 2, hwnd_track: hwnd, dw_hover: 0 };
    TrackMouseEvent(&mut tme);
}

pub const WM_HUD_UPDATE: u32 = WM_USER + 10;
pub const WM_HUD_HIDE:   u32 = WM_USER + 11;
pub const WM_HUD_SHOW:   u32 = WM_USER + 12;

pub const HUD_IDLE:        u32 = 0;
pub const HUD_RECORDING:   u32 = 1;
pub const HUD_REC_PAUSED:  u32 = 2;
pub const HUD_PLAYING:     u32 = 3;
pub const HUD_PLAY_PAUSED: u32 = 4;

static HUD_HWND:    OnceCell<isize> = OnceCell::new();
static HUD_DPI:     AtomicU32 = AtomicU32::new(100);
static HUD_STATE:   AtomicU32 = AtomicU32::new(HUD_IDLE);
static HUD_CURRENT: AtomicU32 = AtomicU32::new(0);
static HUD_TOTAL:   AtomicU32 = AtomicU32::new(0);
static HUD_HOVER:   AtomicI32 = AtomicI32::new(-1); // -1 = none, >=0 = button index
static SAVED_X:     AtomicI32 = AtomicI32::new(i32::MIN);
static SAVED_Y:     AtomicI32 = AtomicI32::new(i32::MIN);

fn dpi_scale() -> f32 { HUD_DPI.load(Ordering::Relaxed) as f32 / 100.0 }
fn dp(n: i32) -> i32  { (n as f32 * dpi_scale()).round() as i32 }
fn hud_hwnd() -> HWND { HWND(*HUD_HWND.get().unwrap_or(&0) as *mut _) }

fn repaint() {
    let h = hud_hwnd();
    if !h.0.is_null() { unsafe { let _ = InvalidateRect(h, None, true); } }
}

fn set(state: u32, cur: u32, tot: u32) {
    HUD_STATE.store(state, Ordering::SeqCst);
    HUD_CURRENT.store(cur, Ordering::SeqCst);
    HUD_TOTAL.store(tot, Ordering::SeqCst);
    repaint();
}

// ── Public API ────────────────────────────────────────────────────────────────
pub fn show_recording(steps: u32)             { set(HUD_RECORDING,  steps, 0); }
pub fn show_rec_paused(steps: u32)            { set(HUD_REC_PAUSED, steps, 0); }
pub fn show_playing(cur: u32, tot: u32)       { set(HUD_PLAYING,    cur,   tot); }
pub fn show_play_paused(cur: u32, tot: u32)   { set(HUD_PLAY_PAUSED,cur,   tot); }
pub fn hide()                                 { set(HUD_IDLE, 0, 0); }
pub fn move_to_cursor_monitor()               {} // no-op; user drags overlay manually

// ── Position persistence ──────────────────────────────────────────────────────
fn pos_path() -> Option<std::path::PathBuf> {
    std::env::var("APPDATA").ok().map(|a|
        std::path::PathBuf::from(a).join("AutoFlow").join("hud_pos.json"))
}

/// Load saved position. Returns None if file missing, unreadable, or out-of-bounds.
fn load_saved_pos() -> Option<(i32, i32)> {
    let raw = std::fs::read_to_string(pos_path()?).ok()?;
    let v: serde_json::Value = serde_json::from_str(&raw).ok()?;
    let x = v["x"].as_i64()? as i32;
    let y = v["y"].as_i64()? as i32;
    // Sanity check: must be within a plausible screen range
    if x < -32000 || x > 32000 || y < -32000 || y > 32000 { return None; }
    Some((x, y))
}

fn save_pos(x: i32, y: i32) {
    if let Some(p) = pos_path() {
        if let Some(d) = p.parent() { let _ = std::fs::create_dir_all(d); }
        let _ = std::fs::write(p, format!("{{\"x\":{},\"y\":{}}}", x, y));
    }
}

// ── Direct command channel (record commands bypass HTTP) ─────────────────────
/// Commands sent directly from HUD wnd_proc to the recorder/player.
/// Using a channel avoids the HTTP/TCP stack, which can fail if the Tokio
/// runtime is busy or the low-level hook has stalled the message pump.
pub enum HudCmd {
    RecordStop,
    RecordPause,
    RecordResume,
    /// F9 hotkey — start recording if idle, stop if recording
    RecordToggle,
}

static HUD_CMD_TX: once_cell::sync::OnceCell<std::sync::Mutex<std::sync::mpsc::SyncSender<HudCmd>>>
    = once_cell::sync::OnceCell::new();

/// Called from main.rs before the message loop starts.
pub fn set_cmd_sender(tx: std::sync::mpsc::SyncSender<HudCmd>) {
    HUD_CMD_TX.set(std::sync::Mutex::new(tx)).ok();
}

fn api(path: &'static str) {
    // Record commands: use the direct channel (no HTTP needed)
    if let Some(mtx) = HUD_CMD_TX.get() {
        let cmd = match path {
            "/api/record/stop"   => Some(HudCmd::RecordStop),
            "/api/record/pause"  => Some(HudCmd::RecordPause),
            "/api/record/resume" => Some(HudCmd::RecordResume),
            _ => None,
        };
        if let Some(c) = cmd {
            if let Ok(tx) = mtx.lock() { let _ = tx.try_send(c); }
            return;
        }
    }

    // Play commands and fallback: HTTP fire-and-forget
    std::thread::spawn(move || {
        use std::io::{Read, Write};
        if let Ok(mut s) = std::net::TcpStream::connect_timeout(
            &"127.0.0.1:7878".parse().unwrap(),
            std::time::Duration::from_millis(800),
        ) {
            let req = format!(
                "POST {} HTTP/1.1\r\nHost: localhost\r\nContent-Length: 0\r\nConnection: close\r\n\r\n",
                path
            );
            if s.write_all(req.as_bytes()).is_ok() {
                let mut buf = [0u8; 512];
                let _ = s.read(&mut buf);
            }
        }
    });
}

// ── Button layout ─────────────────────────────────────────────────────────────
#[derive(Clone)]
struct Btn { label: &'static str, path: &'static str, danger: bool, r: RECT }

fn layout(state: u32, w: i32) -> Vec<Btn> {
    let y = dp(64); let h = dp(26); let g = dp(5);
    macro_rules! btn {
        ($label:literal, $path:literal, $danger:expr, $l:expr, $r:expr) => {
            Btn { label: $label, path: $path, danger: $danger,
                  r: RECT { left: $l, top: y, right: $r, bottom: y + h } }
        }
    }
    match state {
        HUD_IDLE => {
            let bw = (w - g * 5) / 4;
            vec![
                btn!("● Rec",  "/api/record/start", false, g,        g+bw),
                btn!("▶ Play", "/api/play",          false, g*2+bw,   g*2+bw*2),
                btn!("⏭ Step", "/api/play/step",     false, g*3+bw*2, g*3+bw*3),
                btn!("⏹ Stop", "/api/play/stop",     true,  g*4+bw*3, g*4+bw*4),
            ]
        }
        HUD_RECORDING => {
            let bw = (w - g * 3) / 2;
            vec![
                btn!("⏸ Pause", "/api/record/pause", false, g,      g+bw),
                btn!("⏹ Stop",  "/api/record/stop",  true,  g*2+bw, g*2+bw*2),
            ]
        }
        HUD_REC_PAUSED => {
            let bw = (w - g * 3) / 2;
            vec![
                btn!("▶ Resume","/api/record/resume", false, g,      g+bw),
                btn!("⏹ Stop",  "/api/record/stop",   true,  g*2+bw, g*2+bw*2),
            ]
        }
        HUD_PLAYING => {
            let bw = (w - g * 4) / 3;
            vec![
                btn!("⏸ Pause","/api/play/pause", false, g,        g+bw),
                btn!("⏭ Step", "/api/play/step",  false, g*2+bw,   g*2+bw*2),
                btn!("⏹ Stop", "/api/play/stop",  true,  g*3+bw*2, g*3+bw*3),
            ]
        }
        HUD_PLAY_PAUSED | _ => {
            let bw = (w - g * 4) / 3;
            vec![
                btn!("▶ Resume","/api/play/resume", false, g,        g+bw),
                btn!("⏭ Step",  "/api/play/step",   false, g*2+bw,   g*2+bw*2),
                btn!("⏹ Stop",  "/api/play/stop",   true,  g*3+bw*2, g*3+bw*3),
            ]
        }
    }
}

// ── Create window ─────────────────────────────────────────────────────────────
pub fn create_windows() -> anyhow::Result<()> {
    unsafe {
        let cursor = LoadCursorW(None, IDC_ARROW)?;
        let wc = WNDCLASSEXW {
            cbSize:        size_of::<WNDCLASSEXW>() as u32,
            style:         CS_HREDRAW | CS_VREDRAW,
            lpfnWndProc:   Some(wnd_proc),
            lpszClassName: w!("AutoFlowHUD"),
            hCursor:       cursor,
            ..Default::default()
        };
        RegisterClassExW(&wc);

        // DPI from system (primary monitor)
        let raw_dpi = GetDpiForSystem();
        let scale_pct = (raw_dpi * 100 / 96).max(100);
        HUD_DPI.store(scale_pct, Ordering::Relaxed);

        let (ww, wh) = (dp(320), dp(106));

        // Default: bottom-right of primary monitor work area
        let mut work = RECT::default();
        let _ = SystemParametersInfoW(SPI_GETWORKAREA, 0,
            Some(&mut work as *mut RECT as *mut _),
            SYSTEM_PARAMETERS_INFO_UPDATE_FLAGS(0));
        let default_x = work.right  - ww - dp(8);
        let default_y = work.bottom - wh - dp(8);

        // Restore saved position if valid
        let (ix, iy) = load_saved_pos().unwrap_or((default_x, default_y));

        let h = CreateWindowExW(
            WS_EX_LAYERED | WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE,
            w!("AutoFlowHUD"), w!("AutoFlow"), WS_POPUP,
            ix, iy, ww, wh, None, None, None, None,
        )?;
        let _ = SetLayeredWindowAttributes(h, COLORREF(0), 230, LWA_ALPHA);
        HUD_HWND.set(h.0 as isize).ok();
        ShowWindow(h, SW_SHOWNOACTIVATE);

        // F9 = start/stop recording from anywhere (hotkey id 1)
        // Ctrl+F9 = reserved for future step-through toggle (hotkey id 2)
        let _ = RegisterHotKey(h, 1, HOT_KEY_MODIFIERS(0), 0x78); // F9 = VK 0x78
    }
    Ok(())
}

// ── Window procedure ──────────────────────────────────────────────────────────
unsafe extern "system" fn wnd_proc(hwnd: HWND, msg: u32, wp: WPARAM, lp: LPARAM) -> LRESULT {
    match msg {

        // CRITICAL: DefWindowProcW returns MA_NOACTIVATEANDEAT (4) for WS_EX_NOACTIVATE,
        // silently eating every click before WM_LBUTTONDOWN fires.
        // MA_NOACTIVATE (3) = don't activate, but DO deliver the click.
        WM_MOUSEACTIVATE => LRESULT(3),

        WM_SETCURSOR => {
            let cursor = if HUD_HOVER.load(Ordering::Relaxed) >= 0 { IDC_HAND } else { IDC_ARROW };
            if let Ok(c) = LoadCursorW(None, cursor) { SetCursor(c); }
            LRESULT(1)
        }

        WM_MOUSEMOVE => {
            let cx = (lp.0 & 0xFFFF) as i16 as i32;
            let cy = ((lp.0 >> 16) & 0xFFFF) as i16 as i32;

            // Request WM_MOUSELEAVE when mouse exits the window
            request_mouse_leave(hwnd);

            let state = HUD_STATE.load(Ordering::SeqCst);
            let mut rc = RECT::default();
            let _ = GetClientRect(hwnd, &mut rc);

            let mut new_hover: i32 = -1;
            for (i, btn) in layout(state, rc.right).iter().enumerate() {
                if cx >= btn.r.left && cx < btn.r.right && cy >= btn.r.top && cy < btn.r.bottom {
                    new_hover = i as i32;
                    break;
                }
            }
            if HUD_HOVER.swap(new_hover, Ordering::Relaxed) != new_hover {
                let _ = InvalidateRect(hwnd, None, true);
            }
            LRESULT(0)
        }

        x if x == WM_MOUSELEAVE_MSG => {
            HUD_HOVER.store(-1, Ordering::Relaxed);
            let _ = InvalidateRect(hwnd, None, true);
            LRESULT(0)
        }

        WM_HUD_UPDATE | WM_HUD_SHOW => {
            let _ = InvalidateRect(hwnd, None, true);
            LRESULT(0)
        }
        WM_HUD_HIDE => {
            set(HUD_IDLE, 0, 0);
            LRESULT(0)
        }

        WM_EXITSIZEMOVE => {
            let mut r = RECT::default();
            let _ = GetWindowRect(hwnd, &mut r);
            if r.left != SAVED_X.load(Ordering::Relaxed) || r.top != SAVED_Y.load(Ordering::Relaxed) {
                SAVED_X.store(r.left, Ordering::Relaxed);
                SAVED_Y.store(r.top,  Ordering::Relaxed);
                save_pos(r.left, r.top);
            }
            LRESULT(0)
        }

        WM_LBUTTONDOWN => {
            let cx = (lp.0 & 0xFFFF) as i16 as i32;
            let cy = ((lp.0 >> 16) & 0xFFFF) as i16 as i32;

            // Title area = drag handle. SC_MOVE (0xF010) + HTCAPTION (2) = 0xF012
            // initiates a non-activating window drag without WM_NCHITTEST tricks.
            if cy < dp(62) {
                let _ = PostMessageW(hwnd, WM_SYSCOMMAND, WPARAM(0xF012), lp);
                return LRESULT(0);
            }

            // Button area
            let state = HUD_STATE.load(Ordering::SeqCst);
            let mut rc = RECT::default();
            let _ = GetClientRect(hwnd, &mut rc);
            for btn in layout(state, rc.right) {
                if cx >= btn.r.left && cx < btn.r.right && cy >= btn.r.top && cy < btn.r.bottom {
                    api(btn.path);
                    break;
                }
            }
            LRESULT(0)
        }

        WM_PAINT => {
            let mut ps = PAINTSTRUCT::default();
            let hdc = BeginPaint(hwnd, &mut ps);
            let mut rc = RECT::default();
            let _ = GetClientRect(hwnd, &mut rc);
            let w = rc.right;
            let state   = HUD_STATE.load(Ordering::SeqCst);
            let current = HUD_CURRENT.load(Ordering::SeqCst);
            let total   = HUD_TOTAL.load(Ordering::SeqCst);
            let hover   = HUD_HOVER.load(Ordering::Relaxed);

            // Background
            let bg = CreateSolidBrush(COLORREF(0x00_1a1f2e));
            FillRect(hdc, &rc, bg);
            DeleteObject(HGDIOBJ(bg.0 as *mut _));

            // Accent bar (colour changes with state)
            let accent = match state {
                HUD_RECORDING   => 0x00_e53e3eu32,
                HUD_REC_PAUSED  => 0x00_dd6b20u32,
                HUD_PLAYING     => 0x00_38a169u32,
                HUD_PLAY_PAUSED => 0x00_3182ceu32,
                _               => 0x00_3a4a6eu32,
            };
            let ab = CreateSolidBrush(COLORREF(accent));
            FillRect(hdc, &RECT { left:0, top:0, right:w, bottom:4 }, ab);
            DeleteObject(HGDIOBJ(ab.0 as *mut _));

            SetBkMode(hdc, BACKGROUND_MODE(1));

            // App name (tiny, in accent colour)
            let f_xs = CreateFontW(dp(11),0,0,0,700,0,0,0,0,0,0,0,0,w!("Segoe UI"));
            let old  = SelectObject(hdc, HGDIOBJ(f_xs.0 as *mut _));
            SetTextColor(hdc, COLORREF(0x00_5090c0));
            let mut r0 = RECT { left:10, top:7, right:w-10, bottom:22 };
            let mut t0: Vec<u16> = "AutoFlow  \u{2014}  drag here to move".encode_utf16().collect();
            DrawTextW(hdc, &mut t0, &mut r0, DT_SINGLELINE|DT_LEFT);

            // Status line
            let f_md = CreateFontW(dp(14),0,0,0,700,0,0,0,0,0,0,0,0,w!("Segoe UI"));
            SelectObject(hdc, HGDIOBJ(f_md.0 as *mut _));
            SetTextColor(hdc, COLORREF(0x00_e2e8f0));
            let status = match state {
                HUD_RECORDING   => format!("● Recording \u{00b7} {} step{}", current, if current==1{""} else {"s"}),
                HUD_REC_PAUSED  => format!("\u{23f8} Paused \u{00b7} {} step{}", current, if current==1{""} else {"s"}),
                HUD_PLAYING     => format!("\u{25b6} Playing \u{00b7} {}/{}", current+1, total),
                HUD_PLAY_PAUSED => format!("\u{23f8} Paused \u{00b7} step {}/{}", current+1, total),
                _               => "Ready".to_string(),
            };
            let mut r1 = RECT { left:10, top:22, right:w-10, bottom:60 };
            let mut t1: Vec<u16> = status.encode_utf16().collect();
            DrawTextW(hdc, &mut t1, &mut r1, DT_SINGLELINE|DT_LEFT);

            // Buttons with hover highlight
            let f_btn = CreateFontW(dp(11),0,0,0,700,0,0,0,0,0,0,0,0,w!("Segoe UI"));
            SelectObject(hdc, HGDIOBJ(f_btn.0 as *mut _));
            for (i, btn) in layout(state, w).iter().enumerate() {
                let hovered = hover == i as i32;
                // Danger buttons: red, hover → brighter red
                // Normal buttons: dark blue, hover → lighter blue
                let bg_color = if btn.danger {
                    if hovered { 0x00_e05555u32 } else { 0x00_b52a2au32 }
                } else {
                    if hovered { 0x00_4060a0u32 } else { 0x00_2a3560u32 }
                };
                let b = CreateSolidBrush(COLORREF(bg_color));
                FillRect(hdc, &btn.r, b);
                DeleteObject(HGDIOBJ(b.0 as *mut _));

                // Top highlight line
                let top_color = if btn.danger {
                    if hovered { 0x00_ff7070u32 } else { 0x00_d04040u32 }
                } else {
                    if hovered { 0x00_6080c0u32 } else { 0x00_405080u32 }
                };
                let brd = CreateSolidBrush(COLORREF(top_color));
                let bl = RECT { left:btn.r.left, top:btn.r.top, right:btn.r.right, bottom:btn.r.top+1 };
                FillRect(hdc, &bl, brd);
                DeleteObject(HGDIOBJ(brd.0 as *mut _));

                SetTextColor(hdc, COLORREF(0x00_e2e8f0));
                let mut r = btn.r;
                let mut t: Vec<u16> = btn.label.encode_utf16().collect();
                DrawTextW(hdc, &mut t, &mut r, DT_SINGLELINE|DT_CENTER|DT_VCENTER);
            }

            SelectObject(hdc, old);
            DeleteObject(HGDIOBJ(f_xs.0 as *mut _));
            DeleteObject(HGDIOBJ(f_md.0 as *mut _));
            DeleteObject(HGDIOBJ(f_btn.0 as *mut _));
            EndPaint(hwnd, &ps);
            LRESULT(0)
        }

        // F9 global hotkey: toggle recording start/stop
        WM_HOTKEY => {
            if wp.0 == 1 {
                if let Some(mtx) = HUD_CMD_TX.get() {
                    if let Ok(tx) = mtx.lock() {
                        let _ = tx.try_send(HudCmd::RecordToggle);
                    }
                }
            }
            LRESULT(0)
        }

        WM_DESTROY => LRESULT(0),
        _ => DefWindowProcW(hwnd, msg, wp, lp),
    }
}
