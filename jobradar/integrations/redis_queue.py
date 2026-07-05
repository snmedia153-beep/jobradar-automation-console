from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from jobradar.config import Settings


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# Redis 작업 큐 상태 클래스는 처리 결과와 상태 정보를 한곳에 담아 전달합니다.
@dataclass(frozen=True)
class RedisQueueStatus:
    ok: bool
    message: str
    queued: int = 0
    processing: int = 0
    events: int = 0
    url: str = ""


# 여러 워커가 나눠 처리할 작업을 Redis 큐로 관리합니다.
class RedisJobQueue:
    """Small Redis-backed job queue with SQLite mirroring.

    Redis is used for fast queueing, worker wake-up, status events, and GUI progress.
    SQLite remains the durable source for search profiles, slots, results, and job history.
    """

    # 객체가 만들어질 때 필요한 초기값과 의존성을 준비합니다.
    def __init__(self, url: str, prefix: str = "jobradar", events_maxlen: int = 500):
        self.url = url or "redis://localhost:6379/0"
        self.prefix = (prefix or "jobradar").strip().strip(":") or "jobradar"
        self.events_maxlen = max(50, int(events_maxlen or 500))
        import redis  # type: ignore

        self.redis = redis.Redis.from_url(self.url, decode_responses=True, socket_connect_timeout=2, socket_timeout=2)

    @classmethod
    def from_settings(cls, settings: Settings) -> "RedisJobQueue":
        return cls(settings.redis_url, settings.redis_key_prefix, settings.redis_events_maxlen)

    @property
    def queue_key(self) -> str:
        return f"{self.prefix}:queue:jobs"

    @property
    def processing_key(self) -> str:
        return f"{self.prefix}:queue:processing"

    @property
    def events_key(self) -> str:
        return f"{self.prefix}:events"

    @property
    def cancel_key(self) -> str:
        return f"{self.prefix}:cancelled"

    def job_key(self, job_id: int | str) -> str:
        return f"{self.prefix}:job:{job_id}"

    def ping(self) -> bool:
        return bool(self.redis.ping())

    def status(self) -> RedisQueueStatus:
        try:
            self.ping()
            queued = 0
            processing = 0
            # LLEN/SCARD alone can be misleading after a worker crash or an
            # interrupted deploy. Count only jobs whose hash status still
            # matches the bucket so the GUI does not show phantom queue items.
            for raw_id in self.redis.lrange(self.queue_key, 0, -1):
                row_status = str(self.redis.hget(self.job_key(raw_id), "status") or "")
                if row_status in {"queued", "retry_wait"}:
                    queued += 1
            for raw_id in self.redis.smembers(self.processing_key):
                row_status = str(self.redis.hget(self.job_key(raw_id), "status") or "")
                if row_status == "running":
                    processing += 1
            return RedisQueueStatus(
                ok=True,
                message="Redis Queue OK",
                queued=queued,
                processing=processing,
                events=int(self.redis.xlen(self.events_key) or 0),
                url=self.url,
            )
        except Exception as exc:
            return RedisQueueStatus(False, f"Redis Queue 연결 실패: {exc}", url=self.url)

    def _payload_text(self, row: dict[str, Any]) -> str:
        payload = row.get("payload")
        if isinstance(payload, str):
            return payload
        return json.dumps(payload or {}, ensure_ascii=False)

    def enqueue_job(self, row: dict[str, Any]) -> None:
        job_id = int(row["id"])
        key = self.job_key(job_id)
        data = {
            "id": str(job_id),
            "job_type": str(row.get("job_type") or ""),
            "slot_name": str(row.get("slot_name") or ""),
            "profile_name": str(row.get("profile_name") or ""),
            "priority": str(row.get("priority") or 100),
            "status": str(row.get("status") or "queued"),
            "attempts": str(row.get("attempts") or 0),
            "max_attempts": str(row.get("max_attempts") or 3),
            "payload": self._payload_text(row),
            "error_message": str(row.get("error_message") or ""),
            "result": str(row.get("result") or "{}"),
            "created_at": str(row.get("created_at") or _now()),
            "heartbeat_at": str(row.get("heartbeat_at") or ""),
            "progress_percent": str(row.get("progress_percent") or 0),
            "progress_message": str(row.get("progress_message") or ""),
            "updated_at": _now(),
        }
        pipe = self.redis.pipeline()
        pipe.hset(key, mapping=data)
        pipe.rpush(self.queue_key, str(job_id))
        pipe.xadd(
            self.events_key,
            {
                "event": "queued",
                "job_id": str(job_id),
                "job_type": data["job_type"],
                "slot_name": data["slot_name"],
                "profile_name": data["profile_name"],
                "message": "Redis 큐 등록",
                "ts": _now(),
            },
            maxlen=self.events_maxlen,
            approximate=True,
        )
        pipe.execute()

    def enqueue_jobs(self, rows: list[dict[str, Any]]) -> int:
        for row in rows:
            self.enqueue_job(row)
        return len(rows)

    def _matches(self, row: dict[str, str], job_types: list[str] | None, slot_names: list[str] | None) -> bool:
        if job_types and row.get("job_type") not in set(job_types):
            return False
        if slot_names and row.get("slot_name") not in set(slot_names):
            return False
        status = str(row.get("status") or "")
        if status not in {"queued", "retry_wait"}:
            return False
        try:
            if int(row.get("attempts") or 0) >= int(row.get("max_attempts") or 3):
                return False
        except ValueError:
            return False
        return True

    def claim_job_ids(self, limit: int = 4, job_types: list[str] | None = None, slot_names: list[str] | None = None) -> list[int]:
        claimed: list[int] = []
        # Scan a bounded number so unrelated job types remain in the queue.
        scans = max(20, int(limit or 1) * 10)
        skipped: list[str] = []
        for _ in range(scans):
            if len(claimed) >= max(1, limit):
                break
            raw_id = self.redis.lpop(self.queue_key)
            if raw_id is None:
                break
            row = self.redis.hgetall(self.job_key(raw_id))
            if not row:
                continue
            if str(raw_id) in self.redis.smembers(self.cancel_key):
                self.finish_job(int(raw_id), "canceled", {}, "사용자 중지 요청")
                continue
            if not self._matches(row, job_types, slot_names):
                skipped.append(str(raw_id))
                continue
            attempts = int(row.get("attempts") or 0) + 1
            pipe = self.redis.pipeline()
            now = _now()
            pipe.hset(self.job_key(raw_id), mapping={"status": "running", "attempts": str(attempts), "started_at": now, "heartbeat_at": now, "progress_percent": "5", "progress_message": "Worker claimed job", "updated_at": now})
            pipe.sadd(self.processing_key, str(raw_id))
            pipe.xadd(
                self.events_key,
                {
                    "event": "claimed",
                    "job_id": str(raw_id),
                    "job_type": row.get("job_type", ""),
                    "slot_name": row.get("slot_name", ""),
                    "profile_name": row.get("profile_name", ""),
                    "message": "Worker가 Redis 작업을 가져감",
                    "ts": _now(),
                },
                maxlen=self.events_maxlen,
                approximate=True,
            )
            pipe.execute()
            claimed.append(int(raw_id))
        # Put non-matching jobs back at the tail in original scan order.
        if skipped:
            self.redis.rpush(self.queue_key, *skipped)
        return claimed

    def heartbeat_job(self, job_id: int, worker_id: str = "", progress_percent: int | None = None, message: str = "") -> None:
        key = self.job_key(job_id)
        row = self.redis.hgetall(key) or {}
        mapping: dict[str, str] = {"heartbeat_at": _now(), "updated_at": _now()}
        if worker_id:
            mapping["worker_id"] = worker_id
        if progress_percent is not None:
            mapping["progress_percent"] = str(max(0, min(100, int(progress_percent))))
        if message:
            mapping["progress_message"] = message
        pipe = self.redis.pipeline()
        pipe.hset(key, mapping=mapping)
        if message:
            pipe.xadd(
                self.events_key,
                {
                    "event": "heartbeat",
                    "job_id": str(job_id),
                    "job_type": row.get("job_type", ""),
                    "slot_name": row.get("slot_name", ""),
                    "profile_name": row.get("profile_name", ""),
                    "worker_id": worker_id,
                    "progress_percent": str(progress_percent if progress_percent is not None else row.get("progress_percent", "0")),
                    "message": message,
                    "ts": _now(),
                },
                maxlen=self.events_maxlen,
                approximate=True,
            )
        pipe.execute()

    def retry_job(self, job_id: int, error_message: str = "자동 재시도 대기", delay_seconds: int = 0) -> None:
        key = self.job_key(job_id)
        row = self.redis.hgetall(key) or {}
        pipe = self.redis.pipeline()
        pipe.hset(
            key,
            mapping={
                "status": "retry_wait",
                "error_message": error_message or "",
                "progress_percent": "0",
                "progress_message": error_message or "자동 재시도 대기",
                "retry_after": _now(),
                "heartbeat_at": "",
                "updated_at": _now(),
            },
        )
        pipe.srem(self.processing_key, str(job_id))
        pipe.rpush(self.queue_key, str(job_id))
        pipe.xadd(
            self.events_key,
            {
                "event": "retry_wait",
                "job_id": str(job_id),
                "job_type": row.get("job_type", ""),
                "slot_name": row.get("slot_name", ""),
                "profile_name": row.get("profile_name", ""),
                "message": error_message or "자동 재시도 대기",
                "ts": _now(),
            },
            maxlen=self.events_maxlen,
            approximate=True,
        )
        pipe.execute()

    def recover_stale_processing(self, stale_after_seconds: int = 180) -> int:
        from datetime import datetime, timedelta

        cutoff = datetime.now(timezone.utc) - timedelta(seconds=max(30, int(stale_after_seconds or 180)))
        recovered = 0
        for raw_id in list(self.redis.smembers(self.processing_key)):
            row = self.redis.hgetall(self.job_key(raw_id)) or {}
            raw_heartbeat = row.get("heartbeat_at") or row.get("updated_at") or ""
            is_stale = False
            try:
                parsed = datetime.fromisoformat(raw_heartbeat.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                is_stale = parsed < cutoff
            except Exception:
                is_stale = True
            if not is_stale:
                continue
            pipe = self.redis.pipeline()
            pipe.srem(self.processing_key, str(raw_id))
            pipe.hset(self.job_key(raw_id), mapping={"status": "retry_wait", "error_message": "Redis processing heartbeat timeout", "progress_percent": "0", "progress_message": "stale processing recovered", "updated_at": _now()})
            pipe.rpush(self.queue_key, str(raw_id))
            pipe.xadd(
                self.events_key,
                {
                    "event": "stale_retry",
                    "job_id": str(raw_id),
                    "job_type": row.get("job_type", ""),
                    "slot_name": row.get("slot_name", ""),
                    "profile_name": row.get("profile_name", ""),
                    "message": "Redis processing heartbeat timeout",
                    "ts": _now(),
                },
                maxlen=self.events_maxlen,
                approximate=True,
            )
            pipe.execute()
            recovered += 1
        return recovered

    def finish_job(self, job_id: int, status: str, result: dict[str, Any] | None = None, error_message: str = "") -> None:
        result_text = json.dumps(result or {}, ensure_ascii=False)
        key = self.job_key(job_id)
        row = self.redis.hgetall(key) or {}
        pipe = self.redis.pipeline()
        pipe.hset(key, mapping={"status": status, "result": result_text, "error_message": error_message or "", "finished_at": _now(), "updated_at": _now()})
        pipe.srem(self.processing_key, str(job_id))
        pipe.srem(self.cancel_key, str(job_id))
        pipe.xadd(
            self.events_key,
            {
                "event": status,
                "job_id": str(job_id),
                "job_type": row.get("job_type", ""),
                "slot_name": row.get("slot_name", ""),
                "profile_name": row.get("profile_name", ""),
                "message": error_message or f"작업 {status}",
                "ts": _now(),
            },
            maxlen=self.events_maxlen,
            approximate=True,
        )
        pipe.execute()

    def cancel_jobs(self, job_ids: list[int] | None = None, slot_names: list[str] | None = None, job_types: list[str] | None = None, message: str = "사용자 중지 요청") -> int:
        affected = 0
        explicit_ids = [str(item) for item in (job_ids or [])]
        ids: set[str] = set(explicit_ids)
        # Include queued jobs and known processing jobs so the worker can skip/stop quickly.
        for raw_id in self.redis.lrange(self.queue_key, 0, -1):
            row = self.redis.hgetall(self.job_key(raw_id))
            if row and self._matches(row, job_types, slot_names):
                ids.add(str(raw_id))
        for raw_id in self.redis.smembers(self.processing_key):
            row = self.redis.hgetall(self.job_key(raw_id))
            if row and (not job_types or row.get("job_type") in set(job_types)) and (not slot_names or row.get("slot_name") in set(slot_names)):
                ids.add(str(raw_id))
        for raw_id in ids:
            row = self.redis.hgetall(self.job_key(raw_id)) or {}
            pipe = self.redis.pipeline()
            pipe.sadd(self.cancel_key, raw_id)
            pipe.hset(self.job_key(raw_id), mapping={"status": "canceled", "error_message": message, "updated_at": _now()})
            pipe.xadd(
                self.events_key,
                {
                    "event": "canceled",
                    "job_id": raw_id,
                    "job_type": row.get("job_type", ""),
                    "slot_name": row.get("slot_name", ""),
                    "profile_name": row.get("profile_name", ""),
                    "message": message,
                    "ts": _now(),
                },
                maxlen=self.events_maxlen,
                approximate=True,
            )
            pipe.execute()
            affected += 1
        return affected

    def list_jobs(self, limit: int = 100) -> list[dict[str, Any]]:
        ids: list[str] = []
        ids.extend(str(item) for item in self.redis.smembers(self.processing_key))
        ids.extend(str(item) for item in self.redis.lrange(self.queue_key, 0, max(0, limit - 1)))
        seen: set[str] = set()
        rows: list[dict[str, Any]] = []
        active_statuses = {"queued", "retry_wait", "running"}
        for raw_id in ids:
            if raw_id in seen:
                continue
            seen.add(raw_id)
            data = self.redis.hgetall(self.job_key(raw_id))
            if data and str(data.get("status") or "") in active_statuses:
                rows.append(dict(data))
            if len(rows) >= limit:
                break
        return rows

    def recent_events(self, limit: int = 50) -> list[dict[str, Any]]:
        items = self.redis.xrevrange(self.events_key, count=max(1, limit))
        rows: list[dict[str, Any]] = []
        for event_id, data in items:
            row = {"event_id": event_id}
            row.update(data)
            rows.append(row)
        return rows

    def drain(self) -> int:
        queued = int(self.redis.llen(self.queue_key) or 0)
        self.redis.delete(self.queue_key)
        return queued
