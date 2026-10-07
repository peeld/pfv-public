"""Run by the PFV app at startup (src/appPython.cpp): install() runs once the
main window exists. check() is what `PFV --python-check` prints.

`pfvgui` is built into the exe: shiboken6 generates it from src/bindings.h and
src/bindings.xml. pfvgui.mainWindow() is the app's MainWindow (a QMainWindow
subclass with refresh(), openWorkspace(), workTree(), ...), owned by C++. Use
it from the script editor (View > Script Editor).

The PFV backend the GUI calls is pfv_app (dispatch()); scripts can import it,
or pfv itself, directly.
"""
import importlib
import os
import ssl
import sys

import pfvgui
import PySide6
from PySide6.QtCore import qVersion
from PySide6.QtWidgets import QMainWindow


def main_window():
    return pfvgui.mainWindow()


def install():
    main_window().appendLog(f"Python {sys.version.split()[0]}, PySide6 {PySide6.__version__}")


def loaded_dlls():
    """Paths of every module loaded in this process (Windows only)."""
    if sys.platform != "win32":
        return []
    import ctypes
    from ctypes import wintypes
    psapi = ctypes.WinDLL("psapi")
    kernel32 = ctypes.WinDLL("kernel32")
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    process = kernel32.GetCurrentProcess()
    needed = wintypes.DWORD()
    mods = (wintypes.HMODULE * 2048)()
    psapi.EnumProcessModules.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                                         ctypes.POINTER(wintypes.DWORD)]
    if not psapi.EnumProcessModules(process, mods, ctypes.sizeof(mods), ctypes.byref(needed)):
        return []
    kernel32.GetModuleFileNameW.argtypes = [wintypes.HMODULE, wintypes.LPWSTR, wintypes.DWORD]
    out = []
    buf = ctypes.create_unicode_buffer(32768)
    for mod in mods[:needed.value // ctypes.sizeof(wintypes.HMODULE)]:
        if kernel32.GetModuleFileNameW(mod, buf, len(buf)):
            out.append(buf.value)
    return out


def _dlls_named(dlls, *prefixes):
    return [d for d in dlls if os.path.basename(d).lower().startswith(prefixes)]


def _optional(name):
    try:
        mod = importlib.import_module(name)
        return getattr(mod, "__version__", "yes")
    except ImportError:
        return None


def report():
    import pfv_app
    dlls = loaded_dlls()
    boto3 = _optional("boto3")
    lines = [
        f"Python {sys.version}",
        f"  home {sys.prefix}",
        f"  path {sys.path}",
        f"PySide6 {PySide6.__version__}, Qt runtime {qVersion()}",
        f"Python ssl: {ssl.OPENSSL_VERSION}",
        f"PFV backend: {pfv_app.info()}",
        f"boto3 (S3 repos): {boto3 or 'not installed'}",
        f"Main window: {type(main_window()).__module__}.{type(main_window()).__name__}",
        "Loaded:",
    ]
    lines += [f"  {d}" for d in _dlls_named(dlls, "qt6core", "libssl", "libcrypto", "python3",
                                            "pyside6", "shiboken6")]
    return "\n".join(lines)


def check():
    """report(), raising if the PFV backend doesn't import or answer, the
    main window can't be reached, or a second Qt or OpenSSL is loaded."""
    problems = []
    try:
        import pfv_app
        reply = pfv_app.dispatch("workspaces", "{}")
        if '"ok": true' not in reply:
            problems.append(f"pfv_app.dispatch('workspaces') failed: {reply}")
    except Exception as exc:
        problems.append(f"pfv_app: {exc!r}")
        raise RuntimeError("FAILED:\n  " + "\n  ".join(problems))
    text = report()
    win = main_window()
    if not isinstance(win, pfvgui.MainWindow) or not isinstance(win, QMainWindow):
        problems.append(f"main window is a {type(win)}, want pfvgui.MainWindow")
    elif "Python " not in win.logText():
        problems.append(f"install() didn't log to the main window: {win.logText()!r}")
    if sys.platform == "win32":
        dlls = loaded_dlls()
        for prefix in ("qt6core", "libssl", "libcrypto"):
            found = _dlls_named(dlls, prefix)
            if len(found) > 1:
                problems.append(f"expected one {prefix}*.dll loaded, got {found}")
    if problems:
        raise RuntimeError(text + "\n\nFAILED:\n  " + "\n  ".join(problems))
    return text + "\n\nOK"
