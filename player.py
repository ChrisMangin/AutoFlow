"""
player.py — Step replay with pause, resume, single-step, and start-from-index support.

Three-level click fallback:
  1. Find element by accessibility name (UIA)
  2. Find window by title + apply stored window-relative offset
  3. Absolute screen coordinates
"""
import os
import time
import threading
from pynput.mouse    import Button, Controller as MouseController
from pynput.keyboard import Key,    Controller as KbController

_BUTTON_MAP = {"left": Button.left, "right": Button.right, "middle": Button.middle}

def _strip_dirty(title):
    """Strip one leading 'unsaved-changes' asterisk from a window title.
    Many editors (Notepad, Notepad++, VS Code, ...) prepend '*' when a file has
    unsaved edits.  The asterisk is absent when the window first opens or after
    saving, so recorded titles with '*' would never match at playback time.
    Only strips a single leading '*' to avoid mangling intentional asterisks."""
    t = title or ""
    return t[1:] if t.startswith("*") else t


def _find_windows_excluding_self(win_name):
    """
    Enumerate ALL top-level windows whose title equals win_name, returning only
    those that do NOT belong to our own process (AutoFlow itself).
    Returns the first matching hwnd, or 0 if none found.

    Why: FindWindowW returns the first match in z-order, which can be the AutoFlow
    browser tab if it happens to show "New Tab - Google Chrome".  Filtering by PID
    ensures we target the correct Chrome window every time.
    """
    import ctypes, os
    from ctypes import wintypes
    u = ctypes.windll.user32
    own_pid = os.getpid()
    results = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_size_t, ctypes.c_ssize_t)
    def _cb(hwnd, _lp):
        buf = ctypes.create_unicode_buffer(512)
        u.GetWindowTextW(hwnd, buf, 512)
        if _strip_dirty(win_name).lower() in _strip_dirty(buf.value).lower():
            pid = ctypes.c_ulong(0)
            u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value != own_pid:
                results.append(hwnd)
        return True   # keep enumerating

    u.EnumWindows(_cb, 0)
    return results[0] if results else 0

_KEY_MAP = {
    "enter":    Key.enter,    "tab":      Key.tab,
    "esc":      Key.esc,      "escape":   Key.esc,
    "backspace":Key.backspace,"delete":   Key.delete,
    "up":       Key.up,       "down":     Key.down,
    "left":     Key.left,     "right":    Key.right,
    "home":     Key.home,     "end":      Key.end,
    "page_up":  Key.page_up,  "page_down":Key.page_down,
    "space":    Key.space,
    "f1":Key.f1,"f2":Key.f2,"f3":Key.f3,"f4":Key.f4,
    "f5":Key.f5,"f6":Key.f6,"f7":Key.f7,"f8":Key.f8,
    "f9":Key.f9,"f10":Key.f10,"f11":Key.f11,"f12":Key.f12,
    "ctrl":Key.ctrl,"alt":Key.alt,"shift":Key.shift,"win":Key.cmd,
}


def _get_chrome_url_by_hwnd(hwnd):
    """Read Chrome's current address bar via Chrome_OmniboxView -> WM_GETTEXT.
    Works without --force-renderer-accessibility or --remote-debugging-port.
    Returns the URL string or empty string."""
    try:
        import ctypes
        u   = ctypes.windll.user32
        WM_GETTEXT = 0x000D
        # Chrome exposes the omnibox as a child with class "Chrome_OmniboxView"
        omnibox = u.FindWindowExW(hwnd, None, "Chrome_OmniboxView", None)
        if not omnibox:
            return ""
        buf    = ctypes.create_unicode_buffer(2048)
        length = u.SendMessageW(omnibox, WM_GETTEXT, 2047, buf)
        text   = buf.value if length > 0 else ""
        if not text:
            return ""
        if text.startswith(("http", "file", "localhost")):
            return text
        if "." in text and "/" in text:
            return "https://" + text
    except Exception:
        pass
    return ""


def _get_chrome_url_by_title(win_title, timeout=1.5):
    """Find a Chrome window by title (excluding own process) and read its URL."""
    import time
    deadline = time.time() + timeout
    while time.time() < deadline:
        hwnd = _find_windows_excluding_self(win_title)
        if hwnd:
            url = _get_chrome_url_by_hwnd(hwnd)
            if url:
                return url
        time.sleep(0.15)
    return ""


_CHROME_EXES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]

def _open_new_chrome_tab():
    """Open a new Chrome tab.  If Chrome is already running, --new-tab opens in
    the existing window; otherwise launches a fresh instance."""
    import subprocess, os
    expanded = os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe")
    paths = _CHROME_EXES + [expanded]
    exe = next((p for p in paths if os.path.exists(p)), None)
    if exe:
        subprocess.Popen([exe, "--new-tab"])


