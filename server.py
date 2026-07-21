"""
server.py — AutoFlow web server.
Writes autoflow.log next to the EXE for crash diagnosis.
Shows a Windows error dialog on fatal startup failures.
"""
import os, sys, json, time, threading, webbrowser, base64, io, logging, re, shutil

# ── Paths ──────────────────────────────────────────────────────────────
if getattr(sys, 'frozen', False):
    EXE_DIR    = os.path.dirname(sys.executable)
    BUNDLE_DIR = sys._MEIPASS
else:
    EXE_DIR    = os.path.dirname(os.path.abspath(__file__))
    BUNDLE_DIR = EXE_DIR

sys.path.insert(0, BUNDLE_DIR)

STATIC_DIR    = os.path.join(BUNDLE_DIR, "static")
WORKFLOWS_DIR = os.path.join(EXE_DIR,    "workflows")
os.makedirs(WORKFLOWS_DIR, exist_ok=True)

# ── Logging ────────────────────────────────────────────────────────────
LOG_FILE = os.path.join(EXE_DIR, "autoflow.log")
logging.basicConfig(
    filename=LOG_FILE, level=logging.DEBUG,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("autoflow")
log.info("=== AutoFlow starting ===")

# ── Single-instance guard ────────────────────────────────────────────────
# Prevent stacking multiple AutoFlow.exe processes. On Windows, a named
# mutex is the simplest reliable method.
try:
    import ctypes as _ct
    _MUTEX_NAME = "AutoFlowSingleInstance_v1"
    _mutex = _ct.windll.kernel32.CreateMutexW(None, True, _MUTEX_NAME)
    if _ct.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        _ct.windll.user32.MessageBoxW(
            0,
            "AutoFlow is already running.\n\nCheck the system tray.",
            "AutoFlow",
            0x40,  # MB_ICONINFORMATION
        )
        sys.exit(0)
except Exception as _me:
    log.warning("single-instance check failed: %s", _me)

log.info("EXE_DIR=%s  BUNDLE_DIR=%s", EXE_DIR, BUNDLE_DIR)
log.info("STATIC_DIR=%s  exists=%s", STATIC_DIR, os.path.isdir(STATIC_DIR))

def _fatal(msg):
    log.error("FATAL: %s", msg)
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, str(msg), "AutoFlow — Startup Error", 0x10)
    except Exception:
        pass
    sys.exit(1)

# ── App-module imports ─────────────────────────────────────────────────
try:
    from recorder import Recorder
    from player   import Player
    log.info("recorder / player imported OK")
except Exception as e:
    _fatal(f"Could not load recorder/player:\n{e}\n\nCheck autoflow.log")

# ── Pre-init UIA so DPI context is stable before overlay creates its Win32 window ──
# Without this, the first dim show during recording uses wrong (1707x1067) logical-pixel
# dimensions because UIA hasn't yet switched the process to physical-pixel DPI awareness.
try:
    import uiautomation as _uia_preload
    del _uia_preload
    log.info('uiautomation pre-init OK -- DPI context stable')
except Exception as _uia_e:
    log.warning('uiautomation pre-init skipped: %s', _uia_e)

# ── Overlay native (optional — graceful fallback if ctypes fails) ──────
try:
    import overlay_native as _ov
    log.info("overlay_native loaded OK")
except Exception as e:
    _ov = None
    log.warning("overlay_native unavailable: %s", e)

def _ov_show(state=None):
    if _ov:
        try: _ov.show(state)
        except Exception as e: log.warning("ov_show: %s", e)

def _ov_hide():
    if _ov:
        try: _ov.hide()
        except Exception as e: log.warning("ov_hide: %s", e)

# ── Flask / SocketIO ───────────────────────────────────────────────────
try:
    from flask          import Flask, jsonify, request, send_from_directory, Response
    from flask_socketio import SocketIO
    log.info("Flask / SocketIO imported OK")
except Exception as e:
    _fatal(f"Could not load Flask/SocketIO:\n{e}\n\nCheck autoflow.log")

app = Flask(__name__, static_folder=STATIC_DIR)
app.config["SECRET_KEY"] = "autoflow-2025"
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

_recorder    = None
_player      = None
_connected_clients = 0   # live Socket.IO connections; exit when it stays 0
_last_steps  = []   # most recently played workflow; allows overlay Play to replay
PORT      = 5000

# App settings (persisted next to the EXE)
# Settings stored in memory only — persisted client-side via localStorage
_settings = {"screenshot_mode": "all"}


# ── Static ─────────────────────────────────────────────────────────────
@app.route("/")
def index():
    r = send_from_directory(STATIC_DIR, "index.html")
    r.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return r

@app.route("/<path:fn>")
def static_file(fn):
    r = send_from_directory(STATIC_DIR, fn)
    if fn.endswith((".js", ".css", ".html")):
        r.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return r

# ── Quit ───────────────────────────────────────────────────────────────
@app.route("/api/quit", methods=["POST"])
def quit_app():
    def _exit():
        time.sleep(0.3)
        try:
            if _recorder and _recorder._recording:
                _recorder.stop()
        except Exception: pass
        try:
            if _player:
                _player.stop()
        except Exception: pass
        try: _ov_hide()
        except Exception: pass
        log.info("Quit requested -- clean exit")
        os._exit(0)
    threading.Thread(target=_exit, daemon=True).start()
    return jsonify({"ok": True})

# ── Recording helpers ─────────────────────────────────────────────────
_WAIT_THRESHOLD_MS = 400
_WAIT_MIN_MS       = 200

def _inject_waits(steps):
    result = []
    for i, step in enumerate(steps):
        if i > 0:
            prev    = steps[i - 1]
            gap_ms  = max(0, int((step.get("timestamp", 0) - prev.get("timestamp", 0)) * 1000))
            if gap_ms >= _WAIT_THRESHOLD_MS:
                rounded = max(_WAIT_MIN_MS, round(gap_ms / 100) * 100)
                result.append({
                    "id":        -1,
                    "type":      "wait",
                    "timestamp": prev.get("timestamp", 0),
                    "data":      {"ms": rounded},
                })
        result.append(step)
    for idx, s in enumerate(result):
        s["id"] = idx
    return result

