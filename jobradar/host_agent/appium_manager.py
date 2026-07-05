from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from jobradar.config import Settings
from jobradar.device_farm.appium_server import check_appium_status, start_appium
from jobradar.device_farm.runtime import find_executable


ROOT_DIR = Path(__file__).resolve().parents[2]


def _run(cmd: list[str], timeout: float = 8.0) -> tuple[int, str, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, shell=False)
        return p.returncode, p.stdout or "", p.stderr or ""
    except Exception as exc:
        return 1, "", str(exc)


def _pids_on_port_windows(port: int) -> list[int]:
    code, out, _ = _run(["netstat", "-ano", "-p", "TCP"], timeout=8)
    if code != 0:
        return []
    pids: set[int] = set()
    for line in out.splitlines():
        text = line.upper()
        if f":{int(port)}" not in line or "LISTENING" not in text:
            continue
        parts = line.split()
        if len(parts) >= 5:
            try:
                pids.add(int(parts[-1]))
            except Exception:
                pass
    return sorted(pids)


def _pids_on_port_unix(port: int) -> list[int]:
    candidates = [
        ["bash", "-lc", f"lsof -ti tcp:{int(port)} 2>/dev/null"],
        ["bash", "-lc", f"ss -ltnp 2>/dev/null | grep ':{int(port)} ' | sed -E 's/.*pid=([0-9]+).*/\\1/'"],
    ]
    pids: set[int] = set()
    for cmd in candidates:
        code, out, _ = _run(cmd, timeout=5)
        if code == 0:
            for token in re.findall(r"\b\d+\b", out):
                try:
                    pids.add(int(token))
                except Exception:
                    pass
        if pids:
            break
    return sorted(pids)


def pids_on_port(port: int) -> list[int]:
    if os.name == "nt":
        return _pids_on_port_windows(port)
    return _pids_on_port_unix(port)


def stop_port(port: int) -> dict[str, Any]:
    pids = pids_on_port(port)
    killed: list[int] = []
    errors: list[str] = []
    for pid in pids:
        if os.name == "nt":
            code, out, err = _run(["taskkill", "/PID", str(pid), "/F", "/T"], timeout=10)
        else:
            code, out, err = _run(["kill", "-TERM", str(pid)], timeout=5)
            if code != 0:
                code, out, err = _run(["kill", "-KILL", str(pid)], timeout=5)
        if code == 0:
            killed.append(pid)
        else:
            errors.append(f"pid={pid}: {err or out}")
    return {
        "port": int(port),
        "pids": pids,
        "killed": killed,
        "ok": bool(killed) or not pids,
        "message": "실행 중인 프로세스 없음" if not pids else (f"종료 {len(killed)}개" if killed else "; ".join(errors)),
        "errors": errors,
    }


def adb_devices(settings: Settings | None = None) -> dict[str, Any]:
    settings = settings or Settings()
    adb = settings.adb_path or ""
    if not adb:
        try:
            adb = find_executable(settings, "adb", settings.adb_path)
        except Exception:
            adb = "adb"
    code, out, err = _run([adb, "devices", "-l"], timeout=10)
    rows: list[dict[str, Any]] = []
    for line in out.splitlines():
        line = line.strip()
        if not line or line.lower().startswith("list of devices"):
            continue
        parts = line.split()
        if len(parts) >= 2:
            rows.append({"serial": parts[0], "status": parts[1], "raw": line})
    return {"ok": code == 0, "adb": adb, "count": len(rows), "devices": rows, "stdout": out, "stderr": err}


def appium_ports_status(
    ports: list[int],
    host: str = "127.0.0.1",
    settings: Settings | None = None,
    timeout: float = 0.7,
    include_pids: bool = True,
) -> dict[str, Any]:
    settings = settings or Settings()
    rows: list[dict[str, Any]] = []
    safe_timeout = max(0.2, min(float(timeout or 0.7), 5.0))
    for port in ports:
        port = int(port)
        url = f"http://{host}:{port}"
        ok, message = check_appium_status(url, timeout=safe_timeout)  # type: ignore[arg-type]
        slot_name = settings.appium_port_slot_name(port)
        rows.append({
            "slot_name": slot_name,
            "device_type": "usb" if slot_name == "USB Device" else "emulator",
            "port": port,
            "url": url,
            "ok": bool(ok),
            "message": message,
            "pids": pids_on_port(port) if include_pids else [],
        })
    return {
        "ok": all(row["ok"] for row in rows) if rows else False,
        "count": len(rows),
        "target_count": len(rows),
        "running": sum(1 for row in rows if row["ok"]),
        "rows": rows,
        "timeout_seconds": safe_timeout,
    }


