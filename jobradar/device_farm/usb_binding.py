from __future__ import annotations

from typing import Any

from jobradar.config import Settings
from jobradar.db.repository import JobRadarRepository
from jobradar.device_farm.adb import list_devices


def _text(value: Any) -> str:
    return str(value or "").strip()


def _serial(row: Any) -> str:
    if isinstance(row, dict):
        return _text(row.get("serial") or row.get("udid") or row.get("device_id"))
    return _text(getattr(row, "udid", "") or getattr(row, "serial", ""))


def _state(row: Any) -> str:
    if isinstance(row, dict):
        return _text(row.get("status") or row.get("state") or "device")
    return _text(getattr(row, "state", "") or getattr(row, "status", "") or "device")


def _model(row: Any) -> str:
    if isinstance(row, dict):
        return _text(row.get("model") or row.get("product") or row.get("raw"))
    return _text(getattr(row, "model", "") or getattr(row, "raw", ""))


def is_emulator_serial(serial: str) -> bool:
    serial = _text(serial).lower()
    return serial.startswith("emulator-") or serial.startswith("localhost:")


def _expected_emulator_serials(slots: list[dict[str, Any]]) -> set[str]:
    expected: set[str] = set()
    for slot in slots:
        slot_name = _text(slot.get("slot_name"))
        device_type = _text(slot.get("device_type")).lower()
        console_port = int(slot.get("emulator_console_port") or 0)
        udid = _text(slot.get("udid"))
        if device_type == "emulator" or slot_name.startswith("Emulator"):
            if console_port:
                expected.add(f"emulator-{console_port}")
            if udid:
                expected.add(udid)
    return expected


def _find_usb_slot(slots: list[dict[str, Any]]) -> dict[str, Any] | None:
    for slot in slots:
        if _text(slot.get("slot_name")) == "USB Device":
            return slot
    for slot in slots:
        if _text(slot.get("device_type")).lower() == "usb":
            return slot
    return None


def normalize_adb_rows(rows: list[Any]) -> list[dict[str, str]]:
    normalized: list[dict[str, str]] = []
    for row in rows or []:
        serial = _serial(row)
        if not serial:
            continue
        normalized.append({
            "serial": serial,
            "status": _state(row),
            "model": _model(row),
        })
    return normalized


def choose_usb_serial(adb_rows: list[Any], slots: list[dict[str, Any]]) -> dict[str, str]:
    """Pick a physical USB device from ADB rows.

    Real devices have serials such as R3CX..., while emulators are normally
    emulator-5554, emulator-5656, etc.  ADB order is not stable: Samsung/USB
    devices often appear before emulators, so slot sync must exclude emulator
    serials explicitly rather than relying on list order.
    """
    rows = normalize_adb_rows(adb_rows)
    expected_emulators = _expected_emulator_serials(slots)
    for row in rows:
        serial = row["serial"]
        status = row["status"].lower()
        if status != "device":
            continue
        if serial in expected_emulators or is_emulator_serial(serial):
            continue
        return row
    return {}


def ensure_usb_slot_bound(
    settings: Settings,
    repo: JobRadarRepository,
    adb_rows: list[Any] | None = None,
    source: str = "local-adb",
    force: bool = False,
) -> dict[str, Any]:
    """Ensure the USB Device slot has a real ADB UDID.

    This is intentionally called immediately before screenshot/control actions,
    not only from the manual ADB sync button.  In Docker mode the API process may
    not see host USB devices directly, so callers can pass Host Agent /adb/devices
    rows.  The function writes the detected physical serial to device_slots.udid.
    """
    slots = repo.list_device_slots()
    usb_slot = _find_usb_slot(slots)
    if usb_slot is None:
        return {"ok": False, "changed": False, "message": "USB Device 슬롯이 없습니다.", "source": source, "serial": ""}

    current = _text(usb_slot.get("udid"))
    if adb_rows is None:
        try:
            adb_rows = list_devices(settings)
        except Exception as exc:
            adb_rows = []
            return {"ok": False, "changed": False, "message": f"ADB 장치 목록 조회 실패: {exc}", "source": source, "serial": current}

    normalized = normalize_adb_rows(adb_rows)
    chosen = choose_usb_serial(normalized, slots)
    if not chosen:
        message = "ADB에서 실제 USB 기기를 찾지 못했습니다. USB 디버깅 허용/RSA 승인/화면 잠금을 확인하세요."
        if current:
            return {"ok": True, "changed": False, "message": f"{message} 기존 UDID 유지: {current}", "source": source, "serial": current, "devices": normalized}
        return {"ok": False, "changed": False, "message": message, "source": source, "serial": "", "devices": normalized}

    serial = chosen["serial"]
    changed = force or serial != current
    if changed or not current:
        note = f"USB 자동 바인딩({source}): {serial} model={chosen.get('model') or '-'}"
        repo.update_device_slot_runtime(_text(usb_slot.get("slot_name")) or "USB Device", status="connected", udid=serial, notes=note)
    return {
        "ok": True,
        "changed": bool(changed),
        "message": f"USB Device UDID={serial} ({'updated' if changed else 'unchanged'})",
        "source": source,
        "serial": serial,
        "devices": normalized,
    }
