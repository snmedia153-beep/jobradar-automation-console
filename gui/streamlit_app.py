from __future__ import annotations

import html
import json
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st

from jobradar.appium_agent.adb_manager import list_adb_devices, take_adb_screenshot
from jobradar.config import Settings
from jobradar.db.repository import JobRadarRepository
from jobradar.models import AlertRule, SearchProfile
from jobradar.orchestrator import run_multi_emulator_collection
from jobradar.services.alert_service import AlertService
from jobradar.services.exporter import export_csv, export_json
from jobradar.device_farm.adb import list_devices as list_device_farm_devices, stop_device as stop_adb_device
from jobradar.device_farm.appium_server import check_appium_status, start_appium
from jobradar.device_farm.diagnostics import run_diagnostics
from jobradar.device_farm.emulator_launcher import launch_avd, launch_avd_checked, list_avds
from jobradar.device_farm.worker import queue_default_collection, run_worker_once, sync_detected_devices
from jobradar.device_farm.url_utils import resolve_appium_url
from jobradar.device_farm.screenshots import capture_slots_screenshots
from jobradar.device_farm.device_actions import control_slots
from jobradar.integrations.redis_health import check_redis
from jobradar.integrations.redis_queue import RedisJobQueue
from jobradar.integrations.api_client import JobRadarApiClient
from jobradar.integrations.host_agent_client import HostAgentClient

st.set_page_config(page_title="JobRadar Automation Console", page_icon="📡", layout="wide")

settings = Settings()
settings.ensure_dirs()
repo = JobRadarRepository(settings.database_url)
repo.init_db()

STATUS_KO = {
    "queued": "대기중",
    "starting": "시작중",
    "running": "실행중",
    "success": "완료",
    "completed": "완료",
    "failed": "오류",
    "paused": "일시정지",
    "retry_wait": "재시도 대기",
    "reset": "초기화됨",
    "대기": "대기",
}

STATUS_ICON = {
    "queued": "🔵",
    "starting": "🟣",
    "running": "🟢",
    "success": "✅",
    "completed": "✅",
    "failed": "🔴",
    "paused": "🟠",
    "retry_wait": "🔁",
    "reset": "⚪",
    "대기": "⚪",
}


def inject_css() -> None:
    st.markdown(
        """
        <style>
        :root {
            --jr-bg: #f8fafc;
            --jr-card: #ffffff;
            --jr-line: #e5e7eb;
            --jr-text: #111827;
            --jr-muted: #64748b;
            --jr-blue: #2563eb;
            --jr-green: #16a34a;
            --jr-red: #dc2626;
            --jr-orange: #f59e0b;
            --jr-navy: #0f172a;
        }
        html, body, [data-testid="stAppViewContainer"] {background: var(--jr-bg);}
        .block-container {padding-top: 1.35rem; padding-bottom: 2rem; max-width: 1780px;}
        [data-testid="stSidebar"] {background: linear-gradient(180deg, #0b1220 0%, #111827 100%);}
        [data-testid="stSidebar"] * {color: #e5e7eb;}
        [data-testid="stSidebar"] .stRadio label {font-weight: 800;}
        div[data-testid="stMetric"] {
            background: #ffffff; border: 1px solid #e5e7eb; border-radius: 18px;
            padding: 16px 18px 12px 18px; box-shadow: 0 8px 24px rgba(15, 23, 42, .05);
        }
        .stButton > button {border-radius: 12px; min-height: 42px; font-weight: 800;}
        div[data-testid="stVerticalBlockBorderWrapper"] {
            border-radius: 18px !important;
            box-shadow: 0 8px 24px rgba(15, 23, 42, .035);
        }
        .small-caption {font-size: 12px; color: #64748b;}
        .jr-sidebar-brand {padding: 4px 2px 18px 2px; border-bottom: 1px solid rgba(148,163,184,.20); margin-bottom: 16px;}
        .jr-logo-row {display:flex; align-items:center; gap:10px;}
        .jr-logo-mark {width:30px; height:30px; border-radius:9px; background: radial-gradient(circle at 30% 25%, #22c55e, #0ea5e9 55%, #1d4ed8);}
        .jr-brand-title {font-size:20px; font-weight:900; letter-spacing:.3px; color:#f8fafc;}
        .jr-brand-pill {display:inline-flex; margin-top:8px; padding:3px 9px; border:1px solid rgba(148,163,184,.35); border-radius:999px; font-size:11px; color:#cbd5e1; background:rgba(15,23,42,.72);}
        .jr-side-section {margin:14px 0 7px; color:#94a3b8 !important; font-size:12px; font-weight:800; text-transform:uppercase; letter-spacing:.04em;}
        .jr-time-box, .jr-operator-box, .jr-conn-card {background: rgba(15,23,42,.68); border:1px solid rgba(148,163,184,.20); border-radius:14px; padding:11px 12px; margin:8px 0;}
        .jr-conn-card {background: rgba(30,41,59,.78);}
        .jr-conn-title {display:flex; justify-content:space-between; align-items:center; gap:8px; font-size:13px; font-weight:900; color:#f8fafc; margin-bottom:8px;}
        .jr-conn-value {font-size:12px; line-height:1.35; color:#dbeafe !important; background:rgba(2,6,23,.42); border:1px solid rgba(148,163,184,.18); border-radius:9px; padding:7px 8px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;}
        .jr-conn-meta {display:flex; justify-content:space-between; align-items:center; margin-top:8px; font-size:11px; color:#cbd5e1;}
        .jr-badge-ok {display:inline-flex; padding:3px 8px; border-radius:999px; background:rgba(34,197,94,.16); color:#86efac !important; font-weight:900;}
        .jr-badge-warn {display:inline-flex; padding:3px 8px; border-radius:999px; background:rgba(245,158,11,.18); color:#fde68a !important; font-weight:900;}
        .jr-operator-row {display:flex; align-items:center; gap:10px;}
        .jr-avatar {width:32px; height:32px; border-radius:50%; background:#f8fafc; color:#0f172a !important; display:flex; align-items:center; justify-content:center; font-weight:900;}
        .jr-online {font-size:11px; color:#86efac !important;}
        .jr-slot-card {background:#fff; border:1px solid #e5e7eb; border-radius:18px; padding:16px 18px; box-shadow:0 10px 26px rgba(15,23,42,.05); min-height:265px;}
        .jr-slot-card.error {border-color:#fecaca; background:linear-gradient(180deg,#fff 0%,#fffafa 100%);}
        .jr-slot-head {display:flex; justify-content:space-between; gap:12px; align-items:flex-start; margin-bottom:12px;}
        .jr-slot-name {display:flex; align-items:center; gap:8px; font-size:19px; font-weight:900; color:#111827; min-width:0;}
        .jr-dot {width:9px; height:9px; border-radius:999px; background:#94a3b8; flex:0 0 auto;}
        .jr-dot.ok {background:#16a34a;} .jr-dot.err {background:#ef4444;} .jr-dot.run {background:#2563eb;} .jr-dot.wait {background:#94a3b8;}
        .jr-status-pill {font-size:12px; font-weight:900; padding:4px 10px; border-radius:999px; white-space:nowrap;}
        .jr-status-ok {background:#dcfce7; color:#166534;} .jr-status-err {background:#fee2e2; color:#b91c1c;} .jr-status-run {background:#dbeafe; color:#1d4ed8;} .jr-status-wait {background:#f1f5f9; color:#475569;}
        .jr-health-row {display:grid; grid-template-columns:70px 1fr 48px; gap:8px; align-items:center; margin:8px 0 14px; color:#64748b; font-size:13px;}
        .jr-health-track {height:7px; background:#e5e7eb; border-radius:999px; overflow:hidden;}
        .jr-health-fill {height:7px; border-radius:999px; background:#16a34a;} .jr-health-fill.err {background:#ef4444;} .jr-health-fill.warn {background:#f59e0b;}
        .jr-info-grid {display:grid; grid-template-columns:86px minmax(0,1fr); row-gap:7px; column-gap:10px; font-size:13px; margin-bottom:14px;}
        .jr-info-label {color:#64748b; font-weight:700;}
        .jr-info-value {color:#111827; font-weight:800; min-width:0; overflow:hidden; display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; line-height:1.35; word-break:break-word;}
        .jr-stat-grid {display:grid; grid-template-columns:1fr 1fr; gap:10px; margin:10px 0 12px;}
        .jr-stat {border:1px solid #e5e7eb; border-radius:14px; padding:10px 12px; background:#fff; text-align:center; min-width:0;}
        .jr-stat-label {font-size:12px; color:#64748b; font-weight:800;}
        .jr-stat-value {font-size:24px; line-height:1.15; color:#111827; font-weight:900; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;}
        .jr-error-strip {display:flex; align-items:center; justify-content:space-between; gap:8px; background:#fef2f2; color:#b91c1c; border:1px solid #fecaca; border-radius:12px; padding:8px 10px; font-size:12px; font-weight:900; margin:6px 0 10px;}
        .jr-progress-note {background:#eff6ff; color:#1e40af; border:1px solid #dbeafe; border-radius:12px; padding:8px 10px; font-size:12px; font-weight:800; margin:6px 0 10px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;}
        .jr-empty-strip {height:30px; margin:6px 0 10px;}
        .jr-card-actions {display:grid; grid-template-columns:1fr 1fr; gap:10px;}
        .jr-log-scroll {max-height:420px; overflow-y:auto; border:1px solid #e5e7eb; border-radius:12px; background:#fff;}
        .jr-log-row {display:grid; grid-template-columns:125px 78px minmax(0,1fr); gap:10px; padding:9px 12px; border-bottom:1px solid #f1f5f9; font-size:13px; align-items:start;}
        .jr-log-head {position:sticky; top:0; background:#f8fafc; font-weight:900; color:#475569; z-index:1;}
        .jr-log-msg {white-space:pre-wrap; word-break:break-word; color:#111827;}
        .jr-level {display:inline-flex; align-items:center; justify-content:center; border-radius:999px; padding:2px 8px; font-size:11px; font-weight:900;}
        .jr-level-error {background:#fee2e2; color:#b91c1c;} .jr-level-warn {background:#fef3c7; color:#92400e;} .jr-level-info {background:#dbeafe; color:#1d4ed8;}
        .jr-right-card {background:#fff; border:1px solid #e5e7eb; border-radius:18px; padding:16px 18px; box-shadow:0 8px 22px rgba(15,23,42,.035); margin-bottom:14px;}
        .jr-info-note {background:#eff6ff; color:#1e40af; border:1px solid #dbeafe; border-radius:12px; padding:12px; font-size:13px; line-height:1.45;}

        .jr-status-grid {display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:12px; margin:14px 0 18px;}
        .jr-status-card {background:#fff; border:1px solid #e5e7eb; border-radius:16px; padding:14px 16px; box-shadow:0 8px 24px rgba(15,23,42,.04); min-height:112px;}
        .jr-status-title {display:flex; align-items:center; gap:8px; color:#475569; font-size:13px; font-weight:900; margin-bottom:8px;}
        .jr-status-value {font-size:24px; font-weight:950; color:#111827; line-height:1.15;}
        .jr-status-desc {font-size:12px; color:#64748b; line-height:1.42; margin-top:7px;}
        .jr-status-okline {border-color:#bbf7d0;} .jr-status-warnline {border-color:#fed7aa;} .jr-status-badline {border-color:#fecaca;}
        .jr-action-help {background:#f8fafc; border:1px solid #e2e8f0; border-radius:14px; padding:12px 14px; margin:8px 0 14px;}
        .jr-action-help-grid {display:grid; grid-template-columns:repeat(5,minmax(0,1fr)); gap:8px;}
        .jr-action-help-item {font-size:12px; line-height:1.4; color:#475569; background:#fff; border:1px solid #e5e7eb; border-radius:12px; padding:9px 10px;}
        .jr-action-help-item strong {display:block; color:#111827; font-size:12px; margin-bottom:3px;}
        .jr-screenshot-grid {display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:14px;}
        .jr-section-note {background:#eff6ff; color:#1e40af; border:1px solid #dbeafe; border-radius:12px; padding:11px 12px; font-size:13px; line-height:1.45; margin:8px 0 12px;}
        </style>
        """,
        unsafe_allow_html=True,
    )


def parse_json_list(value: str | None) -> list[str]:
    try:
        parsed = json.loads(value) if value else []
        return parsed if isinstance(parsed, list) else []
    except Exception:
        return []


def split_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def ko_status(status: str | None) -> str:
    raw = str(status or "대기")
    return f"{STATUS_ICON.get(raw, '⚪')} {STATUS_KO.get(raw, raw)}"


