"""
overlay_native.py -- Win32 dim layer + tkinter HUD  (v2, pure-Win32 dim).

Dim layer: CreateWindowEx with WS_EX_LAYERED|WS_EX_TRANSPARENT|
           WS_EX_TOPMOST|WS_EX_NOACTIVATE|WS_EX_TOOLWINDOW.
           WndProc is ours from creation -- WM_WINDOWPOSCHANGING is vetoed
           in-place, no subclassing, no patching, immune to DWM resize.
           Show/hide via PostMessageW (_WM_DIM_SHOW/_WM_DIM_HIDE).

HUD:       Tkinter Toplevel on its own daemon thread (show/hide via queue).
           WS_EX_NOACTIVATE is set on the HUD HWND after creation so it
           never steals focus from the recording target.
"""

import ctypes
import threading
import queue
import logging

log = logging.getLogger("autoflow.overlay")

# ── Cross-thread queue (HUD commands) ─────────────────────────────────────────
_q: "queue.Queue" = queue.Queue()
_tk_thread: "threading.Thread | None" = None
_tk_lock = threading.Lock()
_CMD_SHOW   = "show"
_CMD_HIDE   = "hide"
_CMD_UPDATE = "update"
_CMD_ACTION = "action"

# ══════════════════════════════════════════════════════════════════════════════
# Win32 dim layer
# ══════════════════════════════════════════════════════════════════════════════

# ── Constants ─────────────────────────────────────────────────────────────────
_WS_POPUP             = 0x80000000
_WS_EX_LAYERED        = 0x00080000
_WS_EX_TRANSPARENT    = 0x00000020
_WS_EX_TOPMOST        = 0x00000008
_WS_EX_NOACTIVATE     = 0x08000000
_WS_EX_TOOLWINDOW     = 0x00000080
_GWL_EXSTYLE          = -20
_LWA_ALPHA            = 0x00000002
_SW_HIDE              = 0
_SW_SHOWNOACTIVATE    = 4
_HWND_TOPMOST         = -1
_SWP_NOMOVE           = 0x0002
_SWP_NOSIZE           = 0x0001
_SWP_NOACTIVATE       = 0x0010
_SWP_FRAMECHANGED     = 0x0020
_WM_DESTROY           = 0x0002
_WM_WINDOWPOSCHANGING = 0x0046
_WM_APP               = 0x8000
_WM_DIM_SHOW          = _WM_APP + 1
_WM_DIM_HIDE          = _WM_APP + 2
_SM_XVIRTUALSCREEN    = 76
_SM_YVIRTUALSCREEN    = 77
_SM_CXVIRTUALSCREEN   = 78
_SM_CYVIRTUALSCREEN   = 79
_ERROR_CLASS_ALREADY_EXISTS = 1410

# ── Win32 types ───────────────────────────────────────────────────────────────
_WNDPROCTYPE = ctypes.WINFUNCTYPE(
    ctypes.c_ssize_t,   # LRESULT
    ctypes.c_size_t,    # HWND
    ctypes.c_uint,      # UINT  msg
    ctypes.c_size_t,    # WPARAM
    ctypes.c_ssize_t,   # LPARAM
)

class _WINDOWPOS(ctypes.Structure):
    _fields_ = [
        ("hwnd",            ctypes.c_void_p),
        ("hwndInsertAfter", ctypes.c_void_p),
        ("x",               ctypes.c_int),
        ("y",               ctypes.c_int),
        ("cx",              ctypes.c_int),
        ("cy",              ctypes.c_int),
        ("flags",           ctypes.c_uint),
    ]

class _WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize",        ctypes.c_uint),
        ("style",         ctypes.c_uint),
        ("lpfnWndProc",   ctypes.c_void_p),   # filled via cast(cb, c_void_p)
        ("cbClsExtra",    ctypes.c_int),
        ("cbWndExtra",    ctypes.c_int),
        ("hInstance",     ctypes.c_void_p),
        ("hIcon",         ctypes.c_void_p),
        ("hCursor",       ctypes.c_void_p),
        ("hbrBackground", ctypes.c_void_p),
        ("lpszMenuName",  ctypes.c_wchar_p),
        ("lpszClassName", ctypes.c_wchar_p),
        ("hIconSm",       ctypes.c_void_p),
    ]