def _condense_steps(steps):
    """
    Post-process recorded steps to condense navigation and app-open patterns.

    Rules applied in order:
    1. Link click  — click where cdp.href is non-empty → replace with navigate(href).
       Covers bookmark clicks, hyperlinks, any anchor-tag click CDP detected.
    2. New-tab nav — click in a "New Tab" browser window followed by a step in a
       DIFFERENT browser page that has a known URL → replace the click with navigate(url).
    3. Duplicate waits — consecutive wait steps → keep only the longer one.

    Returns the condensed list with IDs renumbered.
    A socketio 'steps_condensed' event is emitted if any steps were folded.
    """
    _BROWSER_CLASSES = {"Chrome_WidgetWin_1", "MozillaWindowClass", "IEFrame"}
    _BROWSER_TITLE_SUFFIXES = (" - google chrome", " - microsoft edge", " - mozilla firefox")
    _NEW_TAB_KW      = {"new tab", "newtab"}

    def _is_browser(el):
        """Win32-class check catches frame/toolbar clicks; most in-page clicks
        land on an inner render-surface HWND with some other class, so also
        fall back to a window-title suffix sniff (works for win32- AND
        UIA-sourced elements alike, since both carry a "window" title)."""
        el = el or {}
        if el.get("class", "") in _BROWSER_CLASSES:
            return True
        return el.get("window", "").lower().endswith(_BROWSER_TITLE_SUFFIXES)

    def _is_newtab(el):
        win = (el or {}).get("window", "").lower()
        return any(kw in win for kw in _NEW_TAB_KW)

    def _best_url(el):
        """Best URL we have from element data: url_after > url > cdp.href."""
        if not el:
            return ""
        # url_after: captured ~1s post-click when the page has loaded
        url = el.get("url_after", "") or el.get("url", "")
        if url:
            return url
        href = (el.get("cdp") or {}).get("href", "")
        if href and href.startswith("http"):
            return href
        return ""

    def _omnibox_url_for_window(win_title):
        """Read URL directly from a still-open Chrome window by title (OmniboxView)."""
        try:
            import ctypes
            u = ctypes.windll.user32
            results = []
            @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_size_t, ctypes.c_ssize_t)
            def _cb(hwnd, _lp):
                b = ctypes.create_unicode_buffer(512)
                u.GetWindowTextW(hwnd, b, 512)
                if b.value == win_title:
                    results.append(hwnd)
                return True
            u.EnumWindows(_cb, 0)
            WM_GETTEXT = 0x000D
            for hwnd in results:
                omnibox = u.FindWindowExW(hwnd, None, "Chrome_OmniboxView", None)
                if omnibox:
                    buf = ctypes.create_unicode_buffer(2048)
                    n   = u.SendMessageW(omnibox, WM_GETTEXT, 2047, buf)
                    txt = buf.value if n > 0 else ""
                    if txt and ("." in txt or txt.startswith(("http", "localhost"))):
                        return txt if txt.startswith("http") else "https://" + txt
        except Exception:
            pass
        return ""

    def _cdp_url_for_window(win_title):
        """Query Chrome's remote-debugging endpoint (localhost:9222/json) for the
        URL of a page whose title matches win_title. Requires Chrome to have been
        launched with --remote-debugging-port=9222; silently returns "" otherwise."""
        try:
            import urllib.request, json as _json
            tabs = _json.loads(
                urllib.request.urlopen("http://localhost:9222/json", timeout=0.5).read()
            )
            needle = win_title.split(" - Google Chrome")[0].lower()
            page = next(
                (t for t in tabs if t.get("type") == "page"
                 and needle in t.get("title", "").lower()),
                None,
            )
            return page.get("url", "") if page else ""
        except Exception:
            return ""

    def _normalize_url(url):
        """Chrome's omnibox display (and the UIA address-bar fallback that
        reads it) hides the https:// scheme by default -- the address bar
        shows "example.com/path" not "https://example.com/path". A bare
        navigate() step handed that string would fail (webbrowser.open can't
        resolve a schemeless host). Add https:// whenever no scheme is present."""
        url = (url or "").strip()
        if not url:
            return url
        if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", url) or url.startswith("file:"):
            return url
        if url.startswith("localhost") or "." in url.split("/", 1)[0]:
            return "https://" + url
        return url

    def _resolve_nav_url(real_el, real_win):
        """Best-effort URL resolution for a "real page" element, tried in order:
        1. url_after / url / cdp.href already captured on the element.
        2. OmniboxView of the still-open Chrome window (fast, exact).
        3. UIA address bar (works even without the OmniboxView path).
        4. CDP :9222/json (only works if Chrome was launched with the flag).
        Always normalized to include a URL scheme before returning."""
        url = _best_url(real_el)
        if not url:
            url = _omnibox_url_for_window(real_win)
        if not url:
            try:
                from recorder import _get_browser_url_timed
                url = _get_browser_url_timed(real_win, timeout=1.5) or ""
            except Exception:
                pass
        if not url:
            url = _cdp_url_for_window(real_win)
        return _normalize_url(url)

    # Generic "clicked the page, not a specific widget" classes.  Bookmark /
    # link clicks that trigger a navigation land UIA/win32 detection on the
    # whole-page container (huge bounding rect, no automation_id, no real
    # name) because the actual click target -- the bookmark, the link -- gets
    # replaced by the new page before we can resolve it. Real interactions
    # (typing into a field, opening a dropdown, clicking a chart) always hit a
    # small, specific, named control instead.
    # Common apps we can confidently re-launch by name if a recording's
    # "getting there" preamble turns out to be opening one of them. Matched by
    # a lowercase substring of the window title -- keep this list conservative
    # (only apps whose bare .exe name reliably resolves via PATH).
    _APP_LAUNCH_MAP = {
        # Longer / more-specific tokens must sort before shorter ones in
        # _guess_launch_path so "notepad++" beats "notepad".
        "notepad++": "notepad++.exe",
        "notepad": "notepad.exe", "wordpad": "write.exe",
        "calculator": "calc.exe", "paint": "mspaint.exe",
        "excel": "excel.exe", "word": "winword.exe", "powerpoint": "powerpnt.exe",
        "outlook": "outlook.exe",
    }
    # Common install-directory probe list for apps not guaranteed on PATH.
    _APP_PROBE_PATHS = {
        "notepad++": [
            (r"C:\Program Files\Notepad++",       "notepad++.exe"),
            (r"C:\Program Files (x86)\Notepad++", "notepad++.exe"),
        ],
    }

    def _guess_launch_path(window_title):
        w = (window_title or "").lower()
        # Sort longest token first so "notepad++" wins over "notepad"
        for token, exe in sorted(_APP_LAUNCH_MAP.items(), key=lambda kv: -len(kv[0])):
            if token in w:
                if shutil.which(exe):
                    return exe   # on PATH or registered in App Paths registry
                for d, fname in _APP_PROBE_PATHS.get(token, []):
                    full = os.path.join(d, fname)
                    if os.path.isfile(full):
                        return full
                return exe       # best-effort: let os.startfile try App Paths anyway
        return ""

    def _strip_dirty(title):
        """Strip a leading '*' (unsaved-changes marker) from a window title.
        Many editors prepend '*' while a file has unsaved edits; the asterisk
        disappears on a fresh open or after saving, so wait_for_window must use
        the clean title to match reliably at playback time."""
        t = title or ""
        return t[1:] if t.startswith("*") else t

    def _same_target(el_a, el_b):
        """True if two click elements represent 'the same place' for
        navigation-chain purposes. Browser pages are compared by window TITLE
        (a page change updates the title but not the OS window itself, and for
        chained navigations across pages the title IS the page identity).
        Desktop apps: use HWND if present (new recordings; unique per window
        instance within a session), else rect+title (old recordings -- require
        BOTH to match to avoid false positives from two different maximised apps
        on the same monitor sharing an identical screen rect).
        A browser window and a native app are always different targets."""
        el_a, el_b = el_a or {}, el_b or {}
        a_browser = _is_browser(el_a)
        b_browser = _is_browser(el_b)
        # A browser tab and a native-app window are never the same target.
        # This prevents a maximised native window and a maximised browser sharing
        # an identical window_rect from being wrongly treated as "settled."
        if a_browser != b_browser:
            return False
        if a_browser:  # both browser: page identity = window title
            return bool(el_a.get("window")) and el_a.get("window") == el_b.get("window")
        # Both non-browser: HWND (new recordings) → rect+title (old) → title only
        ha, hb = el_a.get("hwnd"), el_b.get("hwnd")
        if ha and hb:
            return ha == hb
        ra, rb = el_a.get("window_rect"), el_b.get("window_rect")
        if ra and rb:
            # Require title match too: two maximised apps on the same monitor
            # have identical rects but are definitely different windows.
            return ra == rb and el_a.get("window") == el_b.get("window")
        return bool(el_a.get("window")) and el_a.get("window") == el_b.get("window")

    def _condense_workflow_start(steps):
        """
        Power-Automate-style start condensing, generalized to ANY application
        or website -- not just "click New Tab -> click a bookmark". The idea:
        a recording's opening moves are very often just "get to the place I
        actually want to automate" (open the Start menu, search, click the
        app; or click through a homepage to the dashboard you actually care
        about) -- and none of that should have to be replayed faithfully, since
        it's fragile (depends on search-box position, taskbar layout, bookmark
        bar, intermediate redirect timing...) and it's not what the user
        intended to automate in the first place.

        Algorithm: walk the leading CLICK steps (skipping over `type`/`wait`/
        etc. steps in between, which carry no window info of their own and are
        judged by whatever click they're adjacent to) and look for the first
        point where two consecutive clicks land on "the same place" per
        `_same_target()` -- that's the signal that the chain of hops has
        settled and real, repeatable interaction has begun. Everything before
        that point is preamble and gets collapsed into a single wait_for_window
        (plus a navigate() for browser destinations, or a best-effort app
        launch for a few common desktop apps) so the workflow simply *starts*
        at the real destination instead of clicking its way there.

        If a starting point is already given (i.e. the very first click is
        already "the same place" as the very next one -- nothing to fold), or
        no stable landing point is found within the lookahead window, the
        recording is left completely untouched. Returns (new_steps, folded_count).
        """
        MAX_LOOKAHEAD = 12

        # Build (step_index, element) for the leading run of click steps only,
        # bounded by MAX_LOOKAHEAD click steps (not MAX_LOOKAHEAD steps total,
        # so intervening type/wait steps don't eat into the budget).
        clicks = []
        for idx, s in enumerate(steps):
            if s.get("type") == "click":
                el = (s.get("data") or {}).get("element") or {}
                if not el.get("window"):
                    break   # can't reason about an untargeted click -- stop looking
                clicks.append((idx, el))
                if len(clicks) >= MAX_LOOKAHEAD:
                    break
            elif s.get("type") in ("type", "hotkey", "wait", "scroll", "comment"):
                continue   # no window info of its own -- skip over, keep scanning
            else:
                break       # anything else (loop/if/etc.) -- stop, don't guess

        if len(clicks) < 2:
            return steps, 0   # nothing to compare against -- leave alone

        real_pos = None   # index into `clicks`, not into `steps`
        for k in range(len(clicks) - 1):
            if _same_target(clicks[k][1], clicks[k + 1][1]):
                # New Tab is a transient browser state (address-bar entry point),
                # not a real automation destination.  Two consecutive New Tab
                # clicks would trigger a false "already settled" at k=0 and
                # prevent the scan from reaching the actual landing page.
                if _is_newtab(clicks[k][1]) and _is_newtab(clicks[k + 1][1]):
                    continue
                real_pos = k
                break

        # Fallback: no same-target pair found, but the click chain ends with a
        # single click into a *new* window followed only by non-click steps
        # (type/wait etc.).  That pattern means the user navigated somewhere and
        # then just typed -- the last click IS the settling point and everything
        # before it is navigation preamble.
        if real_pos is None and len(clicks) >= 2:
            last_step_idx = clicks[-1][0]
            post_clicks = [s for s in steps[last_step_idx + 1:] if s.get("type") == "click"]
            if not post_clicks:
                real_pos = len(clicks) - 1

        if real_pos is None or real_pos == 0:
            return steps, 0   # either never settles, or already starts settled

        real_idx = clicks[real_pos][0]     # step index of the first real action
        real_el  = clicks[real_pos][1]
        real_win = real_el.get("window", "")

        replacement = []
        if _is_browser(real_el):
            url = _resolve_nav_url(real_el, real_win)
            if not url:
                log.info("condense: workflow-start preamble detected but no URL "
                          "could be resolved -- leaving preamble untouched")
                return steps, 0
            replacement.append({
                "id": steps[0].get("id", 0), "type": "navigate",
                "timestamp": steps[0].get("timestamp", 0), "data": {"url": url},
            })
        else:
            launch_path = _guess_launch_path(real_win)
            if launch_path:
                replacement.append({
                    "id": steps[0].get("id", 0), "type": "open_file",
                    "timestamp": steps[0].get("timestamp", 0), "data": {"path": launch_path},
                })
        replacement.append({
            "id": steps[0].get("id", 0), "type": "wait_for_window",
            "timestamp": steps[0].get("timestamp", 0),
            "data": {"title": _strip_dirty(real_win), "timeout_ms": 8000},
        })

        log.info("condense: workflow-start preamble (%d steps) -> %s + wait_for_window(%s)",
                 real_idx, " + ".join(r["type"] for r in replacement[:-1]) or "(assume already open)",
                 real_win)
        folded = max(0, real_idx - len(replacement))
        return replacement + steps[real_idx:], folded

    # ── Pre-filter: drop self-referential clicks on the AutoFlow UI ──────────
    # When the user clicks the AutoFlow browser tab (or any AutoFlow window) to
    # stop recording, that click sometimes slips through the recorder's title
    # filter (click lands on an inner Chrome render HWND whose root title isn't
    # resolved at click time).  Remove such steps here so they never corrupt
    # chain-detection comparisons or appear in the output workflow.
    def _is_autoflow_self_click(step):
        if step.get("type") != "click":
            return False
        el  = (step.get("data") or {}).get("element") or {}
        if "autoflow" in (el.get("window") or "").lower():
            return True
        url = (el.get("url") or el.get("url_after") or "").lower()
        return url.startswith(("localhost", "127.0.0.1",
                                "http://localhost", "http://127.0.0.1"))

    _pre_len = len(steps)
    steps = [s for s in steps if not _is_autoflow_self_click(s)]
    if len(steps) < _pre_len:
        log.info("condense: dropped %d self-referential AutoFlow click(s)",
                 _pre_len - len(steps))

    steps, n_folded = _condense_workflow_start(steps)

    condensed = []
    i = 0

    while i < len(steps):
        step = steps[i]
        t = step.get("type", "")
        d = step.get("data", {})
        el = d.get("element") or {}

        if t == "click":
            # Rule 1a: CDP captured an href → this is a navigating link click
            href = (el.get("cdp") or {}).get("href", "")
            if href and href.startswith("http"):
                condensed.append({
                    "id":        step["id"],
                    "type":      "navigate",
                    "timestamp": step.get("timestamp", 0),
                    "data":      {"url": href},
                })
                log.info("condense: step %d link-click → navigate(%s…)", step["id"], href[:60])
                n_folded += 1
                i += 1
                continue

            # Rule 2: click in a New-Tab browser window; next step is a different page.
            # Resolve the URL (element data -> OmniboxView -> UIA -> CDP) and
            # replace the click with navigate() + wait_for_window() so the
            # following step never fires before the destination page exists.
            if _is_browser(el) and _is_newtab(el) and i + 1 < len(steps):
                nxt    = steps[i + 1]
                nxt_el = nxt.get("data", {}).get("element") or {}
                nxt_win = nxt_el.get("window", "")
                if (_is_browser(nxt_el)
                        and not _is_newtab(nxt_el)
                        and nxt_win):
                    nxt_url = _resolve_nav_url(nxt_el, nxt_win)
                    if nxt_url:
                        condensed.append({
                            "id":        step["id"],
                            "type":      "navigate",
                            "timestamp": step.get("timestamp", 0),
                            "data":      {"url": nxt_url},
                        })
                        condensed.append({
                            "id":        step["id"],
                            "type":      "wait_for_window",
                            "timestamp": step.get("timestamp", 0),
                            "data":      {"title": nxt_win, "timeout_ms": 8000},
                        })
                        log.info("condense: step %d new-tab click → navigate(%s…) + wait_for_window(%s)",
                                 step["id"], nxt_url[:60], nxt_win)
                        n_folded += 1
                        i += 1
                        continue

        # Rule 3: consecutive wait steps → keep only the longer duration
        if (t == "wait"
                and condensed
                and condensed[-1].get("type") == "wait"):
            prev_ms = condensed[-1]["data"].get("ms", 0)
            this_ms = d.get("ms", 0)
            if this_ms > prev_ms:
                condensed[-1]["data"]["ms"] = this_ms
            log.debug("condense: merged back-to-back wait steps → %d ms",
                      condensed[-1]["data"]["ms"])
            n_folded += 1
            i += 1
            continue

        # Rule 4: consecutive scroll steps at approximately the same position
        #   → merge into a single scroll with summed delta.
        #   "Approximately same" = within 50 px on both axes.
        if (t == "scroll"
                and condensed
                and condensed[-1].get("type") == "scroll"):
            prev_d = condensed[-1]["data"]
            if (abs(d.get("x", 0) - prev_d.get("x", 0)) <= 50
                    and abs(d.get("y", 0) - prev_d.get("y", 0)) <= 50):
                prev_d["dy"] = prev_d.get("dy", 0) + d.get("dy", 0)
                prev_d["dx"] = prev_d.get("dx", 0) + d.get("dx", 0)
                n_folded += 1
                i += 1
                continue

        condensed.append(step)
        i += 1

    # Rule 5: post-pass — drop scroll steps that are purely navigational.
    #   A scroll is "navigational" when the very next non-scroll step is a
    #   click within 150 px (user was scrolling a list to reach an item, then
    #   clicked it).  The scroll direction is stored as scroll_hint_dy on the
    #   click so the player can scroll-and-retry if image matching needs it.
    _out5 = []
    _j = 0
    while _j < len(condensed):
        _s = condensed[_j]
        if _s.get("type") == "scroll":
            # Peek ahead past any remaining scroll steps to find the next action
            _nxt = next(
                (_x for _x in condensed[_j + 1:] if _x.get("type") not in ("scroll",)),
                None,
            )
            if _nxt and _nxt.get("type") == "click":
                _sd = _s.get("data", {})
                _nd = _nxt.get("data", {})
                _sx, _sy = _sd.get("x", 0), _sd.get("y", 0)
                _cx2, _cy2 = _nd.get("x", 0), _nd.get("y", 0)
                if abs(_sx - _cx2) <= 150 and abs(_sy - _cy2) <= 150:
                    # Navigational scroll — drop it, annotate the click
                    # Accumulate: multiple scroll steps before the same click
                    # must all be summed, not last-one-wins.
                    _prev_hint = _nxt.setdefault("data", {}).get("scroll_hint_dy", 0)
                    _nxt["data"]["scroll_hint_dy"] = _prev_hint + _sd.get("dy", 0)
                    log.info(
                        "condense: step %d scroll(dy=%d) before click(%.0f,%.0f)"
                        " → dropped (navigational; hint stored on click)",
                        _s.get("id", _j), _sd.get("dy", 0), _cx2, _cy2,
                    )
                    n_folded += 1
                    _j += 1
                    continue
        _out5.append(_s)
        _j += 1
    condensed = _out5

    # Re-number IDs sequentially
    for idx, s in enumerate(condensed):
        s["id"] = idx

    if n_folded:
        log.info("condense: %d → %d steps (%d folded into simpler actions)",
                 len(steps), len(condensed), n_folded)
        try:
            socketio.emit("steps_condensed", {
                "original": len(steps),
                "condensed": len(condensed),
                "folded": n_folded,
            })
        except Exception:
            pass

    return condensed


