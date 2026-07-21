"""
main.py — AutoFlow entry point.
Handles both dev (file paths relative to script) and frozen PyInstaller paths.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) if not getattr(sys, 'frozen', False) else os.path.dirname(sys.executable))

import webview
from api import API

def main():
    api = API()

    # When frozen, bundled files live in sys._MEIPASS; otherwise next to this script
    if getattr(sys, 'frozen', False):
        base = sys._MEIPASS
    else:
        base = os.path.dirname(os.path.abspath(__file__))

    ui_path = os.path.join(base, "ui", "index.html").replace("\\", "/")

    window = webview.create_window(
        title="AutoFlow",
        url=f"file:///{ui_path}",
        js_api=api,
        width=1200,
        height=750,
        min_size=(900, 550),
        background_color="#0f1117",
    )
    api.window = window
    webview.start(debug=False)

if __name__ == "__main__":
    main()
