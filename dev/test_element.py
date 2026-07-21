"""
test_element.py — Standalone element detection tester.

Waits 3 seconds (move your mouse to the target), then tests three hit-detection
strategies and prints all results.  Run from the autoflow folder:

    .venv\Scripts\python.exe test_element.py

Strategies:
  A — uiautomation.ControlFromPoint()          (current recorder approach)
  B — Leaf walk down from ControlFromPoint      (find deepest child containing point)
  C — Smallest-area element in the subtree      (most specific bounding rect)

Also tests CDP Page.getLayoutMetrics for browser coordinate conversion.
"""

import time
import ctypes
import json
from ctypes import wintypes

print("Move mouse to target element — waiting 3 seconds…")
time.sleep(3)

import uiautomation as auto

user32 = ctypes.windll.user32
pt = wintypes.POINT()
user32.GetCursorPos(ctypes.byref(pt))
x, y = pt.x, pt.y
print(f"\nCursor: ({x}, {y})")
print("=" * 64)


def _fmt(ctrl):
    try:
        r = ctrl.BoundingRectangle
        area = (r.right - r.left) * (r.bottom - r.top) if r else 0
        return {
            "Type":      ctrl.ControlTypeName,
            "Name":      repr(ctrl.Name),
            "AutoId":    repr(ctrl.AutomationId),
            "ClassName": repr(ctrl.ClassName),
            "Rect":      f"({r.left},{r.top})–({r.right},{r.bottom})" if r else "None",
            "Area":      area,
        }
    except Exception as e:
        return {"error": str(e)}


def _print_ctrl(label, ctrl, ref=None):
    info = _fmt(ctrl)
    print(f"\n[{label}]")
    for k, v in info.items():
        print(f"  {k:10}: {v}")
    if ref and ctrl != ref:
        print("  *** DIFFERENT from reference ***")


# ── Method A: ControlFromPoint (current) ─────────────────────────────────────
print("\n── Method A: uiautomation.ControlFromPoint() ──────────────")
ctrl_a = None
try:
    ctrl_a = auto.ControlFromPoint(x, y)
    if ctrl_a:
        _print_ctrl("A", ctrl_a)
    else:
        print("  (None returned)")
except Exception as e:
    print(f"  ERROR: {e}")


# ── Method B: leaf walk from ControlFromPoint ─────────────────────────────────
def find_leaf(root, px, py, _depth=0):
    """
    Walk children recursively; return the deepest control (first hit, front z-order)
    whose BoundingRectangle still contains (px, py).  Falls back to root if no child
    qualifies.
    """
    if _depth > 25:
        return root
    best = root
    try:
        for child in root.GetChildren():
            try:
                r = child.BoundingRectangle
                if r and r.left <= px <= r.right and r.top <= py <= r.bottom:
                    best = find_leaf(child, px, py, _depth + 1)
                    break  # front-to-back z-order; stop after first hit
            except Exception:
                continue
    except Exception:
        pass
    return best


print("\n── Method B: leaf walk (deepest child containing point) ───")
ctrl_b = None
try:
    if ctrl_a:
        ctrl_b = find_leaf(ctrl_a, x, y)
        _print_ctrl("B", ctrl_b, ref=ctrl_a)
    else:
        print("  (skipped — Method A returned None)")
except Exception as e:
    print(f"  ERROR: {e}")


# ── Method C: smallest-area element in subtree ───────────────────────────────
def find_smallest(root, px, py, _depth=0):
    """Return the element with the smallest bounding area that still contains (px, py)."""
    best_ctrl = root
    best_area = float("inf")

    def _walk(ctrl, depth=0):
        nonlocal best_ctrl, best_area
        if depth > 25:
            return
        try:
            r = ctrl.BoundingRectangle
            if not r:
                return
            if r.left <= px <= r.right and r.top <= py <= r.bottom:
                area = (r.right - r.left) * (r.bottom - r.top)
                if area < best_area:
                    best_area = area
                    best_ctrl = ctrl
                for child in ctrl.GetChildren():
                    _walk(child, depth + 1)
        except Exception:
            pass

    _walk(root)
    return best_ctrl


print("\n── Method C: smallest-area element in subtree ─────────────")
ctrl_c = None
try:
    if ctrl_a:
        ctrl_c = find_smallest(ctrl_a, x, y)
        _print_ctrl("C", ctrl_c, ref=ctrl_a)
    else:
        print("  (skipped — Method A returned None)")
except Exception as e:
    print(f"  ERROR: {e}")


# ── CDP browser test ──────────────────────────────────────────────────────────
print("\n── CDP browser (requires --remote-debugging-port=9222) ────")

