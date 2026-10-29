"""Read-only Windows integrity diagnostics and an explicit visible-client launcher."""
import ctypes as c
from ctypes import wintypes as w
import os
from pathlib import Path
import re
import shutil
import subprocess


def process_token(pid):
    if os.name != "nt":
        return {"pid": pid, "platform": os.name}
    kernel = c.WinDLL("kernel32", use_last_error=True)
    adv = c.WinDLL("advapi32", use_last_error=True)
    kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
    kernel.OpenProcess.restype = w.HANDLE
    kernel.CloseHandle.argtypes = [w.HANDLE]
    adv.OpenProcessToken.argtypes = [w.HANDLE, w.DWORD, c.POINTER(w.HANDLE)]
    adv.GetTokenInformation.argtypes = [w.HANDLE, c.c_int, c.c_void_p, w.DWORD, c.POINTER(w.DWORD)]
    adv.GetSidSubAuthorityCount.argtypes = [c.c_void_p]
    adv.GetSidSubAuthorityCount.restype = c.POINTER(c.c_ubyte)
    adv.GetSidSubAuthority.argtypes = [c.c_void_p, w.DWORD]
    adv.GetSidSubAuthority.restype = c.POINTER(w.DWORD)
    result = {"pid": pid}
    process = kernel.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION only
    if not process:
        return {**result, "error": c.get_last_error()}
    token = w.HANDLE()
    try:
        if not adv.OpenProcessToken(process, 8, c.byref(token)):
            return {**result, "error": c.get_last_error()}
        size = w.DWORD()
        elevation = w.DWORD()
        if adv.GetTokenInformation(token, 20, c.byref(elevation), c.sizeof(elevation), c.byref(size)):
            result["elevated"] = bool(elevation.value)
        adv.GetTokenInformation(token, 25, None, 0, c.byref(size))
        if size.value:
            buf = c.create_string_buffer(size.value)
            if adv.GetTokenInformation(token, 25, buf, size.value, c.byref(size)):
                sid = c.cast(buf, c.POINTER(c.c_void_p)).contents.value
                count = adv.GetSidSubAuthorityCount(sid)[0]
                rid = adv.GetSidSubAuthority(sid, count - 1)[0]
                result["integrity_rid"] = rid
                result["integrity"] = {4096: "low", 8192: "medium", 12288: "high", 16384: "system"}.get(rid, str(rid))
        return result
    finally:
        if token:
            kernel.CloseHandle(token)
        kernel.CloseHandle(process)


def diagnose(session=None):
    result = {"current_process": process_token(os.getpid())}
    if os.name != "nt":
        return result
    if session and not re.fullmatch(r"[A-Za-z0-9_-]+", session):
        raise ValueError("Use a simple Herdr session name")
    config = os.environ.get("HERDR_CONFIG_PATH")
    root = Path(config).parent if config else Path(os.environ["APPDATA"]) / "herdr"
    if session and session != "default":
        root = root / "sessions" / session
    marker = root / "herdr.sock"
    result["marker"] = str(marker)
    if marker.is_file():
        match = re.fullmatch(r"(\d+):\d+\s*", marker.read_text(encoding="utf-8"))
        if match:
            result["server_process"] = process_token(int(match[1]))
    current = result["current_process"].get("integrity_rid")
    server = result.get("server_process", {}).get("integrity_rid")
    if current is not None and server is not None and current < server:
        result["cause"] = "Herdr server has higher Windows integrity than the calling process"
        result["remedy"] = ("In the administrator terminal stop this Herdr session, then start it from a normal terminal. "
                            "Do not elevate Codex or weaken pipe/file ACLs as a workaround.")
    return result


def show(herdr_exe, session=None):
    diagnostic = diagnose(session)
    if diagnostic.get("cause"):
        raise ValueError(diagnostic["cause"] + ". " + diagnostic["remedy"])
    if diagnostic.get("current_process", {}).get("elevated"):
        raise ValueError("Launch the visible client from a normal-permission process")
    if os.name != "nt":
        raise ValueError("Automatic visible-client launch currently supports Windows only")
    terminal = shutil.which("wt.exe")
    command = [herdr_exe] + (["--session", session] if session else [])
    if terminal:
        # New window, not an arbitrary existing elevated Windows Terminal window.
        proc = subprocess.Popen([terminal, "-w", "new", "new-tab", "--title", "Herdr external models", *command],
                                creationflags=subprocess.CREATE_NO_WINDOW)
    else:
        proc = subprocess.Popen(command, creationflags=subprocess.CREATE_NEW_CONSOLE)
    return {"status": "launch_requested", "launcher_pid": proc.pid, "session": session or "default",
            "note": "Visible-client launch requested; verify client/server connection separately"}
