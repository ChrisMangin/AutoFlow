# Changelog

All notable changes to AutoFlow are documented here.

---

## [1.0.0] — 2026-07-21

Initial public release.

### Core features
- **Record** mouse clicks, keystrokes, hotkeys, and scroll events across any Windows application
- **Smart cleanup** on recording stop: consecutive scrolls merged, navigational preamble collapsed, duplicate waits removed
- **Instant thumbnails** — per-click screenshots captured immediately; element detection runs in background
- **UI Automation** element detection with CDP (Chrome accessibility) and win32 fallbacks
- **Image-based click matching** for browser targets without accessibility APIs

### Editing
- Card view (with drag-and-drop reorder) and compact table view
- Full edit modal for all step types
- **Disable steps** — skip without deleting; shows struck-through in exports
- **Undo delete** — `Ctrl+Z` restores last deleted step (up to 10 deep)
- **Per-step notes** — inline annotation area on every card; shows in PDF export
- **Unsaved indicator** — Save button pulses blue; title shows `●` when edits are pending
- Keyboard shortcuts: `Ctrl+R` record, `Ctrl+P` play, `Ctrl+S` save, `Esc` stop, `Ctrl+Z` undo delete

### Step types (21 total)
Click, Type, Hotkey, Wait, Scroll, Navigate, Loop, If/Else, Set Variable, Run Script, Comment,
Error Handler, Launch Browser, Show Message, Wait for Element, Wait for Window, Get/Set Clipboard,
Image Click, Read/Write/Copy/Move/Delete File, HTTP Request, Open File/App, Kill Process,
Close Window, Play Sound, Screenshot

### Playback
- Start from any step; step-through mode; adjustable speed (0.25×–4×)
- Three-level click fallback: UI element → window-relative offset → absolute coords
- `wait_for_window` uses case-insensitive substring matching
- `open_file` falls back to `cmd /c start` for App Paths–registered apps (Excel, Word, etc.)

### Export
- **PDF report** — steps, notes, screenshots; usable as a walkthrough or SOP
- **Python script** — standalone `pyautogui` script, all step types supported
- **ZIP package** — script + `run.bat` launcher + `requirements.txt`

### Infrastructure
- Flask + Socket.IO backend; single-file EXE via PyInstaller
- System tray with minimize, reopen, record-new, and replay-last shortcuts
- Win32 recording HUD: dim overlay + floating status panel that follows your cursor across monitors
- Workflows stored as portable JSON files
- Settings (screenshot capture mode) in browser localStorage — no files written
- Single-instance mutex guard
