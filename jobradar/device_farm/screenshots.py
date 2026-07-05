from __future__ import annotations

import base64
from datetime import datetime
from pathlib import Path
from typing import Any

import requests

try:
    from appium import webdriver
    from appium.options.android import UiAutomator2Options
except Exception:  # Appium client is optional for active-session screenshots.
    webdriver = None  # type: ignore[assignment]
    UiAutomator2Options = None  # type: ignore[assignment]

from jobradar.config import Settings
from jobradar.device_farm.url_utils import resolve_appium_url


def _capability(options: Any, key: str, value: Any) -> None:
    if value is None or value == "":
        return
    try:
        options.set_capability(key, value)
    except Exception:
        setattr(options, key.replace("appium:", ""), value)


def safe_filename(value: Any) -> str:
    text = str(value or "slot").strip() or "slot"
    for ch in ['\\\\', '/', ':', '*', '?', '"', '<', '>', '|', ' ']:
        text = text.replace(ch, '_')
    return text


def infer_slot_udid(slot: dict[str, Any]) -> str:
    udid = str(slot.get("udid") or "").strip()
    if udid:
        return udid
    console_port = int(slot.get("emulator_console_port") or 0)
    if console_port:
        return f"emulator-{console_port}"
    return ""


def _write_base64_png(value: str, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(base64.b64decode(value))


def _request_existing_session_screenshot(appium_url: str, output_path: Path, timeout: float = 8.0) -> tuple[bool, str, str]:
    """Try to capture a screenshot from an already active Appium session.

    This is important when the worker is currently using the slot. Creating a
    second session on the same Appium server may fail, but the Appium server can
    still expose the active session's screenshot endpoint.
    """
    base = appium_url.rstrip("/")
    try:
        sessions_response = requests.get(f"{base}/sessions", timeout=timeout)
        if not sessions_response.ok:
            return False, "", f"/sessions HTTP {sessions_response.status_code}: {sessions_response.text[:300]}"
        sessions_data = sessions_response.json()
        sessions = sessions_data.get("value") or []
        if not sessions:
            return False, "", "활성 Appium 세션 없음"
        session_id = str(sessions[0].get("id") or sessions[0].get("sessionId") or "").strip()
        if not session_id:
            return False, "", "활성 Appium session id를 읽지 못했습니다."
        shot_response = requests.get(f"{base}/session/{session_id}/screenshot", timeout=timeout)
        if not shot_response.ok:
            return False, session_id, f"/screenshot HTTP {shot_response.status_code}: {shot_response.text[:300]}"
        value = (shot_response.json() or {}).get("value")
        if not value:
            return False, session_id, "스크린샷 응답이 비어 있습니다."
        _write_base64_png(str(value), output_path)
        return True, session_id, str(output_path)
    except Exception as exc:
        return False, "", str(exc)


def _create_temp_session_screenshot(settings: Settings, slot: dict[str, Any], appium_url: str, udid: str, output_path: Path) -> tuple[bool, str]:
    if webdriver is None or UiAutomator2Options is None:
        return False, "Appium-Python-Client가 설치되어 있지 않아 임시 세션 스크린샷을 만들 수 없습니다."
    options = UiAutomator2Options()
    _capability(options, "platformName", "Android")
    _capability(options, "appium:automationName", "UiAutomator2")
    _capability(options, "appium:deviceName", udid or settings.android_device_name or "Android")
    _capability(options, "appium:udid", udid)
    _capability(options, "appium:appPackage", settings.android_settings_package)
    _capability(options, "appium:appActivity", settings.android_settings_activity)
    _capability(options, "appium:noReset", True)
    _capability(options, "appium:newCommandTimeout", 60)
    _capability(options, "appium:autoGrantPermissions", True)
    _capability(options, "appium:autoAcceptAlerts", True)

    system_port = int(slot.get("system_port") or 0)
    mjpeg_port = int(slot.get("mjpeg_server_port") or 0)
    if system_port:
        _capability(options, "appium:systemPort", system_port)
    if mjpeg_port:
        _capability(options, "appium:mjpegServerPort", mjpeg_port)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    driver = webdriver.Remote(appium_url, options=options)
    try:
        driver.implicitly_wait(2)
        ok = driver.get_screenshot_as_file(str(output_path))
        if not ok:
            return False, "Appium이 스크린샷 파일 저장 실패를 반환했습니다."
        return True, str(output_path)
    finally:
        try:
            driver.quit()
        except Exception:
            pass


def capture_slot_screenshot(settings: Settings, slot: dict[str, Any], output_dir: Path | None = None) -> dict[str, Any]:
    slot_name = str(slot.get("slot_name") or "slot")
    udid = infer_slot_udid(slot)
    appium_raw_url = str(slot.get("appium_url") or settings.appium_server_url)
    appium_url = resolve_appium_url(settings, appium_raw_url)
    target_dir = output_dir or (settings.output_dir / "screenshots")
    target = target_dir / f"manual_{safe_filename(slot_name)}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"

    if int(slot.get("enabled") if slot.get("enabled") is not None else 1) != 1:
        return {"ok": False, "slot_name": slot_name, "udid": udid, "path": "", "message": "비활성 슬롯입니다.", "method": "skip", "appium_url": appium_url}

    # 1) Prefer existing session screenshot. This works even while a worker is
    # running and avoids ADB visibility issues inside Docker.
    ok, session_id, message = _request_existing_session_screenshot(appium_url, target)
    if ok:
        return {"ok": True, "slot_name": slot_name, "udid": udid, "path": str(target), "message": message, "method": "appium-active-session", "session_id": session_id, "appium_url": appium_url}

    # 2) If the Appium server is idle, create a short native Settings session and
    # capture it. This does not require ChromeDriver.
    if not udid:
        return {"ok": False, "slot_name": slot_name, "udid": udid, "path": "", "message": f"UDID가 없습니다. ADB 동기화 후 다시 시도하세요. ({message})", "method": "appium", "appium_url": appium_url}
    try:
        ok2, message2 = _create_temp_session_screenshot(settings, slot, appium_url, udid, target)
        return {"ok": ok2, "slot_name": slot_name, "udid": udid, "path": str(target) if ok2 else "", "message": message2, "method": "appium-temp-session", "appium_url": appium_url}
    except Exception as exc:
        return {"ok": False, "slot_name": slot_name, "udid": udid, "path": "", "message": str(exc), "method": "appium-temp-session", "appium_url": appium_url}


def capture_slots_screenshots(settings: Settings, slots: list[dict[str, Any]], slot_names: list[str] | None = None) -> dict[str, Any]:
    requested = {str(name) for name in (slot_names or []) if str(name).strip()}
    rows = []
    for slot in slots:
        slot_name = str(slot.get("slot_name") or "")
        if requested and slot_name not in requested:
            continue
        rows.append(capture_slot_screenshot(settings, slot))
    saved = sum(1 for row in rows if row.get("ok"))
    failed = [row for row in rows if not row.get("ok")]
    return {"saved": saved, "failed": len(failed), "rows": rows}