class _MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd",    ctypes.c_void_p),
        ("message", ctypes.c_uint),
        ("wParam",  ctypes.c_size_t),
        ("lParam",  ctypes.c_ssize_t),
        ("time",    ctypes.c_uint),
        ("pt",      ctypes.c_long * 2),
    ]

class _RECT(ctypes.Structure):
    _fields_ = [
        ("left",   ctypes.c_long),
        ("top",    ctypes.c_long),
        ("right",  ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]

class _MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize",    ctypes.c_ulong),
        ("rcMonitor", _RECT),
        ("rcWork",    _RECT),
        ("dwFlags",   ctypes.c_ulong),
    ]

# ── Module-level state ────────────────────────────────────────────────────────
_dim_hwnd      = [None]          # HWND of dim window (int)
_dim_ready_evt = threading.Event()
_dim_wndproc_ref = [None]        # keep WINFUNCTYPE object alive (GC guard)

# ── Helpers ───────────────────────────────────────────────────────────────────
def _vscreen():
    """Physical pixel rect of the full virtual screen (all monitors combined)."""
    u = ctypes.windll.user32
    return (
        u.GetSystemMetrics(_SM_XVIRTUALSCREEN),
        u.GetSystemMetrics(_SM_YVIRTUALSCREEN),
        u.GetSystemMetrics(_SM_CXVIRTUALSCREEN) or u.GetSystemMetrics(0),
        u.GetSystemMetrics(_SM_CYVIRTUALSCREEN) or u.GetSystemMetrics(1),
    )

def _logical_screen():
    """Logical pixel rect for tkinter geometry strings (physical / DPI scale)."""
    u = ctypes.windll.user32
    try:
        hdc   = ctypes.windll.gdi32.CreateDCW("DISPLAY", None, None, None)
        dpi   = ctypes.windll.gdi32.GetDeviceCaps(hdc, 88)  # LOGPIXELSX
        ctypes.windll.gdi32.DeleteDC(hdc)
        scale = dpi / 96.0 if dpi else 1.0
    except Exception:
        scale = 1.0
    vx = int(u.GetSystemMetrics(_SM_XVIRTUALSCREEN)  / scale)
    vy = int(u.GetSystemMetrics(_SM_YVIRTUALSCREEN)  / scale)
    vw = int((u.GetSystemMetrics(_SM_CXVIRTUALSCREEN) or u.GetSystemMetrics(0)) / scale)
    vh = int((u.GetSystemMetrics(_SM_CYVIRTUALSCREEN) or u.GetSystemMetrics(1)) / scale)
    return vx, vy, vw, vh

def _active_monitor_workarea():
    """Return (left, top, right, bottom) of the work area of the monitor under the cursor.
    Work area excludes the taskbar on that specific monitor.
    Falls back to None if ctypes call fails (caller uses primary monitor as default)."""
    try:
        u  = ctypes.windll.user32
        pt = ctypes.wintypes.POINT()
        u.GetCursorPos(ctypes.byref(pt))
        MONITOR_DEFAULTTONEAREST = 2
        hmon = u.MonitorFromPoint(pt, MONITOR_DEFAULTTONEAREST)
        mi = _MONITORINFO()
        mi.cbSize = ctypes.sizeof(mi)
        if u.GetMonitorInfoW(hmon, ctypes.byref(mi)):
            r = mi.rcWork
            return (r.left, r.top, r.right, r.bottom)
    except Exception:
        pass
    return None

