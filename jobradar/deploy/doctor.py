from __future__ import annotations

import os
import platform
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path

from jobradar.config import Settings
from jobradar.db.repository import JobRadarRepository
from jobradar.device_farm.appium_server import check_appium_status
from jobradar.device_farm.url_utils import resolve_appium_url
from jobradar.integrations.redis_health import check_redis


# 배포 상태 확인 클래스는 관련 데이터와 기능을 한곳에 묶어 관리합니다.
@dataclass(frozen=True)
class DeployCheck:
    name: str
    ok: bool
    detail: str

    # 객체 데이터를 딕셔너리로 바꿔 저장, API 응답, 화면 표시에서 쉽게 사용하게 합니다.
    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _http_check(name: str, url: str, timeout: float = 3.0) -> DeployCheck:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return DeployCheck(name, 200 <= int(response.status) < 500, f"HTTP {response.status} {url}")
    except Exception as exc:
        return DeployCheck(name, False, f"{url} - {exc}")


def run_deploy_diagnostics(settings: Settings, repo: JobRadarRepository | None = None) -> list[DeployCheck]:
    """Run release/operations oriented checks.

    This is intentionally lightweight and safe: it does not mutate jobs or data.
    It checks filesystem, DB, Redis, API URL, and the configured Appium slots.
    """
    checks: list[DeployCheck] = []
    checks.append(DeployCheck("OS", True, f"{platform.system()} {platform.release()} / Python {platform.python_version()}"))

    output_dir = Path(settings.output_dir)
    checks.append(DeployCheck("OUTPUT_DIR", output_dir.exists(), str(output_dir.resolve())))
    for child in ["logs", "screenshots", "sessions"]:
        p = output_dir / child
        checks.append(DeployCheck(f"OUTPUT_DIR/{child}", p.exists(), str(p.resolve())))

    db_url = str(settings.database_url)
    try:
        active_repo = repo or JobRadarRepository(settings.database_url)
        active_repo.init_db()
        slots = active_repo.list_device_slots()
        jobs = active_repo.list_worker_jobs(limit=5)
        checks.append(DeployCheck("DB", True, f"{settings.database_backend} / slots={len(slots)} recent_jobs={len(jobs)} / {db_url}"))
    except Exception as exc:
        checks.append(DeployCheck("DB", False, f"{db_url} - {exc}"))
        slots = []

    redis = check_redis(settings.redis_url)
    checks.append(DeployCheck("Redis", redis.ok, f"{redis.url} - {redis.message}"))

    api_url = str(settings.api_url or "").rstrip("/")
    if api_url:
        checks.append(_http_check("FastAPI", api_url + "/health"))

    gui_url = os.getenv("JOBRADAR_GUI_URL", "http://127.0.0.1:8501").rstrip("/")
    checks.append(_http_check("Streamlit GUI", gui_url + "/_stcore/health"))

    if slots:
        for slot in slots:
            name = str(slot.get("slot_name") or "slot")
            raw_url = str(slot.get("appium_url") or settings.appium_server_url)
            url = resolve_appium_url(settings, raw_url)
            ok, msg = check_appium_status(url)
            checks.append(DeployCheck(f"Appium {name}", ok, f"{url} - {msg}"))
    else:
        checks.append(DeployCheck("Appium slots", False, "No slots found. Run slot-init --slots 5."))

    return checks