# ── Overlay routes ─────────────────────────────────────────────────────
@app.route("/overlay")
def overlay():
    return send_from_directory(STATIC_DIR, "overlay.html")

@app.route("/api/overlay/show", methods=["POST"])
def overlay_show():
    _ov_show()
    return jsonify({"ok": True})

@app.route("/api/overlay/hide", methods=["POST"])
def overlay_hide():
    _ov_hide()
    return jsonify({"ok": True})

# ── State broadcast ─────────────────────────────────────────────────────
def _emit_state():
    """Broadcast current recorder/player state; also manages the dim overlay."""
    rec_state  = ("recording" if (_recorder and _recorder._recording and not _recorder._paused)
                  else "paused" if (_recorder and _recorder._recording and _recorder._paused)
                  else "idle")
    play_state = ("playing" if (_player and not _player.is_paused and not _player._stop_evt.is_set())
                  else "paused" if (_player and _player.is_paused and not _player._stop_evt.is_set())
                  else "idle")

    # Auto-manage dim overlay: show while active, hide when both idle
    if rec_state != "idle" or play_state != "idle":
        if   rec_state  == "recording": ov_state = "recording"
        elif rec_state  == "paused":    ov_state = "rec_paused"
        elif play_state == "playing":   ov_state = "playing"
        else:                           ov_state = "play_paused"
        _ov_show(state=ov_state)
    else:
        _ov_hide()

    socketio.emit("app_state", {"record": rec_state, "play": play_state})