# ── WndProc ───────────────────────────────────────────────────────────────────
def _dim_wndproc(hwnd, msg, wp, lp):
    u = ctypes.windll.user32
    if msg == _WM_WINDOWPOSCHANGING:
        # Veto any resize or move -- lock dim layer to full virtual screen.
        # This is the primary guard: fires before DWM applies the change.
        vx, vy, vw, vh = _vscreen()
        try:
            pos        = _WINDOWPOS.from_address(lp)
            pos.x      = vx;   pos.y  = vy
            pos.cx     = vw;   pos.cy = vh
            pos.flags &= ~(_SWP_NOMOVE | _SWP_NOSIZE)
        except Exception:
            pass
        return u.DefWindowProcW(hwnd, msg, wp, lp)

    elif msg == _WM_DIM_SHOW:
        # Called cross-thread via PostMessageW.
        vx, vy, vw, vh = _vscreen()
        u.SetWindowPos(hwnd, _HWND_TOPMOST, vx, vy, vw, vh, _SWP_NOACTIVATE)
        u.ShowWindow(hwnd, _SW_SHOWNOACTIVATE)
        log.info("dim show  hwnd=%d  vscreen=%s", hwnd, (vx, vy, vw, vh))
        return 0

    elif msg == _WM_DIM_HIDE:
        u.ShowWindow(hwnd, _SW_HIDE)
        log.info("dim hide  hwnd=%d", hwnd)
        return 0

    elif msg == _WM_DESTROY:
        u.PostQuitMessage(0)
        return 0

    return u.DefWindowProcW(hwnd, msg, wp, lp)

# ── Dim thread ────────────────────────────────────────────────────────────────
def _dim_thread_main():
    u = ctypes.windll.user32
    k = ctypes.windll.kernel32
    hinst = k.GetModuleHandleW(None)

    # Keep callback alive -- GC of WINFUNCTYPE objects crashes the process.
    cb = _WNDPROCTYPE(_dim_wndproc)
    _dim_wndproc_ref[0] = cb

    cls_name = "AutoFlowDimWnd_v2"

    wc = _WNDCLASSEXW()
    wc.cbSize        = ctypes.sizeof(wc)
    wc.style         = 0
    wc.lpfnWndProc   = ctypes.cast(cb, ctypes.c_void_p).value
    wc.cbClsExtra    = 0
    wc.cbWndExtra    = 0
    wc.hInstance     = hinst
    wc.hIcon         = 0
    wc.hCursor       = 0
    wc.hbrBackground = 0
    wc.lpszMenuName  = None
    wc.lpszClassName = cls_name
    wc.hIconSm       = 0

    atom = u.RegisterClassExW(ctypes.byref(wc))
    err  = k.GetLastError()
    if not atom and err != _ERROR_CLASS_ALREADY_EXISTS:
        log.error("dim RegisterClassExW failed: err=%d", err)
        _dim_ready_evt.set()
        return

    vx, vy, vw, vh = _vscreen()
    hwnd = u.CreateWindowExW(
        _WS_EX_LAYERED | _WS_EX_TRANSPARENT | _WS_EX_TOPMOST
        | _WS_EX_NOACTIVATE | _WS_EX_TOOLWINDOW,
        cls_name,
        "AutoFlowOverlay",
        _WS_POPUP,
        vx, vy, vw, vh,
        None, None, hinst, None,
    )
    if not hwnd:
        log.error("dim CreateWindowExW failed: err=%d", k.GetLastError())
        _dim_ready_evt.set()
        return

    u.SetLayeredWindowAttributes(hwnd, 0, 26, _LWA_ALPHA)  # 26/255 ~ 10%
    _dim_hwnd[0] = hwnd
    _dim_ready_evt.set()
    log.debug("dim layer ready hwnd=%d  vscreen=%s", hwnd, (vx, vy, vw, vh))

    msg_s = _MSG()
    while u.GetMessageW(ctypes.byref(msg_s), None, 0, 0) > 0:
        u.TranslateMessage(ctypes.byref(msg_s))
        u.DispatchMessageW(ctypes.byref(msg_s))