def worker_types_for_mode(mode: str) -> list[str] | None:
    mode = (mode or "playwright").strip().lower()
    if mode == "all":
        return None
    if mode == "appium":
        return ["appium_collect_profile"]
    return ["collect_profile"]


def read_log_tail(path: Path, lines: int = 160) -> str:
    if not path.exists():
        return "아직 로그 파일이 없습니다."
    try:
        content = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        return "\n".join(content[-lines:])
    except Exception as exc:
        return f"로그 읽기 실패: {exc}"


def dataframe_rows(rows: list[dict[str, Any]]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame()
    display_rows = []
    for row in rows:
        item = dict(row)
        item["tech_keywords"] = ", ".join(parse_json_list(item.get("tech_keywords", "")))
        display_rows.append(item)
    return pd.DataFrame(display_rows)


def run_cli(args: list[str]) -> tuple[int, str, str]:
    proc = subprocess.run(
        ["python", "-m", "jobradar.cli", *args],
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode, proc.stdout, proc.stderr


def set_feedback(level: str, message: str) -> None:
    st.session_state["jr_feedback"] = {
        "level": level,
        "message": message,
        "time": datetime.now().strftime("%H:%M:%S"),
    }


def show_feedback() -> None:
    feedback = st.session_state.get("jr_feedback")
    if not feedback:
        return
    prefix = f"[{feedback.get('time')}] "
    level = feedback.get("level", "info")
    message = prefix + str(feedback.get("message", ""))
    if level == "success":
        st.success(message)
    elif level == "warning":
        st.warning(message)
    elif level == "error":
        st.error(message)
    else:
        st.info(message)


def get_redis_queue() -> RedisJobQueue | None:
    if not settings.redis_queue_enabled:
        return None
    try:
        queue = RedisJobQueue.from_settings(settings)
        queue.ping()
        return queue
    except Exception:
        return None


def get_api_client() -> JobRadarApiClient | None:
    if not getattr(settings, "api_enabled", False):
        return None
    client = JobRadarApiClient(settings.api_url, timeout=12.0)
    health = client.health()
    if health.ok:
        return client
    return None


def api_available_label() -> str:
    client = get_api_client()
    return "FastAPI 연결됨" if client is not None else "직접 DB/Redis fallback"


def cancel_worker_jobs_realtime(slot_names: list[str] | None = None, job_types: list[str] | None = None, message: str = "사용자 중지 요청") -> tuple[int, int]:
    api = get_api_client()
    if api is not None:
        mode = "all"
        if job_types == ["appium_collect_profile"]:
            mode = "appium"
        elif job_types == ["collect_profile"]:
            mode = "playwright"
        result = api.cancel_jobs(mode=mode, slot_names=slot_names, message=message)
        if result.ok:
            return int(result.data.get("sqlite") or 0), int(result.data.get("redis") or 0)
    sqlite_affected = repo.cancel_worker_jobs(slot_names=slot_names, job_types=job_types, message=message)
    redis_affected = 0
    queue = get_redis_queue()
    if queue is not None:
        try:
            redis_affected = queue.cancel_jobs(slot_names=slot_names, job_types=job_types, message=message)
        except Exception:
            redis_affected = 0
    return sqlite_affected, redis_affected


def redis_status_snapshot() -> tuple[Any | None, list[dict[str, Any]], list[dict[str, Any]]]:
    queue = get_redis_queue()
    if queue is None:
        return None, [], []
    try:
        return queue.status(), queue.list_jobs(limit=100), queue.recent_events(limit=30)
    except Exception:
        return None, [], []


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        if value in (None, ""):
            return default
        return int(float(value))
    except Exception:
        return default


def _active_worker_jobs(limit: int = 300) -> list[dict[str, Any]]:
    """Return queue-like jobs from the durable DB, not emulator_sessions.

    The dashboard queue panel used to read emulator_sessions. That made the
    panel say there was no waiting session even while Redis/worker_jobs had
    running or queued jobs. Worker jobs are the real source of queue state.
    """
    try:
        jobs = repo.list_worker_jobs(limit=limit)
    except Exception:
        return []
    active_statuses = {"queued", "retry_wait", "running"}
    return [row for row in jobs if str(row.get("status") or "") in active_statuses]


def _profile_keyword_map() -> dict[str, str]:
    try:
        return {profile.name: profile.keyword for profile in repo.list_search_profiles(enabled_only=False, limit=500)}
    except Exception:
        return {}


def _merge_slot_runtime(slot_rows: list[dict[str, Any]], worker_jobs: list[dict[str, Any]], redis_jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Overlay running/queued worker state onto slot cards.

    Appium sessions and job result rows are updated at different times. During a
    long mobile-detail crawl, the slot should still show 작업중/대기중 based on
    worker_jobs/Redis instead of the previous completed session.
    """
    keywords = _profile_keyword_map()
    active_by_slot: dict[str, dict[str, Any]] = {}
    priority = {"running": 0, "queued": 1, "retry_wait": 2}
    for row in worker_jobs:
        slot = str(row.get("slot_name") or "")
        status = str(row.get("status") or "")
        if not slot or status not in priority:
            continue
        current = active_by_slot.get(slot)
        if current is None or priority[status] < priority.get(str(current.get("status") or ""), 99):
            active_by_slot[slot] = row
    redis_by_id = {str(row.get("id") or row.get("job_id") or ""): row for row in redis_jobs}
    merged: list[dict[str, Any]] = []
    for item in slot_rows:
        row = dict(item)
        slot = str(row.get("slot_name") or "")
        job = active_by_slot.get(slot)
        if job:
            job_id = str(job.get("id") or "")
            redis_row = redis_by_id.get(job_id, {})
            status = str(redis_row.get("status") or job.get("status") or "")
            profile_name = str(job.get("profile_name") or redis_row.get("profile_name") or row.get("profile_name") or "")
            row["status"] = status
            row["profile_name"] = profile_name
            row["keyword"] = keywords.get(profile_name, str(row.get("keyword") or ""))
            row["worker_job_id"] = job_id
            row["progress_percent"] = _safe_int(redis_row.get("progress_percent") or job.get("progress_percent"), 0)
            row["progress_message"] = str(redis_row.get("progress_message") or job.get("progress_message") or "")
            row["health_percent"] = max(35, int(row.get("health_percent") or 100))
            if status == "running":
                row["health_percent"] = max(row["health_percent"], 90)
        merged.append(row)
    return merged


def _results_rows(limit: int = 300, keyword: str = "", slot: str = "", active_only: bool = False) -> tuple[list[dict[str, Any]], str]:
    status = "active" if active_only else ""
    api = get_api_client()
    if api is not None:
        result = api.get("/api/results", params={"limit": int(limit), "keyword": keyword, "emulator_slot": slot, "status": status})
        if result.ok:
            return list(result.data.get("results") or []), "api"
    return repo.list_jobs(limit=int(limit), keyword=keyword, emulator_slot=slot, status=status), "db"


def ensure_device_slots(slot_count: int | None = None) -> list[dict[str, Any]]:
    slot_count = slot_count or settings.emulator_slots
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
    return repo.list_device_slots()


def slot_options() -> list[str]:
    slots = ensure_device_slots(settings.emulator_slots)
    return [str(row.get("slot_name")) for row in slots]


def profile_options() -> list[str]:
    profiles = repo.list_search_profiles(enabled_only=False, limit=200)
    return [profile.name for profile in profiles]


def queue_appium_slots(selected_slots: list[str], run_now: bool = False, max_jobs: int | None = None) -> tuple[int, list[dict[str, Any]]]:
    api = get_api_client()
    if api is not None:
        queued_result = api.queue_jobs(mode="appium", slot_names=selected_slots or None, slot_count=settings.emulator_slots)
        if not queued_result.ok:
            set_feedback("warning", f"API 큐 등록 실패, 로컬 fallback 사용: {queued_result.error}")
        else:
            queued = int(queued_result.data.get("queued") or 0)
            results: list[dict[str, Any]] = []
            if run_now:
                run_result = api.run_worker_once(mode="appium", slot_names=selected_slots or None, max_jobs=max_jobs or max(1, len(selected_slots) or settings.emulator_slots))
                if run_result.ok:
                    results = list(run_result.data.get("results") or [])
                    st.session_state["jr_worker_results"] = results
                else:
                    set_feedback("error", f"API 즉시 실행 실패: {run_result.error}")
            return queued, results
    if not repo.list_search_profiles(enabled_only=True, limit=1):
        repo.seed_default_profiles(settings.target_url)
    queued = queue_default_collection(
        settings,
        repo,
        slot_count=settings.emulator_slots,
        mode="appium",
        slot_names=selected_slots or None,
    )
    results: list[dict[str, Any]] = []
    if run_now:
        sync_detected_devices(settings, repo)
        results = run_worker_once(
            settings,
            repo,
            max_jobs=max_jobs or max(1, len(selected_slots) or settings.emulator_slots),
            worker_types=["appium_collect_profile"],
            slot_names=selected_slots or None,
        )
        st.session_state["jr_worker_results"] = results
    return queued, results


def start_appium_for_slots(selected_slots: list[str]) -> tuple[int, list[dict[str, Any]]]:
    started = 0
    rows: list[dict[str, Any]] = []
    slots = [slot for slot in ensure_device_slots(settings.emulator_slots) if not selected_slots or slot.get("slot_name") in selected_slots]
    for slot in slots:
        port = int(slot.get("appium_port") or settings.appium_base_port)
        pid, msg = start_appium(settings, port=port, host=settings.appium_host, log_name=f"appium_{str(slot['slot_name']).replace(' ', '_')}.log")
        url = str(slot.get("appium_url") or f"http://{settings.appium_host}:{port}")
        worker_url = resolve_appium_url(settings, url)
        ok, health = check_appium_status(worker_url)
        repo.update_device_slot_runtime(str(slot["slot_name"]), "appium_starting" if pid else "appium_failed", notes=f"pid={pid} {msg} health={health}")
        started += 1 if pid else 0
        rows.append({"slot": slot.get("slot_name"), "pid": pid, "url": url, "worker_url": worker_url, "message": msg, "health": health, "ok": ok})
    return started, rows


def screenshot_all_devices(slot_names: list[str] | None = None) -> tuple[int, str]:
    """Capture screenshots through the control API/Appium first.

    When the GUI runs in Docker, the Streamlit container cannot see Windows ADB
    devices directly. Appium servers run on the Windows host and *can* see the
    emulators/USB device, so screenshots should go through FastAPI -> Appium.
    ADB is kept only as a local fallback for non-Docker/dev runs.
    """
    selected_slots = slot_names or []

    api = get_api_client()
    if api is not None:
        result = api.capture_screenshots(slot_names=selected_slots)
        if result.ok:
            saved = int(result.data.get("saved") or 0)
            failed = int(result.data.get("failed") or 0)
            rows = list(result.data.get("rows") or [])
            failed_rows = [row for row in rows if not row.get("ok")]
            st.session_state["jr_last_screenshot_rows"] = rows
            if saved:
                return saved, f"Appium 경유 스크린샷 {saved}건을 저장했습니다." + (f" 실패 {failed}건." if failed else "")
            detail = "; ".join(f"{row.get('slot_name')}: {compact_text(row.get('message'), 80)}" for row in failed_rows[:3])
            return 0, "Appium 경유 스크린샷 저장에 실패했습니다." + (f" {detail}" if detail else "")
        set_feedback("warning", f"API 스크린샷 실패, 로컬 fallback 시도: {result.error}")

    # Local fallback 1: use Appium directly from the current Python process.
    try:
        slots = repo.list_device_slots()
        result = capture_slots_screenshots(settings, slots, slot_names=selected_slots)
        saved = int(result.get("saved") or 0)
        failed = int(result.get("failed") or 0)
        rows = list(result.get("rows") or [])
        st.session_state["jr_last_screenshot_rows"] = rows
        repo.create_operation_command(
            "screenshot_all",
            actor="admin",
            status="completed" if saved else "failed",
            payload=result,
            message=f"Appium 직접 스크린샷 저장 {saved}건 · 실패 {failed}건",
        )
        if saved:
            return saved, f"Appium 직접 스크린샷 {saved}건을 저장했습니다." + (f" 실패 {failed}건." if failed else "")
    except Exception as exc:
        set_feedback("warning", f"Appium 직접 스크린샷 실패, ADB fallback 시도: {exc}")

    # Local fallback 2: old ADB path. This only works when GUI runs directly on
    # the Windows host where adb can see the devices.
    devices = [device for device in list_adb_devices() if getattr(device, "status", "") == "device"]
    if not devices:
        repo.create_operation_command(
            "screenshot_all",
            actor="admin",
            status="failed",
            message="ADB 장치가 없어 스크린샷을 저장하지 못했습니다. Docker GUI에서는 FastAPI/Appium 경유가 필요합니다.",
        )
        return 0, "ADB 장치가 없습니다. Docker GUI에서는 Appium 서버와 FastAPI 상태를 확인하세요."

    saved = 0
    failed: list[str] = []
    for device in devices:
        safe_id = device.device_id.replace(":", "_").replace("/", "_")
        target = settings.output_dir / "screenshots" / f"manual_{safe_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
        ok, message = take_adb_screenshot(device.device_id, str(target))
        if ok:
            saved += 1
        else:
            failed.append(f"{device.device_id}: {message}")
    status = "completed" if saved else "failed"
    repo.create_operation_command(
        "screenshot_all",
        actor="admin",
        status=status,
        payload={"saved": saved, "failed": failed},
        message=f"ADB 스크린샷 저장 {saved}건" + (f" · 실패 {len(failed)}건" if failed else ""),
    )
    return saved, f"ADB 스크린샷 {saved}건을 저장했습니다." + (f" 실패 {len(failed)}건." if failed else "")




def device_control_action(
    action: str,
    selected_slots: list[str] | None = None,
    package_name: str = "",
    activity_name: str = "",
    run_now: bool = False,
) -> dict[str, Any]:
    """Run device/Appium control action through FastAPI first, then local fallback."""
    slots = selected_slots or []
    api = get_api_client()
    if api is not None:
        result = api.device_control(
            action=action,
            slot_names=slots,
            package_name=package_name,
            activity_name=activity_name,
            run_now=run_now,
        )
        if result.ok:
            return result.data
        set_feedback("warning", f"API 장치 제어 실패, 로컬 fallback 시도: {result.error}")

    all_slots = repo.list_device_slots()
    selected = slots or [str(slot.get("slot_name") or "") for slot in all_slots if slot.get("slot_name")]
    action_norm = action.strip().lower().replace("-", "_")
    sqlite_count = 0
    redis_count = 0
    queued = 0
    worker_results: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []

    if action_norm in {"immediate_stop", "stop_now", "force_stop"}:
        sqlite_count, redis_count = cancel_worker_jobs_realtime(
            slot_names=selected or None,
            job_types=["appium_collect_profile"],
            message="GUI 즉시 중지 요청",
        )
        rows = control_slots(settings, all_slots, "immediate_stop", slot_names=selected or None)
    elif action_norm == "resume":
        retried = repo.retry_failed_worker_jobs(
            slot_names=selected or None,
            job_types=["appium_collect_profile"],
            message="GUI 작업 이어하기 요청",
        )
        queued = queue_default_collection(settings, repo, slot_count=settings.emulator_slots, mode="appium", slot_names=selected or None)
        redis_queue = get_redis_queue()
        if redis_queue is not None:
            try:
                redis_rows = repo.list_worker_jobs(limit=500, statuses=["queued", "retry_wait"])
                if selected:
                    redis_rows = [row for row in redis_rows if str(row.get("slot_name") or "") in set(selected)]
                redis_count = redis_queue.enqueue_jobs(redis_rows) if redis_rows else 0
            except Exception:
                redis_count = 0
        if run_now:
            worker_results = run_worker_once(
                settings,
                repo,
                max_jobs=max(1, len(selected) or settings.emulator_slots),
                worker_types=["appium_collect_profile"],
                slot_names=selected or None,
            )
            st.session_state["jr_worker_results"] = worker_results
        rows = [{"slot_name": name, "ok": True, "message": f"이어하기 준비: retry={retried}, queued={queued}"} for name in selected]
    else:
        rows = control_slots(
            settings,
            all_slots,
            action_norm,
            slot_names=selected or None,
            package_name=package_name,
            activity_name=activity_name,
        )

    return {
        "ok": any(row.get("ok") for row in rows) or sqlite_count > 0 or queued > 0,
        "rows": rows,
        "sqlite": sqlite_count,
        "redis": redis_count,
        "queued": queued,
        "worker_results": worker_results,
    }


def summarize_control_result(data: dict[str, Any]) -> str:
    rows = list(data.get("rows") or [])
    ok_count = sum(1 for row in rows if row.get("ok"))
    fail_count = len(rows) - ok_count
    parts = [f"성공 {ok_count}건", f"실패 {fail_count}건"]
    if data.get("sqlite") is not None:
        parts.append(f"SQLite {int(data.get('sqlite') or 0)}")
    if data.get("redis") is not None:
        parts.append(f"Redis {int(data.get('redis') or 0)}")
    if data.get("queued"):
        parts.append(f"큐 등록 {int(data.get('queued') or 0)}")
    details = "; ".join(
        f"{row.get('slot_name')}: {compact_text(row.get('message'), 60)}"
        for row in rows[:3]
    )
    return " / ".join(parts) + (f" · {details}" if details else "")


def _host_agent_client() -> HostAgentClient:
    return HostAgentClient(settings.host_agent_url, timeout=float(settings.host_agent_timeout_seconds))


def _host_agent_windows() -> tuple[list[dict[str, Any]], str]:
    api = get_api_client()
    if api is not None:
        result = api.emulator_windows()
        if result.ok:
            return list(result.data.get("windows") or []), "FastAPI → Host Agent"
        return [], f"Host Agent 연결 실패: {result.error}"
    result = _host_agent_client().emulator_windows()
    if result.ok:
        return list(result.data.get("windows") or []), "Host Agent 직접"
    return [], f"Host Agent 연결 실패: {result.error}"


def arrange_emulator_windows_action(layout: str, x: int, y: int, width: int, height: int, gap: int, columns: int, dry_run: bool = False) -> dict[str, Any]:
    api = get_api_client()
    if api is not None:
        result = api.arrange_emulators(layout=layout, x=x, y=y, width=width, height=height, gap=gap, columns=columns, dry_run=dry_run)
        if result.ok:
            return result.data
        return {"ok": False, "message": result.error, "results": []}
    result = _host_agent_client().arrange(layout=layout, x=x, y=y, width=width, height=height, gap=gap, columns=columns, dry_run=dry_run)
    if result.ok:
        return result.data
    return {"ok": False, "message": result.error, "results": []}


def render_window_arrange_panel(key_prefix: str = "win") -> None:
    st.subheader("에뮬레이터 창 정렬")
    st.caption("실제기기는 제외하고 Windows에 떠 있는 Android Emulator 창만 2x2 Grid/가로/세로로 배치합니다. Docker GUI에서는 Windows Host Agent가 필요합니다.")

    with st.container(border=True):
        status_cols = st.columns([1, 2])
        if status_cols[0].button("Host Agent 확인", use_container_width=True, key=f"{key_prefix}_host_check"):
            rows, source = _host_agent_windows()
            st.session_state[f"{key_prefix}_host_windows"] = rows
            st.session_state[f"{key_prefix}_host_source"] = source
            set_feedback("success" if rows else "warning", f"{source} · 감지된 에뮬레이터 창 {len(rows)}개")
            st.rerun()
        status_cols[1].caption(f"Host Agent URL: `{settings.host_agent_url}`")

        w_rows = st.session_state.get(f"{key_prefix}_host_windows", [])
        w_source = st.session_state.get(f"{key_prefix}_host_source", "아직 조회하지 않음")
        st.caption(f"창 목록 소스: {w_source} · 감지 {len(w_rows)}개")
        if w_rows:
            with st.expander("감지된 에뮬레이터 창", expanded=False):
                st.dataframe(pd.DataFrame(w_rows), use_container_width=True, hide_index=True)

        c1, c2, c3 = st.columns(3)
        layout_label = c1.selectbox("배치 방식", ["2x2 Grid", "가로 정렬", "세로 정렬"], key=f"{key_prefix}_layout")
        layout = {"2x2 Grid": "grid2x2", "가로 정렬": "horizontal", "세로 정렬": "vertical"}[layout_label]
        columns = c2.number_input("열 수", min_value=1, max_value=4, value=2, key=f"{key_prefix}_columns")
        gap = c3.number_input("간격(px)", min_value=0, max_value=80, value=12, step=2, key=f"{key_prefix}_gap")

        p1, p2, p3, p4 = st.columns(4)
        x = p1.number_input("시작 X", min_value=-2000, max_value=8000, value=20, step=10, key=f"{key_prefix}_x")
        y = p2.number_input("시작 Y", min_value=-2000, max_value=8000, value=40, step=10, key=f"{key_prefix}_y")
        width = p3.number_input("창 너비", min_value=240, max_value=1600, value=430, step=10, key=f"{key_prefix}_width")
        height = p4.number_input("창 높이", min_value=320, max_value=1800, value=780, step=10, key=f"{key_prefix}_height")

        b1, b2, b3 = st.columns(3)
        if b1.button("2x2 Grid 정렬", type="primary", use_container_width=True, key=f"{key_prefix}_grid_now"):
            data = arrange_emulator_windows_action("grid2x2", int(x), int(y), int(width), int(height), int(gap), int(columns), dry_run=False)
            st.session_state[f"{key_prefix}_arrange_result"] = data
            set_feedback("success" if data.get("ok") else "warning", str(data.get("message") or "창 정렬 요청 완료"))
            st.rerun()
        if b2.button("선택 방식으로 정렬", use_container_width=True, key=f"{key_prefix}_arrange_now"):
            data = arrange_emulator_windows_action(layout, int(x), int(y), int(width), int(height), int(gap), int(columns), dry_run=False)
            st.session_state[f"{key_prefix}_arrange_result"] = data
            set_feedback("success" if data.get("ok") else "warning", str(data.get("message") or "창 정렬 요청 완료"))
            st.rerun()
        if b3.button("미리보기", use_container_width=True, key=f"{key_prefix}_preview"):
            data = arrange_emulator_windows_action(layout, int(x), int(y), int(width), int(height), int(gap), int(columns), dry_run=True)
            st.session_state[f"{key_prefix}_arrange_result"] = data
            set_feedback("info", str(data.get("message") or "미리보기 생성"))
            st.rerun()

        result = st.session_state.get(f"{key_prefix}_arrange_result")
        if result:
            rows = list(result.get("results") or [])
            st.caption(str(result.get("message") or ""))
            if rows:
                st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)


def diagnostics_summary() -> dict[str, Any]:
    """Return a user-facing status snapshot for DB/Redis/API/Host/ADB/Appium."""
    api = get_api_client()
    if api is not None:
        result = api.diagnostics_summary()
        if result.ok:
            return result.data
        return {"ok": False, "source": "api", "message": result.error}
    redis_info = check_redis(settings.redis_url)
    health_rows = []
    for slot in repo.list_device_slots():
        raw_url = str(slot.get("appium_url") or settings.appium_server_url)
        worker_url = resolve_appium_url(settings, raw_url)
        ok, message = check_appium_status(worker_url)
        health_rows.append({"slot": slot.get("slot_name"), "port": slot.get("appium_port"), "url": raw_url, "ok": ok, "message": message})
    devices = [device.to_dict() for device in list_device_farm_devices(settings)]
    return {
        "ok": True,
        "source": "local",
        "database": repo.check_connection(),
        "redis": {"ok": redis_info.ok, "url": redis_info.url, "message": redis_info.message},
        "host_agent": {"ok": False, "url": settings.host_agent_url, "error": "API disabled; Host Agent is checked from API mode"},
        "adb": {"ok": True, "count": len(devices), "devices": devices},
        "appium": {"rows": health_rows},
        "active_worker_jobs": _active_worker_jobs(limit=200),
    }


def render_ops_status_center(key_prefix: str = "ops") -> None:
    data = diagnostics_summary()
    db_ok = bool((data.get("database") or {}).get("ok", True))
    redis_ok = bool((data.get("redis") or {}).get("ok"))
    host_ok = bool((data.get("host_agent") or {}).get("ok"))
    adb_data = data.get("adb") or {}
    adb_count = int(adb_data.get("count") or len(adb_data.get("devices") or []))
    appium_data = data.get("appium") or {}
    appium_rows = list(appium_data.get("rows") or [])
    appium_ok_count = sum(1 for row in appium_rows if row.get("ok"))
    appium_target_count = int(appium_data.get("target_count") or appium_data.get("count") or len(appium_rows) or settings.appium_status_target_count())
    active_jobs = list(data.get("active_worker_jobs") or [])
    running = sum(1 for row in active_jobs if str(row.get("status") or "") == "running")
    waiting = sum(1 for row in active_jobs if str(row.get("status") or "") in {"queued", "retry_wait"})

    st.markdown(
        f'''
        <div class="jr-status-grid">
          <div class="jr-status-card {'jr-status-okline' if adb_count else 'jr-status-badline'}">
            <div class="jr-status-title">🔌 ADB 연결</div>
            <div class="jr-status-value">{adb_count}대</div>
            <div class="jr-status-desc">Windows Host Agent 또는 로컬 ADB에서 감지한 에뮬레이터/실기기 수입니다.</div>
          </div>
          <div class="jr-status-card {'jr-status-okline' if appium_ok_count else 'jr-status-badline'}">
            <div class="jr-status-title">🧭 Appium 서버</div>
            <div class="jr-status-value">{appium_ok_count}/{appium_target_count}</div>
            <div class="jr-status-desc">에뮬레이터 4대 + USB 실기기 1대의 4723~4731 Appium 연결 상태입니다.</div>
          </div>
          <div class="jr-status-card {'jr-status-okline' if redis_ok else 'jr-status-warnline'}">
            <div class="jr-status-title">🧱 Redis 큐</div>
            <div class="jr-status-value">{'OK' if redis_ok else '확인'}</div>
            <div class="jr-status-desc">작업 대기/처리중 상태를 실시간으로 관리합니다.</div>
          </div>
          <div class="jr-status-card {'jr-status-okline' if host_ok else 'jr-status-warnline'}">
            <div class="jr-status-title">🖥 Host Agent</div>
            <div class="jr-status-value">{'ON' if host_ok else 'OFF'}</div>
            <div class="jr-status-desc">Windows 창 정렬, Appium 시작/중지, 폴더 열기를 담당합니다.</div>
          </div>
        </div>
        ''',
        unsafe_allow_html=True,
    )
    st.caption(f"작업 상태: 실행중 {running}개 · 대기/재시도 {waiting}개 · API/진단 소스 {data.get('source', 'api')}")
    with st.expander("상태 상세 보기", expanded=False):
        st.write("Appium")
        if appium_rows:
            st.dataframe(pd.DataFrame(appium_rows), use_container_width=True, hide_index=True)
        else:
            st.caption("Appium 상태 데이터가 없습니다.")
        st.write("ADB")
        devices = list(adb_data.get("devices") or [])
        if devices:
            st.dataframe(pd.DataFrame(devices), use_container_width=True, hide_index=True)
        else:
            st.caption("ADB 장치가 감지되지 않았습니다.")


def render_action_help() -> None:
    st.markdown(
        '''
        <div class="jr-action-help">
          <div class="jr-action-help-grid">
            <div class="jr-action-help-item"><strong>ADB 동기화</strong>현재 연결된 ADB 장치를 슬롯 정보에 반영합니다. 에뮬레이터가 떠 있는데 슬롯이 비어 있을 때 사용합니다.</div>
            <div class="jr-action-help-item"><strong>Appium 전체 시작</strong>Windows Host Agent를 통해 4723~4731 Appium 서버를 켭니다. 작업 큐 등록과는 별개입니다.</div>
            <div class="jr-action-help-item"><strong>Heartbeat 복구</strong>작업 중 멈춘 running job을 회수합니다. 강제 종료 후 작업중으로 남았을 때 사용합니다.</div>
            <div class="jr-action-help-item"><strong>실패 재시도</strong>failed 작업을 retry_wait/queued 상태로 돌려 다시 실행 대상으로 만듭니다.</div>
            <div class="jr-action-help-item"><strong>Appium 전체 중지</strong>작업 취소 후 Host Agent가 Appium 서버 프로세스를 종료합니다.</div>
          </div>
        </div>
        ''',
        unsafe_allow_html=True,
    )


def appium_start_all_action(slot_names: list[str] | None = None) -> dict[str, Any]:
    api = get_api_client()
    if api is not None:
        result = api.appium_start(slot_names=slot_names or [])
        if result.ok:
            return result.data
        return {"ok": False, "message": result.error}
    started, rows = start_appium_for_slots(slot_names or [])
    return {"ok": bool(started), "started": started, "rows": rows, "source": "local"}


def appium_stop_all_action(slot_names: list[str] | None = None) -> dict[str, Any]:
    api = get_api_client()
    if api is not None:
        result = api.appium_stop(slot_names=slot_names or [])
        if result.ok:
            return result.data
        return {"ok": False, "message": result.error}
    affected, redis_affected = cancel_worker_jobs_realtime(slot_names=slot_names or None, job_types=["appium_collect_profile"], message="GUI Appium 전체 중지")
    return {"ok": False, "message": "로컬 fallback은 작업 취소만 수행했습니다. 서버 종료는 Host Agent가 필요합니다.", "sqlite": affected, "redis": redis_affected}


def list_screenshots(limit: int = 80) -> tuple[list[dict[str, Any]], str]:
    api = get_api_client()
    if api is not None:
        result = api.screenshots(limit=limit)
        if result.ok:
            return list(result.data.get("screenshots") or []), str(result.data.get("folder") or "")
    folder = settings.output_dir / "screenshots"
    folder.mkdir(parents=True, exist_ok=True)
    rows = []
    for p in sorted(folder.glob("*.png"), key=lambda x: x.stat().st_mtime, reverse=True)[:limit]:
        rows.append({"name": p.name, "path": str(p), "size_bytes": p.stat().st_size, "modified_at": p.stat().st_mtime})
    return rows, str(folder.resolve())


def open_screenshot_folder_action() -> tuple[bool, str]:
    api = get_api_client()
    if api is not None:
        result = api.open_screenshot_folder()
        if result.ok:
            return True, str(result.data.get("message") or "스크린샷 폴더를 열었습니다.")
        return False, result.error or str(result.data.get("message") or "폴더 열기 실패")
    folder = settings.output_dir / "screenshots"
    return False, f"Docker/GUI에서는 Host Agent가 필요합니다. 폴더 위치: {folder.resolve()}"


def render_screenshots() -> None:
    st.title("스크린샷")
    st.caption("GUI에서 생성한 에뮬레이터/실기기 스크린샷을 확인합니다. Docker에서는 Host Agent를 통해 Windows 폴더 열기를 지원합니다.")
    show_feedback()
    c1, c2, c3 = st.columns([1, 1, 2])
    if c1.button("새로고침", use_container_width=True):
        st.rerun()
    if c2.button("폴더 열기", use_container_width=True):
        ok, msg = open_screenshot_folder_action()
        set_feedback("success" if ok else "warning", msg)
        st.rerun()
    selected_slots = c3.multiselect("캡처 대상", slot_options(), default=[])
    if st.button("선택/전체 스크린샷 생성", type="primary", use_container_width=True):
        saved, msg = screenshot_all_devices(slot_names=selected_slots or None)
        set_feedback("success" if saved else "warning", msg)
        st.rerun()

    rows, folder = list_screenshots(limit=100)
    st.markdown(f'<div class="jr-section-note">저장 폴더: <strong>{safe_html(folder)}</strong></div>', unsafe_allow_html=True)
    if not rows:
        st.info("아직 저장된 스크린샷이 없습니다. 상단의 스크린샷 생성 버튼을 눌러보세요.")
        return
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    st.subheader("최근 이미지")
    cols = st.columns(4)
    for idx, row in enumerate(rows[:24]):
        p = Path(str(row.get("path") or ""))
        with cols[idx % 4]:
            if p.exists():
                st.image(str(p), caption=str(row.get("name") or p.name), use_container_width=True)
            else:
                st.caption(str(row.get("name") or p))

def handle_control(command: str, slot_count: int = 5) -> None:
    if command == "start_all":
        queued, _ = queue_appium_slots([], run_now=False)
        repo.create_operation_command("start_all_appium", actor="admin", status="completed", payload={"queued": queued}, message=f"Appium 전체 작업 큐 등록: {queued}개")
        set_feedback("success", f"작업 큐 등록 완료: {queued}개. Appium 서버가 켜져 있어야 Worker가 실행됩니다.")
    elif command == "pause_all":
        affected, redis_affected = cancel_worker_jobs_realtime(job_types=["appium_collect_profile"], message="GUI 전체 중지 요청")
        repo.update_sessions_status(["queued", "starting", "running", "retry_wait"], "paused", "GUI 전체 중지 요청")
        set_feedback("warning", f"작업 중지 요청 완료: DB {affected}개 / Redis {redis_affected}개. Appium 서버는 유지됩니다.")
    elif command == "retry_failed":
        api = get_api_client()
        if api is not None:
            result = api.retry_failed_jobs(mode="appium")
            set_feedback("success" if result.ok else "error", f"실패 재시도: {result.data if result.ok else result.error}")
        else:
            retried = repo.retry_failed_worker_jobs(job_types=["appium_collect_profile"], message="GUI 실패 재시도")
            set_feedback("info", f"실패 작업 {retried}개를 재시도 대기로 전환했습니다.")
    elif command == "sync":
        api = get_api_client()
        if api is not None:
            result = api.post("/api/devices/sync", {})
            if result.ok:
                updated = int(result.data.get("updated") or 0)
                set_feedback("success", f"ADB 동기화 완료: {updated}개 슬롯 업데이트")
            else:
                set_feedback("error", f"ADB 동기화 실패: {result.error}")
        else:
            updated = sync_detected_devices(settings, repo)
            set_feedback("success", f"ADB 장치 상태를 {updated}개 슬롯에 동기화했습니다.")
    elif command == "recover_heartbeat":
        api = get_api_client()
        if api is not None:
            result = api.recover_stale_jobs(mode="appium")
            set_feedback("success" if result.ok else "error", f"Heartbeat 복구: {result.data if result.ok else result.error}")
        else:
            affected = repo.reset_stale_worker_jobs(message="GUI heartbeat recovery")
            set_feedback("warning", f"오래된 running 작업 {affected}개를 복구했습니다.")
    elif command == "appium_start_all":
        data = appium_start_all_action([])
        rows_value = data.get("rows")
        rows = rows_value if isinstance(rows_value, list) else []
        started_value = data.get("started")
        if isinstance(started_value, list):
            started = len(started_value)
        elif isinstance(started_value, int):
            started = started_value
        else:
            started = len(rows)
        ok = bool(data.get("ok", False))
        source = data.get("source", "api")
        msg = str(data.get("message") or "")
        if ok:
            ready = int(data.get("running") or sum(1 for row in rows if row.get("ok")))
            target = int(data.get("target_count") or data.get("count") or len(rows) or settings.appium_status_target_count())
            already = sum(1 for row in rows if row.get("already_running"))
            set_feedback("success", f"Appium 전체 시작/확인 완료: 사용 가능 {ready}/{target} · 새 시작 {started}개 · 기존 실행 {already}개 · {source}")
        else:
            detail = msg or "Host Agent/Appium 시작 실패. Host Agent 버전과 Appium 상태를 확인하세요."
            set_feedback("error", f"Appium 전체 시작 실패 · {source}: {detail}")
    elif command == "appium_stop_all":
        data = appium_stop_all_action([])
        stopped = data.get("stopped", 0)
        set_feedback("warning" if data.get("ok") else "error", f"Appium 전체 중지 요청: 종료 {stopped}개 · {data.get('message', data.get('source', ''))}")
    elif command == "screenshot_all":
        saved, message = screenshot_all_devices()
        set_feedback("success" if saved else "warning", message + " · 스크린샷 메뉴에서 확인하세요.")
    st.rerun()

def render_action_toolbar(location_key: str, slot_count: int = 4) -> None:
    render_action_help()
    cols = st.columns([1, 1, 1, 1, 1, 1, 1], gap="small")
    if cols[0].button("▶ 작업 큐 등록", type="primary", use_container_width=True, key=f"{location_key}_start"):
        handle_control("start_all", slot_count=slot_count)
    if cols[1].button("🔌 ADB 동기화", use_container_width=True, key=f"{location_key}_sync"):
        handle_control("sync", slot_count=slot_count)
    if cols[2].button("🧭 Appium 시작", use_container_width=True, key=f"{location_key}_appium_start"):
        handle_control("appium_start_all", slot_count=slot_count)
    if cols[3].button("🫀 Heartbeat 복구", use_container_width=True, key=f"{location_key}_recover"):
        handle_control("recover_heartbeat", slot_count=slot_count)
    if cols[4].button("↻ 실패 재시도", use_container_width=True, key=f"{location_key}_retry"):
        handle_control("retry_failed", slot_count=slot_count)
    if cols[5].button("▣ 스크린샷", use_container_width=True, key=f"{location_key}_shot"):
        handle_control("screenshot_all", slot_count=slot_count)
    if cols[6].button("■ Appium 중지", use_container_width=True, key=f"{location_key}_appium_stop"):
        handle_control("appium_stop_all", slot_count=slot_count)




def safe_html(value: Any) -> str:
    return html.escape(str(value or ""))


def compact_text(value: Any, max_chars: int = 64) -> str:
    text = str(value or "").strip()
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "…"


def sidebar_connection_card(title: str, value: str, port: str = "", ok: bool = True) -> str:
    badge = "연결됨" if ok else "확인 필요"
    badge_class = "jr-badge-ok" if ok else "jr-badge-warn"
    value_html = safe_html(value or "-")
    port_html = safe_html(port or "-")
    return (
        '<div class="jr-conn-card">'
        f'<div class="jr-conn-title"><span>{safe_html(title)}</span><span class="{badge_class}">{badge}</span></div>'
        f'<div class="jr-conn-value" title="{value_html}">{value_html}</div>'
        f'<div class="jr-conn-meta"><span>Port</span><strong>{port_html}</strong></div>'
        '</div>'
    )


def extract_port_from_url(value: str, default: str = "") -> str:
    text = str(value or "")
    if "://" in text:
        host_part = text.split("//", 1)[-1].split("/", 1)[0]
        if ":" in host_part:
            return host_part.rsplit(":", 1)[-1]
    return default


def status_visual(status: str, health: int = 100, has_error: bool = False) -> tuple[str, str, str, str]:
    raw = str(status or "대기").lower()
    if has_error or raw in {"failed", "error", "오류"} or health < 40:
        return "오류", "jr-dot err", "jr-status-pill jr-status-err", "err"
    if raw in {"running", "starting"}:
        return STATUS_KO.get(raw, "실행중"), "jr-dot run", "jr-status-pill jr-status-run", "run"
    if raw in {"queued", "retry_wait", "paused", "대기"}:
        return STATUS_KO.get(raw, "대기"), "jr-dot wait", "jr-status-pill jr-status-wait", "wait"
    return STATUS_KO.get(raw, "정상"), "jr-dot ok", "jr-status-pill jr-status-ok", "ok"


def health_class(health: int) -> str:
    if health < 40:
        return "err"
    if health < 75:
        return "warn"
    return ""


def get_slot_error_logs(slot_name: str, session: dict[str, Any] | None = None, limit: int = 160) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    session = session or {}
    now_label = str(session.get("finished_at") or session.get("started_at") or datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    error_message = str(session.get("error_message") or "").strip()
    if error_message:
        rows.append({"time": now_label, "level": "ERROR", "message": error_message})

    try:
        events = repo.list_worker_events(limit=limit, slot_name=slot_name)
        for event in events:
            level = "INFO"
            event_type = str(event.get("event_type") or "")
            message = str(event.get("message") or "")
            haystack = (event_type + " " + message).lower()
            if any(token in haystack for token in ["error", "failed", "exception"]):
                level = "ERROR"
            elif any(token in haystack for token in ["warn", "retry", "stale"]):
                level = "WARN"
            rows.append({
                "time": str(event.get("created_at") or "-"),
                "level": level,
                "message": message or str(event.get("payload") or ""),
            })
    except Exception as exc:
        rows.append({"time": datetime.now().strftime("%H:%M:%S"), "level": "WARN", "message": f"Worker 이벤트 조회 실패: {exc}"})

    safe_slot = slot_name.replace(" ", "_")
    log_candidates = [
        settings.output_dir / "logs" / f"appium_{safe_slot}.log",
        settings.output_dir / "logs" / f"{safe_slot}.log",
    ]
    for path in log_candidates:
        if not path.exists():
            continue
        try:
            for line in path.read_text(encoding="utf-8", errors="ignore").splitlines()[-limit:]:
                clean = line.strip()
                if not clean:
                    continue
                upper = clean.upper()
                level = "ERROR" if "ERROR" in upper or "EXCEPTION" in upper else "WARN" if "WARN" in upper else "INFO"
                rows.append({"time": path.name, "level": level, "message": clean})
        except Exception as exc:
            rows.append({"time": path.name, "level": "WARN", "message": f"로그 파일 읽기 실패: {exc}"})

    if not rows:
        rows.append({"time": datetime.now().strftime("%H:%M:%S"), "level": "INFO", "message": "표시할 에러 로그가 없습니다."})
    return rows[:limit]


def render_error_log_dialog(slot_name: str, session: dict[str, Any] | None = None) -> None:
    @st.dialog(f"{slot_name} - 에러 로그", width="large")
    def _dialog() -> None:
        logs = get_slot_error_logs(slot_name, session=session, limit=180)
        st.caption("긴 오류 메시지는 카드 안에 직접 노출하지 않고, 이 로그 뷰어에서 스크롤로 확인합니다.")
        rows_html = [
            '<div class="jr-log-row jr-log-head"><div>시간</div><div>레벨</div><div>메시지</div></div>'
        ]
        for item in logs:
            level = str(item.get("level") or "INFO").upper()
            level_class = "jr-level-error" if level == "ERROR" else "jr-level-warn" if level == "WARN" else "jr-level-info"
            rows_html.append(
                '<div class="jr-log-row">'
                f'<div>{safe_html(item.get("time"))}</div>'
                f'<div><span class="jr-level {level_class}">{safe_html(level)}</span></div>'
                f'<div class="jr-log-msg">{safe_html(item.get("message"))}</div>'
                '</div>'
            )
        st.markdown(f'<div class="jr-log-scroll">{"".join(rows_html)}</div>', unsafe_allow_html=True)
        c1, c2, _ = st.columns([1, 1, 4])
        if c1.button("닫기", use_container_width=True, key=f"close_error_dialog_{slot_name}"):
            st.session_state.pop("jr_error_dialog_slot", None)
            st.session_state.pop("jr_error_dialog_session", None)
            st.rerun()
        if c2.button("로그 새로고침", use_container_width=True, key=f"refresh_error_dialog_{slot_name}"):
            st.rerun()
    _dialog()


def render_sidebar() -> str:
    db_port = "5432"
    db_url = str(settings.database_url)
    if ":" in db_url and "postgres" in db_url.lower():
        db_port = extract_port_from_url(db_url, "5432") or "5432"
    api_port = extract_port_from_url(settings.api_url, "8000") or "8000"

    st.sidebar.markdown(
        '<div class="jr-sidebar-brand">'
        '<div class="jr-logo-row"><div class="jr-logo-mark"></div><div><div class="jr-brand-title">JobRadar</div><div class="jr-brand-pill">Automation Console</div></div></div>'
        '</div>',
        unsafe_allow_html=True,
    )
    st.sidebar.markdown('<div class="jr-side-section">메인 메뉴</div>', unsafe_allow_html=True)
    page = st.sidebar.radio(
        "메뉴",
        ["대시보드", "캠페인/프로필", "5-슬롯 제어", "실무 운영", "API 서버", "조회 기록", "스크린샷", "알림", "리포트/내보내기", "설정/진단"],
        label_visibility="collapsed",
    )
    st.sidebar.divider()
    st.sidebar.markdown('<div class="jr-side-section">시스템 시간</div>', unsafe_allow_html=True)
    st.sidebar.markdown(
        f'<div class="jr-time-box"><strong>{datetime.now().strftime("%Y-%m-%d %H:%M:%S")}</strong><span style="float:right;color:#94a3b8;">KST</span></div>',
        unsafe_allow_html=True,
    )
    st.sidebar.markdown('<div class="jr-side-section">운영자</div>', unsafe_allow_html=True)
    st.sidebar.markdown(
        '<div class="jr-operator-box"><div class="jr-operator-row"><div class="jr-avatar">A</div><div><strong>로컬 운영 콘솔</strong><br><span class="jr-online">● 온라인</span></div></div></div>',
        unsafe_allow_html=True,
    )
    st.sidebar.markdown('<div class="jr-side-section">연결 정보</div>', unsafe_allow_html=True)
    st.sidebar.markdown(
        sidebar_connection_card("DB (PostgreSQL)", compact_text(db_url, 58), db_port, ok=True)
        + sidebar_connection_card("API 서버", compact_text(settings.api_url, 58), api_port, ok=get_api_client() is not None),
        unsafe_allow_html=True,
    )
    st.sidebar.markdown('<div style="height:20px"></div><span style="color:#94a3b8;font-size:12px;">JobRadar v2.7.0 · UI polish</span>', unsafe_allow_html=True)
    return page


def render_emulator_card(session: dict[str, Any]) -> None:
    slot = str(session.get("slot_name") or "Emulator ?")
    status = str(session.get("status") or "대기")
    keyword = session.get("keyword") or "키워드 미배정"
    count = int(session.get("found_count") or 0)
    health = max(0, min(100, int(session.get("health_percent") or 100)))
    profile = session.get("profile_name") or "미배정"
    device_id = session.get("device_id") or "logical-slot"
    error = str(session.get("error_message") or "").strip()
    has_error = bool(error) or status in {"failed", "오류"}
    status_label, dot_class, pill_class, _ = status_visual(status, health, has_error=has_error)
    health_fill_class = health_class(health)
    card_class = "jr-slot-card error" if has_error else "jr-slot-card"
    error_time = str(session.get("finished_at") or session.get("started_at") or "-")
    error_preview = compact_text(error, 44) if error else f"최근 오류 발생 · {error_time}"
    health_color = "#dc2626" if has_error else "#111827"
    progress_percent = _safe_int(session.get("progress_percent"), 0)
    progress_message = compact_text(str(session.get("progress_message") or ""), 70)
    if has_error:
        state_html = f'<div class="jr-error-strip"><span>⚠ {safe_html(error_preview)}</span></div>'
    elif status in {"running", "queued", "retry_wait"} or progress_percent:
        state_text = progress_message or STATUS_KO.get(status, status)
        state_html = f'<div class="jr-progress-note">{safe_html(state_text)} · {progress_percent}%</div>'
    else:
        state_html = '<div class="jr-empty-strip"></div>'
    html_block = (
        f'<div class="{card_class}">'
        f'<div class="jr-slot-head">'
        f'<div class="jr-slot-name"><span class="{dot_class}"></span><span title="{safe_html(slot)}">{safe_html(slot)}</span></div>'
        f'<div class="{pill_class}">{safe_html(status_label)}</div>'
        f'</div>'
        f'<div class="jr-health-row"><div>Health</div>'
        f'<div class="jr-health-track"><div class="jr-health-fill {health_fill_class}" style="width:{health}%"></div></div>'
        f'<div style="text-align:right;font-weight:800;color:#475569;">{health}%</div></div>'
        f'<div class="jr-info-grid">'
        f'<div class="jr-info-label">현재 프로필</div><div class="jr-info-value" title="{safe_html(profile)}">{safe_html(profile)}</div>'
        f'<div class="jr-info-label">키워드</div><div class="jr-info-value" title="{safe_html(keyword)}">{safe_html(keyword)}</div>'
        f'<div class="jr-info-label">장치</div><div class="jr-info-value" title="{safe_html(device_id)}">{safe_html(device_id)}</div>'
        f'</div>'
        f'<div class="jr-stat-grid">'
        f'<div class="jr-stat"><div class="jr-stat-label">수집 건수</div><div class="jr-stat-value">{count:,}건</div></div>'
        f'<div class="jr-stat"><div class="jr-stat-label">건강도</div><div class="jr-stat-value" style="color:{health_color}">{health / 100:.2f}</div></div>'
        f'</div>{state_html}</div>'
    )
    st.markdown(html_block, unsafe_allow_html=True)
    a1, a2, a3 = st.columns([1, 1, 1], gap="small")
    a1.button("프로필 대기", use_container_width=True, key=f"slot_profile_wait_{slot}", disabled=True)
    a2.button("작업 제어", use_container_width=True, key=f"slot_control_{slot}", disabled=True)
    if has_error:
        if a3.button("에러 로그 보기", use_container_width=True, key=f"slot_error_log_{slot}"):
            st.session_state["jr_error_dialog_slot"] = slot
            st.session_state["jr_error_dialog_session"] = dict(session)
            st.rerun()
    else:
        a3.button("정상", use_container_width=True, key=f"slot_ok_{slot}", disabled=True)


def render_dashboard() -> None:
    st.title("대시보드")
    st.caption("플랫폼 운영 현황을 한눈에 확인하고, 4대 에뮬레이터와 USB 1대 슬롯을 즉시 제어합니다.")
    show_feedback()

    stats = repo.stats()
    redis_status, redis_jobs, redis_events = redis_status_snapshot()
    worker_jobs_all = repo.list_worker_jobs(limit=300)
    active_worker_jobs = [row for row in worker_jobs_all if str(row.get("status") or "") in {"queued", "retry_wait", "running"}]
    running_jobs = [row for row in active_worker_jobs if str(row.get("status") or "") == "running"]
    waiting_jobs = [row for row in active_worker_jobs if str(row.get("status") or "") in {"queued", "retry_wait"}]
    running_slots = {str(row.get("slot_name") or "") for row in running_jobs if row.get("slot_name")}

    c1, c2, c3, c4, c5, c6 = st.columns(6)
    c1.metric("활성 검색 프로필", stats["enabled_profiles"], "기본 4개 권장")
    c2.metric("작업중 슬롯", len(running_slots), f"대기 {len(waiting_jobs)}건")
    c3.metric("전체 조회 기록", f"{stats['total_jobs']:,}")
    c4.metric("오늘 수집 건수", f"{stats['today_jobs']:,}")
    c5.metric("오류 세션", stats["failed_sessions"])
    success_rate = 100.0
    sessions = repo.list_emulator_sessions(limit=200)
    if sessions:
        failures = sum(1 for row in sessions if row.get("status") == "failed")
        success_rate = max(0.0, (len(sessions) - failures) / len(sessions) * 100)
    c6.metric("성공률", f"{success_rate:.2f}%")

    if redis_status is not None:
        r1, r2, r3 = st.columns(3)
        r1.metric("Redis 대기 작업", redis_status.queued, "실행 전 큐")
        r2.metric("Redis 작업중", redis_status.processing, "Worker가 잡은 작업")
        r3.metric("Redis 이벤트", redis_status.events, "최근 상태 이벤트")
    elif settings.redis_queue_enabled:
        st.warning("Redis 실시간 큐가 켜져 있지만 현재 연결되지 않았습니다. Docker redis 컨테이너 상태를 확인하세요.")

    render_ops_status_center("dashboard")

    st.divider()
    render_action_toolbar("dashboard", slot_count=settings.emulator_slots)
    st.caption("수집 결과는 Appium 상세 페이지를 읽는 즉시 DB에 저장됩니다. 조회 기록이 비어 보이면 새로고침 또는 DB/API 상태를 확인하세요.")

    left, right = st.columns([2.1, 1], gap="large")
    with left:
        st.subheader("5슬롯 운영 현황")
        slot_rows = _merge_slot_runtime(repo.recent_slot_summary(slots=settings.emulator_slots), active_worker_jobs, redis_jobs)
        grid_cols = st.columns(2, gap="medium")
        for index, session in enumerate(slot_rows):
            with grid_cols[index % 2]:
                render_emulator_card(session)

        st.subheader("조회 기록 분포")
        chart1, chart2 = st.columns(2)
        with chart1:
            df = pd.DataFrame(stats["by_slot"])
            if not df.empty:
                st.bar_chart(df.set_index("emulator_slot"))
            else:
                st.info("아직 슬롯별 수집 데이터가 없습니다.")
        with chart2:
            df = pd.DataFrame(stats["by_location"])
            if not df.empty:
                st.bar_chart(df.set_index("location"))
            else:
                st.info("아직 지역 통계가 없습니다.")

    with right:
        st.subheader("작업 큐")
        if active_worker_jobs:
            for row in active_worker_jobs[:10]:
                status = str(row.get("status") or "")
                progress = int(row.get("progress_percent") or 0)
                with st.container(border=True):
                    st.write(f"**{row.get('slot_name') or '-'} · {row.get('profile_name') or '-'}**")
                    st.caption(f"{ko_status(status)} · job #{row.get('id')} · attempt {row.get('attempts')}/{row.get('max_attempts')}")
                    if progress:
                        st.progress(min(100, max(0, progress)))
                    msg = str(row.get("progress_message") or row.get("error_message") or "")
                    if msg:
                        st.caption(msg[:180] + ("..." if len(msg) > 180 else ""))
        else:
            st.info("대기/작업중 Worker Job이 없습니다. '전체 시작'을 누르면 큐가 생성됩니다.")

        st.subheader("Redis 실시간 큐")
        if redis_status is None:
            st.caption("Redis 큐가 비활성화되었거나 연결되지 않았습니다.")
        else:
            st.caption(f"{redis_status.message} · queued={redis_status.queued} · processing={redis_status.processing}")
            if redis_jobs:
                redis_df = pd.DataFrame(redis_jobs[:12])
                cols = [c for c in ["id", "status", "slot_name", "profile_name", "progress_percent", "progress_message"] if c in redis_df.columns]
                st.dataframe(redis_df[cols], use_container_width=True, hide_index=True)
            elif redis_events:
                st.dataframe(pd.DataFrame(redis_events[:8]), use_container_width=True, hide_index=True)
            else:
                st.caption("아직 Redis 이벤트가 없습니다.")

        st.subheader("알림 센터")
        alerts = repo.list_alert_events(limit=5)
        if alerts:
            for item in alerts:
                st.caption(f"{item['created_at']} · {item['message']}")
        else:
            st.caption("대기 중인 알림이 없습니다.")

        st.subheader("최근 운영 명령")
        commands = repo.list_operation_commands(limit=8)
        if commands:
            st.dataframe(pd.DataFrame(commands), use_container_width=True, hide_index=True)
        else:
            st.caption("아직 운영 명령이 없습니다.")

    dialog_slot = st.session_state.get("jr_error_dialog_slot")
    if dialog_slot:
        render_error_log_dialog(str(dialog_slot), st.session_state.get("jr_error_dialog_session") or {})

def render_campaigns() -> None:
    st.title("캠페인 / 검색 프로필")
    show_feedback()
    col_a, col_b = st.columns([1.2, 1])
    with col_a:
        st.subheader("검색 프로필 관리")
        with st.form("profile_form", clear_on_submit=False):
            campaign = st.text_input("캠페인명", value=settings.default_campaign_name)
            name = st.text_input("프로필명", value="백엔드 개발자")
            keyword = st.text_input("키워드", value="백엔드 개발자")
            target_url = st.text_area("대상 URL", value=settings.target_url, height=82)
            c1, c2, c3 = st.columns(3)
            priority = c1.number_input("우선순위", 1, 999, 100)
            max_items = c2.number_input("최대 수집", 1, 500, 20)
            scroll_times = c3.number_input("스크롤", 1, 30, 3)
            submitted = st.form_submit_button("프로필 저장", use_container_width=True)
            if submitted:
                repo.upsert_campaign(campaign, "GUI에서 생성된 캠페인", enabled=1)
                profile_id = repo.upsert_search_profile(
                    SearchProfile(
                        campaign_name=campaign,
                        name=name,
                        keyword=keyword,
                        target_url=target_url,
                        priority=int(priority),
                        max_items=int(max_items),
                        scroll_times=int(scroll_times),
                    )
                )
                set_feedback("success", f"프로필 저장 완료: #{profile_id}")
                st.rerun()

        if st.button("기본 4개 프로필 자동 구성", use_container_width=True):
            count = repo.seed_default_profiles(settings.target_url)
            set_feedback("success", f"기본 프로필 {count}개를 구성했습니다.")
            st.rerun()

    with col_b:
        st.subheader("캠페인")
        campaigns = repo.list_campaigns()
        if campaigns:
            st.dataframe(pd.DataFrame(campaigns), use_container_width=True, hide_index=True)
        else:
            st.info("캠페인이 없습니다.")

    st.subheader("검색 프로필 목록")
    profiles = [p.to_dict() for p in repo.list_search_profiles(enabled_only=False, limit=100)]
    if profiles:
        st.dataframe(pd.DataFrame(profiles), use_container_width=True, hide_index=True)
    else:
        st.info("검색 프로필이 없습니다.")


def render_emulators() -> None:
    st.title("5-슬롯 Appium 제어")
    st.caption("Emulator A-D와 USB 실기기를 슬롯 단위로 큐 등록, 실행, 중지합니다. 이 화면은 기존 Playwright 직접 실행을 사용하지 않습니다.")
    show_feedback()

    slots = ensure_device_slots(settings.emulator_slots)
    names = [str(row.get("slot_name")) for row in slots]
    selected = st.multiselect("실행/제어할 슬롯", names, default=names)

    c1, c2, c3, c4 = st.columns(4)
    if c1.button("선택 슬롯 큐 등록", type="primary", use_container_width=True):
        queued, _ = queue_appium_slots(selected, run_now=False)
        set_feedback("success", f"선택 슬롯 Appium 작업 {queued}개를 큐에 등록했습니다.")
        st.rerun()
    if c2.button("선택 슬롯 즉시 실행", use_container_width=True):
        queued, results = queue_appium_slots(selected, run_now=True, max_jobs=len(selected) or 1)
        set_feedback("success" if results else "info", f"큐 등록 {queued}개 / Worker 처리 {len(results)}건")
        st.rerun()
    if c3.button("선택 즉시 중지", use_container_width=True, help="DB/Redis 중지 요청 후 Appium 활성 세션을 강제 종료합니다."):
        data = device_control_action("immediate_stop", selected_slots=selected)
        set_feedback("warning", summarize_control_result(data))
        st.rerun()
    if c4.button("stale running 정리", use_container_width=True):
        affected = repo.reset_stale_worker_jobs(slot_names=selected or None, job_types=["appium_collect_profile"], message="GUI stale running reset")
        set_feedback("warning", f"stale running 작업 {affected}개를 정리했습니다.")
        st.rerun()

    d1, d2, d3, d4 = st.columns(4)
    if d1.button("선택 작업 이어하기", use_container_width=True, help="중지/실패 작업을 재시도 대기 또는 신규 큐로 되돌립니다."):
        data = device_control_action("resume", selected_slots=selected, run_now=False)
        set_feedback("success", summarize_control_result(data))
        st.rerun()
    if d2.button("선택 홈 이동", use_container_width=True):
        data = device_control_action("home", selected_slots=selected)
        set_feedback("success" if any(row.get("ok") for row in data.get("rows", [])) else "warning", summarize_control_result(data))
        st.rerun()
    if d3.button("선택 창 닫기&홈", use_container_width=True, help="Chrome/Saramin 세션을 정리하고 홈 화면으로 이동합니다."):
        data = device_control_action("close_all_home", selected_slots=selected)
        set_feedback("success" if any(row.get("ok") for row in data.get("rows", [])) else "warning", summarize_control_result(data))
        st.rerun()
    with d4.popover("패키지 앱 실행", use_container_width=True):
        pkg = st.text_input("패키지명", value="com.android.chrome", key="emu_pkg_launch")
        act = st.text_input("Activity 선택", value="", placeholder="예: .MainActivity", key="emu_pkg_activity")
        if st.button("선택 슬롯에서 실행", use_container_width=True, key="emu_pkg_launch_btn"):
            data = device_control_action("launch_package", selected_slots=selected, package_name=pkg, activity_name=act)
            set_feedback("success" if any(row.get("ok") for row in data.get("rows", [])) else "warning", summarize_control_result(data))
            st.rerun()

    render_action_toolbar("slot_control", slot_count=settings.emulator_slots)

    st.divider()
    render_window_arrange_panel("slot_control_window")

    st.subheader("슬롯 상태")
    sync_detected_devices(settings, repo)
    slot_df = pd.DataFrame(repo.list_device_slots())
    if not slot_df.empty:
        columns = [
            "slot_name", "enabled", "device_type", "assigned_profile_name", "avd_name", "udid",
            "status", "appium_url", "appium_port", "system_port", "chromedriver_port", "notes", "last_seen_at",
        ]
        st.dataframe(slot_df[[c for c in columns if c in slot_df.columns]], use_container_width=True, hide_index=True)

    st.subheader("최근 Worker 처리 결과")
    if st.session_state.get("jr_worker_results"):
        st.dataframe(pd.DataFrame(st.session_state["jr_worker_results"]), use_container_width=True, hide_index=True)
    else:
        st.info("아직 이번 화면에서 실행한 Worker 결과가 없습니다.")

    st.subheader("Worker Queue")
    jobs = repo.list_worker_jobs(limit=100)
    if jobs:
        st.dataframe(pd.DataFrame(jobs), use_container_width=True, hide_index=True)
    else:
        st.info("대기 중인 Worker 작업이 없습니다.")

    st.subheader("수집 결과 미리보기")
    result_slot = st.selectbox("결과 필터 슬롯", [""] + names, key="slot_result_filter")
    rows = repo.list_jobs(limit=200, emulator_slot=result_slot)
    df = dataframe_rows(rows)
    if not df.empty:
        preferred = ["id", "last_seen_at", "emulator_slot", "profile_name", "title", "company", "location", "detail_url"]
        st.dataframe(df[[c for c in preferred if c in df.columns]], use_container_width=True, hide_index=True)
    else:
        st.info("표시할 수집 결과가 없습니다.")



def render_pro_operations() -> None:
    st.title("실무형 모바일 자동화 운영")
    st.caption("4대 에뮬레이터 + USB 실기기를 Appium 슬롯으로 관리합니다. Docker 전 단계의 로컬 운영 콘솔입니다.")
    show_feedback()

    if not repo.list_search_profiles(enabled_only=True, limit=1):
        repo.seed_default_profiles(settings.target_url)
    slots = ensure_device_slots(settings.emulator_slots)
    slot_names = [str(row.get("slot_name")) for row in slots]
    profiles = profile_options()

    render_ops_status_center("pro_ops")
    render_action_help()

    top1, top2, top3, top4, top5, top6 = st.columns(6)
    if top1.button("5개 슬롯 초기화", type="primary", use_container_width=True):
        count = repo.seed_device_slots(
            slot_count=settings.emulator_slots,
            appium_host=settings.appium_host,
            appium_base_port=settings.appium_base_port,
            appium_port_step=settings.appium_port_step,
            system_port_base=settings.appium_system_port_base,
            mjpeg_port_base=settings.appium_mjpeg_port_base,
            chromedriver_port_base=settings.appium_chromedriver_port_base,
            emulator_port_pairs=settings.parsed_emulator_port_pairs(),
        )
        set_feedback("success", f"장치 슬롯 {count}개를 구성했습니다.")
        st.rerun()
    if top2.button("ADB 동기화", use_container_width=True, help="연결된 ADB 장치를 슬롯에 반영합니다."):
        handle_control("sync", slot_count=settings.emulator_slots)
    if top3.button("Appium 전체 시작", use_container_width=True, help="Host Agent를 통해 4723~4731 서버를 켭니다."):
        handle_control("appium_start_all", slot_count=settings.emulator_slots)
    if top4.button("Heartbeat 복구", use_container_width=True, help="오래 멈춘 running 작업을 회수합니다."):
        handle_control("recover_heartbeat", slot_count=settings.emulator_slots)
    if top5.button("실패 재시도", use_container_width=True, help="failed 작업을 재실행 대상으로 되돌립니다."):
        handle_control("retry_failed", slot_count=settings.emulator_slots)
    if top6.button("Appium 전체 중지", use_container_width=True, help="작업 취소 후 Host Agent로 Appium 서버를 종료합니다."):
        handle_control("appium_stop_all", slot_count=settings.emulator_slots)

    st.divider()
    st.subheader("선택 슬롯 실행")
    selected_slots = st.multiselect("대상 슬롯", slot_names, default=slot_names, key="pro_selected_slots")
    a1, a2, a3, a4 = st.columns(4)
    if a1.button("선택 Appium 시작", use_container_width=True):
        data = appium_start_all_action(selected_slots)
        st.session_state["jr_appium_start_results"] = data.get("rows") or data.get("started") or []
        set_feedback("success" if data.get("ok", True) else "error", f"선택 Appium 시작 요청 완료 · {data.get('source', 'api')}")
        st.rerun()
    if a2.button("선택 큐 등록", type="primary", use_container_width=True):
        queued, _ = queue_appium_slots(selected_slots, run_now=False)
        set_feedback("success", f"선택 슬롯 Appium 작업 {queued}개를 큐에 등록했습니다.")
        st.rerun()
    if a3.button("선택 큐+즉시 실행", use_container_width=True):
        queued, results = queue_appium_slots(selected_slots, run_now=True, max_jobs=len(selected_slots) or 1)
        set_feedback("success" if results else "info", f"큐 등록 {queued}개 / Worker 처리 {len(results)}건")
        st.rerun()
    if a4.button("선택 즉시 중지", use_container_width=True, help="작업 취소 + Redis 정리 + 활성 Appium 세션 강제 종료"):
        data = device_control_action("immediate_stop", selected_slots=selected_slots)
        set_feedback("warning", summarize_control_result(data))
        st.rerun()

    b1, b2, b3, b4 = st.columns(4)
    if b1.button("선택 작업 이어하기", use_container_width=True):
        data = device_control_action("resume", selected_slots=selected_slots, run_now=False)
        set_feedback("success", summarize_control_result(data))
        st.rerun()
    if b2.button("선택 홈 이동", use_container_width=True):
        data = device_control_action("home", selected_slots=selected_slots)
        set_feedback("success" if any(row.get("ok") for row in data.get("rows", [])) else "warning", summarize_control_result(data))
        st.rerun()
    if b3.button("선택 창 닫기&홈", use_container_width=True):
        data = device_control_action("close_all_home", selected_slots=selected_slots)
        set_feedback("success" if any(row.get("ok") for row in data.get("rows", [])) else "warning", summarize_control_result(data))
        st.rerun()
    with b4.popover("패키지 앱 실행", use_container_width=True):
        pkg = st.text_input("패키지명", value="com.android.chrome", key="pro_pkg_launch")
        act = st.text_input("Activity 선택", value="", placeholder="예: .MainActivity", key="pro_pkg_activity")
        if st.button("선택 슬롯에서 실행", use_container_width=True, key="pro_pkg_launch_btn"):
            data = device_control_action("launch_package", selected_slots=selected_slots, package_name=pkg, activity_name=act)
            set_feedback("success" if any(row.get("ok") for row in data.get("rows", [])) else "warning", summarize_control_result(data))
            st.rerun()

    c1, c2 = st.columns(2)
    if c1.button("선택 heartbeat 복구", use_container_width=True):
        result = repo.recover_stale_worker_jobs(
            stale_after_seconds=settings.worker_stale_after_seconds,
            slot_names=selected_slots or None,
            job_types=["appium_collect_profile"],
            message="GUI 선택 슬롯 heartbeat recovery",
            auto_retry=settings.worker_auto_retry,
        )
        set_feedback("warning", f"선택 슬롯 stale 복구: total={result['total']} retry={result['retried']} failed={result['failed']}")
        st.rerun()
    if c2.button("선택 실패 재시도", use_container_width=True):
        affected = repo.retry_failed_worker_jobs(slot_names=selected_slots or None, job_types=["appium_collect_profile"], message="GUI 선택 슬롯 실패 재시도")
        set_feedback("info", f"선택 슬롯 재시도 대기 전환: {affected}개")
        st.rerun()

    st.divider()
    render_window_arrange_panel("pro_ops_window")

    if st.session_state.get("jr_appium_start_results"):
        with st.expander("최근 Appium 시작 결과", expanded=False):
            st.dataframe(pd.DataFrame(st.session_state["jr_appium_start_results"]), use_container_width=True, hide_index=True)
    if st.session_state.get("jr_worker_results"):
        with st.expander("최근 Worker 처리 결과", expanded=True):
            st.dataframe(pd.DataFrame(st.session_state["jr_worker_results"]), use_container_width=True, hide_index=True)

    st.divider()
    st.subheader("슬롯별 검색 조건 / 장치 설정")
    avds, avd_message = list_avds(settings)
    for slot in repo.list_device_slots():
        slot_name = str(slot.get("slot_name"))
        with st.expander(f"{slot_name} 설정", expanded=slot_name in {"Emulator A", "USB Device"}):
            with st.form(f"slot_form_{slot_name}"):
                c1, c2, c3 = st.columns([0.7, 1, 1])
                enabled = c1.checkbox("사용", value=bool(int(slot.get("enabled") if slot.get("enabled") is not None else 1)))
                profile_values = [""] + profiles
                current_profile = str(slot.get("assigned_profile_name") or "")
                profile_index = profile_values.index(current_profile) if current_profile in profile_values else 0
                assigned_profile = c2.selectbox("검색 프로필", profile_values, index=profile_index, help="비워두면 슬롯 순서대로 자동 매핑됩니다.")
                device_type_values = ["emulator", "usb"]
                current_type = str(slot.get("device_type") or ("usb" if slot_name == "USB Device" else "emulator"))
                device_type = c3.selectbox("장치 유형", device_type_values, index=device_type_values.index(current_type) if current_type in device_type_values else 0)

                d1, d2, d3 = st.columns([1.2, 1.2, 1])
                avd_value = str(slot.get("avd_name") or "")
                avd_list = [""] + avds
                avd_name = d1.selectbox("AVD", avd_list, index=avd_list.index(avd_value) if avd_value in avd_list else 0, help=avd_message if not avds else "")
                udid = d2.text_input("UDID", value=str(slot.get("udid") or ""), placeholder="emulator-5554 또는 USB serial")
                proxy_name = d3.text_input("Proxy", value=str(slot.get("proxy_name") or ""))

                p1, p2, p3, p4, p5 = st.columns(5)
                appium_port = p1.number_input("Appium", min_value=0, max_value=65535, value=int(slot.get("appium_port") or 0), step=2)
                system_port = p2.number_input("systemPort", min_value=0, max_value=65535, value=int(slot.get("system_port") or 0), step=1)
                chrome_port = p3.number_input("ChromeDriver", min_value=0, max_value=65535, value=int(slot.get("chromedriver_port") or 0), step=1)
                console_port = p4.number_input("Console", min_value=0, max_value=65535, value=int(slot.get("emulator_console_port") or 0), step=2)
                adb_port = p5.number_input("ADB", min_value=0, max_value=65535, value=int(slot.get("emulator_adb_port") or 0), step=2)
                notes = st.text_input("메모", value=str(slot.get("notes") or ""))
                saved = st.form_submit_button("슬롯 설정 저장", use_container_width=True)
                if saved:
                    appium_url = f"http://{settings.appium_host}:{int(appium_port)}" if int(appium_port) else str(slot.get("appium_url") or "")
                    repo.upsert_device_slot(
                        slot_name,
                        avd_name=avd_name,
                        udid=udid,
                        proxy_name=proxy_name,
                        status=str(slot.get("status") or "idle"),
                        notes=notes,
                        appium_url=appium_url,
                        appium_port=int(appium_port),
                        system_port=int(system_port),
                        chromedriver_port=int(chrome_port),
                        emulator_console_port=int(console_port),
                        emulator_adb_port=int(adb_port),
                        device_type=device_type,
                        assigned_profile_name=assigned_profile,
                        enabled=1 if enabled else 0,
                    )
                    set_feedback("success", f"{slot_name} 설정을 저장했습니다.")
                    st.rerun()

    st.divider()
    left, right = st.columns([1.35, 1], gap="large")
    with left:
        st.subheader("Worker Queue")
        redis_status, redis_jobs, redis_events = redis_status_snapshot()
        if redis_status is not None:
            st.caption(f"Redis: {redis_status.message} · queued={redis_status.queued} · processing={redis_status.processing}")
            if redis_jobs:
                with st.expander("Redis 실시간 작업", expanded=True):
                    st.dataframe(pd.DataFrame(redis_jobs), use_container_width=True, hide_index=True)
            if redis_events:
                with st.expander("Redis 최근 이벤트", expanded=False):
                    st.dataframe(pd.DataFrame(redis_events), use_container_width=True, hide_index=True)
        jobs = repo.list_worker_jobs(limit=150)
        if jobs:
            st.dataframe(pd.DataFrame(jobs), use_container_width=True, hide_index=True)
        else:
            st.info("대기 중인 Worker 작업이 없습니다.")

        st.subheader("Worker heartbeat / progress events")
        events = repo.list_worker_events(limit=80)
        if events:
            st.dataframe(pd.DataFrame(events), use_container_width=True, hide_index=True)
        else:
            st.caption("아직 Worker 이벤트가 없습니다.")

        st.subheader("ADB 장치")
        devices = [device.to_dict() for device in list_device_farm_devices(settings)]
        if devices:
            st.dataframe(pd.DataFrame(devices), use_container_width=True, hide_index=True)
        else:
            st.info("ADB 장치가 없습니다.")
        stop_udid = st.text_input("종료할 에뮬레이터 UDID", value="", placeholder="예: emulator-5554")
        if st.button("선택 에뮬레이터 종료"):
            ok, msg = stop_adb_device(settings, stop_udid)
            set_feedback("success" if ok else "error", msg)
            st.rerun()

    with right:
        st.subheader("Appium Health")
        health_rows = []
        for slot in repo.list_device_slots():
            raw_url = str(slot.get("appium_url") or settings.appium_server_url)
            worker_url = resolve_appium_url(settings, raw_url)
            ok, msg = check_appium_status(worker_url)
            health_rows.append({"slot": slot.get("slot_name"), "url": raw_url, "worker_url": worker_url, "ok": ok, "message": msg})
        st.dataframe(pd.DataFrame(health_rows), use_container_width=True, hide_index=True)

        st.subheader("Proxy 세션 관리")
        with st.form("proxy_form"):
            proxy_name = st.text_input("프록시 이름", value="proxy-a")
            proxy_url = st.text_input("프록시 URL", value="", placeholder="http://user:pass@host:port")
            assigned_slot = st.selectbox("할당 슬롯", [""] + slot_names)
            if st.form_submit_button("Proxy 저장", use_container_width=True):
                row_id = repo.upsert_proxy_profile(proxy_name, proxy_url, assigned_slot=assigned_slot)
                if assigned_slot:
                    slot = repo.get_device_slot(assigned_slot) or {}
                    repo.upsert_device_slot(assigned_slot, proxy_name=proxy_name, udid=str(slot.get("udid") or ""), status=str(slot.get("status") or "idle"), notes=str(slot.get("notes") or ""))
                set_feedback("success", f"Proxy 저장 완료: #{row_id}")
                st.rerun()
        proxies = repo.list_proxy_profiles()
        if proxies:
            st.dataframe(pd.DataFrame(proxies), use_container_width=True, hide_index=True)
        else:
            st.caption("등록된 프록시가 없습니다.")



def render_records() -> None:
    st.title("조회 기록")
    show_feedback()
    stats = repo.stats()
    st.caption(f"현재 DB: {repo.backend_name} · 전체 저장 공고 {stats['total_jobs']:,}건 · 오늘 {stats['today_jobs']:,}건")
    f1, f2, f3, f4, f5 = st.columns([2, 1, 1, 1, 1])
    keyword = f1.text_input("검색", placeholder="키워드, 공고 제목, 회사명")
    slot = f2.selectbox("슬롯", [""] + slot_options())
    limit = f3.number_input("표시 건수", min_value=10, max_value=5000, value=300, step=10)
    active_only = f4.checkbox("활성 공고만", value=False)
    refresh = f5.button("새로고침", use_container_width=True)
    if refresh:
        st.cache_data.clear()
        st.rerun()

    rows, source = _results_rows(limit=int(limit), keyword=keyword, slot=slot, active_only=active_only)
    st.caption(f"조회 소스: {'FastAPI' if source == 'api' else '직접 DB'} · 반환 {len(rows):,}건")
    df = dataframe_rows(rows)
    if not df.empty:
        preferred = [
            "id", "last_seen_at", "emulator_slot", "profile_name", "title", "company", "location",
            "experience", "education", "salary", "deadline", "detail_url", "tech_keywords",
        ]
        columns = [c for c in preferred if c in df.columns]
        st.dataframe(df[columns], use_container_width=True, hide_index=True)
        with st.expander("원본 컬럼 전체 보기", expanded=False):
            st.dataframe(df, use_container_width=True, hide_index=True)
    else:
        if stats["total_jobs"]:
            st.warning("DB에는 수집 데이터가 있지만 현재 필터 조건에 맞는 행이 없습니다. 검색어/슬롯/활성 공고 필터를 해제해 보세요.")
        else:
            st.info("조회 기록이 없습니다. Appium이 상세 페이지를 읽는 즉시 이 화면에 저장 데이터가 표시됩니다.")

    with st.expander("최근 Worker Job 상태", expanded=False):
        jobs = repo.list_worker_jobs(limit=50)
        if jobs:
            job_df = pd.DataFrame(jobs)
            cols = [c for c in ["id", "status", "job_type", "slot_name", "profile_name", "attempts", "progress_percent", "progress_message", "error_message"] if c in job_df.columns]
            st.dataframe(job_df[cols], use_container_width=True, hide_index=True)
        else:
            st.caption("Worker Job 이력이 없습니다.")

def render_alerts() -> None:
    st.title("알림 센터")
    show_feedback()
    with st.form("add_rule_form"):
        c1, c2 = st.columns(2)
        name = c1.text_input("규칙명", value="Python 자동화")
        channel = c2.selectbox("알림 채널", ["console", "telegram", "discord"])
        keywords = st.text_input("포함 키워드", value="Python,Playwright,Appium,자동화")
        exclude_keywords = st.text_input("제외 키워드", value="영업,마케팅")
        c3, c4, c5 = st.columns(3)
        locations = c3.text_input("지역", value="서울,경기")
        job_categories = c4.text_input("직무", value="자동화,QA,백엔드")
        education = c5.text_input("학력", value="")
        experience = st.text_input("경력", value="")
        submitted = st.form_submit_button("알림 규칙 추가", use_container_width=True)
        if submitted:
            rule = AlertRule(
                name=name,
                keywords=split_csv(keywords),
                exclude_keywords=split_csv(exclude_keywords),
                locations=split_csv(locations),
                job_categories=split_csv(job_categories),
                education=split_csv(education),
                experience=split_csv(experience),
                notification_channel=channel,
            )
            rule_id = repo.add_rule(rule)
            set_feedback("success", f"규칙 추가 완료: #{rule_id}")
            st.rerun()

    c1, _ = st.columns([1, 3])
    if c1.button("알림 평가 실행", type="primary", use_container_width=True):
        events = AlertService(repo, settings).evaluate_recent_jobs(limit=300)
        set_feedback("success", f"생성된 알림 이벤트: {len(events)}건")
        st.rerun()

    rules = repo.list_rules()
    if rules:
        st.subheader("규칙")
        st.dataframe(pd.DataFrame([rule.to_dict() for rule in rules]), use_container_width=True, hide_index=True)

    events = repo.list_alert_events(limit=200)
    if events:
        st.subheader("알림 이력")
        st.dataframe(pd.DataFrame(events), use_container_width=True, hide_index=True)
    else:
        st.info("알림 이력이 없습니다.")


def render_reports() -> None:
    st.title("리포트 / 내보내기")
    show_feedback()
    keyword = st.text_input("내보내기 검색어", value="")
    limit = st.number_input("내보내기 건수", 10, 10000, 1000, step=100)
    c1, c2 = st.columns(2)
    if c1.button("JSON / CSV 생성", type="primary", use_container_width=True):
        rows = repo.list_jobs(limit=int(limit), keyword=keyword)
        json_path = settings.output_dir / "jobs_export.json"
        csv_path = settings.output_dir / "jobs_export.csv"
        export_json(rows, json_path)
        export_csv(rows, csv_path)
        set_feedback("success", f"내보내기 완료: {json_path}, {csv_path}")
        st.rerun()
    if c2.button("CLI export 실행", use_container_width=True):
        code, stdout, stderr = run_cli(["export", "--keyword", keyword, "--limit", str(limit)])
        st.code(stdout if code == 0 else stderr)

    st.subheader("최근 수집 실행")
    runs = repo.list_runs(limit=100)
    if runs:
        st.dataframe(pd.DataFrame(runs), use_container_width=True, hide_index=True)
    else:
        st.info("CLI 단일 크롤러 실행 이력이 없습니다.")


def render_settings() -> None:
    st.title("설정 / 진단")
    show_feedback()
    st.subheader("현재 환경")
    st.json(
        {
            "target_url": settings.target_url,
            "headless": settings.headless,
            "max_items": settings.max_items,
            "scroll_times": settings.scroll_times,
            "output_dir": str(settings.output_dir),
            "database_url": str(settings.database_url),
            "database_backend": repo.backend_name,
            "emulator_slots": settings.emulator_slots,
            "appium_server_url": settings.appium_server_url,
            "appium_connect_host": settings.appium_connect_host,
            "redis_url": settings.redis_url,
            "api_enabled": settings.api_enabled,
            "api_url": settings.api_url,
            "api_status": api_available_label(),
            "docker_mode": settings.docker_mode,
        }
    )

    st.subheader("Docker / Postgres / Redis 준비 상태")
    try:
        db_info = repo.check_connection()
        st.success(f"DB OK · backend={db_info['backend']} · {db_info['database_url']}")
    except Exception as exc:
        st.error(f"DB 연결 실패: {exc}")

    redis_health = check_redis(settings.redis_url)
    if redis_health.ok:
        st.success(f"Redis OK · {redis_health.url} · {redis_health.message}")
    else:
        st.warning(f"Redis 확인 필요 · {redis_health.url} · {redis_health.message}")
    st.caption("Docker Compose 기본 DB는 Postgres입니다. SQLite는 로컬 백업/마이그레이션 소스로 사용할 수 있습니다.")

    st.subheader("운영 명령")
    commands = repo.list_operation_commands(limit=200)
    if commands:
        st.dataframe(pd.DataFrame(commands), use_container_width=True, hide_index=True)
    else:
        st.info("운영 명령이 없습니다.")

    st.subheader("실시간 로그")
    log_text = read_log_tail(settings.output_dir / "logs" / "crawler.log")
    st.code(log_text, language="text")

    st.subheader("감사 로그")
    audits = repo.list_audit_logs(limit=200)
    if audits:
        st.dataframe(pd.DataFrame(audits), use_container_width=True, hide_index=True)
    else:
        st.info("감사 로그가 없습니다.")



def render_api_server() -> None:
    st.title("API 서버")
    st.caption("GUI 제어 동작은 FastAPI Control Plane을 우선 호출하고, 연결 실패 시 로컬 DB/Redis 방식으로 전환합니다.")
    show_feedback()

    client = JobRadarApiClient(settings.api_url, timeout=6.0)
    health = client.health()
    c1, c2, c3 = st.columns(3)
    c1.metric("API 모드", "ON" if settings.api_enabled else "OFF")
    c2.metric("API URL", settings.api_url)
    c3.metric("상태", "OK" if health.ok else "FAIL")

    if health.ok:
        st.success("FastAPI Control Plane 연결 성공")
        st.json(health.data)
    else:
        st.error(f"API 연결 실패: {health.error}")
        st.caption("Docker Compose에서 jobradar-api 서비스가 떠 있는지 확인하세요.")

    st.subheader("API 헬스 체크")
    checks = {
        "DB": client.get("/api/db/check"),
        "Redis": client.get("/api/redis/check"),
        "Appium": client.get("/api/appium/health"),
        "Redis Queue": client.get("/api/redis/queue"),
    }
    for name, result in checks.items():
        with st.expander(f"{name} · {'OK' if result.ok else 'FAIL'}", expanded=name in {"DB", "Redis"}):
            if result.ok:
                st.json(result.data)
            else:
                st.error(result.error)

    st.subheader("API 기반 제어")
    slots_resp = client.get("/api/slots")
    slot_names = [str(row.get("slot_name")) for row in slots_resp.data.get("slots", [])] if slots_resp.ok else slot_options()
    selected = st.multiselect("대상 슬롯", slot_names, default=slot_names[:1])
    a, b, c, d = st.columns(4)
    if a.button("API 큐 등록", type="primary", use_container_width=True):
        result = client.queue_jobs(mode="appium", slot_names=selected or None, slot_count=settings.emulator_slots)
        set_feedback("success" if result.ok else "error", f"API 큐 등록: {result.data if result.ok else result.error}")
        st.rerun()
    if b.button("API 중지", use_container_width=True):
        result = client.cancel_jobs(mode="appium", slot_names=selected or None)
        set_feedback("success" if result.ok else "error", f"API 중지: {result.data if result.ok else result.error}")
        st.rerun()
    if c.button("API stale reset", use_container_width=True):
        result = client.reset_jobs(mode="appium", slot_names=selected or None)
        set_feedback("success" if result.ok else "error", f"API reset: {result.data if result.ok else result.error}")
        st.rerun()
    if d.button("API Worker once", use_container_width=True):
        result = client.run_worker_once(mode="appium", slot_names=selected or None, max_jobs=max(1, len(selected)))
        set_feedback("success" if result.ok else "error", f"API Worker: {result.data if result.ok else result.error}")
        st.rerun()

    st.subheader("OpenAPI")
    st.code(f"{settings.api_url.rstrip('/')}/docs", language="text")


inject_css()
page = render_sidebar()
if page == "대시보드":
    render_dashboard()
elif page == "캠페인/프로필":
    render_campaigns()
elif page == "5-슬롯 제어":
    render_emulators()
elif page == "실무 운영":
    render_pro_operations()
elif page == "API 서버":
    render_api_server()
elif page == "조회 기록":
    render_records()
elif page == "스크린샷":
    render_screenshots()
elif page == "알림":
    render_alerts()
elif page == "리포트/내보내기":
    render_reports()
else:
    render_settings()
