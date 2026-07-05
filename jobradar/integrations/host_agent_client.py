from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import requests


# 호스트 agent 결과 클래스는 처리 결과와 상태 정보를 한곳에 담아 전달합니다.
@dataclass
class HostAgentResult:
    ok: bool
    data: dict[str, Any]
    status_code: int = 0
    error: str = ""


# 로컬 PC의 호스트 에이전트와 통신해 에뮬레이터와 Appium을 제어합니다.
class HostAgentClient:
    # 객체가 만들어질 때 필요한 초기값과 의존성을 준비합니다.
    def __init__(self, base_url: str, timeout: float = 5.0):
        self.base_url = (base_url or "http://127.0.0.1:8767").rstrip("/")
        self.timeout = timeout

    def _url(self, path: str) -> str:
        if not path.startswith("/"):
            path = "/" + path
        return self.base_url + path

    def _request(self, method: str, path: str, timeout: float | None = None, **kwargs: Any) -> HostAgentResult:
        try:
            response = requests.request(method, self._url(path), timeout=float(timeout or self.timeout), **kwargs)
            try:
                data = response.json()
            except Exception:
                data = {"text": response.text}
            ok = response.ok and bool(data.get("ok", response.ok))
            if ok:
                return HostAgentResult(ok=True, data=data, status_code=response.status_code, error="")
            detail = str(data.get("message") or data.get("detail") or response.text)
            if response.status_code == 404 and path.startswith("/appium"):
                detail = (
                    "Host Agent가 구버전으로 실행 중입니다. "
                    "PowerShell에서 scripts\\start_host_agent.ps1 -NewWindow 로 재시작한 뒤 "
                    "scripts\\start_host_agent.ps1 -CheckOnly 로 확인하세요. "
                    f"missing endpoint: {path}"
                )
            return HostAgentResult(ok=False, data=data, status_code=response.status_code, error=detail)
        except Exception as exc:
            return HostAgentResult(ok=False, data={}, error=str(exc))

    def health(self) -> HostAgentResult:
        return self._request("GET", "/health")

    def routes(self) -> HostAgentResult:
        return self._request("GET", "/debug/routes")

    def emulator_windows(self) -> HostAgentResult:
        return self._request("GET", "/windows/emulators")


    def adb_devices(self) -> HostAgentResult:
        return self._request("GET", "/adb/devices")

    def appium_status(self, ports: list[int] | None = None, probe_timeout: float = 0.7) -> HostAgentResult:
        params = [("ports", int(p)) for p in (ports or []) if int(p) > 0]
        params.append(("timeout", float(probe_timeout)))
        return self._request("GET", "/appium/status", params=params, timeout=max(float(self.timeout), 15.0))

    def appium_start(self, ports: list[int] | None = None, host: str = "127.0.0.1", verify: bool = False) -> HostAgentResult:
        return self._request(
            "POST",
            "/appium/start",
            json={"ports": ports or [], "host": host, "verify": bool(verify), "status_timeout": 0.4},
            timeout=max(float(self.timeout), 30.0),
        )

    def appium_stop(self, ports: list[int] | None = None) -> HostAgentResult:
        return self._request("POST", "/appium/stop", json={"ports": ports or []}, timeout=max(float(self.timeout), 30.0))

    def open_screenshot_folder(self) -> HostAgentResult:
        return self._request("POST", "/screenshots/open-folder")

    def arrange(
        self,
        layout: str = "grid2x2",
        x: int = 20,
        y: int = 40,
        width: int = 430,
        height: int = 780,
        gap: int = 12,
        columns: int = 2,
        dry_run: bool = False,
    ) -> HostAgentResult:
        return self._request(
            "POST",
            "/windows/arrange",
            json={
                "layout": layout,
                "x": int(x),
                "y": int(y),
                "width": int(width),
                "height": int(height),
                "gap": int(gap),
                "columns": int(columns),
                "dry_run": bool(dry_run),
            },
        )
