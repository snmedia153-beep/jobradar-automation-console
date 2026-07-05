from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from jobradar.config import Settings
from jobradar.host_agent.window_manager import arrange_emulator_windows, list_windows, work_area
from jobradar.host_agent.appium_manager import adb_devices, appium_ports_status, open_screenshot_folder, start_ports, stop_ports

SERVICE_VERSION = "1.3.0"
CAPABILITIES = [
    "windows.emulators",
    "windows.arrange",
    "adb.devices",
    "appium.status",
    "appium.start",
    "appium.stop",
    "screenshots.open_folder",
]


# Appium 포트 요청 클래스는 관련 데이터와 기능을 한곳에 묶어 관리합니다.
class AppiumPortsRequest(BaseModel):
    ports: list[int] = Field(default_factory=list)
    host: str = "127.0.0.1"
    verify: bool = False
    status_timeout: float = 0.4


# arrange 요청 클래스는 관련 데이터와 기능을 한곳에 묶어 관리합니다.
class ArrangeRequest(BaseModel):
    layout: str = Field(default="grid2x2")
    x: int = 20
    y: int = 40
    width: int = 430
    height: int = 780
    gap: int = 12
    columns: int = 2
    titles: list[str] = Field(default_factory=list)
    dry_run: bool = False


app = FastAPI(
    title="JobRadar Windows Host Agent",
    description="Local Windows-only helper for emulator windows, ADB, Appium and screenshot folder actions.",
    version=SERVICE_VERSION,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def _default_ports() -> list[int]:
    # Always include Emulator A-D plus the USB Appium port.  Older setups or
    # .env files may have EMULATOR_SLOTS=4, but USB still needs 4731.
    settings = Settings()
    return settings.all_appium_ports()


def _service_info() -> dict[str, Any]:
    return {
        "ok": True,
        "service": "jobradar-host-agent",
        "version": SERVICE_VERSION,
        "message": "ready",
        "capabilities": CAPABILITIES,
        "work_area": work_area(),
        "endpoints": {
            "health": "/health",
            "routes": "/debug/routes",
            "adb_devices": "/adb/devices",
            "appium_status": "/appium/status",
            "appium_start": "/appium/start",
            "appium_stop": "/appium/stop",
            "emulator_windows": "/windows/emulators",
            "arrange_windows": "/windows/arrange",
            "open_screenshot_folder": "/screenshots/open-folder",
        },
    }


@app.get("/")
def root() -> dict[str, Any]:
    return _service_info()


@app.get("/health")
def health() -> dict[str, Any]:
    return _service_info()


@app.get("/version")
def version() -> dict[str, Any]:
    return _service_info()


@app.get("/debug/routes")
def debug_routes() -> dict[str, Any]:
    routes = []
    for route in app.routes:
        methods = sorted(getattr(route, "methods", []) or [])
        path = getattr(route, "path", "")
        if path:
            routes.append({"path": path, "methods": methods})
    return {"ok": True, "service": "jobradar-host-agent", "version": SERVICE_VERSION, "routes": routes}


@app.get("/adb/devices")
def host_adb_devices() -> dict[str, Any]:
    return adb_devices(Settings())


# Compatibility alias for callers that prefix host-agent endpoints with /api.
@app.get("/api/adb/devices")
def host_adb_devices_api_alias() -> dict[str, Any]:
    return host_adb_devices()


@app.get("/appium/status")
def host_appium_status(
    ports: list[int] | None = Query(default=None),
    timeout: float = Query(default=0.7, ge=0.2, le=5.0),
    include_pids: bool = Query(default=True),
) -> dict[str, Any]:
    settings = Settings()
    selected_ports = [int(p) for p in (ports or []) if int(p) > 0]
    return appium_ports_status(
        selected_ports or _default_ports(),
        host="127.0.0.1",
        settings=settings,
        timeout=float(timeout),
        include_pids=bool(include_pids),
    )


@app.get("/api/appium/status")
def host_appium_status_api_alias(
    ports: list[int] | None = Query(default=None),
    timeout: float = Query(default=0.7, ge=0.2, le=5.0),
    include_pids: bool = Query(default=True),
) -> dict[str, Any]:
    return host_appium_status(ports=ports, timeout=timeout, include_pids=include_pids)


@app.post("/appium/start")
def host_appium_start(body: AppiumPortsRequest | None = None) -> dict[str, Any]:
    body = body or AppiumPortsRequest()
    ports = body.ports or _default_ports()
    return start_ports(
        Settings(),
        ports,
        host=body.host or "127.0.0.1",
        verify=bool(body.verify),
        status_timeout=float(body.status_timeout or 0.4),
    )


@app.post("/api/appium/start")
def host_appium_start_api_alias(body: AppiumPortsRequest | None = None) -> dict[str, Any]:
    return host_appium_start(body)


@app.post("/appium/stop")
def host_appium_stop(body: AppiumPortsRequest | None = None) -> dict[str, Any]:
    body = body or AppiumPortsRequest()
    ports = body.ports or _default_ports()
    return stop_ports(ports)


@app.post("/api/appium/stop")
def host_appium_stop_api_alias(body: AppiumPortsRequest | None = None) -> dict[str, Any]:
    return host_appium_stop(body)


@app.post("/screenshots/open-folder")
def host_open_screenshot_folder() -> dict[str, Any]:
    return open_screenshot_folder(Settings())


@app.post("/api/screenshots/open-folder")
def host_open_screenshot_folder_api_alias() -> dict[str, Any]:
    return host_open_screenshot_folder()


@app.get("/windows/emulators")
def emulator_windows() -> dict[str, Any]:
    rows = [item.to_dict() for item in list_windows(emulators_only=True)]
    return {"ok": True, "count": len(rows), "windows": rows, "work_area": work_area()}


@app.post("/windows/arrange")
def arrange_windows(body: ArrangeRequest) -> dict[str, Any]:
    return arrange_emulator_windows(
        layout=body.layout,
        x=body.x,
        y=body.y,
        width=body.width,
        height=body.height,
        gap=body.gap,
        columns=body.columns,
        titles=body.titles,
        dry_run=body.dry_run,
    )
