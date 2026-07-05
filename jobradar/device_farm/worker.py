from __future__ import annotations

import json
import os
import socket
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from jobradar.config import Settings
from jobradar.db.repository import JobRadarRepository
from jobradar.device_farm.adb import list_devices
from jobradar.orchestrator import run_profile_on_slot


# 기기 adapter 클래스는 관련 데이터와 기능을 한곳에 묶어 관리합니다.
class _DeviceAdapter:
    # 객체가 만들어질 때 필요한 초기값과 의존성을 준비합니다.
    def __init__(self, udid: str):
        self.device_id = udid
        self.status = "device"
        self.raw = udid


def _payload(row: dict[str, Any]) -> dict[str, Any]:
    try:
        parsed = json.loads(row.get("payload") or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _slot_expected_udid(slot: dict[str, Any]) -> str:
    existing = str(slot.get("udid") or "").strip()
    if existing:
        return existing
    console_port = int(slot.get("emulator_console_port") or 0)
    return f"emulator-{console_port}" if console_port else ""


def sync_detected_devices(settings: Settings, repo: JobRadarRepository) -> int:
    """Update slot runtime state without destroying explicit port-based slot mapping."""
    slots = repo.list_device_slots()
    devices = [d for d in list_devices(settings) if d.state == "device"]
    by_udid = {d.udid: d for d in devices}
    used: set[str] = set()
    updated = 0
    for index, slot in enumerate(slots):
        expected = _slot_expected_udid(slot)
        device = by_udid.get(expected) if expected else None
        if device is None and not expected:
            # USB Device or unconfigured logical slot: assign the first still-unmapped real ADB device.
            for candidate in devices:
                if candidate.udid not in used:
                    device = candidate
                    break
        if device is not None:
            used.add(device.udid)
            status = "connected" if device.boot_completed else "detected"
            note = f"ADB 감지: {device.udid} model={device.model or '-'} boot={device.boot_completed}"
            repo.update_device_slot_runtime(slot["slot_name"], status=status, udid=device.udid, notes=note)
        else:
            repo.update_device_slot_runtime(slot["slot_name"], status=str(slot.get("status") or "idle"), udid=expected, notes="장치 미감지")
        updated += 1
    return updated


def _job_type_for_mode(mode: str) -> list[str]:
    normalized = (mode or "playwright").strip().lower()
    if normalized == "both":
        return ["collect_profile", "appium_collect_profile"]
    if normalized == "appium":
        return ["appium_collect_profile"]
    return ["collect_profile"]


def _redis_queue(settings: Settings):
    if not getattr(settings, "redis_queue_enabled", False):
        return None
    try:
        from jobradar.integrations.redis_queue import RedisJobQueue

        queue = RedisJobQueue.from_settings(settings)
        queue.ping()
        return queue
    except Exception as exc:
        print(f"[redis-queue] disabled/fallback to SQLite: {exc}")
        return None


def _worker_id(settings: Settings) -> str:
    configured = str(getattr(settings, "worker_id", "") or "").strip()
    if configured:
        return configured
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


# 하트비트 thread 클래스는 관련 데이터와 기능을 한곳에 묶어 관리합니다.
class _HeartbeatThread:
    # 객체가 만들어질 때 필요한 초기값과 의존성을 준비합니다.
    def __init__(self, settings: Settings, repo: JobRadarRepository, redis_queue: Any, job_id: int, row: dict[str, Any], worker_id: str):
        self.settings = settings
        self.repo = repo
        self.redis_queue = redis_queue
        self.job_id = job_id
        self.row = row
        self.worker_id = worker_id
        self._stop = threading.Event()
        self.thread = threading.Thread(target=self._run, name=f"jobradar-heartbeat-{job_id}", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self._stop.set()
        self.thread.join(timeout=2)

    def beat(self, progress: int | None = None, message: str = "") -> None:
        try:
            self.repo.heartbeat_worker_job(self.job_id, self.worker_id, progress_percent=progress, message=message)
        except Exception as exc:
            print(f"[heartbeat] db failed job={self.job_id}: {exc}")
        if self.redis_queue is not None:
            try:
                self.redis_queue.heartbeat_job(self.job_id, self.worker_id, progress_percent=progress, message=message)
            except Exception as exc:
                print(f"[heartbeat] redis failed job={self.job_id}: {exc}")

    def _run(self) -> None:
        interval = max(3, int(getattr(self.settings, "worker_heartbeat_seconds", 10) or 10))
        while not self._stop.wait(interval):
            self.beat(message="Worker heartbeat")


def queue_default_collection(
    settings: Settings,
    repo: JobRadarRepository,
    slot_count: int = 5,
    mode: str = "appium",
    slot_names: list[str] | None = None,
) -> int:
    if not repo.list_search_profiles(enabled_only=True, limit=1):
        repo.seed_default_profiles(settings.target_url)
    repo.seed_device_slots(
        slot_count=slot_count,
        appium_host=settings.appium_host,
        appium_base_port=settings.appium_base_port,
        appium_port_step=settings.appium_port_step,
        system_port_base=settings.appium_system_port_base,
        mjpeg_port_base=settings.appium_mjpeg_port_base,
        chromedriver_port_base=settings.appium_chromedriver_port_base,
        emulator_port_pairs=settings.parsed_emulator_port_pairs(),
    )
    redis_queue = _redis_queue(settings)
    total = 0
    for job_type in _job_type_for_mode(mode):
        rows = repo.queue_collection_jobs_detailed(slot_count=slot_count, job_type=job_type, seed_slots=False, slot_names=slot_names)
        total += len(rows)
        if redis_queue and rows:
            redis_queue.enqueue_jobs(rows)
            print(f"[redis-queue] enqueued {len(rows)} {job_type} jobs")
    return total

def _run_collect_job(settings: Settings, repo: JobRadarRepository, row: dict[str, Any], force_headless: bool = True) -> dict[str, Any]:
    payload = _payload(row)
    slot_name = row.get("slot_name") or payload.get("slot_name") or "Emulator A"
    profile_name = row.get("profile_name") or payload.get("profile_name") or ""
    profile = repo.get_search_profile_by_name(profile_name)
    if profile is None:
        raise RuntimeError(f"검색 프로필을 찾을 수 없습니다: {profile_name}")

    slot = repo.get_device_slot(slot_name) or {}
    udid = slot.get("udid") or ""
    device = _DeviceAdapter(str(udid)) if udid else None
    result = run_profile_on_slot(
        settings=settings,
        db_path=settings.database_url,
        profile=profile,
        slot_name=slot_name,
        device=device,
        force_headless=force_headless,
    )
    result["worker_mode"] = "playwright"
    return result


def _run_appium_collect_job(settings: Settings, repo: JobRadarRepository, row: dict[str, Any]) -> dict[str, Any]:
    payload = _payload(row)
    slot_name = row.get("slot_name") or payload.get("slot_name") or "Emulator A"
    profile_name = row.get("profile_name") or payload.get("profile_name") or ""
    profile = repo.get_search_profile_by_name(profile_name)
    if profile is None:
        raise RuntimeError(f"검색 프로필을 찾을 수 없습니다: {profile_name}")
    slot = repo.get_device_slot(slot_name)
    if not slot:
        raise RuntimeError(f"장치 슬롯을 찾을 수 없습니다: {slot_name}")
    if not str(slot.get("udid") or ""):
        console_port = int(slot.get("emulator_console_port") or 0)
        if console_port:
            repo.update_device_slot_runtime(slot_name, status=str(slot.get("status") or "connected"), udid=f"emulator-{console_port}", notes="Appium Worker용 UDID 자동 보정")
            slot = repo.get_device_slot(slot_name) or slot
    if not str(slot.get("appium_url") or ""):
        raise RuntimeError(f"{slot_name} Appium URL이 없습니다. slot-init 후 Appium 서버를 시작하세요.")
    # Lazy import keeps non-Appium CLI commands usable even before the Appium
    # Python client is installed in a fresh environment.
    from jobradar.device_farm.appium_mobile_web_worker import run_appium_mobile_web_profile

    return run_appium_mobile_web_profile(settings=settings, repo=repo, profile=profile, slot=slot)


def run_worker_once(
    settings: Settings,
    repo: JobRadarRepository,
    max_jobs: int = 4,
    force_headless: bool = True,
    worker_types: list[str] | None = None,
    slot_names: list[str] | None = None,
) -> list[dict[str, Any]]:
    repo.init_db()
    worker_id = _worker_id(settings)
    try:
        recovered = repo.recover_stale_worker_jobs(
            stale_after_seconds=settings.worker_stale_after_seconds,
            job_types=worker_types,
            slot_names=slot_names,
            message=f"heartbeat timeout recovered by {worker_id}",
            auto_retry=settings.worker_auto_retry,
        )
        if recovered.get("total"):
            print(f"[worker-recover] {recovered}")
    except Exception as exc:
        print(f"[worker-recover] skipped: {exc}")
    redis_queue = _redis_queue(settings)
    if redis_queue:
        try:
            recovered_redis = redis_queue.recover_stale_processing(settings.worker_stale_after_seconds)
            if recovered_redis:
                print(f"[redis-recover] recovered processing jobs: {recovered_redis}")
        except Exception as exc:
            print(f"[redis-recover] skipped: {exc}")
    jobs: list[dict[str, Any]] = []
    redis_job_ids: set[int] = set()
    if redis_queue:
        try:
            ids = redis_queue.claim_job_ids(limit=max_jobs, job_types=worker_types, slot_names=slot_names)
            for job_id in ids:
                row = repo.mark_worker_job_running(job_id, worker_id=worker_id)
                if not row:
                    redis_queue.finish_job(job_id, "failed", {}, "SQLite job row not found")
                    continue
                row_status = str(row.get("status") or "")
                if row_status == "canceled":
                    redis_queue.finish_job(job_id, "canceled", {}, "사용자 중지 요청")
                    continue
                if row_status != "running":
                    redis_queue.finish_job(job_id, row_status or "skipped", {}, f"SQLite job status is {row_status or 'unknown'}")
                    continue
                jobs.append(row)
                redis_job_ids.add(job_id)
        except Exception as exc:
            print(f"[redis-queue] claim failed, fallback to SQLite: {exc}")
            redis_queue = None
    if not jobs:
        jobs = repo.claim_worker_jobs(limit=max_jobs, job_types=worker_types, slot_names=slot_names)
    results: list[dict[str, Any]] = []
    if not jobs:
        return results

    def execute(row: dict[str, Any]) -> dict[str, Any]:
        job_id = int(row["id"])
        active_heartbeat = _HeartbeatThread(settings, repo, redis_queue if job_id in redis_job_ids else None, job_id, row, worker_id)
        active_heartbeat.beat(progress=10, message="작업 시작")
        active_heartbeat.start()
        try:
            if repo.is_worker_job_canceled(job_id):
                raise RuntimeError("사용자 중지 요청")
            if row["job_type"] == "collect_profile":
                active_heartbeat.beat(progress=25, message="Playwright 수집 실행 중")
                result = _run_collect_job(settings, repo, row, force_headless=force_headless)
            elif row["job_type"] == "appium_collect_profile":
                active_heartbeat.beat(progress=25, message="Appium 모바일 수집 실행 중")
                result = _run_appium_collect_job(settings, repo, row)
            else:
                raise RuntimeError(f"지원하지 않는 작업 유형: {row['job_type']}")
            if repo.is_worker_job_canceled(job_id):
                result["status"] = "canceled"
                result["error_message"] = result.get("error_message") or "사용자 중지 요청"
                active_heartbeat.beat(progress=0, message="작업 취소됨")
                repo.finish_worker_job(job_id, status="canceled", result=result, error_message=str(result.get("error_message", "")))
                if redis_queue and job_id in redis_job_ids:
                    redis_queue.finish_job(job_id, "canceled", result, str(result.get("error_message", "")))
                return {"worker_job_id": job_id, "job_type": row.get("job_type"), **result}
            status = "completed" if result.get("status") == "success" else "failed"
            active_heartbeat.beat(progress=95 if status == "completed" else 0, message=f"작업 종료 처리: {status}")
            error_message = str(result.get("error_message", ""))
            if status == "failed" and settings.worker_auto_retry and repo.retry_worker_job(job_id, error_message or "자동 재시도 대기", settings.worker_retry_delay_seconds):
                if redis_queue and job_id in redis_job_ids:
                    redis_queue.retry_job(job_id, error_message or "자동 재시도 대기", settings.worker_retry_delay_seconds)
                return {"worker_job_id": job_id, "job_type": row.get("job_type"), "status": "retry_wait", "error_message": error_message}
            repo.finish_worker_job(job_id, status=status, result=result, error_message=error_message)
            if redis_queue and job_id in redis_job_ids:
                redis_queue.finish_job(job_id, status, result, error_message)
            return {"worker_job_id": job_id, "job_type": row.get("job_type"), **result}
        except Exception as exc:
            error_message = str(exc)
            active_heartbeat.beat(progress=0, message=f"작업 예외: {error_message}")
            if "사용자 중지 요청" in error_message or repo.is_worker_job_canceled(job_id):
                repo.finish_worker_job(job_id, status="canceled", error_message=error_message)
                if redis_queue and job_id in redis_job_ids:
                    redis_queue.finish_job(job_id, "canceled", {}, error_message)
                return {"worker_job_id": job_id, "job_type": row.get("job_type"), "status": "canceled", "error_message": error_message}
            if settings.worker_auto_retry and repo.retry_worker_job(job_id, error_message or "자동 재시도 대기", settings.worker_retry_delay_seconds):
                if redis_queue and job_id in redis_job_ids:
                    redis_queue.retry_job(job_id, error_message or "자동 재시도 대기", settings.worker_retry_delay_seconds)
                return {"worker_job_id": job_id, "job_type": row.get("job_type"), "status": "retry_wait", "error_message": error_message}
            repo.finish_worker_job(job_id, status="failed", error_message=error_message)
            if redis_queue and job_id in redis_job_ids:
                redis_queue.finish_job(job_id, "failed", {}, error_message)
            return {"worker_job_id": job_id, "job_type": row.get("job_type"), "status": "failed", "error_message": error_message}
        finally:
            active_heartbeat.stop()

    with ThreadPoolExecutor(max_workers=max(1, max_jobs)) as executor:
        futures = [executor.submit(execute, row) for row in jobs]
        for future in as_completed(futures):
            results.append(future.result())
    return sorted(results, key=lambda item: str(item.get("slot_name", "")))

def run_worker_daemon(
    settings: Settings,
    repo: JobRadarRepository,
    max_jobs: int = 4,
    force_headless: bool = True,
    once: bool = False,
    worker_types: list[str] | None = None,
    slot_names: list[str] | None = None,
) -> None:
    while True:
        results = run_worker_once(settings, repo, max_jobs=max_jobs, force_headless=force_headless, worker_types=worker_types, slot_names=slot_names)
        if results:
            print(json.dumps(results, ensure_ascii=False, indent=2))
        elif once:
            print("queued worker job이 없습니다.")
        if once:
            break
        time.sleep(max(1, settings.worker_poll_seconds))
