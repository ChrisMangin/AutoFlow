"""
recorder.py — Enhanced recorder.

Key behaviours
--------------
* Clicks on any window whose title contains "autoflow" are silently skipped
  so toolbar buttons and the overlay never appear as recorded steps.
* Screenshot is captured immediately (Phase 1) so the card thumbnail appears
  without waiting for element detection.
* Region crop (~420×260 centred on the click) is stored as screenshot_region
  for the tight card hero image, matching the Scribe/Folge card style.
* Phase 3: a second screenshot is taken ~600 ms after the click and stored as
  screenshot_after — captures menus, tooltips, or dropdowns that open on click.
* Element detection (Phase 2) runs with a 1.5 s timeout via a shared thread
  pool. On timeout, or for browser windows, falls back to win32 window info +
  current browser URL (read from the address bar via UIA).
* Browser hint: if Chrome/Edge falls back to win32-only element info (meaning
  the accessibility API returned nothing useful), on_browser_hint is called once
  per recording session to suggest enabling --force-renderer-accessibility.
* Pause/resume: when paused all input events are silently ignored without
  stopping the pynput listeners.
* Stop is instant: _recording=False gates all handlers; listeners are joined
  in background daemon threads.
"""

import time
import threading
import base64
import io
import ctypes
import concurrent.futures
from ctypes import wintypes
from pynput import mouse, keyboard

_SPECIAL_KEYS = {
    keyboard.Key.enter, keyboard.Key.tab, keyboard.Key.esc,
    keyboard.Key.backspace, keyboard.Key.delete,
    keyboard.Key.up, keyboard.Key.down, keyboard.Key.left, keyboard.Key.right,
    keyboard.Key.home, keyboard.Key.end, keyboard.Key.page_up, keyboard.Key.page_down,
    keyboard.Key.f1,  keyboard.Key.f2,  keyboard.Key.f3,  keyboard.Key.f4,
    keyboard.Key.f5,  keyboard.Key.f6,  keyboard.Key.f7,  keyboard.Key.f8,
    keyboard.Key.f9,  keyboard.Key.f10, keyboard.Key.f11, keyboard.Key.f12,
}
_MODIFIER_KEYS = {
    keyboard.Key.ctrl,  keyboard.Key.ctrl_l,  keyboard.Key.ctrl_r,
    keyboard.Key.alt,   keyboard.Key.alt_l,   keyboard.Key.alt_r,
    keyboard.Key.shift, keyboard.Key.shift_l, keyboard.Key.shift_r,
    keyboard.Key.cmd,   keyboard.Key.cmd_l,   keyboard.Key.cmd_r,
}

_BROWSER_CLASSES = {"Chrome_WidgetWin_1", "MozillaWindowClass", "IEFrame"}

# Window-TITLE based browser detection.  WindowFromPoint(x, y) returns the
# innermost HWND under the cursor -- for clicks *inside* a Chrome/Edge page
# (as opposed to the omnibox/bookmarks bar, which are true Chrome_WidgetWin_1
# children) this is usually an internal render-surface HWND with some other
# class entirely, so a class-only check silently fails for almost every click
# that actually matters and falls back to slow, noisy UIA. Browser window
# titles reliably end in " - Google Chrome" / " - Microsoft Edge" /
# " - Mozilla Firefox", so check that too.
_BROWSER_TITLE_SUFFIXES = (" - google chrome", " - microsoft edge", " - mozilla firefox")

def _looks_like_browser(win_info):
    """True if a _win32_window_at() result belongs to a browser window, using
    both the win32 class (reliable for frame/toolbar clicks) and the window
    title suffix (reliable for clicks anywhere inside the page content)."""
    if not win_info:
        return False
    if win_info.get("class", "") in _BROWSER_CLASSES:
        return True
    return win_info.get("window", "").lower().endswith(_BROWSER_TITLE_SUFFIXES)

_CLASS_LABELS = {
    "Chrome_WidgetWin_1":  "Chrome / Edge",
    "MozillaWindowClass":  "Firefox",
    "IEFrame":             "Internet Explorer",
    "CabinetWClass":       "File Explorer",
    "WorkerW":             "Desktop",
    "Shell_TrayWnd":       "Taskbar",
    "Notepad":             "Notepad",
    "XLMAIN":              "Excel",
    "WINWORD":             "Word",
    "rctrl_renwnd32":      "Outlook",
    "PPTFrameClass":       "PowerPoint",
    "ConsoleWindowClass":  "Terminal",
}

_uia_pool = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="uia")


# ── Win32 fast-path ───────────────────────────────────────────────────────────