def _resolve_click_position(d, use_element):
    el = d.get("element") if use_element else None
    if el:
        # 1. AutomationId match — most stable identifier for native-app controls
        if el.get("automation_id") and el.get("source") not in ("win32", "cdp", None):
            try:
                import uiautomation as auto
                aid = el["automation_id"]
                ctrl = auto.FindControl(
                    auto.GetRootControl(),
                    lambda c, _: c.AutomationId == aid and
                                 (not el.get("type") or c.ControlTypeName == el["type"]),
                    maxDepth=10,
                )
                if ctrl:
                    r = ctrl.BoundingRectangle
                    return (r.left + r.width // 2, r.top + r.height // 2)
            except Exception:
                pass

        # 2. UIA name-first search — for UIA-sourced elements with a meaningful name
        #    (e.g. ListItem "DSM5", TreeItem "2026-Q1", Button "Submit").
        #    These identities are stable even when the window or dropdown shifts
        #    position, making them more reliable than relative coordinates for
        #    dynamic content.  Scoped to the parent window for speed + accuracy.
        _er2 = el.get("elem_rect") or {}
        _area2 = (_er2.get("width", 0) or 0) * (_er2.get("height", 0) or 0)
        if (el.get("name")
                and el.get("source") == "uia"
                and _area2 < 100_000):
            try:
                import uiautomation as auto
                _n2  = el["name"];  _t2 = el.get("type", "")
                _sr2 = auto.GetRootControl()
                _wn2 = el.get("window", "")
                if _wn2:
                    try:
                        _wh2 = _find_windows_excluding_self(_wn2)
                        if _wh2:
                            _wc2 = auto.ControlFromHandle(_wh2)
                            if _wc2:
                                _sr2 = _wc2
                    except Exception:
                        pass
                ctrl = auto.FindControl(
                    _sr2,
                    lambda c, _: c.Name == _n2 and
                                 (not _t2 or c.ControlTypeName == _t2),
                    maxDepth=12,
                )
                if ctrl:
                    r = ctrl.BoundingRectangle
                    return (r.left + r.width // 2, r.top + r.height // 2)
            except Exception:
                pass

        # 3. Window + relative offset — reliable for fixed-position controls
        #    (buttons, text fields).  Fallback when UIA name search above fails.
        if el.get("window") and el.get("rel_x") is not None:
            try:
                import ctypes
                from ctypes import wintypes as _wt
                hwnd = _find_windows_excluding_self(el["window"])
                if hwnd:
                    rect = _wt.RECT()
                    ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))
                    return (rect.left + el["rel_x"], rect.top + el["rel_y"])
                # Window recorded but not present now — hard error, never guess.
                raise RuntimeError(
                    "Window not found: '" + el['window'] + "' — "
                    "ensure the target application is open and try again."
                )
            except RuntimeError:
                raise   # surface to player error handler
            except Exception:
                pass

        # 4. Name match — non-UIA sources (cdp etc); also UIA fallback if step 2 found
        #    the window but step 2b didn't find by name.
        _er = el.get("elem_rect") or {}
        _area = (_er.get("width", 0) or 0) * (_er.get("height", 0) or 0)
        if el.get("name") and el.get("source") not in ("win32", "cdp", None) and _area < 100_000:
            try:
                import uiautomation as auto
                ctrl = auto.FindControl(
                    auto.GetRootControl(),
                    lambda c, _: c.Name == el["name"] and
                                 (not el.get("type") or c.ControlTypeName == el["type"]),
                    maxDepth=10,
                )
                if ctrl:
                    r = ctrl.BoundingRectangle
                    return (r.left + r.width // 2, r.top + r.height // 2)
            except Exception:
                pass

        # 5. ClassName match — stable for specific web controls (e.g. "gf-form-input")
        #    Scoped to the recorded window; skipped for large containers.
        if el.get("class") and el.get("source") == "uia" and _area < 100_000:
            cls      = el["class"]
            win_name = el.get("window", "")
            _GENERIC = {"Button", "Edit", "Static", "ComboBox", "ListBox",
                        "NativeViewHost", ""}
            if cls not in _GENERIC:
                try:
                    import uiautomation as auto
                    search_root = auto.GetRootControl()
                    if win_name:
                        cand = auto.FindControl(
                            search_root,
                            lambda c, _: c.ControlTypeName == "WindowControl"
                                         and c.Name == win_name,
                            maxDepth=3,
                        )
                        if cand:
                            search_root = cand
                    ctrl = auto.FindControl(
                        search_root,
                        lambda c, _: c.ClassName == cls and
                                     (not el.get("type") or c.ControlTypeName == el["type"]),
                        maxDepth=12,
                    )
                    if ctrl:
                        r = ctrl.BoundingRectangle
                        return (r.left + (r.right  - r.left) // 2,
                                r.top  + (r.bottom - r.top)  // 2)
                except Exception:
                    pass

        # 6. elem_rect center — fallback when window lookup failed.
        er = el.get("elem_rect") if el else None
        if er and isinstance(er.get("width"), int) and isinstance(er.get("height"), int):
            cx = er["left"] + er["width"]  // 2
            cy = er["top"]  + er["height"] // 2
            if 0 < cx < 7680 and 0 < cy < 4320:
                return (cx, cy)

        # 7. Image-based location using the pre-click screenshot_region.
        #    Captured synchronously in _on_click while WH_MOUSE_LL hook was active —
        #    so dropdowns, menus, and list items were still rendered on screen.
        #    If scroll_hint_dy is set (navigational scroll was condensed away), this
        #    scrolls in the recorded direction and retries until the item is visible.
        sc_region = d.get("screenshot_region")
        # Skip image matching when a scroll hint is present: pre-scroll in _execute
        # will position the correct item at the click coords.  Image matching here
        # would find the wrong item (the one visible before scrolling).
        _has_scroll_hint = bool(d.get("scroll_hint_dy", 0))
        if sc_region and not _has_scroll_hint and (not el or el.get("source") in ("win32", None)):
            try:
                import pyautogui as _pag, base64 as _b64, io as _sio
                from PIL import Image as _PI
                img_data   = _b64.b64decode(sc_region)
                full_ndl   = _PI.open(_sio.BytesIO(img_data))
                fw, fh     = full_ndl.size
                _icx, _icy = fw // 2, fh // 2
                # 140 × 22 tight crop centred on click — specific enough for a list row
                tight  = full_ndl.crop((_icx - 70, _icy - 11, _icx + 70, _icy + 11))
                medium = full_ndl.crop((_icx - 60, _icy - 15, _icx + 60, _icy + 15))

                def _try_locate():
                    if tight.size[0] >= 20 and tight.size[1] >= 8:
                        loc = _pag.locateOnScreen(tight, confidence=0.80)
                        if loc:
                            return (loc.left + loc.width // 2, loc.top + loc.height // 2)
                    if medium.size[0] >= 20 and medium.size[1] >= 8:
                        loc = _pag.locateOnScreen(medium, confidence=0.75)
                        if loc:
                            return (loc.left + loc.width // 2, loc.top + loc.height // 2)
                    return None

                # First attempt without scrolling
                pos = _try_locate()
                if pos:
                    return pos

                # If a navigational scroll hint is stored, scroll toward the item
                # and retry (up to 8 attempts).  scroll_hint_dy < 0 → scroll down.
                _hint = d.get("scroll_hint_dy", 0)
                if _hint:
                    _scroll_dir = -1 if _hint < 0 else 1  # -1 = down, 1 = up
                    _sx, _sy = d.get("x", 0), d.get("y", 0)
                    for _ in range(8):
                        _pag.scroll(_scroll_dir * 3, x=_sx, y=_sy)
                        time.sleep(0.12)
                        pos = _try_locate()
                        if pos:
                            return pos
            except Exception:
                pass

    return (int(d["x"]), int(d["y"]))


class Player:
    """
    Runs a workflow step list.  Supports:
      pause()       — suspend between steps
      resume()      — continue from pause
      step()        — execute exactly one more step, then auto-pause
      stop()        — abort immediately
      start_index   — begin playback at this step index (default 0)
      start_paused  — if True, start in paused state (useful for step-from-idle)
    """

    def __init__(self, steps, speed=1.0, variables=None,
                 progress_cb=None, done_cb=None, error_cb=None,
                 pause_cb=None, use_element_targeting=True,
                 start_index=0, start_paused=False):
        self._steps   = steps
        self._speed   = max(0.1, float(speed))
        self._vars    = dict(variables or {})
        self._progress_cb = progress_cb
        self._done_cb     = done_cb
        self._error_cb    = error_cb
        self._pause_cb    = pause_cb
        self._use_element = use_element_targeting
        self._start_index = max(0, int(start_index))

        self._stop_evt = threading.Event()   # set → abort
        self._run_evt  = threading.Event()   # set → running; clear → paused
        self._step_evt = threading.Event()   # set → execute one step then re-pause

        if not start_paused:
            self._run_evt.set()   # start in running state

        self._mouse = MouseController()
        self._kb    = KbController()

    # ── Public controls ───────────────────────────────────────────────

    @property
    def is_paused(self):
        return not self._run_evt.is_set()

    def start(self):
        self._stop_evt.clear()
        threading.Thread(target=self._run, daemon=True).start()

    def stop(self):
        self._stop_evt.set()
        self._run_evt.set()    # unblock any waiting thread
        self._step_evt.set()

    def pause(self):
        self._run_evt.clear()
        self._step_evt.clear()

    def resume(self):
        self._step_evt.clear()
        self._run_evt.set()

    def step(self):
        """Advance one step, then auto-pause."""
        self._run_evt.clear()   # will stay paused after the step
        self._step_evt.set()    # unblock _wait_gate for one iteration

    # ── Internal helpers ──────────────────────────────────────────────

    def _wait_gate(self):
        """
        Block here while paused.  Returns True to execute the current step,
        False to abort.  Clears step_evt so the next call blocks again.
        """
        while True:
            if self._stop_evt.is_set():
                return False
            if self._run_evt.is_set():
                return True
            if self._step_evt.is_set():
                self._step_evt.clear()   # consume the one-shot token
                return True              # execute one step
            time.sleep(0.04)

    def _sleep(self, sec):
        end = time.time() + sec
        while time.time() < end and not self._stop_evt.is_set():
            # Check for pause during sleep
            if not self._run_evt.is_set() and not self._step_evt.is_set():
                return
            time.sleep(0.04)

    def _sub(self, text):
        for k, v in self._vars.items():
            text = text.replace(f"{{{{{k}}}}}", str(v))
        return text

    def _wait_for_click_target(self, d, timeout=1.5):
        """Poll UIA until the target element appears, up to `timeout` seconds.
        Skipped entirely when window+rel is available -- resolution uses FindWindowW
        (instant) so waiting for a UIA element is wasted time."""
        el = d.get("element")
        if not el:
            return
        # When window+rel is set we use FindWindowW for resolution -- no UIA needed.
        if el.get("window") and el.get("rel_x") is not None:
            return
        name      = el.get("name", "")
        auto_id   = el.get("automation_id", "")
        ctrl_type = el.get("type", "")
        # Only poll when we have a UIA-sourced, named element
        if el.get("source") in ("win32", "cdp", None):
            return
        if not name and not auto_id:
            return
        try:
            import uiautomation as auto
            deadline = time.time() + timeout
            while time.time() < deadline and not self._stop_evt.is_set():
                try:
                    ctrl = auto.FindControl(
                        auto.GetRootControl(),
                        lambda c, _: (not name      or c.Name            == name)     and
                                     (not auto_id   or c.AutomationId    == auto_id)  and
                                     (not ctrl_type or c.ControlTypeName == ctrl_type),
                        maxDepth=10,
                    )
                    if ctrl:
                        return      # found -- proceed with click
                except Exception:
                    pass
                time.sleep(0.15)
            # Timed out without finding element -- fall through to coord fallback silently
        except ImportError:
            pass

    def _wait_for_window(self, win_name, timeout=3.0):
        """Poll for win_name (excluding own AutoFlow process) until found or timeout.
        Returns hwnd when found, 0 on timeout.  Uses PID-filtered enumeration so
        the AutoFlow browser tab is never mistakenly matched."""
        if not win_name:
            return 0
        deadline = time.time() + timeout
        while time.time() < deadline and not self._stop_evt.is_set():
            hwnd = _find_windows_excluding_self(win_name)
            if hwnd:
                return hwnd
            time.sleep(0.20)
        return 0

    def _bring_window_to_front(self, d):
        """Bring the target window to the foreground before clicking.
        Restores the recorded maximized/normal state — critical for Chrome: the
        bookmarks bar is only at the recorded rel_y when Chrome is maximized.

        Old recordings (made before the "maximized" field was added to
        _win32_window_at) have no such key on the element.  In that case we
        can't know what state the window was recorded in, so we infer it:
        Chrome users overwhelmingly work maximized, so if the window is
        currently NOT maximized and it's a Chrome window, maximize it before
        clicking rather than risk the bookmarks bar / toolbar being in the
        wrong place."""
        el = d.get("element")
        if not el:
            return
        win_name = el.get("window", "")
        if not win_name:
            return
        try:
            import ctypes
            u = ctypes.windll.user32
            hwnd = _find_windows_excluding_self(win_name)
            if hwnd:
                maximized_flag = el.get("maximized", None)
                if maximized_flag is None:
                    # Old recording -- no "maximized" field stored.  Infer from
                    # current window state + Chrome heuristic.
                    try:
                        currently_zoomed = bool(u.IsZoomed(hwnd))
                    except Exception:
                        currently_zoomed = False
                    # The win32-class check catches window-level win32 elements;
                    # UIA-sourced elements (source="uia") report the DOM/AX
                    # ClassName instead (e.g. "scrollbar-view"), so also fall
                    # back to a window-title sniff for "... - Google Chrome".
                    is_chrome = (
                        el.get("class", "") == "Chrome_WidgetWin_1"
                        or "google chrome" in win_name.lower()
                    )
                    should_maximize = not currently_zoomed and is_chrome
                else:
                    should_maximize = bool(maximized_flag)

                if should_maximize:
                    # SW_MAXIMIZE (3): restores bookmarks bar and toolbar positions
                    # to exactly what they were when the step was recorded (or, for
                    # old recordings, to the state Chrome users almost always use).
                    u.ShowWindow(hwnd, 3)
                    time.sleep(0.12)  # wait for Chrome to re-render at full size
                else:
                    u.ShowWindow(hwnd, 9)   # SW_RESTORE (un-minimize only)
                u.SetForegroundWindow(hwnd)
        except Exception:
            pass


    def _run(self):
        try:
            loop_stack = []
            # error_handler step sets this; governs stop/continue/retry behaviour.
            error_config = {"action": "stop", "max_retries": 0}
            i = self._start_index          # honour start_index
            while i < len(self._steps):
                # ── Pause gate ────────────────────────────────────────
                if not self._wait_gate():
                    break

                step = self._steps[i]
                self._sleep(0.02 / self._speed)   # 20 ms base delay (was 80 ms)
                if self._stop_evt.is_set():
                    break

                t = step.get("type")
                d = step.get("data", {})

                # ── Skip disabled steps ───────────────────────────────
                if step.get("disabled"):
                    i += 1
                    continue

                # ── Flow control steps ────────────────────────────────
                if t == "loop":
                    count = int(d.get("count", 1))
                    loop_stack.append({"start": i, "remaining": count - 1})

                elif t == "loop_end":
                    if loop_stack:
                        entry = loop_stack[-1]
                        if entry["remaining"] > 0:
                            entry["remaining"] -= 1
                            i = entry["start"]
                            continue
                        else:
                            loop_stack.pop()

                elif t == "if":
                    val = self._vars.get(d.get("var", ""), "")
                    if str(val) != str(d.get("value", "")):
                        depth = 1
                        while i + 1 < len(self._steps):
                            i += 1
                            tt = self._steps[i].get("type")
                            if tt == "if":    depth += 1
                            elif tt in ("else", "end_if") and depth == 1: break
                            elif tt == "end_if": depth -= 1

                elif t == "else":
                    depth = 1
                    while i + 1 < len(self._steps):
                        i += 1
                        tt = self._steps[i].get("type")
                        if tt == "if":     depth += 1
                        elif tt == "end_if":
                            depth -= 1
                            if depth == 0: break

                elif t == "error_handler":
                    # Sets error handling context for subsequent steps in this run.
                    error_config = {
                        "action":      d.get("action",      "stop"),
                        "max_retries": int(d.get("max_retries", 0)),
                    }

                elif t not in ("end_if", "comment", "loop_end", "else"):
                    # Execute with retry / continue logic from the active error_handler.
                    retries = 0
                    max_r   = int(error_config.get("max_retries", 0))                               if error_config.get("action") == "retry" else 0
                    while True:
                        try:
                            self._execute(step)
                            break
                        except Exception as _exc:
                            if not self._stop_evt.is_set() and retries < max_r:
                                retries += 1
                                self._sleep(1.0)   # pause between retries
                                continue
                            elif error_config.get("action") == "continue":
                                break              # skip step, keep going
                            else:
                                raise              # propagate to outer handler

                # ── Progress callback ─────────────────────────────────
                if self._progress_cb:
                    try: self._progress_cb(i)
                    except Exception: pass

                # ── Auto-pause after a step() call ────────────────────
                if not self._run_evt.is_set() and self._pause_cb:
                    try: self._pause_cb()
                    except Exception: pass

                i += 1

        except Exception as e:
            if self._error_cb:
                try:
                    # Include step context so the frontend can highlight the card
                    step_info = self._steps[i] if i < len(self._steps) else {}
                    label = f"Step {step_info.get('id', i)+1} ({step_info.get('type','?')})"
                    self._error_cb(f"{label}: {e}")
                except Exception:
                    try: self._error_cb(str(e))
                    except Exception: pass
        finally:
            # Mark the run as terminated for state-reporting purposes (whether
            # it finished normally, errored out, or was stopped mid-flight).
            # Without this, a naturally-completed run left _stop_evt unset and
            # _run_evt set, so _emit_state() in server.py kept reporting
            # "playing" forever -- the #1 cause of the HUD overlay "sticking
            # around" after a workflow finished on its own.
            self._stop_evt.set()
            if self._done_cb:
                try: self._done_cb()
                except Exception: pass

    def _execute(self, step):
        t = step.get("type")
        d = step.get("data", {})

        if t == "click":
            if self._use_element:
                el  = d.get("element") or {}
                win = el.get("window", "")

                # ── "New Tab" browser click: smart handling ───────────────────
                # These are bookmark-bar or tab-bar clicks that navigate Chrome.
                # If no "New Tab" window exists at playback time we open one so
                # the click lands correctly rather than falling to raw coords
                # (which would hit whatever is on screen, e.g. AutoFlow's toolbar).
                if win and "new tab" in win.lower():
                    existing_hwnd = _find_windows_excluding_self(win)
                    if not existing_hwnd:
                        _open_new_chrome_tab()
                        # Wait up to 3 s for the new tab to appear
                        self._wait_for_window(win, timeout=3.0)
                elif win:
                    self._wait_for_window(win, timeout=3.0)

                self._wait_for_click_target(d)
                self._bring_window_to_front(d)
            xy  = _resolve_click_position(d, self._use_element)
            btn = _BUTTON_MAP.get(d.get("button", "left"), Button.left)

            # Pre-scroll: condensed navigational scrolls are stored as
            # scroll_hint_dy on the click.  Replay them now so the target
            # item (e.g. a dropdown row) is at the resolved position before
            # the click fires.  scroll_hint_dy < 0 = scroll down.
            _scroll_hint = d.get("scroll_hint_dy", 0)
            if _scroll_hint:
                self._mouse.position = xy
                time.sleep(0.08)
                _remaining = _scroll_hint
                while _remaining != 0:
                    _chunk = max(-3, min(3, _remaining))
                    self._mouse.scroll(0, _chunk)
                    _remaining -= _chunk
                    self._sleep(0.1)
                self._sleep(0.25)  # allow the UI to finish repositioning

            self._mouse.position = xy
            time.sleep(0.02)
            self._mouse.click(btn)

        elif t == "type":
            self._kb.type(self._sub(d.get("text", "")))

        elif t == "hotkey":
            parts = [p.strip().lower() for p in d.get("combo", "").split("+")]
            keys  = [_KEY_MAP.get(p, p) if len(p) > 1 else p for p in parts if p]
            if not keys: return
            if len(keys) == 1:
                self._kb.press(keys[0]); time.sleep(0.05); self._kb.release(keys[0])
            else:
                for k in keys[:-1]:   self._kb.press(k)
                self._kb.press(keys[-1]); self._kb.release(keys[-1])
                for k in reversed(keys[:-1]): self._kb.release(k)

        elif t == "wait":
            # Recorded wait steps are replaced by smart readiness checks:
            # if the NEXT click step targets a different window, _wait_for_window
            # in _execute already waits for it -- no need to sleep here.
            # Only apply a short settling pause (150 ms) to allow JS to update DOM.
            self._sleep(0.15)

        elif t == "scroll":
            self._mouse.position = (int(d["x"]), int(d["y"]))
            self._mouse.scroll(int(d.get("dx", 0)), int(d.get("dy", 0)))

        elif t == "navigate":
            import webbrowser
            webbrowser.open(self._sub(d.get("url", "")))

        elif t == "set_variable":
            n = d.get("name", "")
            if n: self._vars[n] = self._sub(d.get("value", ""))

        elif t == "run_script":
            import subprocess
            cmd = self._sub(d.get("command", ""))
            if cmd: subprocess.Popen(cmd, shell=True)

        elif t == "screenshot":
            try:
                from PIL import ImageGrab
                ImageGrab.grab()
            except Exception: pass

        elif t == "launch_browser":
            # Start Chrome or Edge with --remote-debugging-port=9222 for CDP support.
            import subprocess  # os already imported at module level
            browser = d.get("browser", "chrome").lower()
            url     = self._sub(d.get("url", "https://"))
            use_cdp = d.get("cdp", True)
            chrome_paths = [
                r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
                os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
            ]
            edge_paths = [
                r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
            ]
            paths = edge_paths if browser == "edge" else chrome_paths
            exe   = next((p for p in paths if os.path.exists(p)), None)
            if not exe:
                raise RuntimeError(f"launch_browser: could not find {browser} executable")
            args = [exe]
            if use_cdp:
                args.append("--remote-debugging-port=9222")
            if url:
                args.append(url)
            subprocess.Popen(args)
            self._sleep(1.5)   # allow browser to start before subsequent steps

        elif t == "show_message":
            # Show a native Windows MessageBox; blocks until the user clicks OK.
            import ctypes
            title   = self._sub(d.get("title",   "AutoFlow"))
            message = self._sub(d.get("message", ""))
            mb_type = d.get("type", "info")
            flags   = {"info": 0x40, "warning": 0x30, "error": 0x10}.get(mb_type, 0x40)
            ctypes.windll.user32.MessageBoxW(0, message, title, flags)

        elif t == "wait_for_window":
            # Wait for a Win32 top-level window title to appear (fast, no UIA
            # needed). Used by the condensed navigate+wait pattern that replaces
            # the "click New Tab -> click bookmark" preamble with a single
            # navigate() step: this step waits for the destination page's
            # window title before the next click proceeds.
            title      = d.get("title", "")
            timeout_ms = int(d.get("timeout_ms", 8000))
            hwnd = self._wait_for_window(title, timeout=timeout_ms / 1000.0)
            if not hwnd:
                raise RuntimeError(f"Timed out waiting for window: '{title}'")

        elif t == "wait_for_element":
            # Poll UIA until a named/typed element appears, or timeout expires.
            import uiautomation as auto
            name       = d.get("name", "")
            ctrl_type  = d.get("type", "")
            timeout_ms = int(d.get("timeout_ms", 5000))
            deadline   = time.time() + timeout_ms / 1000.0
            found      = False
            while time.time() < deadline and not self._stop_evt.is_set():
                try:
                    ctrl = auto.FindControl(
                        auto.GetRootControl(),
                        lambda c, _: (not name or c.Name == name) and
                                     (not ctrl_type or c.ControlTypeName == ctrl_type),
                        maxDepth=10,
                    )
                    if ctrl:
                        found = True
                        break
                except Exception:
                    pass
                time.sleep(0.25)
            if not found:
                raise RuntimeError(
                    f"wait_for_element: '{name or ctrl_type}' not found within {timeout_ms} ms"
                )

        elif t == "get_clipboard":
            # Read clipboard text into a variable.
            var = d.get("variable", "")
            if var:
                try:
                    import pyperclip
                    self._vars[var] = pyperclip.paste() or ""
                except ImportError:
                    # ctypes fallback — no external dependency
                    import ctypes
                    CF_UNICODETEXT = 13
                    u2 = ctypes.windll.user32
                    u2.OpenClipboard(0)
                    hnd = u2.GetClipboardData(CF_UNICODETEXT)
                    val = ctypes.wstring_at(hnd) if hnd else ""
                    u2.CloseClipboard()
                    self._vars[var] = val

        elif t == "set_clipboard":
            # Write text to the clipboard.
            text = self._sub(d.get("text", ""))
            try:
                import pyperclip
                pyperclip.copy(text)
            except ImportError:
                # ctypes fallback
                import ctypes
                GMEM_MOVEABLE  = 0x0002
                CF_UNICODETEXT = 13
                kernel32 = ctypes.windll.kernel32
                user32c  = ctypes.windll.user32
                buf  = ctypes.create_unicode_buffer(text)
                size = ctypes.sizeof(buf)
                hnd  = kernel32.GlobalAlloc(GMEM_MOVEABLE, size)
                ptr  = kernel32.GlobalLock(hnd)
                ctypes.memmove(ptr, buf, size)
                kernel32.GlobalUnlock(hnd)
                user32c.OpenClipboard(0)
                user32c.EmptyClipboard()
                user32c.SetClipboardData(CF_UNICODETEXT, hnd)
                user32c.CloseClipboard()

        elif t == "image_click":
            # Image-based click: locate a screenshot region on screen, then click it.
            # Requires: pip install pyautogui opencv-python
            img_b64    = d.get("image_b64", "")
            confidence = float(d.get("confidence", 0.85))
            if not img_b64:
                raise RuntimeError("image_click: no image_b64 stored in step data")
            try:
                import pyautogui, base64, io
                from PIL import Image
                needle = Image.open(io.BytesIO(base64.b64decode(img_b64)))
                loc    = pyautogui.locateOnScreen(needle, confidence=confidence)
                if loc is None:
                    raise RuntimeError("image_click: target image not found on screen")
                cx, cy = pyautogui.center(loc)
                self._mouse.position = (cx, cy)
                time.sleep(0.05)
                self._mouse.click(Button.left)
            except ImportError:
                raise RuntimeError(
                    "image_click requires: pip install pyautogui opencv-python"
                )

        # ── File operations (Power-Automate-style) ────────────────────
        elif t == "read_file":
            path_ = self._sub(d.get("path", ""))
            var   = d.get("variable", "")
            try:
                with open(path_, "r", encoding=d.get("encoding", "utf-8"),
                          errors="replace") as f:
                    content = f.read()
                if var:
                    self._vars[var] = content
            except Exception as e:
                raise RuntimeError(f"read_file: {e}")

        elif t == "write_file":
            path_ = self._sub(d.get("path", ""))
            text_ = self._sub(d.get("text", ""))
            mode  = "a" if d.get("append") else "w"
            try:
                parent = os.path.dirname(path_)
                if parent:
                    os.makedirs(parent, exist_ok=True)
                with open(path_, mode, encoding=d.get("encoding", "utf-8")) as f:
                    f.write(text_)
            except Exception as e:
                raise RuntimeError(f"write_file: {e}")

        elif t == "copy_file":
            import shutil
            src = self._sub(d.get("src", "")); dst = self._sub(d.get("dst", ""))
            try:
                parent = os.path.dirname(dst)
                if parent:
                    os.makedirs(parent, exist_ok=True)
                shutil.copy2(src, dst)
            except Exception as e:
                raise RuntimeError(f"copy_file: {e}")

        elif t == "move_file":
            import shutil
            src = self._sub(d.get("src", "")); dst = self._sub(d.get("dst", ""))
            try:
                parent = os.path.dirname(dst)
                if parent:
                    os.makedirs(parent, exist_ok=True)
                shutil.move(src, dst)
            except Exception as e:
                raise RuntimeError(f"move_file: {e}")

        elif t == "delete_file":
            path_   = self._sub(d.get("path", ""))
            ignore  = bool(d.get("ignore_errors"))
            try:
                if d.get("is_folder"):
                    import shutil
                    shutil.rmtree(path_, ignore_errors=ignore)
                elif os.path.exists(path_):
                    os.remove(path_)
                elif not ignore:
                    raise FileNotFoundError(path_)
            except Exception as e:
                if not ignore:
                    raise RuntimeError(f"delete_file: {e}")

        # ── Web / process / window utilities ───────────────────────────
        elif t == "http_request":
            import urllib.request
            url     = self._sub(d.get("url", ""))
            method  = d.get("method", "GET").upper()
            headers = d.get("headers", {}) or {}
            body    = self._sub(d.get("body", ""))
            var     = d.get("variable", "")
            try:
                data = body.encode("utf-8") if body else None
                req  = urllib.request.Request(url, data=data, headers=headers, method=method)
                with urllib.request.urlopen(req, timeout=float(d.get("timeout_sec", 15))) as resp:
                    result_text = resp.read().decode("utf-8", errors="replace")
                if var:
                    self._vars[var] = result_text
            except Exception as e:
                raise RuntimeError(f"http_request: {e}")

        elif t == "kill_process":
            import subprocess
            name = self._sub(d.get("name", ""))
            if name:
                subprocess.run(["taskkill", "/IM", name, "/F"], capture_output=True)

        elif t == "close_window":
            title = self._sub(d.get("title", ""))
            hwnd = _find_windows_excluding_self(title)
            if hwnd:
                import ctypes
                WM_CLOSE = 0x0010
                ctypes.windll.user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
            elif not d.get("ignore_missing"):
                raise RuntimeError(f"close_window: window '{title}' not found")

        elif t == "open_file":
            path_ = self._sub(d.get("path", ""))
            try:
                os.startfile(path_)
            except Exception:
                # Fallback: `cmd /c start` checks the App Paths registry key where
                # Microsoft Office (excel.exe, winword.exe, etc.) registers itself.
                # os.startfile / ShellExecute does NOT search App Paths.
                try:
                    import subprocess as _sub
                    _sub.Popen(["cmd", "/c", "start", "", path_], shell=False,
                               creationflags=0x08000000)  # CREATE_NO_WINDOW
                except Exception as e2:
                    raise RuntimeError(f"open_file: could not open {path_!r}: {e2}")

        elif t == "play_sound":
            try:
                import winsound
                sound = d.get("sound", "default").lower()
                _SOUND_MAP = {
                    "default": winsound.MB_OK,
                    "error":   winsound.MB_ICONHAND,
                    "warning": winsound.MB_ICONEXCLAMATION,
                    "info":    winsound.MB_ICONASTERISK,
                }
                winsound.MessageBeep(_SOUND_MAP.get(sound, winsound.MB_OK))
            except Exception:
                pass
