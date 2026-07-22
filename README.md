<div align="center">

# AutoFlow
**Desktop automation for Windows - record, edit, replay.**

[![Latest Release](https://img.shields.io/github/v/release/ChrisMangin/AutoFlow?color=4f8ef7&label=download&style=flat-square)](https://github.com/ChrisMangin/AutoFlow/releases/latest)
[![Platform](https://img.shields.io/badge/platform-Windows-0078D4?style=flat-square&logo=windows)](https://github.com/ChrisMangin/AutoFlow)
[![Python](https://img.shields.io/badge/python-3.10%2B-3776AB?style=flat-square&logo=python&logoColor=white)](https://github.com/ChrisMangin/AutoFlow)
[![License](https://img.shields.io/badge/license-MIT-22c55e?style=flat-square)](LICENSE)

</div>

---

AutoFlow lets you **record mouse and keyboard actions** across any Windows application, refine them into reliable workflows, and **play them back** hands-free. No scripting required for most tasks.

> **Quick start:** Download `AutoFlow.exe` from [Releases](https://github.com/ChrisMangin/AutoFlow/releases), run it, click **Record**, do your task, click **Stop**, then **Play**.

---

## Features

### Recording
- Captures clicks, keystrokes, hotkeys, and scrolls across any app
- Smart cleanup on stop: merges consecutive scrolls, collapses redundant UI navigation, deduplicates waits
- Per-click screenshots (thumbnail + full-screen) captured immediately so cards show what was clicked
- Phase-2 element detection via Windows UI Automation - falls back to image matching for browsers

### Editing
- Drag-and-drop reorder; card view and compact table view
- Double-click any card to edit every field inline
- Disable steps without deleting - skipped at playback, commented in script exports
- Undo deleted steps with `Ctrl+Z` (up to 10 deletions)
- Per-step notes for documentation and PDF export
- Unsaved-change indicator: Save button pulses when there are uncommitted edits

### Playback
- Full playback, single-step, or start from any step
- Adjustable speed (0.25x to 4x)
- Three-level click fallback: UI element name, window-relative offset, absolute coordinates
- `wait_for_window` uses case-insensitive substring matching

### Step Types

| Category | Steps |
|----------|-------|
| **Input** | Click, Type, Hotkey, Scroll, Wait, Navigate |
| **Flow** | Loop, If/Else, Set Variable, Run Script, Comment |
| **UI** | Wait for Element, Wait for Window, Launch Browser, Show Message, Image Click |
| **Clipboard** | Get Clipboard, Set Clipboard |
| **Files** | Read File, Write File, Copy File, Move File, Delete File |
| **System** | Open File/App, Kill Process, Close Window, Play Sound |
| **Network** | HTTP Request |
| **Misc** | Screenshot, Error Handler |

### Export

| Format | Description |
|--------|-------------|
| **PDF** | Formatted walkthrough with screenshots and step notes - ready to use as a SOP |
| **Python Script** | Standalone `.py` using `pyautogui`; runs without AutoFlow installed |
| **ZIP Package** | Script + `run.bat` + `requirements.txt`; double-click to run on any Windows machine |

### Other
- System tray integration - minimize to tray, tray hotkeys
- Variables panel with `{variable_name}` substitution in any text field
- Workflows stored as portable JSON files
- Settings persisted in browser localStorage

---

## Quick Start

1. **Download** `AutoFlow.exe` from the [Releases](https://github.com/ChrisMangin/AutoFlow/releases) page
2. **Run** - no installation, no admin rights required
3. Click **Record**, perform your task, click **Stop**
4. Review the captured steps, add notes if needed
5. Click **Save**, then **Play**

The app opens in your default browser. A system-tray icon lets you reopen, record, or replay without keeping the browser tab in focus.

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

### Run in Dev Mode

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
+-- server.py            # Flask + Socket.IO app - entry point
+-- recorder.py          # Mouse/keyboard capture engine
+-- player.py            # Step replay engine
+-- overlay_native.py    # Win32 recording HUD (dim layer + tkinter status)
+-- static/
|   +-- index.html       # Main app UI shell
|   +-- app.js           # Frontend state + rendering (~1100 lines)
|   +-- style.css        # Dark UI theme
|   +-- guide.html       # In-app user guide
|   +-- overlay.html     # Recording HUD web view
+-- workflows/           # Saved workflow JSON files (user data, gitignored)
+-- dev/
|   +-- test_element.py  # Standalone element-detection tester
+-- _legacy/             # Old pywebview entry points (superseded)
+-- AutoFlow.spec        # PyInstaller build spec
+-- requirements.txt
```

---

## License

MIT - see [LICENSE](LICENSE).