# ── Recording ──────────────────────────────────────────────────────────
@app.route("/api/record/start", methods=["POST"])
def start_recording():
    global _recorder
    if _recorder and _recorder._recording:
        return jsonify({"ok": False, "error": "Already recording"})
    _recorder = Recorder(
        on_step=_push_step,
        on_step_update=_push_step_update,
        on_browser_hint=_push_browser_hint,
    )
    try:
        from recorder import set_screenshot_mode
        set_screenshot_mode(_settings.get("screenshot_mode", "all"))
    except Exception:
        pass
    _recorder.start()
    log.info("Recording started (mode=%s)", _settings.get("screenshot_mode", "all"))
    _emit_state()
    return jsonify({"ok": True})

def _sanitize(obj):
    """Recursively convert any non-JSON-primitive value to a safe string."""
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize(v) for v in obj]
    log.warning("sanitize: unexpected type %s in step data, converting to str", type(obj).__name__)
    try:
        return str(obj)
    except Exception:
        return ""

@app.route("/api/record/pause", methods=["POST"])
def pause_recording():
    if _recorder and _recorder._recording:
        _recorder.pause()
        _emit_state()
        log.info("Recording paused")
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "Not recording"})

@app.route("/api/record/resume", methods=["POST"])
def resume_recording():
    if _recorder and _recorder._recording:
        _recorder.resume()
        _emit_state()
        log.info("Recording resumed")
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "Not recording"})

@app.route("/api/record/stop", methods=["POST"])
def stop_recording():
    global _recorder
    if not _recorder:
        return jsonify({"ok": False, "error": "Not recording"})
    raw = _recorder.stop()
    steps = _condense_steps(raw)
    log.info("Recording stopped — %d steps (waits removed; player uses window-ready polling)", len(steps))
    _emit_state()
    return jsonify({"ok": True, "steps": _sanitize(steps)})


def _step_label(step):
    """Short human-readable description of a step for the overlay ticker."""
    t = step.get("type", "?")
    d = step.get("data", {})
    el = d.get("element") or {}
    if t == "click":
        name = el.get("name") or el.get("class") or ""
        win  = el.get("window") or ""
        # trim long window titles like "AFT Insights - Grafana - Google Chrome"
        win_short = win.replace(" - Google Chrome", "").replace(" - Google Chrom", "")
        if len(win_short) > 28:
            win_short = win_short[:26] + "…"
        desc = name[:30] if name else f"({int(d.get('x',0))},{int(d.get('y',0))})"
        return f"↓ Click ‘{desc}’  —  {win_short}"
    elif t == "type":
        txt = d.get("text","")[:20]
        return f"↓ Type “{txt}”"
    elif t == "wait":
        return f"↓ Wait {d.get('ms',0)} ms"
    elif t == "navigate":
        url = d.get("url","")[:40]
        return f"↓ Navigate → {url}"
    elif t == "hotkey":
        return f"↓ Hotkey {d.get('combo','')}"
    elif t == "scroll":
        return f"↓ Scroll"
    else:
        return f"↓ {t.replace('_',' ').title()}"

def _push_step(step):
    try: socketio.emit("step", _sanitize(step))
    except Exception as e: log.warning("push_step error: %s", e)
    try:
        if _ov: _ov.last_action(_step_label(step))
    except Exception: pass

def _push_step_update(i, s):
    try: socketio.emit("step_update", {"index": i, "step": _sanitize(s)})
    except Exception as e: log.warning("push_step_update error: %s", e)

def _push_browser_hint():
    """Emit a one-shot hint when Chrome/Edge falls back to win32 element detection."""
    try:
        socketio.emit("browser_hint", {
            "msg": (
                "Chrome/Edge: element names not detected. "
                "For better results, launch Chrome with --force-renderer-accessibility "
                "or enable accessibility in chrome://accessibility."
            )
        })
    except Exception as e:
        log.warning("push_browser_hint error: %s", e)

# ── Socket.IO events ─────────────────────────────────────────────────────
@socketio.on("connect")
def on_client_connect():
    global _connected_clients
    _connected_clients += 1
    log.info("Socket.IO client connected  (active: %d)", _connected_clients)


@socketio.on("disconnect")
def on_client_disconnect():
    """Auto-stop recording / playback when the browser tab closes or refreshes.
    If no client reconnects within the grace period the process exits — closing
    the tab is treated as quitting the app (a page refresh reconnects in < 2 s
    so the deferred exit is cancelled before it fires)."""
    global _recorder, _player, _connected_clients
    _connected_clients = max(0, _connected_clients - 1)
    log.info("Socket.IO client disconnected (active: %d)", _connected_clients)
    changed = False
    if _recorder and _recorder._recording:
        try:
            _recorder.stop()
            log.info("Recording auto-stopped on client disconnect")
        except Exception as _e:
            log.warning("auto-stop recording: %s", _e)
        changed = True
    if _player and not _player._stop_evt.is_set():
        try:
            _player.stop()
            log.info("Playback auto-stopped on client disconnect")
        except Exception as _e:
            log.warning("auto-stop playback: %s", _e)
        changed = True
    if changed:
        try: _ov_hide()
        except Exception: pass

    # Deferred process exit: wait 3 s for a reconnect (handles page refresh —
    # the browser reconnects in < 1 s).  Only exit when still no clients after
    # the grace period.
    if _connected_clients <= 0:
        def _maybe_exit():
            time.sleep(3.0)
            if _connected_clients <= 0:
                log.info("Tab closed — no clients after grace period, exiting")
                try: _ov_hide()
                except Exception: pass
                os._exit(0)
        threading.Thread(target=_maybe_exit, daemon=True).start()


# ── Playback ───────────────────────────────────────────────────────────
def _make_player(d, start_index=0, start_paused=False):
    """Construct a Player from a request-data dict.  Side-effect: saves steps to
    _last_steps so the overlay's Play button can replay the most recent workflow."""
    global _last_steps
    steps = d.get("steps", [])
    if steps:
        _last_steps = steps
    return Player(
        steps=d.get("steps", []),
        speed=float(d.get("speed", 1.0)),
        variables=d.get("variables", {}),
        use_element_targeting=bool(d.get("useElementTargeting", True)),
        start_index=start_index,
        start_paused=start_paused,
        progress_cb=lambda i: (
            socketio.emit("play_progress", {"index": i}),
            _ov.last_action(f"▶ Step {i+1}/{len(d.get('steps',[]))}: "
                            + _step_label(d['steps'][i]))
            if _ov and i < len(d.get('steps', [])) else None
        ),
        done_cb=lambda:        (_emit_state(), socketio.emit("play_done", {})),
        error_cb=lambda e:     socketio.emit("play_error", {"error": str(e)}),
        pause_cb=lambda:       socketio.emit("play_paused", {}),
    )

@app.route("/api/play", methods=["POST"])
def play_workflow():
    global _player
    d = request.json or {}
    if _player: _player.stop()
    start_index  = int(d.get("startIndex",  0))
    start_paused = bool(d.get("startPaused", False))
    _player = _make_player(d, start_index=start_index, start_paused=start_paused)
    _player.start()
    _emit_state()
    return jsonify({"ok": True})

@app.route("/api/play/pause", methods=["POST"])
def pause_playback():
    if _player:
        _player.pause()
        _emit_state()
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "Not playing"})

@app.route("/api/play/resume", methods=["POST"])
def resume_playback():
    if _player:
        _player.resume()
        _emit_state()
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "Not playing"})

@app.route("/api/play/step", methods=["POST"])
def step_playback():
    """
    Advance one step then auto-pause.
    If a player is already running (or paused mid-play), just step it.
    If no player exists, create one at startIndex in paused mode, then step.
    """
    global _player
    d = request.json or {}

    # Player already active — just advance
    if _player and not _player._stop_evt.is_set():
        _player.step()
        return jsonify({"ok": True})

    # Fresh step: need steps from request body
    steps = d.get("steps")
    if not steps:
        return jsonify({"ok": False, "error": "Not playing and no steps provided"})

    start_index = int(d.get("startIndex", 0))
    _player = _make_player(d, start_index=start_index, start_paused=True)
    _player.start()
    # step_evt is set by step(); the player thread will pick it up even if not yet running
    _player.step()
    _emit_state()
    return jsonify({"ok": True})

@app.route("/api/play/stop", methods=["POST"])
def stop_playback():
    if _player: _player.stop()
    _emit_state()
    return jsonify({"ok": True})

@app.route("/api/stop", methods=["POST"])
def stop_all():
    """Overlay unified stop -- stops recording OR playback, whichever is active."""
    global _recorder, _player
    stopped = False
    if _recorder and _recorder._recording:
        try:
            steps = _condense_steps(_recorder.stop())
            log.info("stop_all: recording stopped -- %d steps", len(steps))
            socketio.emit("record_stopped", {"steps": _sanitize(steps)})
            stopped = True
        except Exception as _e:
            log.warning("stop_all recorder: %s", _e)
    if _player and not _player._stop_evt.is_set():
        try:
            _player.stop()
            log.info("stop_all: playback stopped")
            stopped = True
        except Exception as _e:
            log.warning("stop_all player: %s", _e)
    if stopped:
        try: _ov_hide()
        except Exception: pass
    _emit_state()
    return jsonify({"ok": True, "stopped": stopped})

