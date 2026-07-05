from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse, urlunparse

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError, sync_playwright

from jobradar.config import Settings
from jobradar.logging_utils import get_logger
from jobradar.models import JobPosting
from jobradar.services.parser import normalize_space, parse_job_detail

# 공고 상세로 볼 수 있는 URL만 허용한다. /job-search/ 전체 허용은 회사소개/문의 페이지까지 섞이므로 제외.
JOB_DETAIL_PATH_HINTS = (
    "/zf_user/jobs/relay/view",
    "/recruit/view",
    "/job-detail/",
    "/jobs/relay/view",
)
JOB_QUERY_KEYS = ("rec_idx", "job_idx", "job_cd", "recruit_id")
BLOCKED_PATH_HINTS = (
    "/company-info",
    "/voc/",
    "/help/",
    "/login",
    "/member/",
    "/customer/",
    "/zf_user/company-info",
)
NOISE_TITLES = {
    "로그인", "회원가입", "스크랩", "공유하기", "상세검색", "닫기", "지원하기", "홈",
    "PC버전", "PC 버전", "신고하기", "이전", "다음", "뒤로", "메뉴",
}


# list 후보 데이터 클래스는 관련 데이터와 기능을 한곳에 묶어 관리합니다.
@dataclass
class ListCandidate:
    title: str
    company: str
    url: str
    raw_text: str


def normalize_url(url: str) -> str:
    parsed = urlparse(url)
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", parsed.query, ""))


def looks_like_job_url(url: str) -> bool:
    if not url:
        return False
    parsed = urlparse(url)
    lowered_path = parsed.path.lower()
    if any(hint in lowered_path for hint in BLOCKED_PATH_HINTS):
        return False
    qs = parse_qs(parsed.query)
    if any(key in qs and qs[key] for key in JOB_QUERY_KEYS):
        return True
    return any(hint in lowered_path for hint in JOB_DETAIL_PATH_HINTS)