def _ensure_dim():
    if _dim_hwnd[0]:
        return
    t = threading.Thread(target=_dim_thread_main, daemon=True, name="autoflow-dim")
    t.start()
    _dim_ready_evt.wait(timeout=3.0)

def _dim_show():
    _ensure_dim()
    if _dim_hwnd[0]:
        ctypes.windll.user32.PostMessageW(_dim_hwnd[0], _WM_DIM_SHOW, 0, 0)

def _dim_hide():
    if _dim_hwnd[0]:
        ctypes.windll.user32.PostMessageW(_dim_hwnd[0], _WM_DIM_HIDE, 0, 0)


# ══════════════════════════════════════════════════════════════════════════════
# HUD (tkinter)
# ══════════════════════════════════════════════════════════════════════════════

# ── State -> indicator label ────────────────────────────────────────────────
# Maps state name -> (icon, color, label)
_STATE_INFO = {
    "recording":   ("●", "#e74c3c", "Recording"),
    "rec_paused":  ("⏸", "#f39c12", "Paused"),
    "playing":     ("▶", "#2ecc71", "Playing"),
    "play_paused": ("⏸", "#f39c12", "Step Mode"),
    "idle":        ("⚡", "#4f8ef7", "Idle"),
}

# Buttons split into two rows so each has room for text.
# (label, api_path, is_stop_style)
_BTNS_ROW1 = [
    ("●  Record",  "/api/record/start", False),
    ("◎  Rec New", "/api/record/new",   False),
]
_BTNS_ROW2 = [
    ("▶  Play",    "/api/play/last",    False),
    ("⏩  Step",    "/api/play/step",    False),
    ("⏹  Stop",    "/api/stop",         True),
]

def _post_api(path):
    """Fire-and-forget POST to the local Flask API."""
    import urllib.request
    def _go():
        try:
            urllib.request.urlopen(f"http://localhost:5000{path}", data=b"", timeout=2)
        except Exception:
            pass
    threading.Thread(target=_go, daemon=True, name="ov-api").start()

def _apply_noactivate(hwnd):
    """Apply WS_EX_NOACTIVATE to a Win32 HWND so the window never steals focus."""
    try:
        u = ctypes.windll.user32
        ex = u.GetWindowLongW(hwnd, _GWL_EXSTYLE)
        u.SetWindowLongW(hwnd, _GWL_EXSTYLE, ex | _WS_EX_NOACTIVATE)
    except Exception as exc:
        log.warning("_apply_noactivate: %s", exc)

def _get_top_hwnd(wid):
    u = ctypes.windll.user32
    hwnd = wid
    while True:
        p = u.GetParent(hwnd)
        if not p:
            return hwnd
        hwnd = p