def _safe_ps_literal(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _where_first(names: list[str]) -> str:
    if os.name != "nt":
        for name in names:
            found = shutil_which(name)
            if found:
                return found
        return ""
    for name in names:
        code, out, _ = _run(["where.exe", name], timeout=5)
        if code == 0:
            for line in out.splitlines():
                candidate = line.strip()
                if candidate:
                    return candidate
    return ""


def shutil_which(name: str) -> str:
    try:
        import shutil
        return shutil.which(name) or ""
    except Exception:
        return ""


def _resolve_appium_hint(settings: Settings) -> str:
    explicit = str(getattr(settings, "appium_command", "") or "").strip()
    if explicit and explicit.lower() not in {"appium", "appium.cmd", "appium.exe"}:
        return explicit
    found = _where_first(["appium.cmd", "appium.exe", "appium"])
    if found:
        return found
    # Common npm global install locations.  The generated PowerShell launcher
    # still does a final Get-Command check, but this improves diagnostics.
    candidates: list[Path] = []
    appdata = os.getenv("APPDATA", "")
    if appdata:
        candidates.append(Path(appdata) / "npm" / "appium.cmd")
    userprofile = os.getenv("USERPROFILE", "")
    if userprofile:
        candidates.append(Path(userprofile) / "AppData" / "Roaming" / "npm" / "appium.cmd")
    for path in candidates:
        if path.exists():
            return str(path)
    return explicit or "appium"


def _slot_title(settings: Settings, port: int) -> str:
    return settings.appium_port_slot_name(port).replace("'", "")


def _write_windows_appium_launcher(settings: Settings, port: int, host: str, log_path: Path) -> Path:
    """Create a per-port PowerShell launcher and run it in a new Windows process.

    Starting Appium through Python's direct Popen is fragile on Windows when the
    executable is an npm .cmd shim.  A dedicated PowerShell launcher mirrors the
    manual command that works in the user's console and leaves a visible window
    with useful error messages when Appium is missing or crashes.
    """
    logs_dir = settings.output_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    launcher = logs_dir / f"start_appium_{int(port)}.ps1"
    sdk = str(getattr(settings, "android_sdk_root", "") or os.getenv("ANDROID_SDK_ROOT", "") or os.getenv("ANDROID_HOME", ""))
    appium_hint = _resolve_appium_hint(settings)
    allow_insecure = str(getattr(settings, "appium_allow_insecure", "") or "*:chromedriver_autodownload").strip()
    if allow_insecure == "chromedriver_autodownload":
        allow_insecure = "*:chromedriver_autodownload"
    title = f"JobRadar Appium - {_slot_title(settings, port)} : {int(port)}"
    script = f"""
$ErrorActionPreference = 'Continue'
try {{ $host.UI.RawUI.WindowTitle = {_safe_ps_literal(title)} }} catch {{ }}
$env:ANDROID_HOME = {_safe_ps_literal(sdk)}
$env:ANDROID_SDK_ROOT = {_safe_ps_literal(sdk)}
if ($env:ANDROID_SDK_ROOT) {{
  $env:PATH = "$env:ANDROID_SDK_ROOT\\platform-tools;$env:ANDROID_SDK_ROOT\\emulator;" + $env:PATH
}}
$env:APPIUM_HOME = $env:APPIUM_HOME
$log = {_safe_ps_literal(str(log_path))}
$hint = {_safe_ps_literal(appium_hint)}
$startedAt = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
"[$startedAt] Starting Appium port {int(port)} host {host}" | Tee-Object -FilePath $log -Append
"[$startedAt] APPIUM_HINT=$hint" | Tee-Object -FilePath $log -Append
"[$startedAt] ANDROID_SDK_ROOT=$env:ANDROID_SDK_ROOT" | Tee-Object -FilePath $log -Append
$candidates = @()
if ($env:APPIUM_COMMAND) {{ $candidates += $env:APPIUM_COMMAND }}
if ($hint) {{ $candidates += $hint }}
$candidates += @('appium.cmd', 'appium.exe', 'appium')
$appium = $null
foreach ($candidate in $candidates) {{
  if (-not $candidate) {{ continue }}
  $cmd = Get-Command $candidate -ErrorAction SilentlyContinue
  if ($cmd) {{ $appium = $cmd.Source; break }}
  if (Test-Path $candidate) {{ $appium = $candidate; break }}
}}
if (-not $appium) {{
  "[ERROR] appium command not found. Install: npm i -g appium ; appium driver install uiautomator2" | Tee-Object -FilePath $log -Append
  "[ERROR] PATH=$env:PATH" | Tee-Object -FilePath $log -Append
  Read-Host 'Appium command not found. Press Enter to close'
  exit 127
}}
"[INFO] Resolved Appium: $appium" | Tee-Object -FilePath $log -Append
$argsList = @('--address', {_safe_ps_literal(host)}, '--port', '{int(port)}')
if ({_safe_ps_literal(allow_insecure)}) {{
  $argsList += @('--allow-insecure', {_safe_ps_literal(allow_insecure)})
}}
"[INFO] Command: $appium $($argsList -join ' ')" | Tee-Object -FilePath $log -Append
& $appium @argsList 2>&1 | Tee-Object -FilePath $log -Append
$exitCode = $LASTEXITCODE
"[WARN] Appium process exited with code $exitCode" | Tee-Object -FilePath $log -Append
Read-Host 'Appium exited. Press Enter to close'
""".strip() + "\n"
    launcher.write_text(script, encoding="utf-8")
    return launcher


def _start_appium_windows(settings: Settings, port: int, host: str, log_name: str) -> tuple[int | None, str, dict[str, Any]]:
    log_path = (settings.output_dir / "logs" / log_name).resolve()
    launcher = _write_windows_appium_launcher(settings, int(port), host, log_path)
    ps = shutil_which("powershell.exe") or shutil_which("powershell") or "powershell.exe"
    cmd = [
        ps,
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(launcher.resolve()),
    ]
    try:
        # New console window is intentional: if Appium cannot start, the visible
        # window and log file make the error obvious.  This avoids silent GUI
        # failures when npm/Appium PATH is different from the Docker process.
        flags = subprocess.CREATE_NEW_CONSOLE  # type: ignore[attr-defined]
        proc = subprocess.Popen(
            cmd,
            cwd=str(ROOT_DIR),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
            close_fds=False,
        )
        return proc.pid, "PowerShell Appium launcher started", {
            "mode": "windows_powershell_launcher",
            "launcher": str(launcher.resolve()),
            "log": str(log_path),
            "command": cmd,
            "appium_hint": _resolve_appium_hint(settings),
        }
    except Exception as exc:
        return None, str(exc), {
            "mode": "windows_powershell_launcher",
            "launcher": str(launcher.resolve()),
            "log": str(log_path),
            "command": cmd,
            "appium_hint": _resolve_appium_hint(settings),
        }


def _start_one_appium(settings: Settings, port: int, host: str) -> tuple[int | None, str, dict[str, Any]]:
    log_name = f"appium_{int(port)}.log"
    if os.name == "nt":
        return _start_appium_windows(settings, port, host, log_name)
    pid, message = start_appium(settings, port=port, host=host, log_name=log_name)
    return pid, message, {"mode": "python_detached", "log": str((settings.output_dir / "logs" / log_name).resolve())}


def start_ports(
    settings: Settings,
    ports: list[int],
    host: str = "127.0.0.1",
    verify: bool = False,
    status_timeout: float = 0.4,
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    safe_timeout = max(0.2, min(float(status_timeout or 0.4), 3.0))
    for port in ports:
        port = int(port)
        url = f"http://{host}:{port}"
        ok, msg = check_appium_status(url, timeout=safe_timeout)  # type: ignore[arg-type]
        if ok:
            rows.append({
                "slot_name": settings.appium_port_slot_name(port),
                "port": port,
                "url": url,
                "ok": True,
                "already_running": True,
                "starting": False,
                "pid": None,
                "message": "이미 실행 중",
                "pids": pids_on_port(port),
            })
            continue
        pid, message, debug = _start_one_appium(settings, port, host)
        row: dict[str, Any] = {
            "slot_name": settings.appium_port_slot_name(port),
            "port": port,
            "url": url,
            "ok": bool(pid),
            "already_running": False,
            "starting": bool(pid),
            "pid": pid,
            "message": "시작 창을 열었습니다. 5~15초 뒤 /status가 200으로 바뀝니다." if pid else message,
            "pids": [pid] if pid else [],
            "debug": debug,
        }
        if verify and pid:
            time.sleep(0.8)
            ok2, msg2 = check_appium_status(url, timeout=min(2.0, max(0.5, safe_timeout * 2)))  # type: ignore[arg-type]
            row.update({"ready": bool(ok2), "message": msg2 if ok2 else row["message"]})
        rows.append(row)
    started = sum(1 for row in rows if row.get("ok") and not row.get("already_running"))
    already = sum(1 for row in rows if row.get("already_running"))
    failed = [row for row in rows if not row.get("ok")]
    return {
        "ok": bool(started or already),
        "count": len(rows),
        "running": already,
        "starting": sum(1 for row in rows if row.get("starting")),
        "started": started,
        "failed": len(failed),
        "rows": rows,
        "message": (
            f"Appium 시작 창 {started}개를 열었습니다. 상태 센터에서 5~15초 후 다시 확인하세요."
            if started else "Appium 시작 창을 열지 못했습니다. rows[].debug.log를 확인하세요."
        ),
    }


def stop_ports(ports: list[int]) -> dict[str, Any]:
    rows = [stop_port(int(port)) for port in ports]
    return {"ok": all(row.get("ok") for row in rows) if rows else False, "stopped": sum(1 for row in rows if row.get("killed")), "rows": rows}


def open_screenshot_folder(settings: Settings | None = None) -> dict[str, Any]:
    settings = settings or Settings()
    folder = (settings.output_dir / "screenshots").resolve()
    folder.mkdir(parents=True, exist_ok=True)
    try:
        if os.name == "nt":
            os.startfile(str(folder))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(folder)])
        else:
            subprocess.Popen(["xdg-open", str(folder)])
        return {"ok": True, "path": str(folder), "message": "스크린샷 폴더를 열었습니다."}
    except Exception as exc:
        return {"ok": False, "path": str(folder), "message": str(exc)}