# 사람인 모바일 페이지에서 채용공고 목록과 상세 내용을 수집합니다.
class SaraminCrawler:
    # 객체가 만들어질 때 필요한 초기값과 의존성을 준비합니다.
    def __init__(self, settings: Settings):
        self.settings = settings
        self.settings.ensure_dirs()
        self.logger = get_logger("crawler", self.settings.output_dir)

    def crawl(self) -> list[JobPosting]:
        self.logger.info("Starting Saramin crawler")
        self.logger.info("Target URL: %s", self.settings.target_url)
        candidates: list[ListCandidate] = []
        jobs: list[JobPosting] = []

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=self.settings.headless)
            context = browser.new_context(
                viewport={"width": self.settings.viewport_width, "height": self.settings.viewport_height},
                user_agent=self.settings.user_agent,
                locale="ko-KR",
                timezone_id="Asia/Seoul",
            )
            page = context.new_page()
            detail_page = context.new_page()

            try:
                self.safe_goto(page, self.settings.target_url)
                self.close_possible_popups(page)
                self.wait_for_content(page)
                candidates = self.collect_list_candidates(page)
                self.save_debug(page, "list_loaded")
                self.logger.info("List candidates: %d", len(candidates))

                for index, candidate in enumerate(candidates[: self.settings.max_items], start=1):
                    self.logger.info(
                        "Detail %d/%d: %s",
                        index,
                        min(len(candidates), self.settings.max_items),
                        candidate.title,
                    )
                    try:
                        detail_text = self.fetch_detail_text(detail_page, candidate.url)
                        raw_text = detail_text or candidate.raw_text
                        job = parse_job_detail(
                            detail_url=candidate.url,
                            title=candidate.title,
                            company=candidate.company,
                            raw_text=raw_text,
                        )
                        if job.title and job.detail_url and job.title != "NO_TITLE":
                            jobs.append(job)
                    except Exception as exc:
                        self.logger.warning("Detail failed: url=%s error=%s", candidate.url, exc)
                        self.save_debug(detail_page, f"detail_error_{index}")
            except Exception as exc:
                self.logger.exception("Crawler failed: %s", exc)
                self.save_debug(page, "crawler_error")
                raise
            finally:
                context.close()
                browser.close()

        self.logger.info("Parsed job postings: %d", len(jobs))
        return jobs

    def safe_goto(self, page: Page, url: str) -> None:
        last_error: Exception | None = None
        for attempt in range(1, 4):
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=45_000)
                page.wait_for_timeout(self.settings.request_delay_ms)
                return
            except Exception as exc:
                last_error = exc
                self.logger.warning("goto failed attempt=%d url=%s error=%s", attempt, url, exc)
                page.wait_for_timeout(1000 * attempt)
        if last_error:
            raise last_error

    def close_possible_popups(self, page: Page) -> None:
        for text in ["닫기", "확인", "오늘 하루 보지 않기", "나중에", "취소"]:
            try:
                locator = page.get_by_text(text, exact=False).first
                if locator.is_visible(timeout=700):
                    locator.click(timeout=1000)
                    page.wait_for_timeout(300)
            except Exception:
                continue

    def wait_for_content(self, page: Page) -> None:
        selectors = [
            "a[href*='rec_idx']",
            "a[href*='job_idx']",
            "a[href*='job_cd']",
            "a[href*='/zf_user/jobs/relay/view']",
            "li",
        ]
        for selector in selectors:
            try:
                page.locator(selector).first.wait_for(timeout=8000)
                self.logger.info("Found selector: %s", selector)
                return
            except PlaywrightTimeoutError:
                continue
        self.logger.warning("No strong selector found. Generic extraction will be used.")

    def collect_list_candidates(self, page: Page) -> list[ListCandidate]:
        seen: set[str] = set()
        all_candidates: list[ListCandidate] = []
        previous_count = 0

        for scroll_index in range(self.settings.scroll_times + 1):
            raw_candidates = self.extract_candidates_from_dom(page)
            for raw in raw_candidates:
                url = normalize_url(urljoin(self.settings.target_url, raw.get("url", "")))
                title = normalize_space(raw.get("title", ""))
                company = normalize_space(raw.get("company", ""))
                raw_text = normalize_space(raw.get("raw_text", ""))
                if not self.is_valid_candidate(url, title, raw_text):
                    continue
                if url in seen:
                    continue
                seen.add(url)
                all_candidates.append(ListCandidate(title=title, company=company, url=url, raw_text=raw_text))

            self.logger.info("After scroll %d: unique candidates=%d", scroll_index, len(all_candidates))
            if len(all_candidates) >= self.settings.max_items:
                break

            if scroll_index > 0 and len(all_candidates) == previous_count:
                self.logger.info("No new candidates found. Stop scrolling.")
                break
            previous_count = len(all_candidates)

            page.mouse.wheel(0, 2000)
            page.wait_for_timeout(self.settings.request_delay_ms)

        return all_candidates

    def extract_candidates_from_dom(self, page: Page) -> list[dict[str, Any]]:
        return page.evaluate(
            """
            () => {
                const clean = (value) => (value || '').replace(/\\s+/g, ' ').trim();
                const anchors = Array.from(document.querySelectorAll('a[href]'));
                const cardSelectors = [
                    'li', 'article', '[class*=job]', '[class*=recruit]', '[class*=item]',
                    '[class*=list]', '[class*=box]', '[class*=card]'
                ];
                const blocked = ['/company-info', '/voc/', '/help/', '/login', '/member/', '/customer/'];
                const detailPaths = ['/zf_user/jobs/relay/view', '/recruit/view', '/job-detail/', '/jobs/relay/view'];
                const queryKeys = ['rec_idx', 'job_idx', 'job_cd', 'recruit_id'];

                function absoluteUrl(href) {
                    try { return new URL(href, location.href).href; }
                    catch (_) { return href || ''; }
                }

                function looksLikeJobUrl(url) {
                    try {
                        const u = new URL(url, location.href);
                        const path = u.pathname.toLowerCase();
                        if (blocked.some((hint) => path.includes(hint))) return false;
                        if (queryKeys.some((key) => u.searchParams.get(key))) return true;
                        return detailPaths.some((hint) => path.includes(hint));
                    } catch (_) {
                        return false;
                    }
                }

                function closestCard(anchor) {
                    for (const selector of cardSelectors) {
                        const node = anchor.closest(selector);
                        if (node && clean(node.innerText).length >= clean(anchor.innerText).length) {
                            return node;
                        }
                    }
                    return anchor;
                }

                function bestTitle(anchor, card) {
                    const selectors = [
                        '[class*=title]', '[class*=tit]', '[class*=subject]', '[class*=job_tit]',
                        'strong', 'h2', 'h3'
                    ];
                    for (const selector of selectors) {
                        const node = card.querySelector(selector);
                        const text = clean(node && node.innerText);
                        if (text.length >= 3 && text.length <= 120) return text;
                    }
                    const titleAttr = clean(anchor.getAttribute('title'));
                    if (titleAttr.length >= 3) return titleAttr;
                    const anchorText = clean(anchor.innerText);
                    if (anchorText.length >= 3 && anchorText.length <= 120) return anchorText;
                    const lines = clean(card.innerText).split(' ').filter(Boolean);
                    return lines.slice(0, 14).join(' ');
                }

                function bestCompany(card) {
                    const selectors = [
                        '[class*=company]', '[class*=corp]', '[class*=name]', '[class*=기업]',
                        '[class*=comp]'
                    ];
                    for (const selector of selectors) {
                        const node = card.querySelector(selector);
                        const text = clean(node && node.innerText);
                        if (text && text.length <= 60) return text;
                    }
                    return '';
                }

                const results = [];
                for (const anchor of anchors) {
                    const url = absoluteUrl(anchor.getAttribute('href'));
                    if (!looksLikeJobUrl(url)) continue;
                    const card = closestCard(anchor);
                    const rawText = clean(card.innerText || anchor.innerText || '');
                    results.push({ title: bestTitle(anchor, card), company: bestCompany(card), url, raw_text: rawText });
                }
                return results;
            }
            """,
        )

    def is_valid_candidate(self, url: str, title: str, raw_text: str = "") -> bool:
        if not looks_like_job_url(url):
            return False
        if not title:
            return False
        cleaned = normalize_space(title)
        if cleaned in NOISE_TITLES:
            return False
        if len(cleaned) < 3 or len(cleaned) > 140:
            return False
        combined = f"{cleaned} {raw_text}"
        if any(noise in combined for noise in ["신고하기", "PC 버전", "고객센터", "이메일 문의"]):
            return False
        return True

    def fetch_detail_text(self, page: Page, url: str) -> str:
        self.safe_goto(page, url)
        self.close_possible_popups(page)
        page.wait_for_timeout(self.settings.detail_delay_ms)
        return page.evaluate(
            """
            () => {
                const clean = (value) => (value || '').replace(/\\s+/g, ' ').trim();
                const main = document.querySelector('main') || document.querySelector('[role=main]') || document.body;
                return clean(main.innerText || document.body.innerText || '');
            }
            """
        )

    def save_debug(self, page: Page, name: str) -> None:
        try:
            html_path = self.settings.output_dir / f"debug_{name}.html"
            png_path = self.settings.output_dir / f"debug_{name}.png"
            html_path.write_text(page.content(), encoding="utf-8")
            page.screenshot(path=str(png_path), full_page=True)
        except Exception as exc:
            self.logger.warning("Failed to save debug files: %s", exc)
