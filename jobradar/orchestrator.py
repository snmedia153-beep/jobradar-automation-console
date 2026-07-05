from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from jobradar.appium_agent.adb_manager import AndroidDevice, list_adb_devices, take_adb_screenshot
from jobradar.config import Settings
from jobradar.crawler.saramin_crawler import SaraminCrawler
from jobradar.db.repository import JobRadarRepository
from jobradar.models import EmulatorSession, JobPosting, SearchProfile


def build_search_url(base_url: str, keyword: str) -> str:
    """Keep the user's scoped Saramin URL and add a searchword parameter for profile-level runs."""
    if not keyword:
        return base_url
    parsed = urlparse(base_url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query.setdefault("searchword", keyword)
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, urlencode(query, doseq=True), parsed.fragment))


def slot_names(slot_count: int = 4) -> list[str]:
    return [f"Emulator {chr(65 + i)}" for i in range(slot_count)]


def map_devices_to_slots(slot_count: int = 4) -> dict[str, AndroidDevice | None]:
    devices = [device for device in list_adb_devices() if device.status == "device"]
    mapping: dict[str, AndroidDevice | None] = {}
    for index, slot in enumerate(slot_names(slot_count)):
        mapping[slot] = devices[index] if index < len(devices) else None
    return mapping


def _annotate_jobs(
    jobs: list[JobPosting],
    session_id: int,
    slot_name: str,
    profile: SearchProfile,
) -> list[JobPosting]:
    for job in jobs:
        job.campaign_name = profile.campaign_name
        job.profile_name = profile.name
        job.emulator_slot = slot_name
        job.collected_session_id = session_id
        job.source_site = "saramin"
    return jobs


def run_profile_on_slot(
    settings: Settings,
    db_path: Path,
    profile: SearchProfile,
    slot_name: str,
    device: AndroidDevice | None,
    force_headless: bool = True,
) -> dict[str, object]:
    repo = JobRadarRepository(db_path)
    repo.init_db()

    target_url = build_search_url(profile.target_url, profile.keyword)
    session = EmulatorSession(
        slot_name=slot_name,
        device_id=device.device_id if device else "logical-slot",
        campaign_name=profile.campaign_name,
        profile_name=profile.name,
        keyword=profile.keyword,
        target_url=target_url,
        status="running",
        health_percent=100 if device else 95,
    )
    session_id = repo.create_emulator_session(session)

    output_dir = settings.output_dir / "sessions" / f"session_{session_id}_{slot_name.replace(' ', '_')}"
    crawl_settings = replace(
        settings,
        target_url=target_url,
        headless=True if force_headless else settings.headless,
        max_items=profile.max_items,
        scroll_times=profile.scroll_times,
        output_dir=output_dir,
    )

    screenshot_path = ""
    try:
        if device:
            screenshot_target = settings.output_dir / "screenshots" / f"{slot_name.replace(' ', '_')}_{session_id}.png"
            ok, message = take_adb_screenshot(device.device_id, str(screenshot_target))
            if ok:
                screenshot_path = str(screenshot_target)
            else:
                repo.log_audit("system", "screenshot", "emulator", slot_name, message, level="WARN")

        crawler = SaraminCrawler(crawl_settings)
        jobs = crawler.crawl()
        _annotate_jobs(jobs, session_id, slot_name, profile)
        stats = repo.insert_jobs(jobs)
        repo.finish_emulator_session(
            session_id,
            status="success",
            found_count=len(jobs),
            new_count=stats["new"],
            updated_count=stats["updated"],
            unchanged_count=stats["unchanged"],
            screenshot_path=screenshot_path,
            health_percent=98 if device else 95,
        )
        return {
            "session_id": session_id,
            "slot_name": slot_name,
            "profile_name": profile.name,
            "keyword": profile.keyword,
            "status": "success",
            "found_count": len(jobs),
            **stats,
        }
    except Exception as exc:
        repo.finish_emulator_session(
            session_id,
            status="failed",
            error_message=str(exc),
            screenshot_path=screenshot_path,
            health_percent=60,
        )
        return {
            "session_id": session_id,
            "slot_name": slot_name,
            "profile_name": profile.name,
            "keyword": profile.keyword,
            "status": "failed",
            "found_count": 0,
            "new": 0,
            "updated": 0,
            "unchanged": 0,
            "error_message": str(exc),
        }


def run_multi_emulator_collection(
    settings: Settings,
    repo: JobRadarRepository,
    profiles: list[SearchProfile] | None = None,
    concurrency: int = 4,
    force_headless: bool = True,
) -> list[dict[str, object]]:
    repo.init_db()
    if profiles is None:
        profiles = repo.list_search_profiles(enabled_only=True, limit=concurrency)
    profiles = profiles[:concurrency]
    if not profiles:
        repo.seed_default_profiles(settings.target_url)
        profiles = repo.list_search_profiles(enabled_only=True, limit=concurrency)

    slots = slot_names(concurrency)
    devices_by_slot = map_devices_to_slots(concurrency)
    repo.log_audit(
        "system",
        "start",
        "multi_emulator_collection",
        str(concurrency),
        f"{len(profiles)}개 프로필을 {concurrency}개 슬롯으로 수집 시작",
    )

    results: list[dict[str, object]] = []
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = []
        for index, profile in enumerate(profiles):
            slot = slots[index % len(slots)]
            futures.append(
                executor.submit(
                    run_profile_on_slot,
                    settings,
                    settings.database_url,
                    profile,
                    slot,
                    devices_by_slot.get(slot),
                    force_headless,
                )
            )
        for future in as_completed(futures):
            results.append(future.result())

    failures = sum(1 for item in results if item.get("status") == "failed")
    level = "WARN" if failures else "INFO"
    repo.log_audit(
        "system",
        "finish",
        "multi_emulator_collection",
        str(concurrency),
        f"수집 완료: 성공 {len(results) - failures}개, 실패 {failures}개",
        level=level,
    )
    return sorted(results, key=lambda item: str(item.get("slot_name", "")))
