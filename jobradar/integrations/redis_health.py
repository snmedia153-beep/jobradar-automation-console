from __future__ import annotations

from dataclasses import dataclass


# Redis 상태 클래스는 관련 데이터와 기능을 한곳에 묶어 관리합니다.
@dataclass(frozen=True)
class RedisHealth:
    ok: bool
    message: str
    url: str


def check_redis(url: str, timeout_seconds: float = 1.5) -> RedisHealth:
    """Check Redis connectivity without making Redis mandatory for local runs."""
    safe_url = url or "redis://localhost:6379/0"
    try:
        import redis  # type: ignore
    except ModuleNotFoundError:
        return RedisHealth(False, "Python redis 패키지가 설치되어 있지 않습니다. pip install redis 필요", safe_url)
    try:
        client = redis.Redis.from_url(safe_url, socket_connect_timeout=timeout_seconds, socket_timeout=timeout_seconds)
        pong = client.ping()
        return RedisHealth(bool(pong), "Redis PING OK" if pong else "Redis PING 응답이 비정상입니다.", safe_url)
    except Exception as exc:
        return RedisHealth(False, f"Redis 연결 실패: {exc}", safe_url)
