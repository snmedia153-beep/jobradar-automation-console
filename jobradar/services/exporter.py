from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

FIELDS = [
    "id", "source", "job_id", "title", "company", "location", "job_category",
    "experience", "education", "employment_type", "salary", "deadline", "posted_at",
    "detail_url", "tech_keywords", "first_seen_at", "last_seen_at",
]


def export_json(rows: list[dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)


def export_csv(rows: list[dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
