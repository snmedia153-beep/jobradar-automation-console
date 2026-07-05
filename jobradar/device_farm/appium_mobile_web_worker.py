from __future__ import annotations

import time
from dataclasses import replace
from typing import Any
from urllib.parse import urljoin

from appium import webdriver
from appium.options.android import UiAutomator2Options

from jobradar.config import Settings
from jobradar.crawler.saramin_crawler import looks_like_job_url, normalize_url
from jobradar.device_farm.url_utils import resolve_appium_url
from jobradar.db.repository import JobRadarRepository
from jobradar.models import EmulatorSession, JobPosting, SearchProfile
from jobradar.orchestrator import build_search_url
from jobradar.services.parser import normalize_space, parse_job_detail


# Appium 모바일 웹 error 클래스는 해당 작업에서 발생한 오류를 구분하기 위해 사용합니다.
class AppiumMobileWebError(RuntimeError):
    """Raised when the Appium mobile-web worker cannot start or collect."""


def _capability(options: UiAutomator2Options, key: str, value: Any) -> None:
    """Set an Appium capability while keeping the call compatible across client versions."""
    if value is None or value == "":
        return
    try:
        options.set_capability(key, value)
    except Exception:
        # Some client versions expose capabilities as attributes only for common keys.
        setattr(options, key.replace("appium:", ""), value)


def create_mobile_web_driver(settings: Settings, slot: dict[str, Any]) -> webdriver.Remote:
    udid = str(slot.get("udid") or "").strip()
    if not udid:
        console_port = int(slot.get("emulator_console_port") or 0)
        if console_port:
            udid = f"emulator-{console_port}"
    if not udid:
        raise AppiumMobileWebError("슬롯에 UDID가 없습니다. 먼저 에뮬레이터를 실행하고 ADB 장치 동기화를 하세요.")

    appium_url = resolve_appium_url(settings, str(slot.get("appium_url") or settings.appium_server_url))
    browser_name = settings.appium_browser_name or "Chrome"

    options = UiAutomator2Options()
    _capability(options, "platformName", "Android")
    _capability(options, "appium:automationName", "UiAutomator2")
    _capability(options, "appium:udid", udid)
    _capability(options, "browserName", browser_name)
    _capability(options, "appium:deviceName", settings.android_device_name)
    _capability(options, "appium:newCommandTimeout", settings.appium_new_command_timeout)
    _capability(options, "appium:autoGrantPermissions", True)
    _capability(options, "appium:autoAcceptAlerts", settings.appium_auto_accept_alerts)

    system_port = int(slot.get("system_port") or 0)
    mjpeg_port = int(slot.get("mjpeg_server_port") or 0)
    chromedriver_port = int(slot.get("chromedriver_port") or 0)
    if system_port:
        _capability(options, "appium:systemPort", system_port)
    if mjpeg_port:
        _capability(options, "appium:mjpegServerPort", mjpeg_port)
    if chromedriver_port:
        _capability(options, "appium:chromedriverPort", chromedriver_port)
    if settings.appium_chromedriver_autodownload:
        _capability(options, "appium:chromedriverAutodownload", True)

    # A small pageLoad timeout keeps broken mobile-web sessions from hanging the worker indefinitely.
    driver = webdriver.Remote(appium_url, options=options)
    driver.set_page_load_timeout(settings.appium_page_load_timeout)
    driver.implicitly_wait(1)
    return driver


def _execute_collect_script(driver: webdriver.Remote) -> list[dict[str, str]]:
    script = r"""
        const clean = (value) => (value || '').replace(/\s+/g, ' ').trim();
        const anchors = Array.from(document.querySelectorAll('a[href]'));
        const cardSelectors = [
            'li', 'article', '[class*=job]', '[class*=recruit]', '[class*=item]',
            '[class*=list]', '[class*=box]', '[class*=card]'
        ];
        const blocked = ['/company-info', '/voc/', '/help/', '/login', '/member/', '/customer/'];
        const detailPaths = ['/zf_user/jobs/relay/view', '/recruit/view', '/job-detail/', '/jobs/relay/view'];
        const queryKeys = ['rec_idx', 'job_idx', 'job_cd', 'recruit_id'];
        function absoluteUrl(href) {
            try { return new URL(href, location.href).href; } catch (_) { return href || ''; }
        }
        function looksLikeJobUrl(url) {
            try {
                const u = new URL(url, location.href);
                const path = u.pathname.toLowerCase();
                if (blocked.some((hint) => path.includes(hint))) return false;
                if (queryKeys.some((key) => u.searchParams.get(key))) return true;
                return detailPaths.some((hint) => path.includes(hint));
            } catch (_) { return false; }
        }
        function closestCard(anchor) {
            for (const selector of cardSelectors) {
                const node = anchor.closest(selector);
                if (node && clean(node.innerText).length >= clean(anchor.innerText).length) return node;
            }
            return anchor;
        }
        return anchors.map((a) => {
            const url = absoluteUrl(a.getAttribute('href'));
            const card = closestCard(a);
            const title = clean(a.innerText || a.getAttribute('title') || card.innerText.split('\n')[0]);
            const raw = clean(card.innerText || a.innerText || '');
            const lines = raw.split('\n').map(clean).filter(Boolean);
            const company = lines.find((line) => line !== title && line.length <= 50) || '';
            return {url, title, company, raw_text: raw};
        }).filter((item) => looksLikeJobUrl(item.url) && item.title);
    """
    rows = driver.execute_script(script)
    return rows if isinstance(rows, list) else []


def _body_text(driver: webdriver.Remote) -> str:
    try:
        text = driver.execute_script("return document.body ? document.body.innerText : ''")
        return normalize_space(str(text or ""))
    except Exception:
        return normalize_space(getattr(driver, "page_source", "") or "")