@app.route("/api/record/new", methods=["POST"])
def start_recording_new():
    """Start a fresh recording -- emits clear_steps to the frontend first."""
    global _recorder
    if _recorder and _recorder._recording:
        return jsonify({"ok": False, "error": "Already recording"})
    socketio.emit("clear_steps", {})
    _recorder = Recorder(
        on_step=_push_step,
        on_step_update=_push_step_update,
        on_browser_hint=_push_browser_hint,
    )
    _recorder.start()
    log.info("Recording started (new -- clear_steps emitted)")
    _emit_state()
    return jsonify({"ok": True})

@app.route("/api/play/last", methods=["POST"])
def play_last_workflow():
    """Replay the most recently played workflow (used by overlay Play button)."""
    global _player
    if not _last_steps:
        return jsonify({"ok": False, "error": "No workflow has been played yet"})
    if _player: _player.stop()
    _player = _make_player({"steps": _last_steps})
    _player.start()
    _emit_state()
    return jsonify({"ok": True, "steps": len(_last_steps)})

@app.route("/api/settings", methods=["GET"])
def get_settings():
    return jsonify(_settings)


@app.route("/api/settings", methods=["PATCH"])
def patch_settings():
    body = request.get_json(force=True, silent=True) or {}
    allowed = {"screenshot_mode"}
    for k, v in body.items():
        if k in allowed:
            _settings[k] = v
    log.info("settings updated: %s", _settings)
    return jsonify(_settings)


# ── Workflows ──────────────────────────────────────────────────────────
@app.route("/api/workflows", methods=["GET"])
def list_workflows():
    items = []
    for f in sorted(os.listdir(WORKFLOWS_DIR)):
        if not f.endswith(".json"): continue
        try:
            with open(os.path.join(WORKFLOWS_DIR, f)) as fh:
                d = json.load(fh)
            items.append({"name": d.get("name", f[:-5]), "file": f[:-5],
                          "steps": len(d.get("steps",[])), "created": d.get("created",0)})
        except Exception: pass
    return jsonify({"ok": True, "workflows": items})

@app.route("/api/workflows", methods=["POST"])
def save_workflow():
    d = request.json or {}
    name = d.get("name","").strip()
    if not name: return jsonify({"ok": False, "error": "Name required"})
    safe = "".join(c for c in name if c.isalnum() or c in " _-").strip()
    with open(os.path.join(WORKFLOWS_DIR, f"{safe}.json"), "w") as f:
        json.dump({"name":name,"created":time.time(),
                   "steps":d.get("steps",[]),"variables":d.get("variables",{})}, f, indent=2)
    return jsonify({"ok": True})

@app.route("/api/workflows/<name>", methods=["GET"])
def load_workflow(name):
    path = os.path.join(WORKFLOWS_DIR, f"{name}.json")
    if not os.path.exists(path): return jsonify({"ok":False,"error":"Not found"}), 404
    with open(path) as f: d = json.load(f)
    return jsonify({"ok": True, "workflow": d})

@app.route("/api/workflows/<name>", methods=["DELETE"])
def delete_workflow(name):
    path = os.path.join(WORKFLOWS_DIR, f"{name}.json")
    if os.path.exists(path): os.remove(path)
    return jsonify({"ok": True})

# ── Screenshot ─────────────────────────────────────────────────────────
@app.route("/api/screenshot", methods=["POST"])
def take_screenshot():
    try:
        from PIL import ImageGrab, Image
        img = ImageGrab.grab()
        img.thumbnail((1920, 1200), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=82)
        return jsonify({"ok": True, "image": base64.b64encode(buf.getvalue()).decode()})
    except Exception as e:
        log.exception("screenshot failed")
        return jsonify({"ok": False, "error": str(e)})

# ── PDF report ─────────────────────────────────────────────────────────
@app.route("/api/export/report", methods=["POST"])
def export_report():
    d         = request.json or {}
    wf_name   = d.get("name", "Workflow")
    steps     = d.get("steps", [])
    variables = d.get("variables", {})
    created   = d.get("created", time.time())
    created_str = time.strftime("%Y-%m-%d %H:%M", time.localtime(created))

    ICONS = {
        "click":"🖱️","type":"⌨️","hotkey":"⚡","scroll":"🔄","wait":"⏱️",
        "navigate":"🌐","loop":"🔁","loop_end":"↩️","if":"❓","else":"↕️",
        "end_if":"✓","set_variable":"📦","run_script":"⚙️","screenshot":"📸","comment":"💬",
        "error_handler":"🛡️","launch_browser":"🚀","show_message":"📢",
        "wait_for_element":"⏳","wait_for_window":"🪟","get_clipboard":"📋","set_clipboard":"📌",
        "image_click":"🖼️","read_file":"📄","write_file":"📝","copy_file":"📑",
        "move_file":"📦","delete_file":"🗑️","http_request":"🌍","kill_process":"⛔",
        "close_window":"❌","open_file":"📂","play_sound":"🔊",
    }

    def esc(s): return str(s or "").replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")

    def step_desc(step):
        t, d2 = step.get("type",""), step.get("data",{})
        el = d2.get("element")
        if t=="click":
            base = f"({d2.get('x')}, {d2.get('y')}) — {d2.get('button','left')} click"
            if el and el.get("name"):
                return f"<strong>{esc(el.get('type',''))}{': ' if el.get('type') else ''}{esc(el['name'])}</strong>" \
                       + (f" in <em>{esc(el['window'])}</em>" if el.get("window") else "") \
                       + f" &nbsp;·&nbsp; {base}"
            return base
        if t=="type":     return f'Type: <code>"{esc(d2.get("text",""))}"</code>'
        if t=="hotkey":   return f'Hotkey: <code>{esc(d2.get("combo",""))}</code>'
        if t=="wait":     return f'Wait {d2.get("ms",1000)} ms'
        if t=="navigate": return f'Navigate to <code>{esc(d2.get("url",""))}</code>'
        if t=="loop":     return f'Loop &times;{d2.get("count",1)}'
        if t=="loop_end": return "End Loop"
        if t=="if":       return f'If <code>{{{{d2.get("var","")}}}}</code> = &ldquo;{esc(d2.get("value",""))}&rdquo;'
        if t=="else":     return "Else"
        if t=="end_if":   return "End If"
        if t=="set_variable": return f'Set <code>{{{{d2.get("name","")}}}}</code> = &ldquo;{esc(d2.get("value",""))}&rdquo;'
        if t=="run_script":   return f'Run: <code>{esc(d2.get("command",""))}</code>'
        if t=="comment":      return f'<em>{esc(d2.get("text",""))}</em>'
        if t=="screenshot":   return "Capture screenshot"
        if t=="error_handler": return f'On error: {esc(d2.get("action","stop"))} (max retries: {d2.get("max_retries",0)})'
        if t=="launch_browser": return f'Launch {esc(d2.get("browser","chrome"))} &rarr; <code>{esc(d2.get("url",""))}</code>'
        if t=="show_message":  return f'Show message: <strong>{esc(d2.get("title","AutoFlow"))}</strong> &mdash; {esc(d2.get("message",""))}'
        if t=="wait_for_element": return f'Wait for element &ldquo;{esc(d2.get("name") or d2.get("type") or "?")}&rdquo; ({d2.get("timeout_ms",5000)} ms)'
        if t=="wait_for_window":  return f'Wait for window &ldquo;{esc(d2.get("title",""))}&rdquo; ({d2.get("timeout_ms",8000)} ms)'
        if t=="get_clipboard":  return f'Clipboard &rarr; <code>{{{{{esc(d2.get("variable","?"))}}}}}</code>'
        if t=="set_clipboard":  return f'Clipboard &larr; <code>"{esc(d2.get("text",""))}"</code>'
        if t=="image_click":    return f'Image click (confidence {d2.get("confidence",0.85)})'
        if t=="read_file":   return f'Read file <code>{esc(d2.get("path",""))}</code> &rarr; <code>{{{{{esc(d2.get("variable","?"))}}}}}</code>'
        if t=="write_file":  return f'Write file <code>{esc(d2.get("path",""))}</code>{" (append)" if d2.get("append") else ""}'
        if t=="copy_file":   return f'Copy <code>{esc(d2.get("src",""))}</code> &rarr; <code>{esc(d2.get("dst",""))}</code>'
        if t=="move_file":   return f'Move <code>{esc(d2.get("src",""))}</code> &rarr; <code>{esc(d2.get("dst",""))}</code>'
        if t=="delete_file": return f'Delete <code>{esc(d2.get("path",""))}</code>'
        if t=="http_request": return f'{esc(d2.get("method","GET"))} <code>{esc(d2.get("url",""))}</code> &rarr; <code>{{{{{esc(d2.get("variable","?"))}}}}}</code>'
        if t=="kill_process": return f'Kill process <code>{esc(d2.get("name",""))}</code>'
        if t=="close_window": return f'Close window &ldquo;{esc(d2.get("title",""))}&rdquo;'
        if t=="open_file":    return f'Open <code>{esc(d2.get("path",""))}</code>'
        if t=="play_sound":   return f'Play sound: {esc(d2.get("sound","default"))}'
        return esc(t)

    steps_html = ""
    for i, step in enumerate(steps):
        t    = step.get("type","")
        d2   = step.get("data",{})
        icon = ICONS.get(t,"❓")
        img_b64 = d2.get("screenshot_full") or d2.get("screenshot")
        img_html = (f'<div class="step-img"><img src="data:image/jpeg;base64,{img_b64}" alt="screenshot"></div>'
                    if img_b64 else "")
        note     = step.get("note", "")
        note_html = (f'<div class="step-note"><span class="note-label">📝 Note</span>{esc(note)}</div>'
                     if note else "")
        disabled  = step.get("disabled", False)
        steps_html += f"""
        <div class="step{' disabled' if disabled else ''}">
          <div class="step-hdr">
            <span class="step-num">{i+1}</span>
            <span class="step-icon">{icon}</span>
            <span class="step-type">{t.replace("_"," ").upper()}</span>
            {'<span class="step-tag disabled-tag">SKIPPED</span>' if disabled else ''}
          </div>
          {note_html}
          <div class="step-body">
            <div class="step-desc">{step_desc(step)}</div>
            {img_html}
          </div>
        </div>"""

    vars_html = ""
    if variables:
        rows = "".join(f"<tr><td><code>{{{{{k}}}}}</code></td><td>{esc(v)}</td></tr>"
                       for k,v in variables.items())
        vars_html = f"""<div class="section"><h2>Variables</h2>
          <table class="vars-table"><thead><tr><th>Name</th><th>Value</th></tr></thead>
          <tbody>{rows}</tbody></table></div>"""

    html = f"""<!DOCTYPE html><html lang="en"><head>
<meta charset="UTF-8"><title>{esc(wf_name)} — AutoFlow Report</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:"Segoe UI",Arial,sans-serif;font-size:13px;color:#1a1a2e;background:#fff}}
@media print{{.no-print{{display:none}}.step{{page-break-inside:avoid}}}}
.page{{max-width:860px;margin:0 auto;padding:32px 40px}}
.report-hdr{{border-bottom:3px solid #4f8ef7;padding-bottom:16px;margin-bottom:28px}}
.report-hdr h1{{font-size:26px;font-weight:700}}
.report-hdr .meta{{font-size:12px;color:#666;margin-top:5px}}
.section{{margin-bottom:32px}}
.section h2{{font-size:13px;font-weight:700;text-transform:uppercase;letter-spacing:.8px;
             color:#4f8ef7;margin-bottom:14px;border-bottom:1px solid #e8ecf0;padding-bottom:6px}}
.step{{background:#f8faff;border:1px solid #dde4f0;border-radius:10px;margin-bottom:14px;overflow:hidden}}
.step-note{{background:#e8fdf2;border-left:3px solid #3ec97a;border-radius:0 4px 4px 0;padding:8px 12px;font-size:13px;color:#1a5c38;margin:0 14px 8px;line-height:1.6}}
.note-label{{font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.6px;color:#3ec97a;display:block;margin-bottom:3px}}
.step.disabled{{opacity:.55}}
.disabled-tag{{font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.5px;color:#e05252;border:1px solid #e05252;border-radius:3px;padding:1px 5px;margin-left:auto}}
.step-hdr{{display:flex;align-items:center;gap:10px;padding:10px 14px;background:#fff;border-bottom:1px solid #dde4f0}}
.step-num{{width:26px;height:26px;border-radius:50%;background:#4f8ef7;color:#fff;
           display:flex;align-items:center;justify-content:center;font-size:11px;font-weight:700}}
.step-icon{{font-size:18px}}
.step-type{{font-size:10.5px;font-weight:700;text-transform:uppercase;letter-spacing:.7px;color:#666}}
.step-body{{padding:10px 14px}}
.step-desc{{font-size:13px;line-height:1.6;color:#2d3a4a;margin-bottom:8px}}
.step-desc code{{background:#e8ecf4;border-radius:4px;padding:1px 6px;font-size:12px}}
.step-img img{{max-width:100%;border-radius:6px;border:1px solid #dde4f0;display:block}}
.vars-table{{width:100%;border-collapse:collapse;font-size:12.5px}}
.vars-table th,.vars-table td{{border:1px solid #dde4f0;padding:6px 10px}}
.vars-table th{{background:#f0f4ff;font-weight:600}}
.print-btn{{display:inline-block;margin-bottom:20px;padding:9px 20px;background:#4f8ef7;
            color:#fff;border:none;border-radius:7px;font-size:13px;cursor:pointer;font-weight:600}}
.print-btn:hover{{background:#6ba3ff}}
</style></head><body>
<div class="page">
  <button class="print-btn no-print" onclick="window.print()">🖨 Print / Save as PDF</button>
  <div class="report-hdr">
    <h1>⚡ {esc(wf_name)}</h1>
    <div class="meta">AutoFlow &nbsp;·&nbsp; {created_str} &nbsp;·&nbsp; {len(steps)} steps</div>
  </div>
  {vars_html}
  <div class="section"><h2>Steps</h2>{steps_html}</div>
</div></body></html>"""

    return Response(html, mimetype="text/html")


