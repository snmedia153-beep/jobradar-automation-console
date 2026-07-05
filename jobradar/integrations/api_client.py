from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import requests


# API 결과 클래스는 처리 결과와 상태 정보를 한곳에 담아 전달합니다.
@dataclass
class ApiResult:
    ok: bool
    data: dict[str, Any]
    status_code: int = 0
    error: str = ""


# 대시보드에서 JobRadar API 서버와 통신할 때 쓰는 클라이언트입니다.
class JobRadarApiClient:
    # 객체가 만들어질 때 필요한 초기값과 의존성을 준비합니다.
    def __init__(self, base_url: str, timeout: float = 5.0):
        self.base_url = (base_url or "http://127.0.0.1:8000").rstrip("/")
        self.timeout = timeout

    def _url(self, path: str) -> str:
        if not path.startswith("/"):
            path = "/" + path
        return self.base_url + path

    def _request(self, method: str, path: str, **kwargs: Any) -> ApiResult:
        try:
            timeout = kwargs.pop("timeout", self.timeout)
            response = requests.request(method, self._url(path), timeout=timeout, **kwargs)
            try:
                data = response.json()
            except Exception:
                data = {"text": response.text}
            ok = response.ok and bool(data.get("ok", response.ok))
            return ApiResult(ok=ok, data=data, status_code=response.status_code, error="" if ok else str(data.get("detail") or data.get("message") or response.text))
        except Exception as exc:
            return ApiResult(ok=False, data={}, error=str(exc))

    def get(self, path: str, params: dict[str, Any] | None = None, **kwargs: Any) -> ApiResult:
        return self._request("GET", path, params=params or {}, **kwargs)

    def post(self, path: str, payload: dict[str, Any] | None = None, **kwargs: Any) -> ApiResult:
        return self._request("POST", path, json=payload or {}, **kwargs)

    def health(self) -> ApiResult:
        return self.get("/health")

    def queue_jobs(self, mode: str = "appium", slot_names: list[str] | None = None, slot_count: int | None = None) -> ApiResult:
        return self.post("/api/jobs/queue", {"mode": mode, "slot_names": slot_names or [], "slot_count": slot_count})

    def cancel_jobs(self, mode: str = "appium", slot_names: list[str] | None = None, message: str = "GUI API 중지 요청") -> ApiResult:
        return self.post("/api/jobs/cancel", {"mode": mode, "slot_names": slot_names or [], "message": message})

    def reset_jobs(self, mode: str = "appium", slot_names: list[str] | None = None, message: str = "GUI API stale reset") -> ApiResult:
        return self.post("/api/jobs/reset", {"mode": mode, "slot_names": slot_names or [], "message": message})

    def recover_stale_jobs(self, mode: str = "appium", slot_names: list[str] | None = None, message: str = "GUI API heartbeat recovery") -> ApiResult:
        return self.post("/api/jobs/recover-stale", {"mode": mode, "slot_names": slot_names or [], "message": message})

    def retry_failed_jobs(self, mode: str = "appium", slot_names: list[str] | None = None, message: str = "GUI API failed retry") -> ApiResult:
        return self.post("/api/jobs/retry-failed", {"mode": mode, "slot_names": slot_names or [], "message": message})

    def worker_events(self, limit: int = 100, job_id: int | None = None, slot_name: str = "") -> ApiResult:
        params: dict[str, Any] = {"limit": limit}
        if job_id is not None:
            params["job_id"] = job_id
        if slot_name:
            params["slot_name"] = slot_name
        return self.get("/api/jobs/events", params=params)

    def run_worker_once(self, mode: str = "appium", slot_names: list[str] | None = None, max_jobs: int | None = None) -> ApiResult:
        return self.post("/api/worker/run-once", {"mode": mode, "slot_names": slot_names or [], "max_jobs": max_jobs})

    def capture_screenshots(self, slot_names: list[str] | None = None) -> ApiResult:
        return self.post("/api/devices/screenshots", {"slot_names": slot_names or []}, timeout=70.0)

    def device_control(
        self,
        action: str,
        slot_names: list[str] | None = None,
        package_name: str = "",
        activity_name: str = "",
        run_now: bool = False,
    ) -> ApiResult:
        return self.post(
            "/api/devices/control",
            {
                "action": action,
                "slot_names": slot_names or [],
                "package_name": package_name,
                "activity_name": activity_name,
                "run_now": run_now,
            },
            timeout=25.0,
        )


    def diagnostics_summary(self) -> ApiResult:
        return self.get("/api/diagnostics/summary")

    def appium_start(self, slot_names: list[str] | None = None) -> ApiResult:
        return self.post("/api/appium/start", {"slot_names": slot_names or []}, timeout=25.0)

    def appium_stop(self, slot_names: list[str] | None = None) -> ApiResult:
        return self.post("/api/appium/stop", {"slot_names": slot_names or []}, timeout=25.0)

    def screenshots(self, limit: int = 80) -> ApiResult:
        return self.get("/api/screenshots", params={"limit": int(limit)})

    def open_screenshot_folder(self) -> ApiResult:
        return self.post("/api/screenshots/open-folder", {}, timeout=15.0)

    def host_agent_health(self) -> ApiResult:
        return self.get("/api/host-agent/health")

    def emulator_windows(self) -> ApiResult:
        return self.get("/api/host-agent/emulator-windows")

    def arrange_emulators(
        self,
        layout: str = "grid2x2",
        x: int = 20,
        y: int = 40,
        width: int = 430,
        height: int = 780,
        gap: int = 12,
        columns: int = 2,
        dry_run: bool = False,
    ) -> ApiResult:
        return self.post(
            "/api/host-agent/arrange-emulators",
            {
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