def _collect_candidates(driver: webdriver.Remote, target_url: str, max_items: int, scroll_times: int, delay_seconds: float) -> list[dict[str, str]]:
    seen: set[str] = set()
    candidates: list[dict[str, str]] = []
    previous_count = 0
    for scroll_index in range(max(0, scroll_times) + 1):
        for row in _execute_collect_script(driver):
            url = normalize_url(urljoin(target_url, str(row.get("url") or "")))
            title = normalize_space(str(row.get("title") or ""))
            raw_text = normalize_space(str(row.get("raw_text") or ""))
            company = normalize_space(str(row.get("company") or ""))
            if not looks_like_job_url(url) or not title or url in seen:
                continue
            seen.add(url)
            candidates.append({"url": url, "title": title, "company": company, "raw_text": raw_text})
            if len(candidates) >= max_items:
                return candidates
        if scroll_index > 0 and len(candidates) == previous_count:
            break
        previous_count = len(candidates)
        try:
            driver.execute_script("window.scrollBy(0, Math.max(900, Math.floor(window.innerHeight * 1.3)))")
        except Exception:
            pass
        time.sleep(delay_seconds)
    return candidates


def run_appium_mobile_web_profile(
    settings: Settings,
    repo: JobRadarRepository,
    profile: SearchProfile,
    slot: dict[str, Any],
) -> dict[str, Any]:
    slot_name = str(slot.get("slot_name") or "Emulator")
    udid = str(slot.get("udid") or "").strip() or (f"emulator-{slot.get('emulator_console_port')}" if slot.get("emulator_console_port") else "")
    target_url = build_search_url(profile.target_url, profile.keyword)
    session = EmulatorSession(
        slot_name=slot_name,
        device_id=udid or "appium-slot",
        campaign_name=profile.campaign_name,
        profile_name=profile.name,
        keyword=profile.keyword,
        target_url=target_url,
        status="running",
        health_percent=100 if udid else 70,
    )
    session_id = repo.create_emulator_session(session)
    output_dir = settings.output_dir / "sessions" / f"session_{session_id}_{slot_name.replace(' ', '_')}_appium"
    output_dir.mkdir(parents=True, exist_ok=True)

    driver: webdriver.Remote | None = None
    saved_count = 0
    stats = {"new": 0, "updated": 0, "unchanged": 0}
    try:
        driver = create_mobile_web_driver(settings, slot)
        driver.get(target_url)
        time.sleep(max(1, settings.appium_step_delay_ms / 1000))

        candidates = _collect_candidates(
            driver,
            target_url=target_url,
            max_items=profile.max_items,
            scroll_times=profile.scroll_times,
            delay_seconds=max(0.5, settings.appium_step_delay_ms / 1000),
        )
        repo.update_emulator_session_progress(
            session_id,
            found_count=0,
            new_count=0,
            updated_count=0,
            unchanged_count=0,
            health_percent=92,
            message=f"{slot_name}: 후보 {len(candidates)}건 발견",
        )

        for index, candidate in enumerate(candidates[: profile.max_items], start=1):
            try:
                driver.get(candidate["url"])
                time.sleep(max(0.5, settings.appium_step_delay_ms / 1000))
                raw_text = _body_text(driver) or candidate.get("raw_text", "")
                job = parse_job_detail(
                    detail_url=candidate["url"],
                    title=candidate.get("title", ""),
                    company=candidate.get("company", ""),
                    raw_text=raw_text,
                )
                if job.title and job.detail_url and job.title != "NO_TITLE":
                    job.campaign_name = profile.campaign_name
                    job.profile_name = profile.name
                    job.emulator_slot = slot_name
                    job.collected_session_id = session_id
                    job.source_site = "saramin-appium-mobile-web"
                    # Write each posting immediately so the GUI 조회 기록 and
                    # dashboard counters update while the emulator is still crawling.
                    _, status = repo.upsert_job(job)
                    stats[status] = stats.get(status, 0) + 1
                    saved_count += 1
                    repo.update_emulator_session_progress(
                        session_id,
                        found_count=saved_count,
                        new_count=stats.get("new", 0),
                        updated_count=stats.get("updated", 0),
                        unchanged_count=stats.get("unchanged", 0),
                        health_percent=96,
                        message=f"{slot_name}: {saved_count}/{len(candidates)}건 저장 · {job.company or '-'} / {job.title[:60]}",
                    )
            except Exception as detail_exc:
                repo.log_audit("appium-worker", "detail_failed", "job", str(index), f"{slot_name}: {candidate.get('url')} / {detail_exc}", level="WARN")
                continue

        repo.finish_emulator_session(
            session_id,
            status="success",
            found_count=saved_count,
            new_count=stats["new"],
            updated_count=stats["updated"],
            unchanged_count=stats["unchanged"],
            screenshot_path="",
            health_percent=98,
        )
        return {
            "session_id": session_id,
            "slot_name": slot_name,
            "profile_name": profile.name,
            "keyword": profile.keyword,
            "worker_mode": "appium",
            "status": "success",
            "found_count": saved_count,
            **stats,
        }
    except Exception as exc:
        repo.finish_emulator_session(
            session_id,
            status="failed",
            error_message=str(exc),
            screenshot_path="",
            health_percent=50,
        )
        return {
            "session_id": session_id,
            "slot_name": slot_name,
            "profile_name": profile.name,
            "keyword": profile.keyword,
            "worker_mode": "appium",
            "status": "failed",
            "found_count": 0,
            "new": 0,
            "updated": 0,
            "unchanged": 0,
            "error_message": str(exc),
        }
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass
