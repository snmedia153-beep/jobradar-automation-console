from __future__ import annotations

import shutil
from dataclasses import asdict, dataclass

from jobradar.config import Settings
from jobradar.device_farm.adb import adb_path, list_devices
from jobradar.device_farm.appium_server import appium_command, check_appium_status
from jobradar.device_farm.emulator_launcher import emulator_path, list_avds
from jobradar.device_farm.runtime import run_command


# 진단 item 클래스는 관련 데이터와 기능을 한곳에 묶어 관리합니다.
@dataclass
class DiagnosticItem:
    name: str
    ok: bool
    detail: str

    # 객체 데이터를 딕셔너리로 바꿔 저장, API 응답, 화면 표시에서 쉽게 사용하게 합니다.
    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def run_diagnostics(settings: Settings) -> list[DiagnosticItem]:
    items: list[DiagnosticItem] = []

    python = run_command(["python", "--version"], timeout=5)
    items.append(DiagnosticItem("Python", python.ok, python.summary()))

    playwright = run_command(["python", "-m", "playwright", "--version"], timeout=10)
    items.append(DiagnosticItem("Playwright", playwright.ok, playwright.summary()))

    adb = adb_path(settings)
    adb_version = run_command([adb, "version"], timeout=10)
    items.append(DiagnosticItem("ADB", adb_version.ok, adb_version.summary()))

    emu = emulator_path(settings)
    emu_help = run_command([emu, "-version"], timeout=10)
    items.append(DiagnosticItem("Android Emulator", emu_help.ok, emu_help.summary()))

    avds, avd_msg = list_avds(settings)
    items.append(DiagnosticItem("AVD 목록", bool(avds), f"{len(avds)}개: {', '.join(avds[:6])}" if avds else avd_msg))

    devices = list_devices(settings)
    real_devices = [d for d in devices if d.state == "device"]
    items.append(DiagnosticItem("ADB 장치", bool(real_devices), f"{len(real_devices)}개 연결" if real_devices else "; ".join(d.raw or d.state for d in devices) or "연결 없음"))

    appium = appium_command(settings)
    appium_check = run_command([appium, "--version"], timeout=10)
    items.append(DiagnosticItem("Appium CLI", appium_check.ok, appium_check.summary()))

    ok, msg = check_appium_status(settings.appium_server_url)
    items.append(DiagnosticItem("Appium 기본 서버", ok, msg))

    tesseract = getattr(settings, "tesseract_cmd", "") or shutil.which("tesseract") or ""
    items.append(DiagnosticItem("Tesseract OCR", bool(tesseract), tesseract or "선택 기능: 설치 시 스크린샷 OCR 사용 가능"))

    return items
