from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# 수집한 채용공고 한 건의 주요 정보를 담는 데이터 구조입니다.
@dataclass
class JobPosting:
    source: str
    job_id: str
    title: str
    company: str
    detail_url: str
    location: str = ""
    job_category: str = ""
    experience: str = ""
    education: str = ""
    employment_type: str = ""
    salary: str = ""
    deadline: str = ""
    posted_at: str = ""
    description_text: str = ""
    tech_keywords: list[str] = field(default_factory=list)
    raw_text: str = ""
    content_hash: str = ""
    first_seen_at: str = field(default_factory=now_iso)
    last_seen_at: str = field(default_factory=now_iso)
    is_active: int = 1
    source_site: str = "saramin"
    campaign_name: str = ""
    profile_name: str = ""
    emulator_slot: str = ""
    collected_session_id: int | None = None

    # 객체 데이터를 딕셔너리로 바꿔 저장, API 응답, 화면 표시에서 쉽게 사용하게 합니다.
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# 채용공고를 검사해 알림을 보낼 조건을 담는 데이터 구조입니다.
@dataclass
class AlertRule:
    name: str
    enabled: int = 1
    keywords: list[str] = field(default_factory=list)
    exclude_keywords: list[str] = field(default_factory=list)
    locations: list[str] = field(default_factory=list)
    job_categories: list[str] = field(default_factory=list)
    education: list[str] = field(default_factory=list)
    experience: list[str] = field(default_factory=list)
    min_salary: str = ""
    notification_channel: str = "console"
    id: int | None = None

    # 객체 데이터를 딕셔너리로 바꿔 저장, API 응답, 화면 표시에서 쉽게 사용하게 합니다.
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# 어떤 키워드와 조건으로 사람인 공고를 찾을지 담는 검색 프로필입니다.
@dataclass
class SearchProfile:
    name: str
    keyword: str
    target_url: str
    enabled: int = 1
    priority: int = 100
    max_items: int = 20
    scroll_times: int = 3
    campaign_name: str = "기본 캠페인"
    id: int | None = None

    # 객체 데이터를 딕셔너리로 바꿔 저장, API 응답, 화면 표시에서 쉽게 사용하게 합니다.
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# 여러 에뮬레이터 중 하나의 실행 위치와 연결 정보를 담습니다.
@dataclass
class EmulatorSlot:
    slot_name: str
    device_id: str = ""
    model: str = "Android Emulator"
    android_version: str = ""
    status: str = "대기"
    health_percent: int = 100

    # 객체 데이터를 딕셔너리로 바꿔 저장, API 응답, 화면 표시에서 쉽게 사용하게 합니다.
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# 에뮬레이터에서 수행한 수집 작업의 진행 상태를 기록합니다.
@dataclass
class EmulatorSession:
    slot_name: str
    profile_name: str
    keyword: str
    target_url: str
    status: str = "running"
    campaign_name: str = "기본 캠페인"
    device_id: str = ""
    found_count: int = 0
    new_count: int = 0
    updated_count: int = 0
    unchanged_count: int = 0
    error_message: str = ""
    screenshot_path: str = ""
    health_percent: int = 100
    id: int | None = None

    # 객체 데이터를 딕셔너리로 바꿔 저장, API 응답, 화면 표시에서 쉽게 사용하게 합니다.
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
