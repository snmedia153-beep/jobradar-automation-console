from __future__ import annotations

import json
from typing import Any

import requests

from jobradar.config import Settings
from jobradar.db.repository import JobRadarRepository
from jobradar.models import AlertRule


def _contains_any(text: str, words: list[str]) -> bool:
    if not words:
        return True
    lowered = text.lower()
    return any(word.strip().lower() in lowered for word in words if word.strip())


def _contains_none(text: str, words: list[str]) -> bool:
    lowered = text.lower()
    return not any(word.strip().lower() in lowered for word in words if word.strip())


def _field_matches(value: str, filters: list[str]) -> bool:
    if not filters:
        return True
    lowered = (value or "").lower()
    return any(item.strip().lower() in lowered for item in filters if item.strip())


def build_match_text(job: dict[str, Any]) -> str:
    parts = [
        job.get("title", ""),
        job.get("company", ""),
        job.get("location", ""),
        job.get("job_category", ""),
        job.get("experience", ""),
        job.get("education", ""),
        job.get("salary", ""),
        job.get("description_text", ""),
        job.get("tech_keywords", ""),
    ]
    return "\n".join(str(part) for part in parts if part)


def job_matches_rule(job: dict[str, Any], rule: AlertRule) -> bool:
    text = build_match_text(job)
    if not _contains_any(text, rule.keywords):
        return False
    if not _contains_none(text, rule.exclude_keywords):
        return False
    if not _field_matches(job.get("location", ""), rule.locations):
        return False
    if not _field_matches(job.get("job_category", ""), rule.job_categories):
        return False
    if not _field_matches(job.get("education", ""), rule.education):
        return False
    if not _field_matches(job.get("experience", ""), rule.experience):
        return False
    return True


def format_alert_message(rule: AlertRule, job: dict[str, Any]) -> str:
    tech_keywords = job.get("tech_keywords", "")
    try:
        parsed = json.loads(tech_keywords) if isinstance(tech_keywords, str) else tech_keywords
        if isinstance(parsed, list):
            tech_keywords = ", ".join(parsed[:8])
    except Exception:
        pass

    return (
        f"[JobRadar] {rule.name}\n"
        f"공고: {job.get('title', '')}\n"
        f"회사: {job.get('company', '')}\n"
        f"지역: {job.get('location', '')}\n"
        f"경력: {job.get('experience', '')}\n"
        f"학력: {job.get('education', '')}\n"
        f"연봉: {job.get('salary', '')}\n"
        f"기술: {tech_keywords}\n"
        f"URL: {job.get('detail_url', '')}"
    )


# 알림 규칙에 맞는 채용공고를 찾아 텔레그램 또는 디스코드로 전송합니다.
class AlertService:
    # 객체가 만들어질 때 필요한 초기값과 의존성을 준비합니다.
    def __init__(self, repo: JobRadarRepository, settings: Settings):
        self.repo = repo
        self.settings = settings

    def evaluate_recent_jobs(self, limit: int = 200) -> list[dict[str, Any]]:
        rules = self.repo.list_rules(enabled_only=True)
        jobs = self.repo.list_jobs(limit=limit)
        created_events = []

        for rule in rules:
            if rule.id is None:
                continue
            for job in jobs:
                if not job_matches_rule(job, rule):
                    continue
                message = format_alert_message(rule, job)
                created = self.repo.create_alert_event(
                    rule_id=rule.id,
                    job_posting_id=int(job["id"]),
                    channel=rule.notification_channel,
                    message=message,
                )
                if not created:
                    continue
                self.send(rule.notification_channel, message)
                created_events.append({"rule": rule.name, "job": job.get("title", ""), "message": message})
        return created_events

    def send(self, channel: str, message: str) -> None:
        channel = (channel or "console").lower()
        if channel == "telegram":
            self._send_telegram(message)
        elif channel == "discord":
            self._send_discord(message)
        else:
            print(message)

    def _send_telegram(self, message: str) -> None:
        if not self.settings.telegram_bot_token or not self.settings.telegram_chat_id:
            print("[telegram-not-configured]")
            print(message)
            return
        url = f"https://api.telegram.org/bot{self.settings.telegram_bot_token}/sendMessage"
        requests.post(url, json={"chat_id": self.settings.telegram_chat_id, "text": message}, timeout=10)

    def _send_discord(self, message: str) -> None:
        if not self.settings.discord_webhook_url:
            print("[discord-not-configured]")
            print(message)
            return
        requests.post(self.settings.discord_webhook_url, json={"content": message}, timeout=10)
