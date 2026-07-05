from __future__ import annotations

import base64
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import requests

try:
    from appium import webdriver
    from appium.options.android import UiAutomator2Options
except Exception:  # Appium client is optional for pure REST actions.
    webdriver = None  # type: ignore[assignment]
    UiAutomator2Options = None  # type: ignore[assignment]

from jobradar.config import Settings
from jobradar.device_farm.screenshots import infer_slot_udid, safe_filename
from jobradar.device_farm.url_utils import resolve_appium_url


# 기기 action 결과 클래스는 처리 결과와 상태 정보를 한곳에 담아 전달합니다.
@dataclass
class DeviceActionResult:
    slot_name: str
    action: str
    ok: bool
    message: str
    appium_url: str = ""
    session_id: str = ""
    udid: str = ""
    package_name: str = ""

    # 객체 데이터를 딕셔너리로 바꿔 저장, API 응답, 화면 표시에서 쉽게 사용하게 합니다.
    def to_dict(self) -> dict[str, Any]:
        return {
            "slot_name": self.slot_name,
            "action": self.action,
            "ok": self.ok,
            "message": self.message,
            "appium_url": self.appium_url,
            "session_id": self.session_id,
            "udid": self.udid,
            "package_name": self.package_name,
        }


def _capability(options: Any, key: str, value: Any) -> None:
    if value is None or value == "":
        return
    try:
        options.set_capability(key, value)
    except Exception:
        setattr(options, key.replace("appium:", ""), value)


def _slot_name(slot: dict[str, Any]) -> str:
    return str(slot.get("slot_name") or "slot")


def _slot_appium_url(settings: Settings, slot: dict[str, Any]) -> str:
    return resolve_appium_url(settings, str(slot.get("appium_url") or settings.appium_server_url)).rstrip("/")


def _request(method: str, url: str, timeout: float = 6.0, **kwargs: Any) -> tuple[bool, Any, str]:
    try:
        response = requests.request(method, url, timeout=timeout, **kwargs)
        text = response.text or ""
        try:
            payload = response.json()
        except Exception:
            payload = {"text": text}
        if response.ok:
            return True, payload, "OK"
        return False, payload, f"HTTP {response.status_code}: {text[:500]}"
    except Exception as exc:
        return False, None, str(exc)


def active_sessions(appium_url: str) -> list[str]:
    ok, payload, _ = _request("GET", f"{appium_url.rstrip('/')}/sessions")
    if not ok or not isinstance(payload, dict):
        return []
    sessions = payload.get("value") or []
    ids: list[str] = []
    if isinstance(sessions, list):
        for item in sessions:
            if isinstance(item, dict):
                session_id = str(item.get("id") or item.get("sessionId") or "").strip()
                if session_id:
                    ids.append(session_id)
    return ids


def _create_temp_settings_session(settings: Settings, slot: dict[str, Any], appium_url: str) -> tuple[str, Any]:
    udid = infer_slot_udid(slot)
    if not udid:
        raise RuntimeError("UDID가 없습니다. 먼저 ADB 동기화 또는 슬롯 설정을 확인하세요.")
    if webdriver is None or UiAutomator2Options is None:
        raise RuntimeError("Appium-Python-Client가 설치되어 있지 않아 임시 세션을 만들 수 없습니다.")

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
    driver = webdriver.Remote(appium_url, options=options)
    return str(getattr(driver, "session_id", "") or ""), driver


def _with_session(settings: Settings, slot: dict[str, Any], action: str, callback: Any) -> DeviceActionResult:
    slot_name = _slot_name(slot)
    appium_url = _slot_appium_url(settings, slot)
    udid = infer_slot_udid(slot)
    sessions = active_sessions(appium_url)
    if sessions:
        session_id = sessions[0]
        try:
            ok, message = callback(appium_url, session_id, False)
            return DeviceActionResult(slot_name, action, ok, message, appium_url, session_id, udid)
        except Exception as exc:
            return DeviceActionResult(slot_name, action, False, str(exc), appium_url, session_id, udid)

    driver = None
    session_id = ""
    try:
        session_id, driver = _create_temp_settings_session(settings, slot, appium_url)
        ok, message = callback(appium_url, session_id, True)
        return DeviceActionResult(slot_name, action, ok, message, appium_url, session_id, udid)
    except Exception as exc:
        return DeviceActionResult(slot_name, action, False, str(exc), appium_url, session_id, udid)
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass


def stop_now_slot(settings: Settings, slot: dict[str, Any]) -> DeviceActionResult:
    """Immediately terminate active Appium sessions for a slot.

    Worker cancellation should be written to DB/Redis by the caller first. Deleting
    the session forces any currently blocked Appium command to fail fast, so the
    worker can exit instead of waiting for a long page-load timeout.
    """
    slot_name = _slot_name(slot)
    appium_url = _slot_appium_url(settings, slot)
    udid = infer_slot_udid(slot)
    ids = active_sessions(appium_url)
    if not ids:
        return DeviceActionResult(slot_name, "immediate_stop", True, "활성 Appium 세션이 없습니다. 큐/DB 중지 요청만 반영되었습니다.", appium_url, "", udid)
    deleted = 0
    errors: list[str] = []
    for session_id in ids:
        ok, _, message = _request("DELETE", f"{appium_url}/session/{session_id}", timeout=8)
        if ok:
            deleted += 1
        else:
            errors.append(f"{session_id}: {message}")
    if errors:
        return DeviceActionResult(slot_name, "immediate_stop", deleted > 0, f"세션 {deleted}개 종료, 오류 {len(errors)}개: {'; '.join(errors)[:800]}", appium_url, ",".join(ids), udid)
    return DeviceActionResult(slot_name, "immediate_stop", True, f"활성 Appium 세션 {deleted}개를 즉시 종료했습니다.", appium_url, ",".join(ids), udid)


