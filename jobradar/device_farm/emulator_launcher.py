from __future__ import annotations

import socket
import time
from dataclasses import dataclass
from pathlib import Path

from jobradar.config import Settings
from jobradar.device_farm.adb import list_devices, wait_for_boot
from jobradar.device_farm.runtime import find_executable, run_command, start_detached


# 에뮬레이터 launch 결과 클래스는 처리 결과와 상태 정보를 한곳에 담아 전달합니다.
@dataclass
class EmulatorLaunchResult:
    pid: int | None
    ok: bool
    status: str
    message: str
    udid: str
    console_port: int
    adb_port: int

    # 긴 실행 결과에서 핵심 메시지만 뽑아 보기 쉽게 정리합니다.
    def summary(self) -> str:
        return (
            f"status={self.status} pid={self.pid} udid={self.udid or '-'} "
            f"ports={self.console_port},{self.adb_port} {self.message}"
        )


def emulator_path(settings: Settings) -> str:
    return find_executable(settings, "emulator", getattr(settings, "emulator_path", ""))


def list_avds(settings: Settings) -> tuple[list[str], str]:
    exe = emulator_path(settings)
    result = run_command([exe, "-list-avds"], timeout=20)
    if not result.ok:
        return [], result.summary()
    avds = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    return avds, "OK"


def is_port_open(host: str, port: int, timeout: float = 0.35) -> bool:
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def emulator_udid(console_port: int | None) -> str:
    return f"emulator-{console_port}" if console_port else ""


def _build_emulator_command(
    settings: Settings,
    avd_name: str,
    console_port: int | None = None,
    adb_port: int | None = None,
    headless: bool = False,
    gpu_mode: str | None = None,
    snapshot: bool = False,
) -> list[str]:
    cmd = [emulator_path(settings), "-avd", avd_name]
    if console_port and adb_port:
        cmd.extend(["-ports", f"{int(console_port)},{int(adb_port)}"])
    elif console_port:
        cmd.extend(["-port", str(int(console_port))])

    # Deterministic automation is more stable with cold boot/no snapshot by default.
    if snapshot:
        cmd.append("-no-snapshot-save")
    else:
        cmd.append("-no-snapshot")
    cmd.extend(["-no-boot-anim", "-no-audio"])

    selected_gpu = gpu_mode or settings.emulator_gpu_mode
    if headless:
        cmd.extend(["-no-window", "-gpu", "swiftshader_indirect"])
    elif selected_gpu:
        cmd.extend(["-gpu", selected_gpu])
    return cmd


def launch_avd(
    settings: Settings,
    avd_name: str,
    port: int | None = None,
    headless: bool = False,
    *,
    console_port: int | None = None,
    adb_port: int | None = None,
    gpu_mode: str | None = None,
    snapshot: bool = False,
) -> tuple[int | None, str]:
    """Backward compatible lightweight launcher."""
    if console_port is None and port is not None:
        console_port = port
    if adb_port is None and console_port:
        adb_port = console_port + 1
    result = launch_avd_checked(
        settings,
        avd_name,
        console_port=console_port,
        adb_port=adb_port,
        headless=headless,
        wait=False,
        gpu_mode=gpu_mode,
        snapshot=snapshot,
    )
    return result.pid, result.summary()


def launch_avd_checked(
    settings: Settings,
    avd_name: str,
    *,
    console_port: int | None = None,
    adb_port: int | None = None,
    headless: bool = False,
    wait: bool = True,
    boot_timeout: int | None = None,
    gpu_mode: str | None = None,
    snapshot: bool = False,
) -> EmulatorLaunchResult:
    if not avd_name:
        return EmulatorLaunchResult(None, False, "launch_failed", "AVD 이름이 비어 있습니다.", "", int(console_port or 0), int(adb_port or 0))

    if console_port and not adb_port:
        adb_port = console_port + 1
    if adb_port and not console_port:
        console_port = adb_port - 1
    udid = emulator_udid(console_port)

    if console_port and is_port_open("127.0.0.1", console_port):
        return EmulatorLaunchResult(None, False, "port_busy", f"console port {console_port}가 이미 사용 중입니다.", udid, int(console_port), int(adb_port or 0))
    if adb_port and is_port_open("127.0.0.1", adb_port):
        return EmulatorLaunchResult(None, False, "port_busy", f"adb port {adb_port}가 이미 사용 중입니다.", udid, int(console_port or 0), int(adb_port))

    cmd = _build_emulator_command(
        settings,
        avd_name,
        console_port=console_port,
        adb_port=adb_port,
        headless=headless,
        gpu_mode=gpu_mode,
        snapshot=snapshot,
    )
    log = settings.output_dir / "logs" / f"emulator_{avd_name}.log"
    pid, msg = start_detached(cmd, stdout_path=log, stderr_path=log)
    if not pid:
        return EmulatorLaunchResult(None, False, "launch_failed", msg, udid, int(console_port or 0), int(adb_port or 0))

    if not wait:
        return EmulatorLaunchResult(pid, True, "launching", msg, udid, int(console_port or 0), int(adb_port or 0))

    timeout = int(boot_timeout or settings.emulator_boot_timeout_seconds)
    deadline = time.time() + timeout
    last_note = "프로세스 시작됨"
    while time.time() < deadline:
        time.sleep(3)
        devices = {device.udid: device for device in list_devices(settings)}
        if udid and udid in devices:
            dev = devices[udid]
            if dev.boot_completed:
                return EmulatorLaunchResult(pid, True, "connected", "ADB boot_completed=1", udid, int(console_port or 0), int(adb_port or 0))
            last_note = f"ADB 등록됨, boot={dev.boot_completed}"
        elif console_port and adb_port:
            console_open = is_port_open("127.0.0.1", console_port)
            adb_open = is_port_open("127.0.0.1", adb_port)
            last_note = f"대기 중: console_open={console_open} adb_open={adb_open}"
    return EmulatorLaunchResult(pid, False, "boot_timeout", f"{timeout}s 내 ADB 부팅 완료 실패 · {last_note}", udid, int(console_port or 0), int(adb_port or 0))