def _win32_window_at(x, y):
    """Instant ctypes window info at (x, y). Never blocks."""
    try:
        user32 = ctypes.windll.user32
        hwnd = user32.WindowFromPoint(wintypes.POINT(x, y))
        if not hwnd:
            return None
        # GA_ROOT (2) walks to the topmost ancestor, handling both child windows
        # and popup-owned windows (e.g. Chrome_RenderWidgetHostHWND, which may be
        # a WS_POPUP rather than a true WS_CHILD so GetParent returns 0 prematurely).
        _GA_ROOT = 2
        root = user32.GetAncestor(hwnd, _GA_ROOT) or hwnd
        tbuf = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(root, tbuf, 512)
        title = tbuf.value
        cbuf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cbuf, 256)
        cls = cbuf.value
        rect = wintypes.RECT()
        user32.GetWindowRect(root, ctypes.byref(rect))
        w = rect.right - rect.left
        h = rect.bottom - rect.top
        if not title and not cls:
            return None
        # IsZoomed returns True when window is maximized.  Stored so the player
        # can restore the same state before clicking (critical for Chrome bookmarks bar).
        try:
            maximized = bool(user32.IsZoomed(root))
        except Exception:
            maximized = False
        return {
            "name":        title,
            "type":        _CLASS_LABELS.get(cls, cls),
            "class":       cls,
            "window":      title,
            "window_rect": {"left": rect.left, "top": rect.top, "width": w, "height": h},
            "hwnd":        root,
            "rel_x":       x - rect.left,
            "rel_y":       y - rect.top,
            "maximized":   maximized,
            "source":      "win32",
        }
    except Exception:
        return None


def _is_own_process_window(x, y):
    """
    Return True if the Win32 window under (x, y) belongs to THIS process.
    Used to filter clicks on the AutoFlow overlay / HUD, which have no window
    title so the "autoflow" string filter misses them.
    """
    import os
    try:
        user32 = ctypes.windll.user32
        hwnd   = user32.WindowFromPoint(wintypes.POINT(x, y))
        if not hwnd:
            return False
        pid = ctypes.c_ulong(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return pid.value == os.getpid()
    except Exception:
        return False


# ── Browser URL via UIA omnibox ───────────────────────────────────────────────

def _get_chrome_url_by_hwnd(hwnd):
    """Read Chrome's address bar via Chrome_OmniboxView -> WM_GETTEXT.
    No special Chrome flags needed.  Fast (< 5 ms).  Returns URL or empty string."""
    try:
        u   = ctypes.windll.user32
        WM_GETTEXT = 0x000D
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
        if "." in text and ("/" in text or len(text) > 8):
            return "https://" + text
    except Exception:
        pass
    return ""


def _get_browser_url(window_title):
    """
    Read the current URL from a browser's address bar.
    Primary: Chrome_OmniboxView WM_GETTEXT (fast, no special flags).
    Fallback: UIA EditControl search (slower, works for Edge/Firefox too).
    Returns the URL string or None.
    """
    # Primary: direct Win32 message to Chrome omnibox
    try:
        u = ctypes.windll.user32
        buf = ctypes.create_unicode_buffer(512)

        results = []
        @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_size_t, ctypes.c_ssize_t)
        def _cb(hwnd, _lp):
            buf2 = ctypes.create_unicode_buffer(512)
            u.GetWindowTextW(hwnd, buf2, 512)
            if buf2.value == window_title:
                results.append(hwnd)
            return True
        u.EnumWindows(_cb, 0)

        for hwnd in results:
            url = _get_chrome_url_by_hwnd(hwnd)
            if url:
                return url
    except Exception:
        pass

    # Fallback: UIA (works for non-Chrome browsers)
    try:
        import uiautomation as auto
        browser_win = auto.FindControl(
            auto.GetRootControl(),
            lambda c, _: c.ControlTypeName == "WindowControl" and c.Name == window_title,
            maxDepth=3,
        )
        if not browser_win:
            return None
        addr = auto.FindControl(
            browser_win,
            lambda c, _: (
                c.ControlTypeName == "EditControl" and
                any(k in (c.Name or "").lower() for k in ("address", "search", "url", "location"))
            ),
            maxDepth=10,
        )
        if addr:
            val = addr.GetValuePattern().Value
            if val and ("." in val or val.startswith(("http", "localhost", "file:"))):
                return val
    except Exception:
        pass
    return None


def _get_browser_url_timed(window_title, timeout=1.5):
    f = _uia_pool.submit(_get_browser_url, window_title)
    try:
        return f.result(timeout=timeout)
    except Exception:
        return None


# ── UIA helpers ──────────────────────────────────────────────────────────────

def _build_ancestor_path(ctrl, max_depth=5):
    """Walk UIA tree upward and return 'Window > Pane > Button' for identification."""
    parts = []
    p = ctrl
    for _ in range(max_depth):
        try:
            pp = p.GetParentControl()
            if pp is None or pp == p:
                break
            label = pp.Name or pp.ControlTypeName or ""
            if label:
                parts.append(label)
            p = pp
        except Exception:
            break
    return " > ".join(reversed(parts))


def _find_label_for(ctrl):
    """
    Check preceding siblings for a Label/Text control.
    Common pattern: unlabelled inputs are preceded by a visible label element.
    """
    try:
        parent = ctrl.GetParentControl()
        if parent is None:
            return None
        for sib in parent.GetChildren():
            if sib == ctrl:
                break
            if sib.ControlTypeName in ("TextControl", "StaticControl", "LabelControl"):
                name = sib.Name or ""
                if name:
                    return name
    except Exception:
        pass
    return None



