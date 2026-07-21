# -*- mode: python ; coding: utf-8 -*-
block_cipher = None

a = Analysis(
    ["server.py"],
    pathex=["."],
    binaries=[],
    datas=[
        ("static",    "static"),
        ("workflows", "workflows"),
    ],
    hiddenimports=[
        "pynput.keyboard._win32",
        "pynput.mouse._win32",
        "engineio.async_drivers.threading",
        "flask_socketio",
        "simple_websocket",
        "wsproto",
        "comtypes",
        "comtypes.client",
        "pystray._win32",
        "tkinter",
        "_tkinter",
        "tkinter.ttk",
        "tkinter.messagebox",
        "websocket",
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=["matplotlib","numpy","scipy","webview","pythonnet"],
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="AutoFlow",
    debug=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    icon=None,
)
