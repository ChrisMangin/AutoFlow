# Changelog

All notable changes to AutoFlow are documented here.

---

## [2.0.0] — 2026-07-23

Complete rewrite in Rust. **3.1 MB** single EXE, instant startup, no Python or runtime dependencies.

### Infrastructure
- **Rust rewrite** — axum + tokio HTTP/WebSocket server replaces Flask + Socket.IO
- `rust-embed` bundles all static assets into the EXE — no external `static/` folder
- windows-rs for all Win32, COM, and UIA calls (no ctypes, no pywin32)
- System tray via `tray-icon`; frameless HUD via raw Win32 `CreateWindowExW`
- Single-instance guard via `TcpListener::bind` — duplicate launch opens browser to existing instance

### Recording
- **F9 global hotkey** — start/stop recording from any application without switching focus
- Win32 `WH_MOUSE_LL` + `WH_KEYBOARD_LL` hooks for system-wide capture
- UI Automation (UIA/COM/STA) element detection with accessible name → help text → AutomationId → child walk fallback
- Consecutive scrolls at the same position merge automatically
- **Region screenshot thumbnails** captured per click with red target-circle annotation
- Smart new-tab detection: retries context check up to 4 times (400 ms apart) to skip blank/loading pages

### HUD Overlay
- Frameless floating Win32 window always above all apps; drag to reposition, position persisted
- Buttons communicate directly via `std::sync::mpsc` channel — no HTTP round-trip
- Hides immediately when the last browser tab's WebSocket disconnects

### Playback
- **Smart window-ready check** before every click: waits for target window to respond before acting
- Async playback engine with pause/resume/step-through
- Recursive **Run Workflow** step for chaining workflows
- Adjustable speed: 0.25×–4×; start from any step

### Editing
- **AI step naming** via local Ollama (`qwen3:8b`) — `✨ AI Names` renames all steps in one click
- **Two-click delete confirmation** — arm (✕) then confirm (red "Delete?" for 3.5s)
- Undo delete: `Ctrl+Z` restores up to 10 steps in sequence

### Export
- **Task Scheduler export** — `⏰ Schedule` produces a ZIP with Python script + Windows Task Scheduler XML
- PDF, Python script, and ZIP package exports retained from v1.0.0

---

## [1.0.0] — 2026-07-21

Initial public release (Python / Flask / PyInstaller).

### Core features
- Record mouse clicks, keystrokes, hotkeys, and scroll events across any Windows application
- Smart cleanup on recording stop: consecutive scrolls merged, navigational preamble collapsed
- Instant thumbnails — per-click screenshots; element detection runs in background
- UI Automation element detection with CDP (Chrome accessibility) and Win32 fallbacks
- Image-based click matching for browser targets without accessibility APIs

### Editing
- Card view (drag-and-drop) and compact table view
- Disable steps, undo delete, per-step notes, unsaved-change indicator
- Keyboard shortcuts: `Ctrl+R` record, `Ctrl+P` play, `Ctrl+S` save, `Esc` stop, `Ctrl+Z` undo delete

### Step types (21)
Click, Type, Hotkey, Wait, Scroll, Navigate, Loop, If/Else, Set Variable, Run Script, Comment,
Error Handler, Launch Browser, Show Message, Wait for Element/Window, Get/Set Clipboard,
Image Click, Read/Write/Copy/Move/Delete File, HTTP Request, Open File/App, Kill Process, Close Window, Play Sound, Screenshot

### Export
- PDF report, Python script, ZIP package

### Infrastructure
- Flask + Socket.IO; single-file EXE via PyInstaller
- System tray, Win32 HUD overlay, portable JSON workflow storage
