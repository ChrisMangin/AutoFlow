"""
api.py — JS ↔ Python bridge exposed to pywebview.
All public methods are callable from JS as window.pywebview.api.<method>().
"""
import json
import os
import sys
import time
import threading
import base64
import io

from recorder import Recorder
from player import Player


def _get_workflows_dir():
    """Workflows live next to the exe when frozen, or next to this script in dev."""
    if getattr(sys, 'frozen', False):
        base = os.path.dirname(sys.executable)
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(base, "workflows")
    os.makedirs(path, exist_ok=True)
    return path


def _screenshot_b64():
    """Capture full screen, return base64-encoded JPEG thumbnail."""
    try:
        from PIL import ImageGrab, Image
        img = ImageGrab.grab()
        img.thumbnail((320, 200), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=70)
        return base64.b64encode(buf.getvalue()).decode()
    except Exception:
        return None


class API:
    def __init__(self):
        self.window = None
        self._recorder = None
        self._player = None
        self._current_steps = []

    # ------------------------------------------------------------------ #
    # Recording                                                            #
    # ------------------------------------------------------------------ #

    def start_recording(self):
        if self._recorder and self._recorder._recording:
            return {"ok": False, "error": "Already recording"}
        self._current_steps = []
        self._recorder = Recorder(on_step=self._on_step_recorded)
        self._recorder.start()
        return {"ok": True}

    def stop_recording(self):
        if not self._recorder:
            return {"ok": False, "error": "Not recording"}
        steps = self._recorder.stop()
        self._current_steps = steps
        return {"ok": True, "steps": steps}

    def _on_step_recorded(self, step):
        if self.window:
            try:
                self.window.evaluate_js(f"window._autoflow.onStep({json.dumps(step)})")
            except Exception:
                pass

    # ------------------------------------------------------------------ #
    # Playback                                                             #
    # ------------------------------------------------------------------ #

    def play_workflow(self, steps, speed=1.0):
        if self._player:
            self._player.stop()
        self._player = Player(
            steps=steps,
            speed=float(speed),
            progress_cb=self._on_play_progress,
            done_cb=self._on_play_done,
            error_cb=self._on_play_error,
        )
        self._player.start()
        return {"ok": True}

    def stop_playback(self):
        if self._player:
            self._player.stop()
        return {"ok": True}

    def _on_play_progress(self, index):
        if self.window:
            try:
                self.window.evaluate_js(f"window._autoflow.onPlayProgress({index})")
            except Exception:
                pass

    def _on_play_done(self):
        if self.window:
            try:
                self.window.evaluate_js("window._autoflow.onPlayDone()")
            except Exception:
                pass

    def _on_play_error(self, msg):
        if self.window:
            try:
                self.window.evaluate_js(f"window._autoflow.onPlayError({json.dumps(msg)})")
            except Exception:
                pass

    # ------------------------------------------------------------------ #
    # Workflows                                                            #
    # ------------------------------------------------------------------ #

    def save_workflow(self, name, steps):
        if not name:
            return {"ok": False, "error": "Name required"}
        safe_name = "".join(c for c in name if c.isalnum() or c in " _-").strip()
        path = os.path.join(_get_workflows_dir(), f"{safe_name}.json")
        data = {"name": name, "created": time.time(), "steps": steps}
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        return {"ok": True, "path": path}

    def load_workflow(self, name):
        path = os.path.join(_get_workflows_dir(), f"{name}.json")
        if not os.path.exists(path):
            return {"ok": False, "error": "Not found"}
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {"ok": True, "workflow": data}

    def list_workflows(self):
        items = []
        for fname in sorted(os.listdir(_get_workflows_dir())):
            if fname.endswith(".json"):
                path = os.path.join(_get_workflows_dir(), fname)
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        d = json.load(f)
                    items.append({
                        "name": d.get("name", fname[:-5]),
                        "file": fname[:-5],
                        "steps": len(d.get("steps", [])),
                        "created": d.get("created", 0),
                    })
                except Exception:
                    pass
        return {"ok": True, "workflows": items}

    def delete_workflow(self, name):
        path = os.path.join(_get_workflows_dir(), f"{name}.json")
        if os.path.exists(path):
            os.remove(path)
        return {"ok": True}

    # ------------------------------------------------------------------ #
    # Misc                                                                 #
    # ------------------------------------------------------------------ #

    def take_screenshot(self):
        b64 = _screenshot_b64()
        return {"ok": b64 is not None, "image": b64}

    def get_steps(self):
        return {"ok": True, "steps": self._current_steps}

    def set_steps(self, steps):
        self._current_steps = steps
        return {"ok": True}
