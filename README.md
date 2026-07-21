<div align="center">
  <h1>⚡ AutoFlow</h1>
  <p><strong>Desktop automation for Windows — record, edit, replay.</strong></p>
  <p>
    <a href="https://github.com/ChrisMangin/AutoFlow/releases/latest">
      <img src="https://img.shields.io/github/v/release/ChrisMangin/AutoFlow?color=4f8ef7&label=download" alt="Latest Release">
    </a>
    <img src="https://img.shields.io/badge/platform-Windows-blue" alt="Windows">
    <img src="https://img.shields.io/badge/python-3.10%2B-blue" alt="Python">
    <img src="https://img.shields.io/badge/license-MIT-green" alt="MIT License">
  </p>
</div>

---

AutoFlow lets you **record mouse and keyboard actions** across any Windows application, refine them into reliable workflows, and **play them back** hands-free. No scripting required for most tasks.

## Features

### Recording
- Captures clicks, keystrokes, hotkeys, and scrolls across any app
- Smart cleanup on stop: merges consecutive scrolls, collapses redundant UI navigation, deduplicates waits
- Per-click screenshots (thumbnail + full-screen) captured immediately so cards show what was clicked
- Phase-2 element detection via Windows UI Automation — falls back to image matching for browsers

### Editing
- Drag-and-drop reorder; card view and compact table view
- Double-click any card to edit every field
- Disable steps without deleting (skipped at playback, commented in script exports)
- Undo deleted step: `Ctrl+Z` restores up to 10 deletions
- Per-step notes — annotate steps inline for documentation and PDF export
- Unsaved-change indicator: Save button pulses when there are uncommitted edits

### Playback
- Full playback, single-step, start-from-any-step
- Adjustable speed (0.25×–4×)
- Three-level click fallback: UI element name → window-relative offset → absolute coords
- `wait_for_window` uses case-insensitive substring matching

### Step Types
| Category | Steps |
|---|---|
| Input | Click, Type, Hotkey, Scroll, Wait, Navigate |
| Flow | Loop, If/Else, Set Variable, Run Script, Comment |
| UI | Wait for Element, Wait for Window, Launch Browser, Show Message, Image Click |
| Clipboard | Get Clipboard, Set Clipboard |
| Files | Read File, Write File, Copy File, Move File, Delete File |
| System | Open File/App, Kill Process, Close Window, Play Sound |
| Network | HTTP Request |
| Misc | Screenshot, Error Handler |

### Export
- **PDF** — formatted report with screenshots and step notes; ready to use as a walkthrough or SOP
- **Python Script** — standalone `.py` using `pyautogui`; runs without AutoFlow installed
- **ZIP Package** — script + `run.bat` + `requirements.txt`; double-click to run on any Windows machine

### Other
- System tray integration (minimize to tray, tray hotkeys)
- Variables panel with `{variable_name}` substitution in any text field
- Workflows stored as portable JSON files
- Settings (screenshot capture mode) persisted in browser localStorage

---

## Quick Start

1. **Download** `AutoFlow.exe` from the [Releases](https://github.com/ChrisMangin/AutoFlow/releases) page
2. **Run** — no installation needed, no admin required
3. Click **● Record**, perform your task, click **■ Stop**
4. Review the captured steps, add notes if you like
5. Click **💾 Save**, then **▶ Play**

The app opens in your default browser. A system-tray icon lets you reopen, record, or replay without keeping the browser tab focused.

---

## Building from Source

### Prerequisites
- Python 3.10+
- [`uv`](https://github.com/astral-sh/uv) (recommended) or `pip`

### Setup

```bash
git clone https://github.com/ChrisMangin/AutoFlow.git
cd AutoFlow
uv venv .venv
.venv\Scripts\activate
uv pip install -r requirements.txt
```

### Run in dev mode

```bash
python server.py
```

### Build EXE

```bash
python -m PyInstaller AutoFlow.spec --noconfirm
# Output: dist/AutoFlow.exe
```

---

## Project Structure

```
AutoFlow/
├── server.py           # Flask + Socket.IO app — entry point
├── recorder.py         # Mouse/keyboard capture engine
├── player.py           # Step replay engine
├── overlay_native.py   # Win32 recording HUD (dim layer + tkinter status)
├── static/
│   ├── index.html      # Main app UI shell
│   ├── app.js          # Frontend state + rendering (~1100 lines)
│   ├── style.css       # Dark UI theme
│   ├── guide.html      # In-app user guide
│   └── overlay.html    # Recording HUD web view
├── workflows/          # Saved workflow JSON files (user data, gitignored)
├── dev/
│   └── test_element.py # Standalone element-detection tester
├── _legacy/
│   ├── api.py          # Old pywebview JS bridge (superseded by server.py)
│   └── main.py         # Old pywebview entry point (superseded by server.py)
├── AutoFlow.spec       # PyInstaller build spec
├── requirements.txt
└── README.md
```

---

## License

MIT — see [LICENSE](LICENSE).