def _find_leaf_element(ctrl, x, y, _depth=0):
    """
    Walk the UIA tree downward from ctrl to find the most specific (deepest,
    front z-order) child whose BoundingRectangle still contains (x, y).

    UIA's ControlFromPoint() often returns a container/pane rather than the
    actual leaf element (button, link, input).  Walking children gives the
    true target — matching Power Automate's recorder behaviour.
    """
    if _depth > 25:
        return ctrl  # safety limit
    try:
        for child in ctrl.GetChildren():
            try:
                r = child.BoundingRectangle
                if r and r.left <= x <= r.right and r.top <= y <= r.bottom:
                    return _find_leaf_element(child, x, y, _depth + 1)
            except Exception:
                continue
    except Exception:
        pass
    return ctrl


def _get_dom_element_at(x, y, win_rect=None, cdp_port=9222):
    """
    Query DOM element at screen coords via Chrome DevTools Protocol.
    Requires Chrome/Edge launched with --remote-debugging-port=9222 and
    websocket-client installed (pip install websocket-client).
    Falls back silently if CDP is unavailable.
    """
    import urllib.request, json
    try:
        tabs = json.loads(
            urllib.request.urlopen(
                "http://localhost:{}/json".format(cdp_port), timeout=0.5
            ).read()
        )
        page = next(
            (t for t in tabs
             if t.get("type") == "page" and "webSocketDebuggerUrl" in t),
            None,
        )
        if not page:
            return None

        try:
            import websocket  # websocket-client
        except ImportError:
            return None

        ws = websocket.create_connection(page["webSocketDebuggerUrl"], timeout=1)

        # Step 1: Use Page.getLayoutMetrics to compute toolbar height dynamically.
        # cssVisualViewport.clientHeight = visible content area height in CSS px.
        # toolbar_height = window_height - clientHeight  (replaces hardcoded 95 px).
        toolbar_px = 95  # safe fallback
        if win_rect:
            try:
                ws.send(json.dumps({"id": 1, "method": "Page.getLayoutMetrics", "params": {}}))
                metrics = json.loads(ws.recv())
                vp = metrics.get("result", {}).get("cssVisualViewport", {})
                vp_h = vp.get("clientHeight", 0)
                if vp_h:
                    win_h = win_rect.get("height", 0)
                    computed = win_h - vp_h
                    if 20 <= computed <= 300:   # clamp to sane range
                        toolbar_px = computed
            except Exception:
                pass

            vp_x = x - win_rect.get("left", 0)
            vp_y = y - win_rect.get("top",  0) - toolbar_px
        else:
            vp_x, vp_y = x, y

        # Step 2: Query DOM element at computed viewport coordinates.
        js = (
            "JSON.stringify((function(){"
            "var e=document.elementFromPoint(" + str(vp_x) + "," + str(vp_y) + ");"
            "if(!e)return null;"
            "return{"
            "tag:e.tagName,"
            "id:e.id,"
            "cls:e.className,"
            "text:(e.innerText||e.value||e.alt||e.title||'').slice(0,100),"
            "role:e.getAttribute('role'),"
            "ariaLabel:e.getAttribute('aria-label'),"
            "name:e.getAttribute('name'),"
            "placeholder:e.getAttribute('placeholder'),"
            "href:e instanceof HTMLAnchorElement?e.href:null"
            "}"
            "})()"
        )
        ws.send(json.dumps({"id": 2, "method": "Runtime.evaluate",
                            "params": {"expression": js}}))
        result = json.loads(ws.recv())
        ws.close()
        val = result.get("result", {}).get("result", {}).get("value")
        return json.loads(val) if val else None
    except Exception:
        return None


# ── UIA element detection ─────────────────────────────────────────────────────

