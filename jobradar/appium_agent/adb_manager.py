from __future__ import annotations

import subprocess
from dataclasses import dataclass


# 안드로이드 기기 클래스는 관련 데이터와 기능을 한곳에 묶어 관리합니다.
@dataclass
class AndroidDevice:
    device_id: str
    status: str
    raw: str


def run_command(command: list[str], timeout: int = 10) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except FileNotFoundError:
        return 127, "", f"Command not found: {command[0]}"
    except subprocess.TimeoutExpired as exc:
        return 124, exc.stdout or "", exc.stderr or "timeout"


def list_adb_devices() -> list[AndroidDevice]:
    code, stdout, stderr = run_command(["adb", "devices"], timeout=10)
    if code != 0:
        return [AndroidDevice(device_id="adb-error", status=stderr.strip() or "error", raw=stderr)]

    devices: list[AndroidDevice] = []
    for line in stdout.splitlines()[1:]:
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) >= 2:
            devices.append(AndroidDevice(device_id=parts[0], status=parts[1], raw=line))
    return devices


def take_adb_screenshot(device_id: str, output_path: str) -> tuple[bool, str]:
    remote_path = "/sdcard/jobradar_screenshot.png"
    code, _, stderr = run_command(["adb", "-s", device_id, "shell", "screencap", "-p", remote_path], timeout=15)
    if code != 0:
        return False, stderr
    code, _, stderr = run_command(["adb", "-s", device_id, "pull", remote_path, output_path], timeout=15)
    if code != 0:
        return False, stderr
    return True, output_path
