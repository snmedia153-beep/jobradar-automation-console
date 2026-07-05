from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from jobradar.config import Settings
from jobradar.db.repository import JobRadarRepository
from jobradar.device_farm.adb import list_devices as list_adb_devices, stop_device as stop_adb_device
from jobradar.device_farm.appium_server import check_appium_status, start_appium
from jobradar.device_farm.worker import queue_default_collection, run_worker_once, sync_detected_devices
from jobradar.device_farm.usb_binding import ensure_usb_slot_bound
from jobradar.device_farm.url_utils import resolve_appium_url
from jobradar.device_farm.screenshots import capture_slots_screenshots
from jobradar.device_farm.device_actions import control_slots
from jobradar.integrations.redis_health import check_redis
from jobradar.integrations.redis_queue import RedisJobQueue
from jobradar.integrations.host_agent_client import HostAgentClient
from jobradar.models import AlertRule, SearchProfile
from jobradar.services.alert_service import AlertService

settings = Settings()
repo = JobRadarRepository(settings.database_url)


def _redis_queue() -> RedisJobQueue | None:
    if not settings.redis_queue_enabled:
        return None
    try:
        queue = RedisJobQueue.from_settings(settings)
        queue.ping()
        return queue
    except Exception:
        return None




def _host_agent() -> HostAgentClient | None:
    if not getattr(settings, "host_agent_enabled", True):
        return None
    client = HostAgentClient(settings.host_agent_url, timeout=float(settings.host_agent_timeout_seconds))
    health = client.health()
    if health.ok:
        return client
    return None



def _ensure_usb_binding_from_host_agent(reason: str = "api") -> dict[str, Any]:
    """Bind USB Device slot using Host Agent ADB rows when available.

    Docker API containers usually cannot see the Windows host USB stack directly.
    The Host Agent can, so device control/screenshot endpoints call this just
    before reading slots.  This prevents USB Device from failing with
    'UDID가 없습니다' even though Host Agent /adb/devices shows the phone.
    """
    client = _host_agent()
    if client is not None:
        result = client.adb_devices()
        if result.ok:
            rows = result.data.get("devices") or []
            return ensure_usb_slot_bound(settings, repo, adb_rows=rows, source=f"host-agent:{reason}")
    return ensure_usb_slot_bound(settings, repo, source=f"api-local:{reason}")


def _slot_ports(slot_names: list[str] | None = None) -> list[int]:
    names = set(slot_names or [])
    ports: list[int] = []
    for slot in repo.list_device_slots():
        slot_name = str(slot.get("slot_name") or "")
        if names and slot_name not in names:
            continue
        port = int(slot.get("appium_port") or 0)
        if port > 0:
            ports.append(port)
    if not ports:
        ports = settings.all_appium_ports()
    # De-duplicate while preserving order.
    deduped: list[int] = []
    for port in ports:
        if int(port) not in deduped:
            deduped.append(int(port))
    return deduped


def _screenshot_rows(limit: int = 80) -> list[dict[str, Any]]:
    folder = settings.output_dir / "screenshots"
    folder.mkdir(parents=True, exist_ok=True)
    files = sorted(
        [p for p in folder.glob("*.png") if p.is_file()],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )[: max(1, int(limit))]
    rows: list[dict[str, Any]] = []
    for p in files:
        stat = p.stat()
        rows.append({
            "name": p.name,
            "path": str(p),
            "size_bytes": stat.st_size,
            "modified_at": stat.st_mtime,
        })
    return rows

def _worker_types(mode: str | None) -> list[str] | None:
    mode = (mode or "all").strip().lower()
    if mode == "appium":
        return ["appium_collect_profile"]
    if mode == "playwright":
        return ["collect_profile"]
    return None


def _slots(value: Any) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, str):
        items = [part.strip() for part in value.split(",") if part.strip()]
        return items or None
    if isinstance(value, list):
        items = [str(part).strip() for part in value if str(part).strip()]
        return items or None
    return None


