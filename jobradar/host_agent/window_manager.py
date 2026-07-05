from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass, asdict
from typing import Any


user32 = ctypes.windll.user32 if hasattr(ctypes, "windll") else None


# 창 info 클래스는 처리 결과와 상태 정보를 한곳에 담아 전달합니다.
@dataclass
class WindowInfo:
    hwnd: int
    title: str
    pid: int
    x: int
    y: int
    width: int
    height: int

    # 객체 데이터를 딕셔너리로 바꿔 저장, API 응답, 화면 표시에서 쉽게 사용하게 합니다.
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# arrange 결과 클래스는 처리 결과와 상태 정보를 한곳에 담아 전달합니다.
@dataclass
class ArrangeResult:
    hwnd: int
    title: str
    ok: bool
    x: int
    y: int
    width: int
    height: int
    message: str = ""

    # 객체 데이터를 딕셔너리로 바꿔 저장, API 응답, 화면 표시에서 쉽게 사용하게 합니다.
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# rect 클래스는 관련 데이터와 기능을 한곳에 묶어 관리합니다.
class RECT(ctypes.Structure):
    _fields_ = [
        ("left", wintypes.LONG),
        ("top", wintypes.LONG),
        ("right", wintypes.LONG),
        ("bottom", wintypes.LONG),
    ]


def _require_windows() -> None:
    if user32 is None:
        raise RuntimeError("Windows 전용 기능입니다. Host Agent는 Windows PowerShell에서 실행해야 합니다.")


def _get_window_text(hwnd: int) -> str:
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buffer = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buffer, length + 1)
    return buffer.value or ""


def _get_pid(hwnd: int) -> int:
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


def _get_rect(hwnd: int) -> tuple[int, int, int, int]:
    rect = RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return 0, 0, 0, 0
    return int(rect.left), int(rect.top), int(rect.right - rect.left), int(rect.bottom - rect.top)


def _is_visible_top_window(hwnd: int) -> bool:
    if not user32.IsWindowVisible(hwnd):
        return False
    if user32.GetParent(hwnd):
        return False
    title = _get_window_text(hwnd).strip()
    if not title:
        return False
    # Skip tool/owned windows where possible.
    try:
        GWL_EXSTYLE = -20
        WS_EX_TOOLWINDOW = 0x00000080
        style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        if style & WS_EX_TOOLWINDOW:
            return False
    except Exception:
        pass
    return True


def _matches_emulator_title(title: str, tokens: list[str] | None = None) -> bool:
    low = title.lower()
    default_tokens = [
        "android emulator",
        "pixel_",
        "pixel ",
        "api_",
        "emulator-",
        "medium_tablet",
        "medium tablet",
    ]
    all_tokens = default_tokens + [t.lower() for t in (tokens or []) if t]
    return any(token and token in low for token in all_tokens)


def list_windows(tokens: list[str] | None = None, emulators_only: bool = True) -> list[WindowInfo]:
    _require_windows()
    windows: list[WindowInfo] = []

    EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def callback(hwnd: int, lparam: int) -> bool:
        try:
            if not _is_visible_top_window(hwnd):
                return True
            title = _get_window_text(hwnd).strip()
            if emulators_only and not _matches_emulator_title(title, tokens):
                return True
            x, y, w, h = _get_rect(hwnd)
            if w < 120 or h < 120:
                return True
            windows.append(WindowInfo(hwnd=int(hwnd), title=title, pid=_get_pid(hwnd), x=x, y=y, width=w, height=h))
        except Exception:
            pass
        return True

    user32.EnumWindows(EnumWindowsProc(callback), 0)
    windows.sort(key=lambda w: _sort_key(w.title))
    return windows


def _sort_key(title: str) -> tuple[int, str]:
    low = title.lower()
    priority = 50
    for idx, token in enumerate(["_a", " a", "-a", "_b", " b", "-b", "_c", " c", "-c", "_d", " d", "-d"]):
        if token in low:
            priority = idx
            break
    for port_idx, port in enumerate(["5554", "5656", "5658", "5660"]):
        if port in low:
            priority = min(priority, port_idx * 3)
    return priority, low


def work_area() -> dict[str, int]:
    _require_windows()
    rect = RECT()
    SPI_GETWORKAREA = 0x0030
    if user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(rect), 0):
        return {
            "x": int(rect.left),
            "y": int(rect.top),
            "width": int(rect.right - rect.left),
            "height": int(rect.bottom - rect.top),
        }
    return {"x": 0, "y": 0, "width": int(user32.GetSystemMetrics(0)), "height": int(user32.GetSystemMetrics(1))}


def _layout_positions(count: int, layout: str, x: int, y: int, width: int, height: int, gap: int, columns: int | None = None) -> list[tuple[int, int, int, int]]:
    area = work_area()
    layout = (layout or "grid2x2").strip().lower()
    if count <= 0:
        return []
    if layout in {"horizontal", "row", "가로"}:
        cols = count
    elif layout in {"vertical", "column", "세로"}:
        cols = 1
    else:
        cols = max(1, int(columns or 2))
    rows = (count + cols - 1) // cols

    x0 = int(x if x is not None else area["x"] + 20)
    y0 = int(y if y is not None else area["y"] + 40)
    w = int(width or 430)
    h = int(height or 780)
    gap = int(gap or 12)

    # Auto shrink to keep windows within the current work area.
    available_w = max(320, area["x"] + area["width"] - x0 - gap * (cols - 1) - 12)
    available_h = max(360, area["y"] + area["height"] - y0 - gap * (rows - 1) - 12)
    w = min(w, max(260, available_w // cols))
    h = min(h, max(360, available_h // rows))

    positions: list[tuple[int, int, int, int]] = []
    for i in range(count):
        col = i % cols
        row = i // cols
        positions.append((x0 + col * (w + gap), y0 + row * (h + gap), w, h))
    return positions


def arrange_emulator_windows(
    layout: str = "grid2x2",
    x: int = 20,
    y: int = 40,
    width: int = 430,
    height: int = 780,
    gap: int = 12,
    columns: int = 2,
    titles: list[str] | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    _require_windows()
    windows = list_windows(tokens=titles, emulators_only=True)
    positions = _layout_positions(len(windows), layout, x, y, width, height, gap, columns)
    results: list[ArrangeResult] = []
    SW_RESTORE = 9
    for win, pos in zip(windows, positions):
        px, py, pw, ph = pos
        ok = True
        msg = "preview" if dry_run else "moved"
        if not dry_run:
            try:
                user32.ShowWindow(wintypes.HWND(win.hwnd), SW_RESTORE)
                ok = bool(user32.MoveWindow(wintypes.HWND(win.hwnd), int(px), int(py), int(pw), int(ph), True))
                msg = "moved" if ok else "MoveWindow failed"
            except Exception as exc:
                ok = False
                msg = str(exc)
        results.append(ArrangeResult(hwnd=win.hwnd, title=win.title, ok=ok, x=px, y=py, width=pw, height=ph, message=msg))
    return {
        "ok": all(row.ok for row in results) if results else False,
        "count": len(results),
        "layout": layout,
        "work_area": work_area(),
        "windows": [w.to_dict() for w in windows],
        "results": [r.to_dict() for r in results],
        "message": f"에뮬레이터 창 {len(results)}개를 {layout} 레이아웃으로 배치했습니다." if results else "배치할 Android Emulator 창을 찾지 못했습니다.",
    }