def _get_element_at(x, y):
    """
    UIA element detection with two retries.  Walks text children when the
    element itself has no Name.  Always call via _get_element_timed().
    """
    for delay in (0.0, 0.2, 0.5):  # 3 attempts; last delay helps slow-loading accessibility trees
        if delay:
            time.sleep(delay)
        try:
            import uiautomation as auto
            ctrl = auto.ControlFromPoint(x, y)
            if not ctrl:
                continue

            # Apply leaf walk: ControlFromPoint often returns a container/pane.
            # Walking children finds the actual element (button, input, link) under
            # the cursor, matching how Power Automate's recorder works.
            ctrl = _find_leaf_element(ctrl, x, y)

            name      = ctrl.Name           or ""
            ctrl_type = ctrl.ControlTypeName or ""
            ctrl_cls  = ctrl.ClassName       or ""

            # Scan immediate text/label children when element has no name
            if not name:
                try:
                    for child in ctrl.GetChildren():
                        if child.ControlTypeName in ("TextControl", "StaticControl"):
                            child_name = child.Name or ""
                            if child_name:
                                name = child_name
                                break
                except Exception:
                    pass

            # Try HelpText (tooltip / accessible description) as name fallback
            if not name:
                try:
                    ht = ctrl.HelpText or ""
                    if ht:
                        name = ht
                except Exception:
                    pass

            # Try LegacyIAccessible pattern — exposes name/value even when UIA Name is empty.
            # Common in Chrome web controls: aria-label lands here even without --force-renderer-accessibility.
            if not name:
                try:
                    legacy = ctrl.GetLegacyIAccessiblePattern()
                    if legacy:
                        la_name  = legacy.Name  or ""
                        la_value = legacy.Value or ""
                        if la_name:
                            name = la_name
                        elif la_value and len(la_value) < 120:
                            name = la_value   # input's current text is better than nothing
                except Exception:
                    pass

            # Walk up to the nearest WindowControl (native apps) or the Chrome browser
            # frame (which roots at a DocumentControl/"Chrome Legacy Window", not a
            # WindowControl).  Accept any top-level ancestor that has a non-empty Name
            # and a BoundingRectangle if no WindowControl is found within 25 hops.
            window_title = ""
            window_rect  = None
            try:
                p = ctrl
                best_named = None   # closest named ancestor as fallback
                for _ in range(25):
                    ct = p.ControlTypeName
                    if ct == "WindowControl":
                        window_title = p.Name or ""
                        r = p.BoundingRectangle
                        window_rect = {
                            "left":   r.left,
                            "top":    r.top,
                            "width":  r.right  - r.left,
                            "height": r.bottom - r.top,
                        }
                        break
                    # Chrome's web content tree roots at a DocumentControl; accept it
                    if ct in ("DocumentControl", "PaneControl") and (p.Name or ""):
                        if best_named is None:
                            best_named = p
                    pp = p.GetParentControl()
                    if pp is None or pp == p:
                        break
                    p = pp
                # Fallback: use the best named ancestor we found (Chrome Legacy Window etc.)
                if window_rect is None and best_named is not None:
                    window_title = best_named.Name or ""
                    r = best_named.BoundingRectangle
                    if r:
                        window_rect = {
                            "left":   r.left,
                            "top":    r.top,
                            "width":  r.right  - r.left,
                            "height": r.bottom - r.top,
                        }
            except Exception:
                pass

            # Try sibling label lookup when element still has no name
            if not name:
                try:
                    label = _find_label_for(ctrl)
                    if label:
                        name = label
                except Exception:
                    pass

            # Capture extra identification fields
            try:
                automation_id = ctrl.AutomationId or ""
            except Exception:
                automation_id = ""
            try:
                control_type = ctrl.ControlType or 0
            except Exception:
                control_type = 0

            ancestor_path = _build_ancestor_path(ctrl)

            try:
                er = ctrl.BoundingRectangle
                elem_rect = {
                    "left":   er.left,
                    "top":    er.top,
                    "width":  er.right  - er.left,  # er.width is a method; compute explicitly
                    "height": er.bottom - er.top,
                }
            except Exception:
                elem_rect = None

            # If we still have no name but a specific ClassName, use it as the
            # display label.  ClassName is the CSS class in Chrome's web tree
            # (e.g. "gf-form-input"), which is stable across sessions.
            _GENERIC_CLASSES = {"Button", "Edit", "Static", "ComboBox", "ListBox",
                                 "NativeViewHost", "Chrome_RenderWidgetHostHWND", ""}
            class_label = ctrl_cls if ctrl_cls not in _GENERIC_CLASSES else ""

            if not name and class_label:
                name = class_label   # visible on card; player still uses coords fallback

            if not name and not automation_id and not window_title and ctrl_type in ("", "PaneControl"):
                continue

            return {
                "name":          name,
                "type":          ctrl_type,
                "class":         ctrl_cls,
                "class_label":   class_label,   # non-generic ClassName for display / matching
                "automation_id": automation_id,
                "control_type":  control_type,
                "ancestor_path": ancestor_path,
                "elem_rect":     elem_rect,
                "window":        window_title,
                "window_rect":   window_rect,
                "rel_x": (x - window_rect["left"]) if window_rect else None,
                "rel_y": (y - window_rect["top"])  if window_rect else None,
                "source": "uia",
            }
        except Exception:
            pass
    return None


def _get_element_timed(x, y, timeout=1.5):
    f = _uia_pool.submit(_get_element_at, x, y)
    try:
        return f.result(timeout=timeout)
    except Exception:
        return None


# ── Screenshot ────────────────────────────────────────────────────────────────

# Screenshot mode: "all" = full virtual desktop, "active" = monitor under click
_screenshot_mode = ["all"]


def set_screenshot_mode(mode):
    """Set to "all" (full virtual desktop) or "active" (monitor under click)."""
    _screenshot_mode[0] = mode if mode in ("all", "active") else "all"