# ── Shared script generation helper ────────────────────────────────────
def _generate_script(wf_name, steps, variables, created, for_zip=False):
    """Return a standalone Python script string reproducing the workflow."""
    created_str = time.strftime("%Y-%m-%d %H:%M", time.localtime(created))
    safe_name   = re.sub(r"[^a-zA-Z0-9_]", "_", wf_name).strip("_") or "workflow"
    run_hint    = (f"Run with: python {safe_name}.py  (or double-click run.bat)"
                   if for_zip else f"Run with: python {safe_name}.py")

    def sub(text):
        for k, v in variables.items():
            text = text.replace(f"{{{k}}}", str(v))
        return text

    def safe_var(name):
        return re.sub(r"[^a-zA-Z0-9_]", "_", str(name)).lstrip("0123456789") or "var"

    lines = [
        '"""',
        f"AutoFlow generated script: {wf_name}",
        f"Created:  {created_str}",
        f"Steps:    {len(steps)}",
        run_hint,
        '"""',
        "",
        "# Auto-install pyautogui if missing",
        "try:",
        "    import pyautogui",
        "except ImportError:",
        "    import subprocess, sys",
        '    print("Installing pyautogui...")',
        "    subprocess.run([sys.executable, '-m', 'pip', 'install', 'pyautogui'], check=True)",
        "    import pyautogui",
        "import time, os, subprocess as _sp",
        "",
        "pyautogui.FAILSAFE = True   # move mouse to top-left corner to abort",
        "pyautogui.PAUSE    = 0.05",
        "",
        "try:",
    ]

    if variables:
        lines.append("    # ── Variables ─────────────────────────")
        for k, v in variables.items():
            lines.append(f"    {safe_var(k)} = {repr(str(v))}")
        lines.append("")

    lines.append("    # ── Steps ────────────────────────────────────────────")

    indent = 1
    for i, step in enumerate(steps):
        t        = step.get("type", "")
        d2       = step.get("data", {})
        el       = d2.get("element")
        px       = "    " * indent
        disabled = step.get("disabled", False)

        if disabled:
            lines.append(f"{px}# [DISABLED] step {i+1}: {t}")
            continue

        if t == "click":
            x, y  = int(d2.get("x", 0)), int(d2.get("y", 0))
            btn   = d2.get("button", "left")
            label = ""
            if el and el.get("name"):
                label = f"  # {el.get('type','')+(': ' if el.get('type') else '')}{el.get('name','').replace(chr(10),' ')}"
            btn_arg = f', button="{btn}"' if btn != "left" else ""
            lines.append(f"{px}pyautogui.click({x}, {y}{btn_arg}){label}")

        elif t == "type":
            text = sub(d2.get("text", ""))
            if "\n" in text:
                parts = text.split("\n")
                for pi, part in enumerate(parts):
                    if part:
                        lines.append(f"{px}pyautogui.write({repr(part)}, interval=0.05)")
                    if pi < len(parts) - 1:
                        lines.append(f'{px}pyautogui.press("enter")')
            else:
                lines.append(f"{px}pyautogui.write({repr(text)}, interval=0.05)")

        elif t == "hotkey":
            parts = [p.strip() for p in d2.get("combo", "").split("+") if p.strip()]
            if parts:
                keys_str = ", ".join(repr(p) for p in parts)
                if len(parts) == 1:
                    lines.append(f"{px}pyautogui.press({keys_str})")
                else:
                    lines.append(f"{px}pyautogui.hotkey({keys_str})")

        elif t == "wait":
            lines.append(f"{px}time.sleep({int(d2.get('ms', 1000)) / 1000.0})")

        elif t == "scroll":
            x, y = int(d2.get("x", 0)), int(d2.get("y", 0))
            dy   = int(d2.get("dy", 0))
            lines.append(f"{px}pyautogui.scroll({dy}, x={x}, y={y})")

        elif t == "navigate":
            url = sub(d2.get("url", ""))
            lines.append(f"{px}import webbrowser; webbrowser.open({repr(url)})")

        elif t == "loop":
            count = int(d2.get("count", 1))
            lines.append(f"{px}for _loop_{i} in range({count}):")
            indent += 1
            lines.append(f"{'    ' * indent}pass  # loop body")

        elif t == "loop_end":
            indent = max(1, indent - 1)

        elif t == "if":
            var_name = safe_var(d2.get("var", "cond"))
            val      = d2.get("value", "")
            lines.append(f"{px}if {var_name} == {repr(val)}:")
            indent += 1
            lines.append(f"{'    ' * indent}pass  # if body placeholder")

        elif t == "else":
            indent = max(1, indent - 1)
            lines.append(f"{'    ' * indent}else:")
            indent += 1
            lines.append(f"{'    ' * indent}pass  # else body placeholder")

        elif t == "end_if":
            indent = max(1, indent - 1)

        elif t == "set_variable":
            name = safe_var(d2.get("name", "var"))
            val  = sub(d2.get("value", ""))
            lines.append(f"{px}{name} = {repr(val)}")

        elif t == "run_script":
            cmd = sub(d2.get("command", ""))
            lines.append(f"{px}_sp.Popen({repr(cmd)}, shell=True)")

        elif t == "screenshot":
            lines.append(f"{px}pyautogui.screenshot()")

        elif t == "comment":
            txt = d2.get("text", "").replace("\n", " ")
            lines.append(f"{px}# {txt}")

        elif t == "open_file":
            path = sub(d2.get("path", ""))
            lines.append(f"{px}os.startfile({repr(path)})")

        elif t == "wait_for_window":
            title     = sub(d2.get("title", ""))
            timeout_s = int(d2.get("timeout_ms", 8000)) / 1000.0
            lines += [
                f"{px}# wait up to {timeout_s}s for window: {repr(title)}",
                f"{px}_wfw_t0 = time.time()",
                f"{px}while time.time() - _wfw_t0 < {timeout_s}:",
                f"{px}    try:",
                f"{px}        import pygetwindow as _pgw",
                f"{px}        if any({repr(title)} in w.title for w in _pgw.getAllWindows()): break",
                f"{px}    except Exception: pass",
                f"{px}    time.sleep(0.5)",
            ]

        elif t == "launch_browser":
            url = sub(d2.get("url", "about:blank"))
            lines.append(f"{px}import webbrowser; webbrowser.open({repr(url)})")

        elif t == "show_message":
            msg   = sub(d2.get("message", ""))
            title = sub(d2.get("title", "AutoFlow"))
            lines += [
                f"{px}try:",
                f"{px}    import ctypes as _ct",
                f"{px}    _ct.windll.user32.MessageBoxW(0, {repr(msg)}, {repr(title)}, 0x40)",
                f"{px}except Exception: print({repr(msg)})",
            ]

        elif t == "get_clipboard":
            var = safe_var(d2.get("var", "clipboard"))
            lines += [
                f"{px}try:",
                f"{px}    import pyperclip as _pc; {var} = _pc.paste()",
                f"{px}except Exception: {var} = ''",
            ]

        elif t == "set_clipboard":
            val = sub(d2.get("value", ""))
            lines += [
                f"{px}try:",
                f"{px}    import pyperclip as _pc; _pc.copy({repr(val)})",
                f"{px}except Exception: pass",
            ]

        elif t == "image_click":
            conf = float(d2.get("confidence", 0.8))
            lines += [
                f"{px}# image_click — template image is embedded in the workflow JSON",
                f"{px}# To use: export the template image, then:",
                f"{px}# _loc = pyautogui.locateCenterOnScreen('template.png', confidence={conf})",
                f"{px}# if _loc: pyautogui.click(_loc)",
            ]

        elif t == "write_file":
            path    = sub(d2.get("path", ""))
            content = sub(d2.get("content", ""))
            mode    = "a" if d2.get("append") else "w"
            lines.append(f"{px}with open({repr(path)}, {repr(mode)}, encoding='utf-8') as _f: _f.write({repr(content)})")

        elif t == "read_file":
            path = sub(d2.get("path", ""))
            var  = safe_var(d2.get("var", "file_content"))
            lines.append(f"{px}with open({repr(path)}, encoding='utf-8') as _f: {var} = _f.read()")

        elif t == "copy_file":
            src = sub(d2.get("src", ""))
            dst = sub(d2.get("dst", ""))
            lines.append(f"{px}import shutil; shutil.copy2({repr(src)}, {repr(dst)})")

        elif t == "move_file":
            src = sub(d2.get("src", ""))
            dst = sub(d2.get("dst", ""))
            lines.append(f"{px}import shutil; shutil.move({repr(src)}, {repr(dst)})")

        elif t == "delete_file":
            path = sub(d2.get("path", ""))
            lines.append(f"{px}os.remove({repr(path)})")

        elif t == "http_request":
            method  = d2.get("method", "GET").upper()
            url     = sub(d2.get("url", ""))
            headers = d2.get("headers", {})
            body    = sub(d2.get("body", ""))
            rvar    = safe_var(d2.get("response_var", "")) if d2.get("response_var") else None
            lines.append(f"{px}import urllib.request as _ur")
            if body:
                lines.append(f"{px}_req = _ur.Request({repr(url)}, method={repr(method)}, data={repr(body.encode())}, headers={repr(headers)})")
            else:
                lines.append(f"{px}_req = _ur.Request({repr(url)}, method={repr(method)}, headers={repr(headers)})")
            if rvar:
                lines.append(f"{px}with _ur.urlopen(_req) as _r: {rvar} = _r.read().decode('utf-8', errors='replace')")
            else:
                lines.append(f"{px}_ur.urlopen(_req).close()")

        elif t == "kill_process":
            proc = sub(d2.get("process", ""))
            lines.append(f"{px}_sp.run(['taskkill', '/f', '/im', {repr(proc)}], capture_output=True)")

        elif t == "close_window":
            title = sub(d2.get("title", ""))
            lines += [
                f"{px}try:",
                f"{px}    import pygetwindow as _pgw",
                f"{px}    for _w in _pgw.getWindowsWithTitle({repr(title)}): _w.close()",
                f"{px}except Exception: pass",
            ]

        elif t == "play_sound":
            path = sub(d2.get("path", ""))
            lines += [
                f"{px}try:",
                f"{px}    import winsound; winsound.PlaySound({repr(path)}, winsound.SND_FILENAME)",
                f"{px}except Exception: pass",
            ]

        elif t == "wait_for_element":
            timeout_ms = int(d2.get("timeout_ms", 5000))
            name       = d2.get("name", "")
            lines.append(f"{px}time.sleep({timeout_ms / 1000.0})  # wait_for_element '{name}' (sleep approximation)")

        elif t == "error_handler":
            action = d2.get("action", "stop")
            lines.append(f"{px}# error_handler: action={repr(action)} (not enforced in standalone script)")

        else:
            lines.append(f"{px}# TODO: step type {repr(t)} not yet supported in standalone script export")

    lines += [
        "except Exception as _err:",
        '    print(f"\\nError: {_err}")',
        "",
        'input("\\nWorkflow complete. Press Enter to exit...")',
    ]
    return "\n".join(lines) + "\n"