# ── tkinter HUD thread ────────────────────────────────────────────────────────
def _tk_main():
    try:
        import tkinter as tk
    except ImportError:
        log.warning("overlay: tkinter not available -- HUD disabled")
        return

    root = tk.Tk()
    root.overrideredirect(True)
    root.geometry("1x1+-32000+-32000")
    try:
        root.attributes("-alpha", 0.0)
    except Exception:
        pass

    # Position the HUD on whichever monitor the cursor is currently on so it
    # appears on the correct screen in multi-monitor setups.
    # rcWork already excludes that monitor's taskbar, so no extra TASKBAR offset needed.
    _wa = _active_monitor_workarea()
    if _wa:
        ml, mt, mr, mb = _wa
        MW = mr - ml   # physical-pixel width of this monitor
        MH = mb - mt   # physical-pixel usable height (taskbar excluded by rcWork)
    else:
        ml, mt = 0, 0
        MW = root.winfo_screenwidth()
        MH = root.winfo_screenheight() - max(50, root.winfo_screenheight() * 5 // 100)
        mr = ml + MW
        mb = mt + MH

    # Alias for padding calculations (keep using MW/MH instead of SW/SH)
    SW, SH = MW, MH

    # HUD: 24% wide, 18% tall (generous so all rows fit).  Hard minimums.
    HUD_W = max(360, MW * 24 // 100)
    HUD_H = max(165, MH * 18 // 100)

    hud_x = mr - HUD_W   # flush to right edge of active monitor
    hud_y = mb - HUD_H   # flush to bottom of work area (taskbar already excluded)

    PX  = max(10, MW // 220)
    PY  = max( 6, MH // 180)
    BPX = max(10, MW // 210)
    BPY = max( 6, MH // 175)

    def _btn(parent, lbl, path, stop):
        b = tk.Button(
            parent, text=lbl,
            bg="#7a1818" if stop else "#1e2d4a",
            fg="#f0f0f0",
            activebackground="#b03030" if stop else "#2e4478",
            activeforeground="#ffffff",
            font=("Segoe UI", 10, "bold"),
            relief="flat", bd=0, padx=BPX, pady=BPY,
            cursor="hand2",
            command=lambda p=path: _post_api(p),
        )
        b.pack(side="left", fill="x", expand=True, padx=(0, max(3, SW // 800)))
        return b

    hud = tk.Toplevel(root)
    hud.overrideredirect(True)
    hud.attributes("-topmost", True)
    hud.configure(bg="#0a0d18")
    hud.geometry(f"{HUD_W}x{HUD_H}+{hud_x}+{hud_y}")
    hud.withdraw()

    def _set_noactivate():
        try:
            hud.update_idletasks()
            _apply_noactivate(_get_top_hwnd(hud.winfo_id()))
        except Exception as exc:
            log.warning("hud noactivate: %s", exc)
    root.after(200, _set_noactivate)

    border = tk.Frame(hud, bg="#3a4a80", padx=1, pady=1)
    border.pack(fill="both", expand=True)
    inner  = tk.Frame(border, bg="#141928", padx=PX, pady=PY)
    inner.pack(fill="both", expand=True)

    # ── Header row ────────────────────────────────────────────────────────
    hdr = tk.Frame(inner, bg="#141928")
    hdr.pack(fill="x", pady=(0, PY))
    lbl_icon  = tk.Label(hdr, text="\u26a1", bg="#141928", fg="#4f8ef7",
                         font=("Segoe UI", 13))
    lbl_icon.pack(side="left")
    lbl_title = tk.Label(hdr, text="AutoFlow", bg="#141928", fg="#e8ecf4",
                         font=("Segoe UI", 11, "bold"))
    lbl_title.pack(side="left", padx=(8, 0))
    lbl_state = tk.Label(hdr, text="Idle", bg="#141928", fg="#4f8ef7",
                         font=("Segoe UI", 10))
    lbl_state.pack(side="right")

    # ── Divider ───────────────────────────────────────────────────────────
    tk.Frame(inner, bg="#2a3050", height=1).pack(fill="x", pady=(0, PY))

    # ── Button row 1: Record | Rec New ───────────────────────────────────
    r1 = tk.Frame(inner, bg="#141928")
    r1.pack(fill="x", pady=(0, max(3, PY // 2)))
    for lbl, path, stop in _BTNS_ROW1:
        _btn(r1, lbl, path, stop)

    # ── Button row 2: Play | Step | Stop ─────────────────────────────────
    r2 = tk.Frame(inner, bg="#141928")
    r2.pack(fill="x", pady=(0, PY))
    for lbl, path, stop in _BTNS_ROW2:
        _btn(r2, lbl, path, stop)

    # ── Last-action ticker ───────────────────────────────────────────────
    tk.Frame(inner, bg="#2a3050", height=1).pack(fill="x", pady=(0, max(3, PY//2)))
    lbl_action = tk.Label(inner, text="No actions yet", bg="#141928", fg="#5a6a8a",
                          font=("Segoe UI", 9), anchor="w", wraplength=HUD_W - PX*2 - 4)
    lbl_action.pack(fill="x")

    # ── Drag: header area only ────────────────────────────────────────────
    _drag = {"ox": 0, "oy": 0}
    def _hdr_press(e):
        _drag["ox"] = e.x_root - hud.winfo_x()
        _drag["oy"] = e.y_root - hud.winfo_y()
    def _hdr_drag(e):
        hud.geometry(f"+{e.x_root - _drag['ox']}+{e.y_root - _drag['oy']}")
    for w in (hud, border, inner, hdr, lbl_icon, lbl_title, lbl_state):
        w.bind("<ButtonPress-1>", _hdr_press)
        w.bind("<B1-Motion>",     _hdr_drag)

    # ── State + action updates ────────────────────────────────────────────
    def _apply_state(state):
        info = _STATE_INFO.get(state or "idle", _STATE_INFO["idle"])
        icon, color, label = info
        lbl_icon.config(text=icon, fg=color)
        lbl_state.config(text=label, fg=color)

    def _apply_action(text):
        lbl_action.config(text=text or "No actions yet", fg="#8a9aba")

    _shown = [False]
    def _do_show(state):
        if not _shown[0]:
            # Re-query the monitor under the cursor each time the HUD is
            # shown so it always appears on the screen the user is working on,
            # not on whichever screen was active when the app first launched.
            _wa_now = _active_monitor_workarea()
            if _wa_now:
                _ml, _mt, _mr, _mb = _wa_now
                hud.geometry(f"{HUD_W}x{HUD_H}+{_mr - HUD_W}+{_mb - HUD_H}")
            hud.deiconify(); hud.lift(); _shown[0] = True
        _apply_state(state)
    def _do_hide():
        if _shown[0]:
            hud.withdraw(); _shown[0] = False
    def _do_update(state):
        _apply_state(state)
    def _do_action(text):
        _apply_action(text)

    def _poll():
        try:
            while True:
                item = _q.get_nowait()
                cmd  = item[0]
                if   cmd == _CMD_SHOW:   _do_show  (item[1] if len(item) > 1 else None)
                elif cmd == _CMD_HIDE:   _do_hide  ()
                elif cmd == _CMD_UPDATE: _do_update(item[1] if len(item) > 1 else None)
                elif cmd == _CMD_ACTION: _do_action(item[1] if len(item) > 1 else "")
        except queue.Empty:
            pass
        root.after(50, _poll)

    root.after(50, _poll)
    root.mainloop()


def _ensure_hud():
    global _tk_thread
    with _tk_lock:
        if _tk_thread is None or not _tk_thread.is_alive():
            _tk_thread = threading.Thread(
                target=_tk_main, daemon=True, name="autoflow-hud"
            )
            _tk_thread.start()


# ══════════════════════════════════════════════════════════════════════════════
# Public API
# ══════════════════════════════════════════════════════════════════════════════

def show(state=None):
    """Show dim layer + HUD.  state: 'recording'|'rec_paused'|'playing'|'play_paused'."""
    try:
        _ensure_hud()
        _dim_show()
        _q.put((_CMD_SHOW, state))
    except Exception as exc:
        log.warning("overlay.show: %s", exc)

def hide():
    """Hide dim layer and HUD."""
    try:
        _dim_hide()
        if _tk_thread and _tk_thread.is_alive():
            _q.put((_CMD_HIDE,))
    except Exception as exc:
        log.warning("overlay.hide: %s", exc)

def update(state=None):
    """Update HUD label/buttons while already visible."""
    try:
        if _tk_thread and _tk_thread.is_alive():
            _q.put((_CMD_UPDATE, state))
    except Exception as exc:
        log.warning("overlay.update: %s", exc)

def last_action(text):
    """Update the last-action ticker in the HUD."""
    try:
        _ensure_hud()
        if _tk_thread and _tk_thread.is_alive():
            _q.put((_CMD_ACTION, str(text)[:120]))
    except Exception as exc:
        log.warning("overlay.last_action: %s", exc)