def _grab_region(x, y, rw, rh):
    """
    Capture a rw x rh region centred on physical click point (x, y).
    Uses a tight bbox grab — much faster than full-screen + crop.
    "all"    -- virtual-desktop coordinates (multi-monitor safe).
    "active" -- clamps to the monitor containing (x, y).
    """
    from PIL import ImageGrab
    u = ctypes.windll.user32

    if _screenshot_mode[0] == "active":
        class _PT(ctypes.Structure):
            _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]
        class _RC(ctypes.Structure):
            _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                        ("right", ctypes.c_long), ("bottom", ctypes.c_long)]
        class _MI(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_ulong), ("rcMonitor", _RC),
                        ("rcWork", _RC), ("dwFlags", ctypes.c_ulong)]
        pt = _PT(x, y)
        hmon = u.MonitorFromPoint(pt, 2)
        mi = _MI(); mi.cbSize = ctypes.sizeof(_MI)
        u.GetMonitorInfoW(hmon, ctypes.byref(mi))
        # Clamp region to monitor bounds
        ml, mt = mi.rcMonitor.left, mi.rcMonitor.top
        mr, mb = mi.rcMonitor.right, mi.rcMonitor.bottom
        rl = max(ml, x - rw // 2); rt = max(mt, y - rh // 2)
        rr = min(mr, rl + rw);     rb = min(mb, rt + rh)
    else:
        vx = u.GetSystemMetrics(76)  # SM_XVIRTUALSCREEN
        vy = u.GetSystemMetrics(77)  # SM_YVIRTUALSCREEN
        vw = u.GetSystemMetrics(78)  # SM_CXVIRTUALSCREEN
        vh = u.GetSystemMetrics(79)  # SM_CYVIRTUALSCREEN
        rl = max(vx, x - rw // 2); rt = max(vy, y - rh // 2)
        rr = min(vx + vw, rl + rw); rb = min(vy + vh, rt + rh)

    # Tight bbox — only copies the small region, not the full desktop.
    return ImageGrab.grab(bbox=(rl, rt, rr, rb), all_screens=True)


def _capture_screenshot(x, y):
    """
    Full-screen JPEG with red crosshair + region crop.
    Returns (thumbnail_b64, full_b64, region_b64).
    Single full-screen grab reused for both outputs — avoids the previous
    double-grab (once in _grab_region, once for the crosshair overlay).
    """
    try:
        from PIL import ImageGrab, ImageDraw, Image
        u2 = ctypes.windll.user32

        # ── One grab for everything ───────────────────────────────────
        if _screenshot_mode[0] == "active":
            class _PT2(ctypes.Structure):
                _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]
            class _RC2(ctypes.Structure):
                _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                            ("right", ctypes.c_long), ("bottom", ctypes.c_long)]
            class _MI2(ctypes.Structure):
                _fields_ = [("cbSize", ctypes.c_ulong), ("rcMonitor", _RC2),
                            ("rcWork", _RC2), ("dwFlags", ctypes.c_ulong)]
            _pt2 = _PT2(x, y)
            _hm2 = u2.MonitorFromPoint(_pt2, 2)
            _mi2 = _MI2(); _mi2.cbSize = ctypes.sizeof(_MI2)
            u2.GetMonitorInfoW(_hm2, ctypes.byref(_mi2))
            ml, mt = _mi2.rcMonitor.left, _mi2.rcMonitor.top
            mr, mb = _mi2.rcMonitor.right, _mi2.rcMonitor.bottom
            img = ImageGrab.grab(bbox=(ml, mt, mr, mb), all_screens=True)
            ix, iy = x - ml, y - mt
        else:
            img = ImageGrab.grab(all_screens=True)
            ix = x - u2.GetSystemMetrics(76)
            iy = y - u2.GetSystemMetrics(77)

        iw, ih = img.size

        # ── Region crop (420×260 centred on click) ────────────────────
        rw, rh = 420, 260
        rl = max(0, ix - rw // 2); rt = max(0, iy - rh // 2)
        rr = min(iw, rl + rw);     rb = min(ih, rt + rh)
        region = img.crop((rl, rt, rr, rb))
        buf = io.BytesIO(); region.save(buf, format="JPEG", quality=80)
        region_b64 = base64.b64encode(buf.getvalue()).decode()

        # ── Crosshair annotation ───────────────────────────────────────
        draw = ImageDraw.Draw(img)
        r = 18
        draw.ellipse([ix-r, iy-r, ix+r, iy+r], outline="#ff3b30", width=3)
        draw.line([ix-40,  iy,     ix-r-1, iy],     fill="#ff3b30", width=2)
        draw.line([ix+r+1, iy,     ix+40,  iy],     fill="#ff3b30", width=2)
        draw.line([ix,     iy-40,  ix,     iy-r-1], fill="#ff3b30", width=2)
        draw.line([ix,     iy+r+1, ix,     iy+40],  fill="#ff3b30", width=2)

        # Full (capped at 1920×1200 to keep payload size reasonable)
        full = img.copy(); full.thumbnail((1920, 1200), Image.LANCZOS)
        buf = io.BytesIO(); full.save(buf, format="JPEG", quality=78)
        full_b64 = base64.b64encode(buf.getvalue()).decode()

        # Thumb (small preview)
        thumb = img.copy(); thumb.thumbnail((320, 200), Image.LANCZOS)
        buf = io.BytesIO(); thumb.save(buf, format="JPEG", quality=68)
        thumb_b64 = base64.b64encode(buf.getvalue()).decode()

        return thumb_b64, full_b64, region_b64
    except Exception:
        return None, None, None


# ── Recorder ──────────────────────────────────────────────────────────────────

class Recorder:
    def __init__(self, on_step=None, on_step_update=None, on_browser_hint=None):
        self.on_step          = on_step
        self.on_step_update   = on_step_update
        self.on_browser_hint  = on_browser_hint   # called once if browser UIA falls back to win32
        self._steps           = []
        self._lock            = threading.Lock()
        self._recording       = False
        self._paused          = False
        self._start_time      = None
        self._mouse_listener  = None
        self._kb_listener     = None
        self._pending_text    = ""
        self._text_start_ts   = None
        self._modifiers       = set()
        self._browser_hint_sent = False   # fire at most once per recording session

    # ── Public ────────────────────────────────────────────────────────

    def start(self):
        self._steps              = []
        self._pending_text       = ""
        self._modifiers          = set()
        self._paused             = False
        self._browser_hint_sent  = False
        self._start_time         = time.time()
        self._recording          = True
        self._mouse_listener = mouse.Listener(
            on_click=self._on_click, on_scroll=self._on_scroll, suppress=False)
        self._kb_listener = keyboard.Listener(
            on_press=self._on_key_press, on_release=self._on_key_release, suppress=False)
        self._mouse_listener.start()
        self._kb_listener.start()

    def pause(self):
        self._paused = True

    def resume(self):
        self._paused = False

    def stop(self):
        self._recording = False
        self._paused    = False
        self._flush_text()
        ml, kl = self._mouse_listener, self._kb_listener
        self._mouse_listener = None
        self._kb_listener    = None
        if ml: threading.Thread(target=ml.stop, daemon=True).start()
        if kl: threading.Thread(target=kl.stop, daemon=True).start()
        with self._lock:
            return list(self._steps)

    def get_steps(self):
        with self._lock:
            return list(self._steps)

    # ── Internals ─────────────────────────────────────────────────────

    def _elapsed(self):
        return round(time.time() - self._start_time, 3)

    def _flush_text(self):
        if self._pending_text:
            self._add_step({
                "type":      "type",
                "timestamp": self._text_start_ts,
                "data":      {"text": self._pending_text},
            })
            self._pending_text  = ""
            self._text_start_ts = None

    def _add_step(self, step):
        with self._lock:
            step["id"] = len(self._steps)
            self._steps.append(step)
        if self.on_step:
            try: self.on_step(step)
            except Exception: pass
        return step["id"]

    def _push_update(self, idx):
        with self._lock:
            if idx >= len(self._steps):
                return
            step = dict(self._steps[idx])
        if self.on_step_update:
            try: self.on_step_update(idx, step)
            except Exception: pass

    def _enrich_click(self, idx, x, y):
        """
        Phase 1 — Screenshot (~150 ms): capture + emit so thumbnail appears immediately.
        Phase 2 — Element (~0–1.5 s): UIA with timeout; win32 + browser URL fallback.
        Phase 3 — After screenshot (~600 ms post-click): captures menus / dropdowns
                   that open as a result of the click.
        """
        time.sleep(0.1)

        # ── Phase 1: screenshot ───────────────────────────────────────
        # Capture post-click full screenshot and thumbnail for documentation.
        # screenshot_region was already captured synchronously in _on_click
        # (pre-click, with the dropdown still open) — do NOT overwrite it here
        # so image-based playback can locate the item at its original position.
        thumb, full, _region_post = _capture_screenshot(x, y)
        with self._lock:
            if idx < len(self._steps):
                d = self._steps[idx]["data"]
                d["screenshot"]      = thumb
                d["screenshot_full"] = full
                # Preserve pre-click region; fall back to post-click if sync grab failed
                if not d.get("screenshot_region"):
                    d["screenshot_region"] = _region_post
        self._push_update(idx)

        # ── Phase 2: element detection ───────────────────────────────
        win_info   = _win32_window_at(x, y)
        is_browser = _looks_like_browser(win_info)

        if not self._recording:
            element = win_info
        elif is_browser:
            # Chrome without --force-renderer-accessibility returns useless
            # NativeViewHost containers from UIA.  Win32 gives us window + rel
            # coords (all the player needs) without a 1.5 s timeout penalty.
            element = win_info
        else:
            element = _get_element_timed(x, y, timeout=1.5)
            if element is None:
                element = win_info

        # If UIA found the element but the window-walk missed (Chrome web tree has no
        # WindowControl ancestor), fill window info from the reliable win32 path.
        if (element is not None
                and element.get("source") == "uia"
                and element.get("window_rect") is None
                and win_info and win_info.get("window_rect")):
            element = dict(element)
            element["window"]      = element.get("window") or win_info.get("window") or ""
            element["window_rect"] = win_info["window_rect"]
            element["hwnd"]        = win_info.get("hwnd")
            element["rel_x"]       = x - win_info["window_rect"]["left"]
            element["rel_y"]       = y - win_info["window_rect"]["top"]

        # Clean up generic/useless element names from Chrome's non-accessibility tree
        if element and element.get("name") in ("NativeViewHost", "Chrome_RenderWidgetHostHWND", ""):
            element = dict(element)
            element["name"] = ""   # blank is better than a confusing class name

        # For browser windows: enrich with URL via OmniboxView (fast, no flags needed).
        # Captured on every browser step so the current page URL is always stored.
        if is_browser and element is not None:
            window_title = win_info.get("window", "") if win_info else ""
            # Primary: OmniboxView WM_GETTEXT (< 5 ms, no special Chrome flags)
            url = ""
            try:
                u3 = ctypes.windll.user32
                results3 = []
                @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_size_t, ctypes.c_ssize_t)
                def _cb3(hwnd, _lp):
                    b3 = ctypes.create_unicode_buffer(512)
                    u3.GetWindowTextW(hwnd, b3, 512)
                    if b3.value == window_title:
                        results3.append(hwnd)
                    return True
                u3.EnumWindows(_cb3, 0)
                WM_GETTEXT = 0x000D
                for _h in results3:
                    omnibox = u3.FindWindowExW(_h, None, "Chrome_OmniboxView", None)
                    if omnibox:
                        buf3 = ctypes.create_unicode_buffer(2048)
                        n3   = u3.SendMessageW(omnibox, WM_GETTEXT, 2047, buf3)
                        txt3 = buf3.value if n3 > 0 else ""
                        if txt3 and ("." in txt3 or txt3.startswith(("http", "localhost"))):
                            url = txt3 if txt3.startswith("http") else "https://" + txt3
                            break
            except Exception:
                pass
            # Fallback: UIA address bar (works for Edge/Firefox too)
            if not url:
                url = _get_browser_url_timed(window_title, timeout=1.0) or ""
            if url:
                element = dict(element)
                element["url"] = url   # full, untruncated -- used for navigate() resolution
                # Only fall back to the URL as the *display* name when there's
                # truly nothing better -- and truncate it. This used to also
                # override any win32-sourced element unconditionally, which
                # became the dominant path once browser clicks stopped using
                # UIA (see _looks_like_browser): every single browser click
                # ended up displaying its full URL -- including long query
                # strings -- as its name, visually blowing out the step card
                # list. `url` (untruncated) is still stored separately above
                # for anything that needs the real address.
                if not element.get("name"):
                    element["name"] = url if len(url) <= 70 else url[:67] + "..."

            # Try CDP for richer DOM element info (id, aria-label, role, text, etc.)
            win_rect = win_info.get("window_rect") if win_info else None
            dom_el   = _get_dom_element_at(x, y, win_rect=win_rect)
            if dom_el:
                dom_name = (
                    dom_el.get("ariaLabel") or
                    dom_el.get("placeholder") or
                    dom_el.get("text") or
                    dom_el.get("id") or
                    dom_el.get("name") or ""
                )
                if dom_name:
                    element = dict(element)
                    if not element.get("name") or element.get("source") in ("win32", None):
                        element["name"] = dom_name[:100]
                    element["source"] = "cdp"
                    element["cdp"] = {
                        "tag":         dom_el.get("tag"),
                        "id":          dom_el.get("id"),
                        "class":       dom_el.get("cls"),
                        "role":        dom_el.get("role"),
                        "aria_label":  dom_el.get("ariaLabel"),
                        "href":        dom_el.get("href"),
                    }

            # Browser hint suppressed: window+rel playback works without Chrome
            # accessibility.  Force-set the flag so the hint is never sent.
            self._browser_hint_sent = True

        with self._lock:
            if idx < len(self._steps):
                self._steps[idx]["data"]["element"] = element
        self._push_update(idx)

        # ── URL-after: 1 s post-click the page has likely loaded.  Read the
        # address bar of whichever Chrome window is now at (x, y).  This URL
        # is used by _condense_steps to build navigate() steps even when the
        # click happened in "New Tab" (the window title changes after nav).
        time.sleep(0.6)
        if is_browser:
            try:
                post_win = _win32_window_at(x, y)
                post_hwnd = None
                if post_win:
                    pw_title = post_win.get("window", "")
                    if pw_title:
                        u2 = ctypes.windll.user32
                        @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_size_t, ctypes.c_ssize_t)
                        def _cb2(hwnd, _lp):
                            b2 = wintypes.create_unicode_buffer(512) if hasattr(wintypes, 'create_unicode_buffer') else ctypes.create_unicode_buffer(512)
                            u2.GetWindowTextW(hwnd, b2, 512)
                            if b2.value == pw_title:
                                post_hwnd_list.append(hwnd)
                            return True
                        post_hwnd_list = []
                        u2.EnumWindows(_cb2, 0)
                        post_hwnd = post_hwnd_list[0] if post_hwnd_list else None
                if post_hwnd:
                    url_after = _get_chrome_url_by_hwnd(post_hwnd)
                    if url_after:
                        with self._lock:
                            if idx < len(self._steps):
                                self._steps[idx]["data"]["url_after"] = url_after
                        self._push_update(idx)
            except Exception:
                pass

        # ── Phase 3: after-screenshot (captures menus / dropdowns) ───
        # ~500 ms after element detection is done, capture a second region crop.
        time.sleep(0.5)
        if self._recording:
            try:
                region_after = _grab_region(x, y, 420, 260)
                buf = io.BytesIO()
                region_after.save(buf, format="JPEG", quality=80)
                after_b64 = base64.b64encode(buf.getvalue()).decode()
                with self._lock:
                    if idx < len(self._steps):
                        self._steps[idx]["data"]["screenshot_after"] = after_b64
                self._push_update(idx)
            except Exception:
                pass

    def _on_click(self, x, y, button, pressed):
        if not self._recording or self._paused or not pressed:
            return
        # Filter: skip clicks on any AutoFlow window (toolbar, overlay, etc.).
        # Title check catches the browser tab ("AutoFlow - Google Chrome").
        # PID check catches tkinter overlay / HUD windows that have no title.
        win = _win32_window_at(x, y)
        if win and "autoflow" in (win.get("window") or "").lower():
            return
        if _is_own_process_window(x, y):
            return
        self._flush_text()

        # ── Phase 0: synchronous pre-click region capture ────────────────
        # WH_MOUSE_LL fires BEFORE the click is dispatched to the target window,
        # so dropdowns and menus are still visible RIGHT NOW.  ImageGrab takes
        # ~20-50 ms — well within the Windows hook-timeout limit.
        # This clean (no crosshair) region is stored as screenshot_region so the
        # player can use image matching to locate the item regardless of position.
        pre_region = None
        try:
            _region = _grab_region(x, y, 420, 260)
            _buf = io.BytesIO()
            _region.save(_buf, format="JPEG", quality=84)
            pre_region = base64.b64encode(_buf.getvalue()).decode()
        except Exception:
            pass

        step = {
            "type":      "click",
            "timestamp": self._elapsed(),
            "data": {
                "x": x, "y": y,
                "button":              button.name,
                "element":             None,
                "screenshot":          None,
                "screenshot_full":     None,
                "screenshot_region":   pre_region,   # pre-click: dropdown/menu visible
                "screenshot_after":    None,
            },
        }
        idx = self._add_step(step)
        threading.Thread(target=self._enrich_click, args=(idx, x, y), daemon=True).start()

    def _on_scroll(self, x, y, dx, dy):
        if not self._recording or self._paused:
            return
        win = _win32_window_at(x, y)
        if win and "autoflow" in (win.get("window") or "").lower():
            return
        self._flush_text()
        self._add_step({
            "type":      "scroll",
            "timestamp": self._elapsed(),
            "data":      {"x": x, "y": y, "dx": dx, "dy": dy},
        })

    def _on_key_press(self, key):
        if not self._recording or self._paused:
            return
        if key in _MODIFIER_KEYS:
            self._modifiers.add(key); return

        active_mods = {k for k in self._modifiers
                       if k not in (keyboard.Key.shift, keyboard.Key.shift_l, keyboard.Key.shift_r)}
        if active_mods:
            self._flush_text()
            parts = []
            if any(k in self._modifiers for k in (keyboard.Key.ctrl,  keyboard.Key.ctrl_l,  keyboard.Key.ctrl_r)):  parts.append("ctrl")
            if any(k in self._modifiers for k in (keyboard.Key.alt,   keyboard.Key.alt_l,   keyboard.Key.alt_r)):   parts.append("alt")
            if any(k in self._modifiers for k in (keyboard.Key.shift, keyboard.Key.shift_l, keyboard.Key.shift_r)): parts.append("shift")
            if any(k in self._modifiers for k in (keyboard.Key.cmd,   keyboard.Key.cmd_l,   keyboard.Key.cmd_r)):   parts.append("win")
            try:
                k = key.char if hasattr(key, "char") and key.char else key.name
            except AttributeError:
                k = str(key)
            parts.append(k)
            self._add_step({"type":"hotkey","timestamp":self._elapsed(),"data":{"combo":"+".join(parts)}})
            return

        if key in _SPECIAL_KEYS:
            self._flush_text()
            self._add_step({"type":"hotkey","timestamp":self._elapsed(),"data":{"combo":key.name}})
            return

        try:
            char = key.char
            if char:
                if not self._pending_text:
                    self._text_start_ts = self._elapsed()
                self._pending_text += char
        except AttributeError:
            pass

    def _on_key_release(self, key):
        self._modifiers.discard(key)