# ── Python script export ───────────────────────────────────────────────
@app.route("/api/export/script", methods=["POST"])
def export_script():
    """Generate a standalone Python script that replays the workflow."""
    d         = request.json or {}
    wf_name   = d.get("name", "workflow")
    steps     = d.get("steps", [])
    variables = d.get("variables", {})
    created   = d.get("created", time.time())
    safe_name = re.sub(r"[^a-zA-Z0-9_]", "_", wf_name).strip("_") or "workflow"
    script    = _generate_script(wf_name, steps, variables, created, for_zip=False)
    return Response(
        script,
        mimetype="text/x-python",
        headers={"Content-Disposition": f'attachment; filename="{safe_name}.py"'},
    )

# ── Import / Export helpers ─────────────────────────────────────────────────

def _parse_script(text):
    """Parse an AutoFlow-exported Python script back into steps + variables.
    Best-effort: handles all step types that export_script emits.  Complex
    scripts that were hand-edited may not round-trip perfectly."""
    import re, ast as _ast
    steps     = []
    variables = {}
    in_steps  = False
    in_vars   = False
    block_stack   = []   # [(block_type, keyword_indent_level)]
    pending_writes= []   # accumulate multi-line type step

    def flush_writes():
        if pending_writes:
            steps.append({"type": "type", "data": {"text": "\n".join(pending_writes)}})
            pending_writes.clear()

    def close_blocks(cur_indent):
        while block_stack and block_stack[-1][1] >= cur_indent:
            btype, _ = block_stack.pop()
            if btype == "loop":
                steps.append({"type": "loop_end", "data": {}})
            elif btype in ("if", "else"):
                steps.append({"type": "end_if", "data": {}})

    for raw in text.splitlines():
        s = raw.strip()
        if not s:
            continue
        indent = (len(raw) - len(raw.lstrip())) // 4

        # Section markers
        if "# ── Variables" in raw or "# -- Variables" in raw:
            in_vars = True; in_steps = False; continue
        if "# ── Steps" in raw or "# -- Steps" in raw:
            in_steps = True; in_vars = False; continue

        if in_vars and not in_steps:
            m = re.match(r"(\w+)\s*=\s*(.+)", s)
            if m and m.group(1) not in ("pyautogui", "time"):
                try:    variables[m.group(1)] = str(_ast.literal_eval(m.group(2)))
                except: variables[m.group(1)] = m.group(2).strip("'\"")
            continue

        if not in_steps:
            continue
        if s.startswith(("except Exception", 'input("', 'input(\'')):
            break

        # Close outer blocks when indent decreases (but not for else:)
        if s != "else:":
            close_blocks(indent)

        # Skip pass placeholders
        if re.match(r"pass\s+#.*placeholder", s) or s == "pass":
            continue

        # Click
        if m := re.match(r"pyautogui\.click\((-?\d+),\s*(-?\d+)(?:,\s*button=[\"'](\w+)[\"'])?\)", s):
            flush_writes()
            steps.append({"type": "click", "data": {
                "x": int(m.group(1)), "y": int(m.group(2)),
                "button": m.group(3) or "left"}})

        # Write (part of type step)
        elif m := re.match(r"pyautogui\.write\((.+?),\s*interval=", s):
            try:    txt = str(_ast.literal_eval(m.group(1)))
            except: txt = m.group(1).strip("'\"")
            pending_writes.append(txt)

        # Enter key inside multi-line type
        elif s in ('pyautogui.press("enter")', "pyautogui.press('enter')"):
            if pending_writes:
                pending_writes.append("")   # becomes \n when joined

        # Press (single key)
        elif m := re.match(r'pyautogui\.press\(["\'](.+?)["\']\)', s):
            flush_writes()
            steps.append({"type": "hotkey", "data": {"combo": m.group(1)}})

        # Hotkey
        elif m := re.match(r"pyautogui\.hotkey\((.+)\)", s):
            flush_writes()
            try:
                keys = [str(_ast.literal_eval(k.strip())) for k in m.group(1).split(",")]
                steps.append({"type": "hotkey", "data": {"combo": "+".join(keys)}})
            except Exception:
                pass

        # Sleep / Wait
        elif m := re.match(r"time\.sleep\((\d+(?:\.\d+)?)\)", s):
            flush_writes()
            steps.append({"type": "wait", "data": {"ms": int(float(m.group(1)) * 1000)}})

        # Scroll
        elif m := re.match(r"pyautogui\.scroll\((-?\d+),\s*x=(-?\d+),\s*y=(-?\d+)\)", s):
            flush_writes()
            steps.append({"type": "scroll", "data": {
                "dy": int(m.group(1)), "x": int(m.group(2)), "y": int(m.group(3))}})

        # Navigate
        elif m := re.match(r"import webbrowser;\s*webbrowser\.open\((.+)\)", s):
            flush_writes()
            try:
                url = str(_ast.literal_eval(m.group(1)))
                steps.append({"type": "navigate", "data": {"url": url}})
            except Exception:
                pass

        # Loop
        elif m := re.match(r"for _loop_\d+ in range\((\d+)\):", s):
            flush_writes()
            steps.append({"type": "loop", "data": {"count": int(m.group(1))}})
            block_stack.append(("loop", indent))

        # If
        elif m := re.match(r"if (\w+) == (.+):", s):
            flush_writes()
            try:    val = str(_ast.literal_eval(m.group(2)))
            except: val = m.group(2).strip("'\"")
            steps.append({"type": "if", "data": {"var": m.group(1), "operator": "==", "value": val}})
            block_stack.append(("if", indent))

        # Else
        elif s == "else:":
            flush_writes()
            steps.append({"type": "else", "data": {}})
            if block_stack and block_stack[-1][0] == "if":
                block_stack[-1] = ("else", block_stack[-1][1])

        # Comment (skip template noise)
        elif s.startswith("#"):
            txt = s[1:].strip()
            if txt and not any(txt.startswith(p) for p in ("TODO:", "Auto-install", "\u2500", "--")):
                flush_writes()
                steps.append({"type": "comment", "data": {"text": txt}})

    flush_writes()
    close_blocks(0)
    return steps, variables