def home_slot(settings: Settings, slot: dict[str, Any]) -> DeviceActionResult:
    def callback(appium_url: str, session_id: str, temporary: bool) -> tuple[bool, str]:
        ok, _, message = _request("POST", f"{appium_url}/session/{session_id}/appium/device/press_keycode", json={"keycode": 3}, timeout=8)
        return ok, "홈 화면으로 이동했습니다." if ok else message

    return _with_session(settings, slot, "home", callback)


def launch_package_slot(settings: Settings, slot: dict[str, Any], package_name: str, activity_name: str = "") -> DeviceActionResult:
    package_name = str(package_name or "").strip()
    activity_name = str(activity_name or "").strip()
    if not package_name:
        return DeviceActionResult(_slot_name(slot), "launch_package", False, "패키지명이 비어 있습니다.", _slot_appium_url(settings, slot), "", infer_slot_udid(slot))

    def callback(appium_url: str, session_id: str, temporary: bool) -> tuple[bool, str]:
        # Prefer activate_app because it works with an existing browser session and
        # keeps Appium in control. Activity is accepted as optional fallback.
        ok, _, message = _request("POST", f"{appium_url}/session/{session_id}/appium/device/activate_app", json={"appId": package_name}, timeout=10)
        if ok:
            return True, f"{package_name} 실행 요청 완료"
        if activity_name:
            ok2, _, message2 = _request(
                "POST",
                f"{appium_url}/session/{session_id}/appium/device/start_activity",
                json={"appPackage": package_name, "appActivity": activity_name},
                timeout=10,
            )
            return ok2, f"{package_name}/{activity_name} 실행 요청 완료" if ok2 else f"activate_app 실패: {message}; start_activity 실패: {message2}"
        return False, message

    result = _with_session(settings, slot, "launch_package", callback)
    result.package_name = package_name
    return result


def close_all_and_home_slot(settings: Settings, slot: dict[str, Any], packages: list[str] | None = None) -> DeviceActionResult:
    packages = [p.strip() for p in (packages or [settings.appium_browser_package, "com.android.chrome", "org.chromium.chrome"]) if p and p.strip()]
    seen: set[str] = set()
    packages = [p for p in packages if not (p in seen or seen.add(p))]

    def callback(appium_url: str, session_id: str, temporary: bool) -> tuple[bool, str]:
        messages: list[str] = []
        # Move current web session to about:blank first when possible. This prevents
        # long Saramin pages from staying in the foreground after the control action.
        _request("POST", f"{appium_url}/session/{session_id}/url", json={"url": "about:blank"}, timeout=5)
        for package in packages:
            ok, _, message = _request("POST", f"{appium_url}/session/{session_id}/appium/device/terminate_app", json={"appId": package}, timeout=8)
            messages.append(f"{package}:{'OK' if ok else message[:120]}")
        ok_home, _, home_message = _request("POST", f"{appium_url}/session/{session_id}/appium/device/press_keycode", json={"keycode": 3}, timeout=8)
        messages.append(f"HOME:{'OK' if ok_home else home_message[:120]}")
        return ok_home or any("OK" in item for item in messages), " · ".join(messages)

    return _with_session(settings, slot, "close_all_home", callback)


def control_slots(
    settings: Settings,
    slots: list[dict[str, Any]],
    action: str,
    slot_names: list[str] | None = None,
    package_name: str = "",
    activity_name: str = "",
) -> list[dict[str, Any]]:
    selected = {str(name) for name in (slot_names or []) if str(name).strip()}
    action = (action or "").strip().lower().replace("-", "_")
    rows: list[dict[str, Any]] = []
    for slot in slots:
        slot_name = _slot_name(slot)
        if selected and slot_name not in selected:
            continue
        if action in {"immediate_stop", "stop_now", "force_stop"}:
            result = stop_now_slot(settings, slot)
        elif action in {"home", "go_home"}:
            result = home_slot(settings, slot)
        elif action in {"close_all_home", "close_home", "close_all_and_home"}:
            result = close_all_and_home_slot(settings, slot)
        elif action in {"launch_package", "start_package", "activate_app"}:
            result = launch_package_slot(settings, slot, package_name=package_name, activity_name=activity_name)
        else:
            result = DeviceActionResult(slot_name, action or "unknown", False, f"지원하지 않는 장치 제어 action입니다: {action}", _slot_appium_url(settings, slot), "", infer_slot_udid(slot), package_name)
        rows.append(result.to_dict())
    return rows