def _cdp_layout_metrics(ws_url):
    try:
        import websocket
        ws = websocket.create_connection(ws_url, timeout=2)
        ws.send(json.dumps({"id": 1, "method": "Page.getLayoutMetrics", "params": {}}))
        result = json.loads(ws.recv())
        ws.close()
        return result.get("result", {}).get("cssVisualViewport", {})
    except Exception as e:
        return {"error": str(e)}


try:
    import urllib.request
    tabs = json.loads(
        urllib.request.urlopen("http://localhost:9222/json", timeout=1).read()
    )
    page = next(
        (t for t in tabs if t.get("type") == "page" and "webSocketDebuggerUrl" in t),
        None,
    )
    if page:
        print(f"  Tab URL   : {page.get('url', '?')[:80]}")

        vp = _cdp_layout_metrics(page["webSocketDebuggerUrl"])
        print(f"  cssVisualViewport: {vp}")

        # Compute toolbar height from window rect + viewport clientHeight
        hwnd = user32.WindowFromPoint(wintypes.POINT(x, y))
        root_hwnd = hwnd
        while True:
            p = user32.GetParent(root_hwnd)
            if not p:
                break
            root_hwnd = p

        rect = wintypes.RECT()
        user32.GetWindowRect(root_hwnd, ctypes.byref(rect))
        win_h = rect.bottom - rect.top
        win_w = rect.right  - rect.left

        vp_h = vp.get("clientHeight", 0)
        vp_w = vp.get("clientWidth",  0)

        if vp_h:
            toolbar_h = win_h - vp_h
            print(f"\n  Win size  : {win_w}×{win_h}")
            print(f"  VP size   : {vp_w}×{vp_h}")
            print(f"  Toolbar h : {toolbar_h}  (was hardcoded 95)")

            vp_x = x - rect.left
            vp_y = y - rect.top - toolbar_h
            print(f"  VP coords : ({vp_x}, {vp_y})")

            # Query DOM element
            try:
                import websocket
                js = (
                    "JSON.stringify((function(){"
                    f"var e=document.elementFromPoint({vp_x},{vp_y});"
                    "if(!e)return null;"
                    "return{tag:e.tagName,id:e.id,cls:e.className,"
                    "text:(e.innerText||e.value||e.alt||e.title||'').slice(0,80),"
                    "role:e.getAttribute('role'),"
                    "ariaLabel:e.getAttribute('aria-label'),"
                    "name:e.getAttribute('name'),"
                    "placeholder:e.getAttribute('placeholder')}"
                    "})()"
                )
                ws2 = websocket.create_connection(page["webSocketDebuggerUrl"], timeout=2)
                ws2.send(json.dumps({
                    "id": 2, "method": "Runtime.evaluate",
                    "params": {"expression": js},
                }))
                r2 = json.loads(ws2.recv())
                ws2.close()
                val = r2.get("result", {}).get("result", {}).get("value")
                dom = json.loads(val) if val else None
                print(f"  DOM elem  : {dom}")
            except Exception as e2:
                print(f"  CDP eval error: {e2}")
        else:
            print("  (couldn't get viewport height from CDP)")
    else:
        print("  No Chrome tab found — start Chrome with --remote-debugging-port=9222")
except Exception as e:
    print(f"  CDP unavailable: {e}")


# ── Summary ───────────────────────────────────────────────────────────────────
print("\n" + "=" * 64)
print("SUMMARY")
print("=" * 64)

def _score(ctrl):
    """Score 0–4: name present, AutoId present, not Pane/Group, has ClassName."""
    if ctrl is None:
        return -1
    try:
        has_name  = bool(ctrl.Name and ctrl.Name.strip())
        has_aid   = bool(ctrl.AutomationId and ctrl.AutomationId.strip())
        not_pane  = ctrl.ControlTypeName not in ("PaneControl", "GroupControl", "")
        has_class = bool(ctrl.ClassName and ctrl.ClassName.strip())
        return sum([has_name, has_aid, not_pane, has_class])
    except Exception:
        return -1


results = [
    ("A — ControlFromPoint",        ctrl_a),
    ("B — leaf walk",               ctrl_b),
    ("C — smallest-area",           ctrl_c),
]

best_score = -1
best_name  = None
for lbl, ctrl in results:
    s = _score(ctrl)
    if ctrl:
        try:
            info = f"Name={ctrl.Name!r}  AutoId={ctrl.AutomationId!r}  Type={ctrl.ControlTypeName}"
        except Exception:
            info = "(error reading attrs)"
    else:
        info = "(unavailable)"
    marker = " ★" if s > best_score and s >= 0 else ""
    print(f"  {lbl:30}: score={s}/4  {info}{marker}")
    if s > best_score:
        best_score = s
        best_name  = lbl

print(f"\nRecommended strategy: {best_name or '(none — all failed)'}")
print("\nUse this result to update _get_element_at() in recorder.py.")