@app.route("/api/import/script", methods=["POST"])
def import_script():
    """Parse an exported AutoFlow Python script (.py) back into a workflow."""
    d    = request.json or {}
    text = d.get("text", "")
    name = d.get("name", "Imported Workflow")
    if not text:
        return jsonify({"error": "No script text provided"}), 400
    steps, variables = _parse_script(text)
    return jsonify({"steps": steps, "variables": variables, "name": name, "created": time.time()})


@app.route("/api/export/zip", methods=["POST"])
def export_zip():
    """Export workflow as a runnable ZIP: Python script + launcher batch file.
    The batch checks for Python, installs pyautogui if needed, and runs the script.
    Recipients without Python get clear instructions; no AutoFlow installation needed."""
    import zipfile as _zf, io as _io

    d         = request.json or {}
    wf_name   = d.get("name", "workflow")
    steps     = d.get("steps", [])
    variables = d.get("variables", {})
    created   = d.get("created", time.time())

    # Use shared script generator
    script = _generate_script(wf_name, steps, variables, created, for_zip=True)

    bat = f"""@echo off
title AutoFlow Workflow Runner
echo =====================================================
echo  AutoFlow Workflow: {wf_name}
echo =====================================================
echo.

:: Check if Python is installed
python --version >nul 2>&1
if errorlevel 1 (
    echo Python is NOT installed on this computer.
    echo.
    echo Please install Python from:
    echo   https://www.python.org/downloads/
    echo.
    echo IMPORTANT: During installation, check the box that says
    echo            "Add Python to PATH"
    echo.
    pause
    exit /b 1
)

echo Python found. Installing required packages...
pip install pyautogui --quiet --disable-pip-version-check
echo.
echo Starting workflow in 3 seconds...
echo Move mouse to the TOP-LEFT corner at any time to abort.
echo.
timeout /t 3 /nobreak >nul

python "{safe_name}.py"
echo.
pause
"""

    readme = f"""AutoFlow Workflow Package
=========================
Workflow: {wf_name}
Created:  {created_str}
Steps:    {len(steps)}

HOW TO RUN
----------
Option 1 (easiest): Double-click run.bat
  - Checks that Python is installed
  - Installs the required pyautogui package automatically
  - Runs the workflow

Option 2 (if you already have Python):
  pip install pyautogui
  python {safe_name}.py

REQUIREMENTS
------------
- Windows 10 or 11
- Python 3.8 or later (download from https://www.python.org/downloads/)
  When installing, check "Add Python to PATH"

SAFETY TIP
----------
Move the mouse to the TOP-LEFT corner of the screen to abort playback at any time.
"""

    buf = _io.BytesIO()
    with _zf.ZipFile(buf, "w", _zf.ZIP_DEFLATED) as zf:
        zf.writestr(f"{safe_name}.py",  script)
        zf.writestr("run.bat",          bat)
        zf.writestr("requirements.txt", "pyautogui\n")
        zf.writestr("README.txt",       readme)
    buf.seek(0)
    return Response(
        buf.getvalue(),
        mimetype="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{safe_name}.zip"'},
    )



# ── System tray ────────────────────────────────────────────────────────
def _make_tray_image():
    from PIL import Image, ImageDraw
    img  = Image.new("RGBA", (64,64), (0,0,0,0))
    draw = ImageDraw.Draw(img)
    draw.ellipse([2,2,62,62], fill="#4f8ef7")
    draw.polygon([(35,6),(20,34),(30,34),(25,58),(44,30),(34,30),(39,6)], fill="white")
    return img


def _tray_quit(icon):
    """Clean shutdown from system tray: stop active operations, hide overlay, exit."""
    try:
        if _recorder and _recorder._recording:
            _recorder.stop()
    except Exception: pass
    try:
        if _player:
            _player.stop()
    except Exception: pass
    try: _ov_hide()
    except Exception: pass
    try: icon.stop()
    except Exception: pass
    log.info("Tray quit -- exiting")
    os._exit(0)

def _run_tray():
    try:
        import pystray
        image = _make_tray_image()
        menu  = pystray.Menu(
            pystray.MenuItem("Open AutoFlow", lambda *_: webbrowser.open(f"http://localhost:{PORT}"), default=True),
            pystray.MenuItem("Quit", lambda icon, _: _tray_quit(icon)),
        )
        icon  = pystray.Icon("AutoFlow", image, f"AutoFlow  ·  localhost:{PORT}", menu)
        log.info("System tray running")
        icon.run()
    except Exception as e:
        log.warning("System tray unavailable (%s) — keeping alive via Event", e)
        threading.Event().wait()

# ── Server thread ──────────────────────────────────────────────────────
def _run_server():
    try:
        log.info("Flask/SocketIO binding on 127.0.0.1:%d", PORT)
        socketio.run(app, host="127.0.0.1", port=PORT, debug=False, use_reloader=False, allow_unsafe_werkzeug=True)
    except Exception as e:
        log.exception("Server crashed")
        _fatal(f"Server failed to start on port {PORT}:\n{e}\n\nCheck autoflow.log")

def _open_browser():
    time.sleep(1.5)
    url = f"http://localhost:{PORT}"
    log.info("Opening browser: %s", url)
    webbrowser.open(url)

# ── Entry ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    threading.Thread(target=_run_server,   daemon=True).start()
    threading.Thread(target=_open_browser, daemon=True).start()
    _run_tray()
