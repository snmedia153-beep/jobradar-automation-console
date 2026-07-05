from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from pathlib import Path

from jobradar.config import Settings
from jobradar.device_farm.runtime import find_executable, run_command


# 기기 info 클래스는 처리 결과와 상태 정보를 한곳에 담아 전달합니다.
@dataclass
class DeviceInfo:
    udid: str
    state: str
    model: str = ""
    android_version: str = ""
    boot_completed: bool = False
    transport: str = "adb"
    raw: str = ""

    # 객체 데이터를 딕셔너리로 바꿔 저장, API 응답, 화면 표시에서 쉽게 사용하게 합니다.
    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def adb_path(settings: Settings) -> str:
    return find_executable(settings, "adb", getattr(settings, "adb_path", ""))


def list_devices(settings: Settings) -> list[DeviceInfo]:
    adb = adb_path(settings)
    result = run_command([adb, "devices"], timeout=12)
    if not result.ok:
        return [DeviceInfo(udid="adb-error", state=result.summary(), raw=result.stderr)]
    devices: list[DeviceInfo] = []
    for line in result.stdout.splitlines()[1:]:
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) >= 2:
            udid, state = parts[0], parts[1]
            devices.append(DeviceInfo(udid=udid, state=state, raw=line))
    for item in devices:
        if item.state == "device":
            item.model = shell_getprop(settings, item.udid, "ro.product.model")
            item.android_version = shell_getprop(settings, item.udid, "ro.build.version.release")
            item.boot_completed = shell_getprop(settings, item.udid, "sys.boot_completed") == "1"
    return devices


def shell_getprop(settings: Settings, udid: str, prop: str) -> str:
    adb = adb_path(settings)
    result = run_command([adb, "-s", udid, "shell", "getprop", prop], timeout=8)
    return result.stdout.strip() if result.ok else ""


def wait_for_boot(settings: Settings, udid: str, timeout_sec: int = 180) -> bool:
    adb = adb_path(settings)
    run_command([adb, "-s", udid, "wait-for-device"], timeout=timeout_sec)
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        if shell_getprop(settings, udid, "sys.boot_completed") == "1":
            return True
        time.sleep(3)
    return False


def take_screenshot(settings: Settings, udid: str, output_path: str | Path) -> tuple[bool, str]:
    adb = adb_path(settings)
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    remote_path = "/sdcard/jobradar_screenshot.png"
    r1 = run_command([adb, "-s", udid, "shell", "screencap", "-p", remote_path], timeout=20)
    if not r1.ok:
        return False, r1.summary()
    r2 = run_command([adb, "-s", udid, "pull", remote_path, str(output)], timeout=20)
    if not r2.ok:
        return False, r2.summary()
    return True, str(output)


def stop_device(settings: Settings, udid: str) -> tuple[bool, str]:
    adb = adb_path(settings)
    if not udid:
        return False, "UDID가 비어 있습니다."
    result = run_command([adb, "-s", udid, "emu", "kill"], timeout=15)
    return result.ok, result.summary()