def _ok(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    data = {"ok": True}
    if payload:
        data.update(payload)
    return data


# API 서버가 켜지고 꺼질 때 필요한 초기화와 정리 작업을 담당합니다.
@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.ensure_dirs()
    repo.init_db()
    repo.seed_device_slots(
        slot_count=settings.emulator_slots,
        appium_host=settings.appium_host,
        appium_base_port=settings.appium_base_port,
        appium_port_step=settings.appium_port_step,
        system_port_base=settings.appium_system_port_base,
        mjpeg_port_base=settings.appium_mjpeg_port_base,
        chromedriver_port_base=settings.appium_chromedriver_port_base,
        emulator_port_pairs=settings.parsed_emulator_port_pairs(),
    )
    yield


app = FastAPI(
    title="JobRadar Control API",
    description="JobRadar GUI/Worker control plane. Streamlit talks to this API instead of touching Redis/Postgres directly.",
    version="7.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health() -> dict[str, Any]:
    db = repo.check_connection()
    redis = check_redis(settings.redis_url)
    return _ok(
        {
            "service": "jobradar-api",
            "database": db,
            "redis": {"ok": redis.ok, "url": redis.url, "message": redis.message},
            "docker_mode": settings.docker_mode,
            "api_url": settings.api_url,
        }
    )


@app.get("/api/health")
def api_health() -> dict[str, Any]:
    return health()


@app.get("/api/db/check")
def db_check() -> dict[str, Any]:
    return _ok(repo.check_connection())


@app.get("/api/redis/check")
def redis_check() -> dict[str, Any]:
    info = check_redis(settings.redis_url)
    return {"ok": info.ok, "url": info.url, "message": info.message}


@app.get("/api/dashboard/stats")
def dashboard_stats() -> dict[str, Any]:
    redis_status: dict[str, Any] | None = None
    queue = _redis_queue()
    if queue is not None:
        status = queue.status()
        redis_status = status.__dict__
    return _ok({"stats": repo.stats(), "redis": redis_status, "database_backend": repo.backend_name})


@app.post("/api/slots/init")
def slots_init(body: dict[str, Any] | None = None) -> dict[str, Any]:
    body = body or {}
    slot_count = int(body.get("slot_count") or settings.emulator_slots)
    count = repo.seed_device_slots(
        slot_count=slot_count,
        appium_host=str(body.get("appium_host") or settings.appium_host),
        appium_base_port=int(body.get("appium_base_port") or settings.appium_base_port),
        appium_port_step=int(body.get("appium_port_step") or settings.appium_port_step),
        system_port_base=int(body.get("system_port_base") or settings.appium_system_port_base),
        mjpeg_port_base=int(body.get("mjpeg_port_base") or settings.appium_mjpeg_port_base),
        chromedriver_port_base=int(body.get("chromedriver_port_base") or settings.appium_chromedriver_port_base),
        emulator_port_pairs=settings.parsed_emulator_port_pairs(),
    )
    return _ok({"count": count, "slots": repo.list_device_slots()})


@app.get("/api/slots")
def slots() -> dict[str, Any]:
    return _ok({"slots": repo.list_device_slots()})


@app.post("/api/slots/{slot_name}")
def save_slot(slot_name: str, body: dict[str, Any]) -> dict[str, Any]:
    current = repo.get_device_slot(slot_name) or {}
    repo.upsert_device_slot(
        slot_name,
        avd_name=str(body.get("avd_name", current.get("avd_name") or "")),
        udid=str(body.get("udid", current.get("udid") or "")),
        proxy_name=str(body.get("proxy_name", current.get("proxy_name") or "")),
        status=str(body.get("status", current.get("status") or "idle")),
        notes=str(body.get("notes", current.get("notes") or "")),
        appium_url=str(body.get("appium_url", current.get("appium_url") or "")),
        appium_port=int(body.get("appium_port") or current.get("appium_port") or 0),
        system_port=int(body.get("system_port") or current.get("system_port") or 0),
        mjpeg_server_port=int(body.get("mjpeg_server_port") or current.get("mjpeg_server_port") or 0),
        chromedriver_port=int(body.get("chromedriver_port") or current.get("chromedriver_port") or 0),
        emulator_console_port=int(body.get("emulator_console_port") or current.get("emulator_console_port") or 0),
        emulator_adb_port=int(body.get("emulator_adb_port") or current.get("emulator_adb_port") or 0),
        device_type=str(body.get("device_type", current.get("device_type") or "")),
        assigned_profile_name=str(body.get("assigned_profile_name", current.get("assigned_profile_name") or "")),
        enabled=1 if body.get("enabled", current.get("enabled", 1)) else 0,
    )
    return _ok({"slot": repo.get_device_slot(slot_name)})


@app.post("/api/slots/{slot_name}/profile")
def assign_slot_profile(slot_name: str, body: dict[str, Any]) -> dict[str, Any]:
    profile_name = str(body.get("profile_name") or "")
    repo.set_device_slot_profile(slot_name, profile_name)
    return _ok({"slot": repo.get_device_slot(slot_name)})


@app.post("/api/slots/{slot_name}/enabled")
def set_slot_enabled(slot_name: str, body: dict[str, Any]) -> dict[str, Any]:
    repo.set_device_slot_enabled(slot_name, 1 if body.get("enabled", True) else 0)
    return _ok({"slot": repo.get_device_slot(slot_name)})


@app.get("/api/profiles")
def profiles(enabled_only: bool = False, limit: int = 200) -> dict[str, Any]:
    return _ok({"profiles": [p.to_dict() for p in repo.list_search_profiles(enabled_only=enabled_only, limit=limit)]})


@app.post("/api/profiles/seed")
def seed_profiles() -> dict[str, Any]:
    count = repo.seed_default_profiles(settings.target_url)
    return _ok({"count": count, "profiles": [p.to_dict() for p in repo.list_search_profiles(enabled_only=False, limit=200)]})


@app.post("/api/profiles")
def create_profile(body: dict[str, Any]) -> dict[str, Any]:
    profile = SearchProfile(
        campaign_name=str(body.get("campaign_name") or settings.default_campaign_name),
        name=str(body.get("name") or body.get("keyword") or "새 검색 프로필"),
        keyword=str(body.get("keyword") or body.get("name") or ""),
        target_url=str(body.get("target_url") or settings.target_url),
        enabled=1 if body.get("enabled", True) else 0,
        priority=int(body.get("priority") or 100),
        max_items=int(body.get("max_items") or settings.max_items),
        scroll_times=int(body.get("scroll_times") or settings.scroll_times),
    )
    repo.upsert_campaign(profile.campaign_name, "API에서 생성된 캠페인", enabled=1)
    profile_id = repo.upsert_search_profile(profile)
    return _ok({"id": profile_id})


@app.get("/api/results")
def results(
    limit: int = 300,
    keyword: str = "",
    emulator_slot: str = "",
    status: str = "",
) -> dict[str, Any]:
    return _ok({"results": repo.list_jobs(limit=limit, keyword=keyword, emulator_slot=emulator_slot, status=status)})


@app.get("/api/runs")
def runs(limit: int = 100) -> dict[str, Any]:
    return _ok({"runs": repo.list_runs(limit=limit)})


@app.get("/api/jobs/worker")
def worker_jobs(limit: int = 150, statuses: str = "") -> dict[str, Any]:
    status_list = [item.strip() for item in statuses.split(",") if item.strip()] or None
    return _ok({"jobs": repo.list_worker_jobs(limit=limit, statuses=status_list)})


@app.post("/api/jobs/queue")
def queue_jobs(body: dict[str, Any] | None = None) -> dict[str, Any]:
    body = body or {}
    mode = str(body.get("mode") or "appium")
    slot_names = _slots(body.get("slot_names") if "slot_names" in body else body.get("slots"))
    slot_count = int(body.get("slot_count") or settings.emulator_slots)
    count = queue_default_collection(settings, repo, slot_count=slot_count, mode=mode, slot_names=slot_names)
    queue = _redis_queue()
    redis_status = queue.status().__dict__ if queue is not None else None
    return _ok({"queued": count, "mode": mode, "slots": slot_names, "redis": redis_status})


@app.post("/api/jobs/cancel")
def cancel_jobs(body: dict[str, Any] | None = None) -> dict[str, Any]:
    body = body or {}
    slot_names = _slots(body.get("slot_names") if "slot_names" in body else body.get("slots"))
    job_types = _worker_types(str(body.get("mode") or body.get("type") or "all"))
    message = str(body.get("message") or "API 중지 요청")
    sqlite_count = repo.cancel_worker_jobs(slot_names=slot_names, job_types=job_types, message=message)
    redis_count = 0
    queue = _redis_queue()
    if queue is not None:
        redis_count = queue.cancel_jobs(slot_names=slot_names, job_types=job_types, message=message)
    return _ok({"sqlite": sqlite_count, "redis": redis_count})


@app.post("/api/jobs/reset")
def reset_jobs(body: dict[str, Any] | None = None) -> dict[str, Any]:
    body = body or {}
    slot_names = _slots(body.get("slot_names") if "slot_names" in body else body.get("slots"))
    job_types = _worker_types(str(body.get("mode") or body.get("type") or "all"))
    message = str(body.get("message") or "API stale running reset")
    count = repo.reset_stale_worker_jobs(slot_names=slot_names, job_types=job_types, message=message)
    return _ok({"reset": count})


@app.post("/api/jobs/recover-stale")
def recover_stale_jobs(body: dict[str, Any] | None = None) -> dict[str, Any]:
    body = body or {}
    slot_names = _slots(body.get("slot_names") if "slot_names" in body else body.get("slots"))
    job_types = _worker_types(str(body.get("mode") or body.get("type") or "all"))
    stale_after = int(body.get("stale_after_seconds") or settings.worker_stale_after_seconds)
    message = str(body.get("message") or "API heartbeat stale recovery")
    result = repo.recover_stale_worker_jobs(
        stale_after_seconds=stale_after,
        slot_names=slot_names,
        job_types=job_types,
        message=message,
        auto_retry=settings.worker_auto_retry,
    )
    queue = _redis_queue()
    redis_recovered = queue.recover_stale_processing(stale_after) if queue is not None else 0
    return _ok({"sqlite": result, "redis_recovered": redis_recovered})


@app.post("/api/jobs/retry-failed")
def retry_failed_jobs(body: dict[str, Any] | None = None) -> dict[str, Any]:
    body = body or {}
    slot_names = _slots(body.get("slot_names") if "slot_names" in body else body.get("slots"))
    job_types = _worker_types(str(body.get("mode") or body.get("type") or "all"))
    message = str(body.get("message") or "API failed job retry")
    count = repo.retry_failed_worker_jobs(slot_names=slot_names, job_types=job_types, message=message)
    rows = repo.list_worker_jobs(limit=200, statuses=["retry_wait"])
    queue = _redis_queue()
    redis_count = queue.enqueue_jobs(rows) if queue is not None and rows else 0
    return _ok({"retried": count, "redis_enqueued": redis_count})


@app.get("/api/jobs/events")
def worker_events(limit: int = 100, job_id: int | None = None, slot_name: str = "") -> dict[str, Any]:
    return _ok({"events": repo.list_worker_events(limit=limit, job_id=job_id, slot_name=slot_name)})


@app.get("/api/jobs/{job_id}/events")
def worker_job_events(job_id: int, limit: int = 100) -> dict[str, Any]:
    return _ok({"events": repo.list_worker_events(limit=limit, job_id=job_id)})


@app.get("/api/logs/slot/{slot_name}")
def slot_log(slot_name: str, lines: int | None = None) -> dict[str, Any]:
    safe_name = slot_name.replace(" ", "_").replace("/", "_").replace("\\", "_")
    candidates = [
        settings.output_dir / "logs" / f"appium_{safe_name}.log",
        settings.output_dir / "logs" / f"worker_{safe_name}.log",
    ]
    limit = max(20, min(1000, int(lines or settings.slot_log_tail_lines)))
    for path in candidates:
        if path.exists():
            content = path.read_text(encoding="utf-8", errors="ignore").splitlines()[-limit:]
            return _ok({"slot_name": slot_name, "path": str(path), "lines": content})
    return _ok({"slot_name": slot_name, "path": "", "lines": ["아직 슬롯 로그 파일이 없습니다."]})


@app.post("/api/worker/run-once")
def run_worker(body: dict[str, Any] | None = None) -> dict[str, Any]:
    body = body or {}
    mode = str(body.get("mode") or "appium")
    worker_types = _worker_types(mode)
    if mode == "all":
        worker_types = None
    slot_names = _slots(body.get("slot_names") if "slot_names" in body else body.get("slots"))
    max_jobs = int(body.get("max_jobs") or max(1, len(slot_names or []) or settings.emulator_slots))
    sync_detected_devices(settings, repo)
    rows = run_worker_once(settings, repo, max_jobs=max_jobs, worker_types=worker_types, slot_names=slot_names)
    return _ok({"processed": len(rows), "results": rows})


@app.get("/api/redis/queue")
def redis_queue() -> dict[str, Any]:
    queue = _redis_queue()
    if queue is None:
        return {"ok": False, "message": "Redis queue disabled or unavailable", "jobs": []}
    return _ok({"status": queue.status().__dict__, "jobs": queue.list_jobs(limit=200)})


@app.get("/api/redis/events")
def redis_events(limit: int = 50) -> dict[str, Any]:
    queue = _redis_queue()
    if queue is None:
        return {"ok": False, "message": "Redis queue disabled or unavailable", "events": []}
    return _ok({"events": queue.recent_events(limit=limit)})


@app.post("/api/redis/drain")
def redis_drain() -> dict[str, Any]:
    queue = _redis_queue()
    if queue is None:
        return {"ok": False, "message": "Redis queue disabled or unavailable", "drained": 0}
    return _ok({"drained": queue.drain()})


@app.get("/api/host-agent/health")
def host_agent_health() -> dict[str, Any]:
    client = HostAgentClient(settings.host_agent_url, timeout=float(settings.host_agent_timeout_seconds))
    result = client.health()
    if result.ok:
        return _ok({"host_agent": result.data, "url": settings.host_agent_url})
    return {"ok": False, "url": settings.host_agent_url, "message": result.error or "Host Agent 연결 실패"}


@app.get("/api/host-agent/emulator-windows")
def host_agent_emulator_windows() -> dict[str, Any]:
    client = _host_agent()
    if client is None:
        return {"ok": False, "url": settings.host_agent_url, "message": "Host Agent가 실행 중이 아니거나 연결할 수 없습니다.", "windows": []}
    result = client.emulator_windows()
    if not result.ok:
        return {"ok": False, "url": settings.host_agent_url, "message": result.error or "창 목록 조회 실패", "windows": []}
    return _ok({"url": settings.host_agent_url, **result.data})


@app.post("/api/host-agent/arrange-emulators")
def host_agent_arrange_emulators(body: dict[str, Any] | None = None) -> dict[str, Any]:
    body = body or {}
    client = _host_agent()
    if client is None:
        return {"ok": False, "url": settings.host_agent_url, "message": "Host Agent가 실행 중이 아닙니다. Windows PowerShell에서 scripts\\start_host_agent.ps1을 먼저 실행하세요.", "results": []}
    result = client.arrange(
        layout=str(body.get("layout") or "grid2x2"),
        x=int(body.get("x") or 20),
        y=int(body.get("y") or 40),
        width=int(body.get("width") or 430),
        height=int(body.get("height") or 780),
        gap=int(body.get("gap") or 12),
        columns=int(body.get("columns") or 2),
        dry_run=bool(body.get("dry_run") or False),
    )
    status = "completed" if result.ok else "failed"
    repo.create_operation_command(
        "arrange_emulator_windows",
        actor="admin",
        status=status,
        payload=result.data,
        message=str(result.data.get("message") if result.data else result.error),
    )
    if result.ok:
        return _ok({"url": settings.host_agent_url, **result.data})
    return {"ok": False, "url": settings.host_agent_url, "message": result.error or result.data.get("message") or "창 정렬 실패", **(result.data or {})}


@app.get("/api/appium/health")
def appium_health(slot: list[str] | None = Query(default=None)) -> dict[str, Any]:
    selected = set(_slots(slot) or [])
    rows: list[dict[str, Any]] = []
    for item in repo.list_device_slots():
        slot_name = str(item.get("slot_name") or "")
        if selected and slot_name not in selected:
            continue
        raw_url = str(item.get("appium_url") or settings.appium_server_url)
        worker_url = resolve_appium_url(settings, raw_url)
        ok, message = check_appium_status(worker_url)
        rows.append({"slot_name": slot_name, "url": raw_url, "worker_url": worker_url, "ok": ok, "message": message})
    return _ok({"health": rows})


@app.post("/api/appium/start")
def appium_start(body: dict[str, Any] | None = None) -> dict[str, Any]:
    body = body or {}
    slot_names = _slots(body.get("slot_names") if "slot_names" in body else body.get("slots")) or []
    ports = _slot_ports(slot_names)
    client = _host_agent()
    if client is not None:
        health = client.health()
        if health.ok:
            capabilities = set(health.data.get("capabilities") or [])
            if "appium.start" not in capabilities:
                message = (
                    "Host Agent가 구버전입니다. Windows PowerShell에서 "
                    "scripts\\start_host_agent.ps1 -NewWindow 로 재시작한 뒤 "
                    "scripts\\start_host_agent.ps1 -CheckOnly 로 확인하세요."
                )
                repo.create_operation_command("appium_start_all", actor="admin", status="failed", payload=health.data, message=message)
                return {"ok": False, "source": "host-agent-old", "message": message, "ports": ports, "host_agent": health.data}
        result = client.appium_start(ports=ports, host="127.0.0.1")
        if result.ok:
            repo.create_operation_command("appium_start_all", actor="admin", status="completed", payload=result.data, message=f"Host Agent Appium 시작 요청: {len(ports)}개 포트")
            return _ok({"source": "host-agent", "expected_count": len(ports), **result.data})
        repo.create_operation_command("appium_start_all", actor="admin", status="failed", payload=result.data, message=result.error or "Host Agent Appium 시작 실패")
        return {"ok": False, "source": "host-agent", "message": result.error, "ports": ports, "host_agent": health.data if health.ok else {"error": health.error}}

    # Fallback: only works when API itself runs on Windows host. Docker containers
    # normally cannot start/stop Windows host Appium processes.
    rows: list[dict[str, Any]] = []
    names = set(slot_names)
    for slot in repo.list_device_slots():
        slot_name = str(slot.get("slot_name") or "")
        if names and slot_name not in names:
            continue
        port = int(slot.get("appium_port") or settings.appium_base_port)
        pid, message = start_appium(settings, port=port, host=settings.appium_host, log_name=f"appium_{slot_name.replace(' ', '_')}.log")
        repo.update_device_slot_runtime(slot_name, "appium_starting" if pid else "appium_failed", notes=f"pid={pid} {message}")
        rows.append({"slot_name": slot_name, "port": port, "pid": pid, "message": message})
    return _ok({"source": "api-local", "started": rows})


@app.post("/api/appium/stop")
def appium_stop(body: dict[str, Any] | None = None) -> dict[str, Any]:
    body = body or {}
    slot_names = _slots(body.get("slot_names") if "slot_names" in body else body.get("slots")) or []
    ports = _slot_ports(slot_names)
    # Stop jobs first so worker state does not stay running while the server is killed.
    sqlite_count = repo.cancel_worker_jobs(slot_names=slot_names or None, job_types=["appium_collect_profile"], message="Appium 서버 전체 중지 요청")
    redis_count = 0
    queue = _redis_queue()
    if queue is not None:
        redis_count = queue.cancel_jobs(slot_names=slot_names or None, job_types=["appium_collect_profile"], message="Appium 서버 전체 중지 요청")
    client = _host_agent()
    if client is None:
        return {"ok": False, "message": "Host Agent가 실행 중이 아니어서 Windows Appium 서버를 중지할 수 없습니다.", "sqlite": sqlite_count, "redis": redis_count, "ports": ports}
    result = client.appium_stop(ports=ports)
    repo.create_operation_command("appium_stop_all", actor="admin", status="completed" if result.ok else "failed", payload=result.data, message=f"Host Agent Appium 중지 요청: {len(ports)}개 포트")
    if result.ok:
        return _ok({"source": "host-agent", "sqlite": sqlite_count, "redis": redis_count, **result.data})
    return {"ok": False, "source": "host-agent", "message": result.error, "sqlite": sqlite_count, "redis": redis_count, "ports": ports}


@app.post("/api/devices/sync")
def devices_sync() -> dict[str, Any]:
    count = sync_detected_devices(settings, repo)
    usb_binding = _ensure_usb_binding_from_host_agent("devices-sync")
    return _ok({"updated": count, "usb_binding": usb_binding, "slots": repo.list_device_slots()})


@app.get("/api/devices/adb")
def devices_adb() -> dict[str, Any]:
    return _ok({"devices": [device.to_dict() for device in list_adb_devices(settings)]})




@app.post("/api/devices/screenshots")
def devices_screenshots(body: dict[str, Any] | None = None) -> dict[str, Any]:
    body = body or {}
    slot_names = _slots(body.get("slot_names")) or []
    usb_binding = _ensure_usb_binding_from_host_agent("screenshots")
    slots = repo.list_device_slots()
    result = capture_slots_screenshots(settings, slots, slot_names=slot_names)
    repo.create_operation_command(
        "screenshot_all",
        actor="admin",
        status="completed" if int(result.get("saved") or 0) else "failed",
        payload=result,
        message=f"Appium 스크린샷 저장 {int(result.get('saved') or 0)}건 · 실패 {int(result.get('failed') or 0)}건",
    )
    result["usb_binding"] = usb_binding
    return _ok(result)




@app.post("/api/devices/control")
def devices_control(body: dict[str, Any] | None = None) -> dict[str, Any]:
    body = body or {}
    action = str(body.get("action") or "").strip().lower().replace("-", "_")
    slot_names = _slots(body.get("slot_names") if "slot_names" in body else body.get("slots")) or []
    usb_binding = _ensure_usb_binding_from_host_agent(f"control:{action or 'unknown'}")
    slots = repo.list_device_slots()
    selected_slots = slot_names or [str(slot.get("slot_name") or "") for slot in slots if slot.get("slot_name")]
    package_name = str(body.get("package_name") or body.get("package") or "").strip()
    activity_name = str(body.get("activity_name") or body.get("activity") or "").strip()

    sqlite_count = 0
    redis_count = 0
    queued = 0
    worker_results: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []

    if action in {"immediate_stop", "stop_now", "force_stop"}:
        sqlite_count = repo.cancel_worker_jobs(
            slot_names=selected_slots or None,
            job_types=["appium_collect_profile"],
            message="API 즉시 중지 요청",
        )
        queue = _redis_queue()
        if queue is not None:
            redis_count = queue.cancel_jobs(
                slot_names=selected_slots or None,
                job_types=["appium_collect_profile"],
                message="API 즉시 중지 요청",
            )
        rows = control_slots(settings, slots, "immediate_stop", slot_names=selected_slots)
        for item in rows:
            repo.update_device_slot_runtime(
                str(item.get("slot_name") or ""),
                status="stopped" if item.get("ok") else "stop_failed",
                notes=str(item.get("message") or ""),
            )
        repo.create_operation_command(
            "device_immediate_stop",
            actor="admin",
            status="completed" if any(row.get("ok") for row in rows) or sqlite_count or redis_count else "failed",
            payload={"rows": rows, "sqlite": sqlite_count, "redis": redis_count},
            message=f"즉시 중지: slots={','.join(selected_slots or ['all'])} sqlite={sqlite_count} redis={redis_count}",
        )
        return _ok({"action": action, "rows": rows, "sqlite": sqlite_count, "redis": redis_count, "usb_binding": usb_binding})

    if action in {"resume", "continue", "continue_work"}:
        # 이어하기는 기존 중지/실패 작업을 살릴 수 있으면 retry_wait로 되돌리고,
        # 없으면 현재 슬롯/프로필 기준으로 새 Appium 작업을 큐에 등록합니다.
        retried = repo.retry_failed_worker_jobs(
            slot_names=selected_slots or None,
            job_types=["appium_collect_profile"],
            message="API 이어하기 요청",
        )
        queued = queue_default_collection(
            settings,
            repo,
            slot_count=settings.emulator_slots,
            mode="appium",
            slot_names=selected_slots or None,
        )
        queue = _redis_queue()
        if queue is not None:
            retry_wait_rows = repo.list_worker_jobs(limit=300, statuses=["retry_wait", "queued"])
            retry_wait_rows = [row for row in retry_wait_rows if not selected_slots or str(row.get("slot_name") or "") in set(selected_slots)]
            if retry_wait_rows:
                redis_count = queue.enqueue_jobs(retry_wait_rows)
        if bool(body.get("run_now", False)):
            worker_results = run_worker_once(
                settings,
                repo,
                max_jobs=max(1, len(selected_slots) or settings.emulator_slots),
                worker_types=["appium_collect_profile"],
                slot_names=selected_slots or None,
            )
        repo.create_operation_command(
            "device_resume",
            actor="admin",
            status="completed",
            payload={"retried": retried, "queued": queued, "redis_enqueued": redis_count, "worker_results": worker_results},
            message=f"이어하기: retried={retried} queued={queued} redis={redis_count}",
        )
        return _ok({"action": action, "retried": retried, "queued": queued, "redis_enqueued": redis_count, "worker_results": worker_results, "usb_binding": usb_binding})

    if action in {"home", "go_home", "close_all_home", "close_home", "close_all_and_home", "launch_package", "start_package", "activate_app"}:
        rows = control_slots(
            settings,
            slots,
            action,
            slot_names=selected_slots,
            package_name=package_name,
            activity_name=activity_name,
        )
        runtime_status = {
            "home": "home",
            "go_home": "home",
            "close_all_home": "home",
            "close_home": "home",
            "close_all_and_home": "home",
            "launch_package": "app_launched",
            "start_package": "app_launched",
            "activate_app": "app_launched",
        }.get(action, "controlled")
        for item in rows:
            repo.update_device_slot_runtime(
                str(item.get("slot_name") or ""),
                status=runtime_status if item.get("ok") else "control_failed",
                notes=str(item.get("message") or ""),
            )
        repo.create_operation_command(
            f"device_{action}",
            actor="admin",
            status="completed" if any(row.get("ok") for row in rows) else "failed",
            payload={"rows": rows, "package_name": package_name, "activity_name": activity_name},
            message=f"장치 제어 {action}: {sum(1 for row in rows if row.get('ok'))}/{len(rows)} 성공",
        )
        return _ok({"action": action, "rows": rows, "usb_binding": usb_binding})

    raise HTTPException(status_code=400, detail=f"Unsupported action: {action}")

@app.post("/api/devices/stop")
def devices_stop(body: dict[str, Any]) -> dict[str, Any]:
    udid = str(body.get("udid") or "")
    if not udid:
        raise HTTPException(status_code=400, detail="udid is required")
    ok, message = stop_adb_device(settings, udid)
    return {"ok": ok, "message": message}



def _diagnostic_appium_rows(host_appium_data: dict[str, Any] | None = None) -> tuple[list[dict[str, Any]], int]:
    """Build Appium status rows from DB slots, filling gaps Host Agent missed.

    The Host Agent may be started with an old environment such as
    EMULATOR_SLOTS=4, so its /appium/status response can omit the USB server
    on 4731.  The GUI should still report the real target as 5 slots and check
    the missing port directly through the API's host.docker.internal bridge.
    """
    host_appium_data = host_appium_data or {}
    by_port: dict[int, dict[str, Any]] = {}
    for row in host_appium_data.get("rows") or []:
        try:
            by_port[int(row.get("port") or 0)] = dict(row)
        except Exception:
            continue

    slots = repo.list_device_slots()
    known_ports = {int(slot.get("appium_port") or 0) for slot in slots if int(slot.get("appium_port") or 0) > 0}
    for port in settings.all_appium_ports():
        known_ports.add(int(port))

    rows: list[dict[str, Any]] = []
    for port in sorted(known_ports, key=lambda p: settings.all_appium_ports().index(p) if p in settings.all_appium_ports() else 9999 + p):
        slot = next((item for item in slots if int(item.get("appium_port") or 0) == int(port)), None)
        slot_name = str(slot.get("slot_name") if slot else settings.appium_port_slot_name(int(port)))
        raw_url = str(slot.get("appium_url") if slot else f"http://{settings.appium_host}:{int(port)}")
        host_row = by_port.get(int(port))
        if host_row:
            ok = bool(host_row.get("ok"))
            message = str(host_row.get("message") or "")
            source = "host-agent"
            url = str(host_row.get("url") or raw_url)
            pids = host_row.get("pids") or []
        else:
            worker_url = resolve_appium_url(settings, raw_url)
            ok, message = check_appium_status(worker_url)
            source = "api-probe"
            url = raw_url
            pids = []
        rows.append({
            "slot_name": slot_name,
            "device_type": str(slot.get("device_type") if slot else ("usb" if slot_name == "USB Device" else "emulator")),
            "udid": str(slot.get("udid") if slot else ""),
            "port": int(port),
            "url": url,
            "ok": bool(ok),
            "message": message,
            "source": source,
            "pids": pids,
        })
    return rows, len(rows)

@app.get("/api/diagnostics/summary")
def diagnostics_summary() -> dict[str, Any]:
    db = repo.check_connection()
    redis = check_redis(settings.redis_url)
    host = HostAgentClient(settings.host_agent_url, timeout=float(settings.host_agent_timeout_seconds))
    host_health = host.health()
    adb = host.adb_devices() if host_health.ok else _ok({"devices": [device.to_dict() for device in list_adb_devices(settings)]})
    usb_binding = ensure_usb_slot_bound(settings, repo, adb_rows=(adb.data.get("devices") if hasattr(adb, "data") else adb.get("devices", [])), source="diagnostics")
    expected_ports = _slot_ports()
    appium = host.appium_status(ports=expected_ports) if host_health.ok else _ok({"rows": []})
    if host_health.ok and not appium.ok and appium.status_code == 404:
        appium.data.update({"ok": False, "old_host_agent": True, "message": appium.error})
    appium_rows, appium_target_count = _diagnostic_appium_rows(appium.data if hasattr(appium, "data") else {})
    appium_summary = {
        "ok": bool(appium_rows) and all(bool(row.get("ok")) for row in appium_rows),
        "rows": appium_rows,
        "count": len(appium_rows),
        "target_count": appium_target_count,
        "running": sum(1 for row in appium_rows if row.get("ok")),
        "source": "host-agent+api-probe" if host_health.ok else "api-probe",
        "raw_host_agent": appium.data if hasattr(appium, "data") else appium,
    }
    queue = _redis_queue()
    qstatus = queue.status().__dict__ if queue is not None else None
    jobs = repo.list_worker_jobs(limit=200, statuses=["queued", "running", "retry_wait"])
    return _ok({
        "database": db,
        "redis": {"ok": redis.ok, "url": redis.url, "message": redis.message},
        "host_agent": {"ok": host_health.ok, "url": settings.host_agent_url, "data": host_health.data, "error": host_health.error},
        "adb": adb.data if hasattr(adb, "data") else adb,
        "usb_binding": usb_binding,
        "appium": appium_summary,
        "redis_queue": qstatus,
        "active_worker_jobs": jobs,
    })


@app.get("/api/screenshots")
def screenshots(limit: int = 80) -> dict[str, Any]:
    return _ok({"folder": str((settings.output_dir / "screenshots").resolve()), "screenshots": _screenshot_rows(limit=limit)})


@app.post("/api/screenshots/open-folder")
def screenshots_open_folder() -> dict[str, Any]:
    client = _host_agent()
    if client is not None:
        result = client.open_screenshot_folder()
        if result.ok:
            return _ok(result.data)
        return {"ok": False, "message": result.error or result.data.get("message") or "Host Agent 폴더 열기 실패", "path": result.data.get("path")}
    return {"ok": False, "message": "Host Agent가 실행 중이 아니어서 Windows 폴더를 열 수 없습니다.", "folder": str((settings.output_dir / "screenshots").resolve())}


@app.get("/api/alerts")
def alerts(limit: int = 100) -> dict[str, Any]:
    return _ok({"rules": [r.to_dict() for r in repo.list_rules()], "events": repo.list_alert_events(limit=limit)})


@app.post("/api/alerts/rules")
def add_alert_rule(body: dict[str, Any]) -> dict[str, Any]:
    rule = AlertRule(
        name=str(body.get("name") or "새 알림 규칙"),
        keywords=_slots(body.get("keywords")) or [],
        exclude_keywords=_slots(body.get("exclude_keywords")) or [],
        locations=_slots(body.get("locations")) or [],
        job_categories=_slots(body.get("job_categories")) or [],
        education=_slots(body.get("education")) or [],
        experience=_slots(body.get("experience")) or [],
        notification_channel=str(body.get("notification_channel") or "console"),
    )
    rule_id = repo.add_rule(rule)
    return _ok({"id": rule_id})


@app.post("/api/alerts/evaluate")
def evaluate_alerts(body: dict[str, Any] | None = None) -> dict[str, Any]:
    body = body or {}
    limit = int(body.get("limit") or 300)
    events = AlertService(repo, settings).evaluate_recent_jobs(limit=limit)
    return _ok({"created": len(events)})


@app.get("/api/audit")
def audit(limit: int = 100) -> dict[str, Any]:
    return _ok({"audit": repo.list_audit_logs(limit=limit)})


@app.get("/api/commands")
def commands(limit: int = 100) -> dict[str, Any]:
    return _ok({"commands": repo.list_operation_commands(limit=limit)})
