from __future__ import annotations

import hashlib
import re
from urllib.parse import parse_qs, urlparse

from jobradar.models import JobPosting

TECH_KEYWORDS = [
    "Python", "Java", "JavaScript", "TypeScript", "React", "Vue", "Next.js",
    "Node", "Spring", "Django", "FastAPI", "Flask", "SQL", "MySQL", "PostgreSQL",
    "MongoDB", "Redis", "Docker", "Kubernetes", "AWS", "GCP", "Azure",
    "Linux", "Playwright", "Selenium", "Appium", "OCR", "OpenCV", "QA", "자동화",
    "크롤링", "데이터", "백엔드", "프론트엔드", "풀스택", "DevOps", "CI/CD",
]

LOCATION_WORDS = [
    "서울", "경기", "인천", "부산", "대구", "광주", "대전", "울산", "세종",
    "강원", "충북", "충남", "전북", "전남", "경북", "경남", "제주",
]

EDUCATION_WORDS = ["학력무관", "고졸", "초대졸", "대졸", "대학교", "석사", "박사", "학력"]
EXPERIENCE_WORDS = ["신입", "경력", "무관", "년 이상", "년차"]
EMPLOYMENT_WORDS = ["정규직", "계약직", "인턴", "프리랜서", "파견직", "아르바이트", "위촉직"]
SALARY_WORDS = ["연봉", "월급", "시급", "급여", "면접 후 결정", "회사내규", "만원"]
DEADLINE_WORDS = ["D-", "상시", "채용시", "마감", "접수기간", "오늘마감"]
CATEGORY_WORDS = [
    "백엔드", "프론트엔드", "풀스택", "서버", "QA", "테스트", "자동화", "데이터",
    "DevOps", "인프라", "모바일", "Android", "iOS", "AI", "머신러닝",
]


def normalize_space(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"\s+", " ", value).strip()


def text_lines(value: str) -> list[str]:
    return [normalize_space(line) for line in value.splitlines() if normalize_space(line)]


def extract_job_id(url: str) -> str:
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    for key in ("rec_idx", "job_idx", "job_cd", "recruit_id", "idx"):
        if key in qs and qs[key]:
            return f"{key}:{qs[key][0]}"
    stable = f"{parsed.netloc}{parsed.path}?{parsed.query}"
    return hashlib.sha1(stable.encode("utf-8")).hexdigest()[:16]


def content_hash(*parts: str) -> str:
    raw = "\n".join(parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def find_line(lines: list[str], words: list[str]) -> str:
    for line in lines:
        if any(word.lower() in line.lower() for word in words):
            return line
    return ""


def find_all_keywords(text: str, words: list[str]) -> list[str]:
    lowered = text.lower()
    found = []
    for word in words:
        if word.lower() in lowered:
            found.append(word)
    return sorted(set(found), key=lambda x: x.lower())


def compact_description(raw_text: str, max_len: int = 6000) -> str:
    cleaned = normalize_space(raw_text)
    return cleaned[:max_len]


def parse_job_detail(detail_url: str, title: str, company: str, raw_text: str) -> JobPosting:
    lines = text_lines(raw_text)
    merged = "\n".join(lines)

    tech_keywords = find_all_keywords(merged + " " + title, TECH_KEYWORDS)
    location = find_line(lines, LOCATION_WORDS)
    education = find_line(lines, EDUCATION_WORDS)
    experience = find_line(lines, EXPERIENCE_WORDS)
    employment_type = find_line(lines, EMPLOYMENT_WORDS)
    salary = find_line(lines, SALARY_WORDS)
    deadline = find_line(lines, DEADLINE_WORDS)
    job_category = ", ".join(find_all_keywords(title + " " + merged[:1000], CATEGORY_WORDS))

    if not company:
        company = guess_company_from_lines(lines, title)

    return JobPosting(
        source="saramin",
        job_id=extract_job_id(detail_url),
        title=normalize_space(title) or guess_title_from_lines(lines),
        company=normalize_space(company),
        detail_url=detail_url,
        location=location,
        job_category=job_category,
        experience=experience,
        education=education,
        employment_type=employment_type,
        salary=salary,
        deadline=deadline,
        posted_at="",
        description_text=compact_description(merged),
        tech_keywords=tech_keywords,
        raw_text=merged[:12000],
        content_hash=content_hash(title, company, merged),
    )


def guess_company_from_lines(lines: list[str], title: str) -> str:
    for line in lines[:15]:
        if line and line != title and len(line) <= 40:
            if not any(noise in line for noise in ["로그인", "회원가입", "지원", "스크랩", "공유"]):
                return line
    return ""


def guess_title_from_lines(lines: list[str]) -> str:
    for line in lines[:20]:
        if 4 <= len(line) <= 90:
            if not any(noise in line for noise in ["로그인", "회원가입", "상세검색", "스크랩"]):
                return line
    return "NO_TITLE"
