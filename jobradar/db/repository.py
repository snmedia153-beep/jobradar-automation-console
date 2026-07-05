from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from jobradar.db.postgres_schema import POSTGRES_SCHEMA_SQL
from jobradar.db.schema import SCHEMA_SQL
from jobradar.models import AlertRule, EmulatorSession, JobPosting, SearchProfile, now_iso


# PostgreSQL cursor adapter 클래스는 관련 데이터와 기능을 한곳에 묶어 관리합니다.
class _PostgresCursorAdapter:
    # 객체가 만들어질 때 필요한 초기값과 의존성을 준비합니다.
    def __init__(self, cursor: Any, lastrowid: int | None = None):
        self._cursor = cursor
        self.lastrowid = lastrowid

    @property
    def rowcount(self) -> int:
        return int(getattr(self._cursor, "rowcount", 0) or 0)

    def fetchone(self) -> Any:
        return self._cursor.fetchone()

    def fetchall(self) -> list[Any]:
        return list(self._cursor.fetchall())

    # 결과를 반복문에서 차례로 읽을 수 있게 해 줍니다.
    def __iter__(self):
        return iter(self._cursor)


# PostgreSQL 연결 adapter 클래스는 관련 데이터와 기능을 한곳에 묶어 관리합니다.
class _PostgresConnectionAdapter:
    # 객체가 만들어질 때 필요한 초기값과 의존성을 준비합니다.
    def __init__(self, conn: Any):
        self._conn = conn

    # with 문에 들어갈 때 사용할 연결 객체를 준비합니다.
    def __enter__(self) -> "_PostgresConnectionAdapter":
        return self

    # with 문이 끝날 때 연결 정리와 커밋/롤백 처리를 맡습니다.
    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if exc_type is None:
            self._conn.commit()
        else:
            self._conn.rollback()
        self._conn.close()

    @staticmethod
    def _translate(sql: str) -> str:
        # Project SQL uses sqlite-style positional placeholders. The statements
        # do not contain literal question marks, so this conversion is safe here.
        return sql.replace("?", "%s").replace("CURRENT_TIMESTAMP", "(CURRENT_TIMESTAMP::text)")

    def execute(self, sql: str, params: Iterable[Any] | None = None) -> _PostgresCursorAdapter:
        statement = self._translate(sql)
        cur = self._conn.execute(statement, tuple(params or ()))
        lastrowid: int | None = None
        if statement.lstrip().upper().startswith("INSERT") and " RETURNING " not in statement.upper():
            try:
                row = self._conn.execute("SELECT lastval() AS id").fetchone()
                if row and row.get("id") is not None:
                    lastrowid = int(row["id"])
            except Exception:
                lastrowid = None
        return _PostgresCursorAdapter(cur, lastrowid=lastrowid)

    def executescript(self, script: str) -> None:
        for statement in script.split(";"):
            sql = statement.strip()
            if sql:
                self.execute(sql)


# SQLite 또는 PostgreSQL에 데이터를 저장하고 조회하는 모든 데이터베이스 작업을 모아 둡니다.
class JobRadarRepository:
    # 객체가 만들어질 때 필요한 초기값과 의존성을 준비합니다.
    def __init__(self, db_path: Path | str):
        self.database_url = str(db_path)
        self.is_postgres = self.database_url.startswith(("postgresql://", "postgres://"))
        self.db_path: Path | None = None
        if not self.is_postgres:
            self.db_path = Path(self.database_url)
            self.db_path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def backend_name(self) -> str:
        return "postgres" if self.is_postgres else "sqlite"

    def connect(self):
        if self.is_postgres:
            try:
                import psycopg  # type: ignore
                from psycopg.rows import dict_row  # type: ignore
            except Exception as exc:
                raise RuntimeError("Postgres 사용에는 psycopg[binary] 패키지가 필요합니다. pip install -r requirements.txt 를 다시 실행하세요.") from exc
            conn = psycopg.connect(self.database_url, row_factory=dict_row, connect_timeout=10)
            return _PostgresConnectionAdapter(conn)
        assert self.db_path is not None
        conn = sqlite3.connect(self.db_path, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def init_db(self) -> None:
        with self.connect() as conn:
            conn.executescript(POSTGRES_SCHEMA_SQL if self.is_postgres else SCHEMA_SQL)
            self._migrate_job_postings(conn)
            self._migrate_device_slots(conn)
            self._migrate_worker_jobs(conn)

    def check_connection(self) -> dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute("SELECT 1 AS ok").fetchone()
        return {"backend": self.backend_name, "ok": bool(row and row["ok"] == 1), "database_url": self.database_url}

    def _table_columns(self, conn: Any, table_name: str) -> set[str]:
        if self.is_postgres:
            rows = conn.execute(
                """
                SELECT column_name AS name
                FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = ?
                """,
                (table_name,),
            ).fetchall()
            return {str(row["name"]) for row in rows}
        return {row["name"] for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()}

    def _migrate_job_postings(self, conn: Any) -> None:
        columns = self._table_columns(conn, "job_postings")
        migrations = {
            "source_site": "ALTER TABLE job_postings ADD COLUMN source_site TEXT DEFAULT 'saramin'",
            "campaign_name": "ALTER TABLE job_postings ADD COLUMN campaign_name TEXT DEFAULT ''",
            "profile_name": "ALTER TABLE job_postings ADD COLUMN profile_name TEXT DEFAULT ''",
            "emulator_slot": "ALTER TABLE job_postings ADD COLUMN emulator_slot TEXT DEFAULT ''",
            "collected_session_id": "ALTER TABLE job_postings ADD COLUMN collected_session_id INTEGER",
        }
        for column, sql in migrations.items():
            if column not in columns:
                conn.execute(sql)

    def _migrate_device_slots(self, conn: Any) -> None:
        columns = self._table_columns(conn, "device_slots")
        migrations = {
            "emulator_console_port": "ALTER TABLE device_slots ADD COLUMN emulator_console_port INTEGER DEFAULT 0",
            "emulator_adb_port": "ALTER TABLE device_slots ADD COLUMN emulator_adb_port INTEGER DEFAULT 0",
            "enabled": "ALTER TABLE device_slots ADD COLUMN enabled INTEGER DEFAULT 1",
            "assigned_profile_name": "ALTER TABLE device_slots ADD COLUMN assigned_profile_name TEXT DEFAULT ''",
            "device_type": "ALTER TABLE device_slots ADD COLUMN device_type TEXT DEFAULT 'emulator'",
        }
        for column, sql in migrations.items():
            if column not in columns:
                conn.execute(sql)

    def _migrate_worker_jobs(self, conn: Any) -> None:
        columns = self._table_columns(conn, "worker_jobs")
        migrations = {
            "worker_id": "ALTER TABLE worker_jobs ADD COLUMN worker_id TEXT DEFAULT ''",
            "heartbeat_at": "ALTER TABLE worker_jobs ADD COLUMN heartbeat_at TEXT",
            "progress_percent": "ALTER TABLE worker_jobs ADD COLUMN progress_percent INTEGER DEFAULT 0",
            "progress_message": "ALTER TABLE worker_jobs ADD COLUMN progress_message TEXT DEFAULT ''",
            "retry_after": "ALTER TABLE worker_jobs ADD COLUMN retry_after TEXT",
        }
        for column, sql in migrations.items():
            if column not in columns:
                conn.execute(sql)
        if self.is_postgres:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS worker_events (
                    id SERIAL PRIMARY KEY,
                    worker_job_id INTEGER,
                    event_type TEXT NOT NULL,
                    slot_name TEXT DEFAULT '',
                    profile_name TEXT DEFAULT '',
                    worker_id TEXT DEFAULT '',
                    progress_percent INTEGER DEFAULT 0,
                    message TEXT DEFAULT '',
                    payload TEXT DEFAULT '{}',
                    created_at TEXT DEFAULT (CURRENT_TIMESTAMP::text)
                )
                """
            )
        else:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS worker_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    worker_job_id INTEGER,
                    event_type TEXT NOT NULL,
                    slot_name TEXT DEFAULT '',
                    profile_name TEXT DEFAULT '',
                    worker_id TEXT DEFAULT '',
                    progress_percent INTEGER DEFAULT 0,
                    message TEXT DEFAULT '',
                    payload TEXT DEFAULT '{}',
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_worker_jobs_heartbeat ON worker_jobs(status, heartbeat_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_worker_events_job ON worker_events(worker_job_id, id DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_worker_events_created ON worker_events(created_at)")

    @staticmethod
    def default_slot_name(index: int) -> str:
        if index == 4:
            return "USB Device"
        return f"Emulator {chr(65 + index)}"

    @staticmethod
    def slot_sort_key(slot_name: str) -> tuple[int, str]:
        name = str(slot_name or "")
        if name.startswith("Emulator ") and len(name) >= 10:
            return (0, name)
        if name == "USB Device":
            return (1, name)
        return (2, name)

    @staticmethod
    def _json(value: list[str]) -> str:
        return json.dumps(value or [], ensure_ascii=False)

    @staticmethod
    def _json_list(value: str | None) -> list[str]:
        if not value:
            return []
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return [str(item) for item in parsed]
        except json.JSONDecodeError:
            return []
        return []

    @staticmethod
    def _utc_text(offset_seconds: int = 0) -> str:
        return (datetime.utcnow() + timedelta(seconds=offset_seconds)).strftime("%Y-%m-%d %H:%M:%S")

    def upsert_job(self, job: JobPosting) -> tuple[int, str]:
        """Insert or update a job. Returns (row_id, status) where status is new/updated/unchanged."""
        now = now_iso()
        tech_keywords = self._json(job.tech_keywords)
        with self.connect() as conn:
            existing = conn.execute(
                "SELECT id, content_hash FROM job_postings WHERE source=? AND job_id=?",
                (job.source, job.job_id),
            ).fetchone()
            if existing is None:
                cur = conn.execute(
                    """
                    INSERT INTO job_postings (
                        source, job_id, title, company, location, job_category, experience,
                        education, employment_type, salary, deadline, posted_at, detail_url,
                        description_text, tech_keywords, raw_text, content_hash, first_seen_at,
                        last_seen_at, is_active, source_site, campaign_name, profile_name,
                        emulator_slot, collected_session_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        job.source,
                        job.job_id,
                        job.title,
                        job.company,
                        job.location,
                        job.job_category,
                        job.experience,
                        job.education,
                        job.employment_type,
                        job.salary,
                        job.deadline,
                        job.posted_at,
                        job.detail_url,
                        job.description_text,
                        tech_keywords,
                        job.raw_text,
                        job.content_hash,
                        now,
                        now,
                        1,
                        job.source_site,
                        job.campaign_name,
                        job.profile_name,
                        job.emulator_slot,
                        job.collected_session_id,
                    ),
                )
                return int(cur.lastrowid), "new"

            status = "unchanged"
            if existing["content_hash"] != job.content_hash:
                status = "updated"

            conn.execute(
                """
                UPDATE job_postings SET
                    title=?, company=?, location=?, job_category=?, experience=?, education=?,
                    employment_type=?, salary=?, deadline=?, posted_at=?, detail_url=?,
                    description_text=?, tech_keywords=?, raw_text=?, content_hash=?, last_seen_at=?,
                    is_active=1, source_site=?, campaign_name=?, profile_name=?, emulator_slot=?,
                    collected_session_id=?, updated_at=CURRENT_TIMESTAMP
                WHERE id=?
                """,
                (
                    job.title,
                    job.company,
                    job.location,
                    job.job_category,
                    job.experience,
                    job.education,
                    job.employment_type,
                    job.salary,
                    job.deadline,
                    job.posted_at,
                    job.detail_url,
                    job.description_text,
                    tech_keywords,
                    job.raw_text,
                    job.content_hash,
                    now,
                    job.source_site,
                    job.campaign_name,
                    job.profile_name,
                    job.emulator_slot,
                    job.collected_session_id,
                    existing["id"],
                ),
            )
            return int(existing["id"]), status

    def insert_jobs(self, jobs: Iterable[JobPosting]) -> dict[str, int]:
        stats = {"new": 0, "updated": 0, "unchanged": 0}
        for job in jobs:
            _, status = self.upsert_job(job)
            stats[status] += 1
        return stats

    def list_jobs(self, limit: int = 200, keyword: str = "", emulator_slot: str = "", status: str = "") -> list[dict[str, Any]]:
        query = "SELECT * FROM job_postings WHERE 1=1"
        params: list[Any] = []
        if keyword:
            query += " AND (title LIKE ? OR company LIKE ? OR description_text LIKE ? OR tech_keywords LIKE ? OR profile_name LIKE ?)"
            like = f"%{keyword}%"
            params.extend([like, like, like, like, like])
        if emulator_slot:
            query += " AND emulator_slot=?"
            params.append(emulator_slot)
        if status == "active":
            query += " AND is_active=1"
        query += " ORDER BY last_seen_at DESC, id DESC LIMIT ?"
        params.append(limit)
        with self.connect() as conn:
            rows = conn.execute(query, params).fetchall()
            return [dict(row) for row in rows]

    def get_job_by_id(self, row_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM job_postings WHERE id=?", (row_id,)).fetchone()
            return dict(row) if row else None

    def add_rule(self, rule: AlertRule) -> int:
        with self.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO alert_rules (
                    name, enabled, keywords, exclude_keywords, locations, job_categories,
                    min_salary, education, experience, notification_channel
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    rule.name,
                    rule.enabled,
                    self._json(rule.keywords),
                    self._json(rule.exclude_keywords),
                    self._json(rule.locations),
                    self._json(rule.job_categories),
                    rule.min_salary,
                    self._json(rule.education),
                    self._json(rule.experience),
                    rule.notification_channel,
                ),
            )
            row_id = int(cur.lastrowid)
        self.log_audit("admin", "create", "alert_rule", str(row_id), "알림 규칙 생성")
        return row_id

    def list_rules(self, enabled_only: bool = False) -> list[AlertRule]:
        query = "SELECT * FROM alert_rules"
        if enabled_only:
            query += " WHERE enabled=1"
        query += " ORDER BY id DESC"
        with self.connect() as conn:
            rows = conn.execute(query).fetchall()
        return [self._row_to_rule(row) for row in rows]

    def set_rule_enabled(self, rule_id: int, enabled: int) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE alert_rules SET enabled=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (1 if enabled else 0, rule_id),
            )
        self.log_audit("admin", "update", "alert_rule", str(rule_id), f"알림 규칙 상태 변경: {enabled}")

    def create_alert_event(self, rule_id: int, job_posting_id: int, channel: str, message: str) -> bool:
        try:
            with self.connect() as conn:
                conn.execute(
                    """
                    INSERT INTO alert_events (rule_id, job_posting_id, channel, message, status)
                    VALUES (?, ?, ?, ?, 'created')
                    """,
                    (rule_id, job_posting_id, channel, message),
                )
            return True
        except sqlite3.IntegrityError:
            return False

    def list_alert_events(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT ae.*, ar.name AS rule_name, jp.title, jp.company, jp.detail_url
                FROM alert_events ae
                JOIN alert_rules ar ON ar.id = ae.rule_id
                JOIN job_postings jp ON jp.id = ae.job_posting_id
                ORDER BY ae.created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def upsert_campaign(self, name: str, description: str = "", enabled: int = 1, schedule_note: str = "") -> int:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO campaigns (name, description, enabled, schedule_note)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    description=excluded.description,
                    enabled=excluded.enabled,
                    schedule_note=excluded.schedule_note,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (name, description, enabled, schedule_note),
            )
            row = conn.execute("SELECT id FROM campaigns WHERE name=?", (name,)).fetchone()
            return int(row["id"])

    def list_campaigns(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM campaigns ORDER BY enabled DESC, id DESC").fetchall()
            return [dict(row) for row in rows]

    def upsert_search_profile(self, profile: SearchProfile) -> int:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO search_profiles (
                    campaign_name, name, keyword, target_url, enabled, priority, max_items, scroll_times
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    campaign_name=excluded.campaign_name,
                    keyword=excluded.keyword,
                    target_url=excluded.target_url,
                    enabled=excluded.enabled,
                    priority=excluded.priority,
                    max_items=excluded.max_items,
                    scroll_times=excluded.scroll_times,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (
                    profile.campaign_name,
                    profile.name,
                    profile.keyword,
                    profile.target_url,
                    profile.enabled,
                    profile.priority,
                    profile.max_items,
                    profile.scroll_times,
                ),
            )
            row = conn.execute("SELECT id FROM search_profiles WHERE name=?", (profile.name,)).fetchone()
            return int(row["id"])

    def list_search_profiles(self, enabled_only: bool = False, limit: int = 100) -> list[SearchProfile]:
        query = "SELECT * FROM search_profiles"
        params: list[Any] = []
        if enabled_only:
            query += " WHERE enabled=1"
        query += " ORDER BY priority ASC, id ASC LIMIT ?"
        params.append(limit)
        with self.connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._row_to_profile(row) for row in rows]

    def set_profile_enabled(self, profile_id: int, enabled: int) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE search_profiles SET enabled=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (1 if enabled else 0, profile_id),
            )
        self.log_audit("admin", "update", "search_profile", str(profile_id), f"검색 프로필 상태 변경: {enabled}")

    def seed_default_profiles(self, base_target_url: str) -> int:
        self.upsert_campaign(
            "IT 신입/경력 채용 모니터링",
            "사람인 모바일 채용공고를 4개 에뮬레이터 슬롯으로 나눠 수집합니다.",
            1,
            "매일 09:00 - 18:00",
        )
        templates = [
            ("백엔드 개발자", "백엔드 개발자", 10, 20),
            ("프론트엔드 개발자", "프론트엔드 개발자", 20, 20),
            ("데이터 엔지니어", "데이터 엔지니어", 30, 20),
            ("DevOps 엔지니어", "DevOps 엔지니어", 40, 20),
        ]
        count = 0
        for name, keyword, priority, max_items in templates:
            self.upsert_search_profile(
                SearchProfile(
                    campaign_name="IT 신입/경력 채용 모니터링",
                    name=name,
                    keyword=keyword,
                    target_url=base_target_url,
                    priority=priority,
                    max_items=max_items,
                    scroll_times=3,
                )
            )
            count += 1
        self.log_audit("system", "seed", "search_profiles", "default", "기본 검색 프로필 4개 구성 완료")
        return count

    def create_emulator_session(self, session: EmulatorSession) -> int:
        with self.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO emulator_sessions (
                    slot_name, device_id, campaign_name, profile_name, keyword, target_url,
                    status, found_count, new_count, updated_count, unchanged_count, error_message,
                    screenshot_path, health_percent
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session.slot_name,
                    session.device_id,
                    session.campaign_name,
                    session.profile_name,
                    session.keyword,
                    session.target_url,
                    session.status,
                    session.found_count,
                    session.new_count,
                    session.updated_count,
                    session.unchanged_count,
                    session.error_message,
                    session.screenshot_path,
                    session.health_percent,
                ),
            )
            return int(cur.lastrowid)

    def update_emulator_session_progress(
        self,
        session_id: int,
        found_count: int = 0,
        new_count: int = 0,
        updated_count: int = 0,
        unchanged_count: int = 0,
        health_percent: int = 95,
        message: str = "",
    ) -> None:
        """Update a running Appium session as soon as each posting is parsed.

        Before this method existed, Appium results were written only when the
        whole profile finished. Long mobile-detail crawls therefore looked like
        they were not accumulating data even though Appium logs showed pages
        being parsed.
        """
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE emulator_sessions SET
                    found_count=?, new_count=?, updated_count=?, unchanged_count=?,
                    health_percent=?
                WHERE id=?
                """,
                (found_count, new_count, updated_count, unchanged_count, health_percent, session_id),
            )
        if message:
            self.log_audit("appium-worker", "session_progress", "emulator_session", str(session_id), message)

    def finish_emulator_session(
        self,
        session_id: int,
        status: str,
        found_count: int = 0,
        new_count: int = 0,
        updated_count: int = 0,
        unchanged_count: int = 0,
        error_message: str = "",
        screenshot_path: str = "",
        health_percent: int = 100,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE emulator_sessions SET
                    status=?, found_count=?, new_count=?, updated_count=?, unchanged_count=?,
                    error_message=?, screenshot_path=?, health_percent=?, finished_at=CURRENT_TIMESTAMP
                WHERE id=?
                """,
                (
                    status,
                    found_count,
                    new_count,
                    updated_count,
                    unchanged_count,
                    error_message,
                    screenshot_path,
                    health_percent,
                    session_id,
                ),
            )
        level = "ERROR" if status == "failed" else "INFO"
        self.log_audit("system", "finish", "emulator_session", str(session_id), f"세션 종료: {status}", level=level)

    def list_emulator_sessions(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM emulator_sessions ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(row) for row in rows]

    def recent_slot_summary(self, slots: int = 4) -> list[dict[str, Any]]:
        default = [
            {"slot_name": self.default_slot_name(i), "status": "대기", "keyword": "", "profile_name": "", "found_count": 0, "health_percent": 100, "started_at": "", "finished_at": ""}
            for i in range(slots)
        ]
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT es.*
                FROM emulator_sessions es
                INNER JOIN (
                    SELECT slot_name, MAX(id) AS max_id FROM emulator_sessions GROUP BY slot_name
                ) latest ON latest.max_id = es.id
                ORDER BY es.slot_name ASC
                """
            ).fetchall()
        by_slot = {row["slot_name"]: dict(row) for row in rows}
        for index, item in enumerate(default):
            if item["slot_name"] in by_slot:
                default[index] = by_slot[item["slot_name"]]
        return default

    def log_audit(
        self,
        actor: str,
        action: str,
        entity_type: str = "",
        entity_id: str = "",
        message: str = "",
        level: str = "INFO",
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO audit_logs (actor, action, entity_type, entity_id, level, message)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (actor, action, entity_type, entity_id, level.upper(), message),
            )

    def list_audit_logs(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM audit_logs ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(row) for row in rows]

    def create_operation_command(
        self,
        command: str,
        actor: str = "admin",
        payload: dict[str, Any] | None = None,
        status: str = "queued",
        message: str = "",
    ) -> int:
        payload_text = json.dumps(payload or {}, ensure_ascii=False)
        with self.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO operation_commands (command, status, actor, payload, message)
                VALUES (?, ?, ?, ?, ?)
                """,
                (command, status, actor, payload_text, message),
            )
            command_id = int(cur.lastrowid)
        self.log_audit(actor, "command", "operation_command", str(command_id), message or f"운영 명령 등록: {command}")
        return command_id

    def list_operation_commands(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM operation_commands ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def update_sessions_status(self, from_statuses: Iterable[str], to_status: str, message: str = "") -> int:
        statuses = [str(status) for status in from_statuses]
        if not statuses:
            return 0
        placeholders = ",".join("?" for _ in statuses)
        params: list[Any] = [to_status]
        set_parts = ["status=?"]
        if message:
            set_parts.append("error_message=?")
            params.append(message)
        if to_status in {"reset", "paused", "failed", "success", "completed"}:
            set_parts.append("finished_at=COALESCE(finished_at, CURRENT_TIMESTAMP)")
        params.extend(statuses)
        with self.connect() as conn:
            cur = conn.execute(
                f"""
                UPDATE emulator_sessions
                SET {', '.join(set_parts)}
                WHERE status IN ({placeholders})
                """,
                params,
            )
            return int(cur.rowcount or 0)

    def queue_collection_sessions(self, slot_count: int = 4, actor: str = "admin") -> int:
        profiles = self.list_search_profiles(enabled_only=True, limit=slot_count)
        if not profiles:
            return 0
        current_by_slot = {item["slot_name"]: item for item in self.recent_slot_summary(slots=slot_count)}
        active_statuses = {"queued", "starting", "running", "retry_wait"}
        created = 0
        for index, profile in enumerate(profiles[:slot_count]):
            slot_name = f"Emulator {chr(65 + index)}"
            current = current_by_slot.get(slot_name, {})
            if str(current.get("status", "")) in active_statuses:
                continue
            session_id = self.create_emulator_session(
                EmulatorSession(
                    slot_name=slot_name,
                    device_id="logical-slot",
                    campaign_name=profile.campaign_name,
                    profile_name=profile.name,
                    keyword=profile.keyword,
                    target_url=profile.target_url,
                    status="queued",
                    health_percent=95,
                )
            )
            self.log_audit(actor, "queue", "emulator_session", str(session_id), f"{slot_name} 대기열 등록: {profile.name}")
            created += 1
        return created

    def control_emulator_sessions(self, command: str, actor: str = "admin", slot_count: int = 4) -> dict[str, Any]:
        command = command.strip().lower()
        if command == "start_all":
            created = self.queue_collection_sessions(slot_count=slot_count, actor=actor)
            command_id = self.create_operation_command(
                command,
                actor=actor,
                payload={"slot_count": slot_count, "queued_sessions": created},
                message=f"전체 시작 명령 등록 · 대기열 {created}개 생성",
            )
            return {"command_id": command_id, "affected": created, "message": f"전체 시작 명령을 등록했습니다. 대기열 {created}개가 준비되었습니다."}
        if command == "pause_all":
            affected = self.update_sessions_status(["queued", "starting", "running", "retry_wait"], "paused", "사용자 일시정지 요청")
            command_id = self.create_operation_command(command, actor=actor, payload={"affected": affected}, message=f"일시정지 처리: {affected}개 세션")
            return {"command_id": command_id, "affected": affected, "message": f"{affected}개 세션을 일시정지했습니다."}
        if command == "retry_failed":
            affected = self.update_sessions_status(["failed"], "retry_wait", "사용자 재시도 요청")
            command_id = self.create_operation_command(command, actor=actor, payload={"affected": affected}, message=f"재시도 대기 처리: {affected}개 세션")
            return {"command_id": command_id, "affected": affected, "message": f"실패 세션 {affected}개를 재시도 대기 상태로 변경했습니다."}
        if command == "sync":
            command_id = self.create_operation_command(command, actor=actor, payload={"slot_count": slot_count}, status="completed", message="동기화 상태 갱신 완료")
            return {"command_id": command_id, "affected": 0, "message": "DB/세션 상태를 다시 동기화했습니다."}
        if command == "reset_sessions":
            affected = self.update_sessions_status(["queued", "starting", "running", "paused", "retry_wait", "failed"], "reset", "사용자 세션 초기화")
            command_id = self.create_operation_command(command, actor=actor, payload={"affected": affected}, message=f"세션 초기화: {affected}개")
            return {"command_id": command_id, "affected": affected, "message": f"{affected}개 세션을 초기화했습니다."}
        command_id = self.create_operation_command(command, actor=actor, status="failed", message=f"알 수 없는 명령: {command}")
        return {"command_id": command_id, "affected": 0, "message": f"알 수 없는 명령입니다: {command}"}

    def create_run(self, source: str, target_url: str) -> int:
        with self.connect() as conn:
            cur = conn.execute(
                "INSERT INTO crawler_runs (source, target_url, status) VALUES (?, ?, 'running')",
                (source, target_url),
            )
            return int(cur.lastrowid)

    def finish_run(
        self,
        run_id: int,
        status: str,
        found_count: int = 0,
        new_count: int = 0,
        updated_count: int = 0,
        error_message: str = "",
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE crawler_runs SET status=?, found_count=?, new_count=?, updated_count=?,
                    error_message=?, finished_at=CURRENT_TIMESTAMP
                WHERE id=?
                """,
                (status, found_count, new_count, updated_count, error_message, run_id),
            )

    def list_runs(self, limit: int = 30) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM crawler_runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]

    def stats(self) -> dict[str, Any]:
        with self.connect() as conn:
            total = conn.execute("SELECT COUNT(*) AS c FROM job_postings").fetchone()["c"]
            rules = conn.execute("SELECT COUNT(*) AS c FROM alert_rules WHERE enabled=1").fetchone()["c"]
            alerts = conn.execute("SELECT COUNT(*) AS c FROM alert_events").fetchone()["c"]
            profiles = conn.execute("SELECT COUNT(*) AS c FROM search_profiles WHERE enabled=1").fetchone()["c"]
            active_sessions = conn.execute("SELECT COUNT(*) AS c FROM emulator_sessions WHERE status='running'").fetchone()["c"]
            failed_sessions = conn.execute("SELECT COUNT(*) AS c FROM emulator_sessions WHERE status='failed'").fetchone()["c"]
            if self.is_postgres:
                today_jobs = conn.execute(
                    """
                    SELECT COUNT(*) AS c
                    FROM job_postings
                    WHERE last_seen_at ~ '^\\d{4}-\\d{2}-\\d{2}'
                      AND SUBSTRING(last_seen_at FROM 1 FOR 10)::date = CURRENT_DATE
                    """
                ).fetchone()["c"]
            else:
                today_jobs = conn.execute(
                    "SELECT COUNT(*) AS c FROM job_postings WHERE date(substr(last_seen_at, 1, 10)) = date('now')"
                ).fetchone()["c"]
            by_location = conn.execute(
                """
                SELECT COALESCE(NULLIF(location, ''), '미분류') AS location, COUNT(*) AS c
                FROM job_postings GROUP BY COALESCE(NULLIF(location, ''), '미분류')
                ORDER BY c DESC LIMIT 20
                """
            ).fetchall()
            by_education = conn.execute(
                """
                SELECT COALESCE(NULLIF(education, ''), '미분류') AS education, COUNT(*) AS c
                FROM job_postings GROUP BY COALESCE(NULLIF(education, ''), '미분류')
                ORDER BY c DESC LIMIT 20
                """
            ).fetchall()
            by_slot = conn.execute(
                """
                SELECT COALESCE(NULLIF(emulator_slot, ''), '미지정') AS emulator_slot, COUNT(*) AS c
                FROM job_postings GROUP BY COALESCE(NULLIF(emulator_slot, ''), '미지정')
                ORDER BY c DESC LIMIT 8
                """
            ).fetchall()
        return {
            "total_jobs": total,
            "today_jobs": today_jobs,
            "enabled_rules": rules,
            "alert_events": alerts,
            "enabled_profiles": profiles,
            "active_sessions": active_sessions,
            "failed_sessions": failed_sessions,
            "by_location": [dict(row) for row in by_location],
            "by_education": [dict(row) for row in by_education],
            "by_slot": [dict(row) for row in by_slot],
        }


    def seed_device_slots(
        self,
        slot_count: int = 5,
        appium_host: str = "127.0.0.1",
        appium_base_port: int = 4723,
        appium_port_step: int = 2,
        system_port_base: int = 8201,
        mjpeg_port_base: int = 9201,
        chromedriver_port_base: int = 9515,
        emulator_port_pairs: list[tuple[int, int]] | None = None,
    ) -> int:
        """Create practical slots for 4 emulators plus an optional USB device.

        Slot 1~4 are Emulator A~D. Slot 5 is USB Device and intentionally has no
        emulator console/ADB port; its UDID should be filled by ADB sync or manually.
        """
        created_or_updated = 0
        with self.connect() as conn:
            for index in range(slot_count):
                slot_name = self.default_slot_name(index)
                appium_port = appium_base_port + (index * appium_port_step)
                appium_url = f"http://{appium_host}:{appium_port}"
                device_type = "usb" if slot_name == "USB Device" else "emulator"
                if device_type == "emulator":
                    if emulator_port_pairs and index < len(emulator_port_pairs):
                        console_port, adb_port = emulator_port_pairs[index]
                    else:
                        console_port = 5554 + (index * 2)
                        adb_port = console_port + 1
                else:
                    console_port, adb_port = 0, 0
                conn.execute(
                    """
                    INSERT INTO device_slots (
                        slot_name, device_type, enabled, appium_url, appium_port, system_port,
                        mjpeg_server_port, chromedriver_port, emulator_console_port, emulator_adb_port, status
                    ) VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, ?, 'idle')
                    ON CONFLICT(slot_name) DO UPDATE SET
                        device_type=excluded.device_type,
                        appium_url=excluded.appium_url,
                        appium_port=excluded.appium_port,
                        system_port=excluded.system_port,
                        mjpeg_server_port=excluded.mjpeg_server_port,
                        chromedriver_port=excluded.chromedriver_port,
                        emulator_console_port=excluded.emulator_console_port,
                        emulator_adb_port=excluded.emulator_adb_port,
                        updated_at=CURRENT_TIMESTAMP
                    """,
                    (
                        slot_name,
                        device_type,
                        appium_url,
                        appium_port,
                        system_port_base + index,
                        mjpeg_port_base + index,
                        chromedriver_port_base + index,
                        console_port,
                        adb_port,
                    ),
                )
                created_or_updated += 1
        self.log_audit("system", "seed", "device_slots", "default", f"장치 슬롯 {created_or_updated}개 구성")
        return created_or_updated

    def list_device_slots(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM device_slots
                ORDER BY CASE slot_name
                    WHEN 'Emulator A' THEN 1
                    WHEN 'Emulator B' THEN 2
                    WHEN 'Emulator C' THEN 3
                    WHEN 'Emulator D' THEN 4
                    WHEN 'USB Device' THEN 5
                    ELSE 99 END, slot_name ASC
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def get_device_slot(self, slot_name: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM device_slots WHERE slot_name=?", (slot_name,)).fetchone()
        return dict(row) if row else None

    def upsert_device_slot(
        self,
        slot_name: str,
        avd_name: str = "",
        udid: str = "",
        proxy_name: str = "",
        status: str = "idle",
        notes: str = "",
        appium_url: str = "",
        appium_port: int = 0,
        system_port: int = 0,
        mjpeg_server_port: int = 0,
        chromedriver_port: int = 0,
        emulator_console_port: int = 0,
        emulator_adb_port: int = 0,
        device_type: str = "",
        assigned_profile_name: str = "",
        enabled: int | None = None,
    ) -> None:
        inferred_type = device_type or ("usb" if slot_name == "USB Device" else "emulator")
        enabled_value = 1 if enabled is None else 1 if enabled else 0
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO device_slots (
                    slot_name, avd_name, udid, device_type, enabled, assigned_profile_name,
                    proxy_name, status, notes, appium_url, appium_port, system_port,
                    mjpeg_server_port, chromedriver_port, emulator_console_port, emulator_adb_port,
                    last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(slot_name) DO UPDATE SET
                    avd_name=COALESCE(NULLIF(excluded.avd_name, ''), device_slots.avd_name),
                    udid=COALESCE(NULLIF(excluded.udid, ''), device_slots.udid),
                    device_type=COALESCE(NULLIF(excluded.device_type, ''), device_slots.device_type),
                    enabled=excluded.enabled,
                    assigned_profile_name=excluded.assigned_profile_name,
                    proxy_name=excluded.proxy_name,
                    status=excluded.status,
                    notes=excluded.notes,
                    appium_url=COALESCE(NULLIF(excluded.appium_url, ''), device_slots.appium_url),
                    appium_port=CASE WHEN excluded.appium_port > 0 THEN excluded.appium_port ELSE device_slots.appium_port END,
                    system_port=CASE WHEN excluded.system_port > 0 THEN excluded.system_port ELSE device_slots.system_port END,
                    mjpeg_server_port=CASE WHEN excluded.mjpeg_server_port > 0 THEN excluded.mjpeg_server_port ELSE device_slots.mjpeg_server_port END,
                    chromedriver_port=CASE WHEN excluded.chromedriver_port > 0 THEN excluded.chromedriver_port ELSE device_slots.chromedriver_port END,
                    emulator_console_port=CASE WHEN excluded.emulator_console_port > 0 THEN excluded.emulator_console_port ELSE device_slots.emulator_console_port END,
                    emulator_adb_port=CASE WHEN excluded.emulator_adb_port > 0 THEN excluded.emulator_adb_port ELSE device_slots.emulator_adb_port END,
                    last_seen_at=CURRENT_TIMESTAMP,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (
                    slot_name,
                    avd_name,
                    udid,
                    inferred_type,
                    enabled_value,
                    assigned_profile_name,
                    proxy_name,
                    status,
                    notes,
                    appium_url,
                    appium_port,
                    system_port,
                    mjpeg_server_port,
                    chromedriver_port,
                    emulator_console_port,
                    emulator_adb_port,
                ),
            )
        self.log_audit("admin", "upsert", "device_slot", slot_name, f"슬롯 저장: {slot_name} / {avd_name or udid or '-'}")

    def set_device_slot_profile(self, slot_name: str, profile_name: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE device_slots SET assigned_profile_name=?, updated_at=CURRENT_TIMESTAMP WHERE slot_name=?",
                (profile_name, slot_name),
            )
        self.log_audit("admin", "assign", "device_slot", slot_name, f"슬롯 검색 프로필 지정: {profile_name or '자동'}")

    def set_device_slot_enabled(self, slot_name: str, enabled: int) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE device_slots SET enabled=?, updated_at=CURRENT_TIMESTAMP WHERE slot_name=?",
                (1 if enabled else 0, slot_name),
            )
        self.log_audit("admin", "enable", "device_slot", slot_name, f"슬롯 사용 여부: {1 if enabled else 0}")

    def update_device_slot_runtime(self, slot_name: str, status: str, udid: str = "", notes: str = "") -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE device_slots SET status=?, udid=COALESCE(NULLIF(?, ''), udid),
                    notes=COALESCE(NULLIF(?, ''), notes), last_seen_at=CURRENT_TIMESTAMP,
                    updated_at=CURRENT_TIMESTAMP
                WHERE slot_name=?
                """,
                (status, udid, notes, slot_name),
            )
        self.log_audit("system", "runtime", "device_slot", slot_name, f"{slot_name} 상태: {status} {notes}")

    def upsert_proxy_profile(self, name: str, proxy_url: str, assigned_slot: str = "", enabled: int = 1, status: str = "ready") -> int:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO proxy_profiles (name, proxy_url, assigned_slot, enabled, status)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    proxy_url=excluded.proxy_url,
                    assigned_slot=excluded.assigned_slot,
                    enabled=excluded.enabled,
                    status=excluded.status,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (name, proxy_url, assigned_slot, 1 if enabled else 0, status),
            )
            row = conn.execute("SELECT id FROM proxy_profiles WHERE name=?", (name,)).fetchone()
        row_id = int(row["id"])
        self.log_audit("admin", "upsert", "proxy_profile", str(row_id), f"프록시 프로필 저장: {name}")
        return row_id

    def list_proxy_profiles(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM proxy_profiles ORDER BY enabled DESC, name ASC").fetchall()
        return [dict(row) for row in rows]

    def get_search_profile_by_name(self, name: str) -> SearchProfile | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM search_profiles WHERE name=?", (name,)).fetchone()
        return self._row_to_profile(row) if row else None

    def enqueue_worker_job(
        self,
        job_type: str,
        slot_name: str = "",
        profile_name: str = "",
        priority: int = 100,
        payload: dict[str, Any] | None = None,
        max_attempts: int = 3,
    ) -> int:
        payload_text = json.dumps(payload or {}, ensure_ascii=False)
        with self.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO worker_jobs (job_type, slot_name, profile_name, priority, payload, max_attempts)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (job_type, slot_name, profile_name, priority, payload_text, max_attempts),
            )
            row_id = int(cur.lastrowid)
        self.log_audit("system", "queue", "worker_job", str(row_id), f"작업 큐 등록: {job_type} {slot_name} {profile_name}")
        return row_id

    def queue_collection_jobs_detailed(
        self,
        slot_count: int = 5,
        job_type: str = "collect_profile",
        seed_slots: bool = True,
        emulator_port_pairs: list[tuple[int, int]] | None = None,
        slot_names: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Create collection jobs and return rows that were inserted.

        This detailed variant is used by the Redis queue bridge so SQLite remains
        the durable job history while Redis becomes the real-time dispatch queue.
        """
        profiles = self.list_search_profiles(enabled_only=True, limit=200)
        if not profiles:
            return []
        if seed_slots:
            self.seed_device_slots(slot_count=slot_count, emulator_port_pairs=emulator_port_pairs)
        all_slots = self.list_device_slots()
        requested = {name for name in (slot_names or []) if name}
        slots = [slot for slot in all_slots if not requested or slot["slot_name"] in requested]
        if slot_count and not requested:
            slots = slots[:slot_count]
        profile_by_name = {profile.name: profile for profile in profiles}
        created_rows: list[dict[str, Any]] = []
        with self.connect() as conn:
            for index, slot in enumerate(slots):
                if int(slot.get("enabled") if slot.get("enabled") is not None else 1) != 1:
                    continue
                assigned_profile = str(slot.get("assigned_profile_name") or "").strip()
                profile = profile_by_name.get(assigned_profile) if assigned_profile else None
                if profile is None:
                    profile = profiles[index % len(profiles)]
                slot_name = str(slot.get("slot_name") or self.default_slot_name(index))
                existing = conn.execute(
                    """
                    SELECT id FROM worker_jobs
                    WHERE job_type=? AND slot_name=? AND profile_name=?
                      AND status IN ('queued', 'retry_wait', 'running')
                    LIMIT 1
                    """,
                    (job_type, slot_name, profile.name),
                ).fetchone()
                if existing:
                    continue
                payload = {
                    "profile_name": profile.name,
                    "slot_name": slot_name,
                    "worker_mode": "appium" if job_type.startswith("appium") else "playwright",
                }
                payload_text = json.dumps(payload, ensure_ascii=False)
                cur = conn.execute(
                    """
                    INSERT INTO worker_jobs (job_type, slot_name, profile_name, priority, payload, max_attempts)
                    VALUES (?, ?, ?, ?, ?, 3)
                    """,
                    (job_type, slot_name, profile.name, profile.priority, payload_text),
                )
                created_rows.append(
                    {
                        "id": int(cur.lastrowid),
                        "job_type": job_type,
                        "slot_name": slot_name,
                        "profile_name": profile.name,
                        "priority": profile.priority,
                        "status": "queued",
                        "attempts": 0,
                        "max_attempts": 3,
                        "payload": payload_text,
                        "result": "{}",
                        "error_message": "",
                    }
                )
        target_desc = ",".join(slot_names or []) if slot_names else f"{slot_count} slots"
        self.log_audit("system", "queue", "worker_job", job_type, f"{job_type} 수집 작업 {len(created_rows)}개 큐 등록 · {target_desc}")
        return created_rows

    def queue_collection_jobs(
        self,
        slot_count: int = 5,
        job_type: str = "collect_profile",
        seed_slots: bool = True,
        emulator_port_pairs: list[tuple[int, int]] | None = None,
        slot_names: list[str] | None = None,
    ) -> int:
        return len(
            self.queue_collection_jobs_detailed(
                slot_count=slot_count,
                job_type=job_type,
                seed_slots=seed_slots,
                emulator_port_pairs=emulator_port_pairs,
                slot_names=slot_names,
            )
        )

    def list_worker_jobs(self, limit: int = 100, statuses: list[str] | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM worker_jobs"
        params: list[Any] = []
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            query += f" WHERE status IN ({placeholders})"
            params.extend(statuses)
        query += " ORDER BY CASE status WHEN 'running' THEN 0 WHEN 'queued' THEN 1 WHEN 'failed' THEN 2 ELSE 3 END, priority ASC, id DESC LIMIT ?"
        params.append(limit)
        with self.connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def claim_worker_jobs(
        self,
        limit: int = 4,
        job_types: list[str] | None = None,
        slot_names: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        params: list[Any] = []
        type_filter = ""
        if job_types:
            placeholders = ",".join("?" for _ in job_types)
            type_filter = f" AND job_type IN ({placeholders})"
            params.extend(job_types)
        slot_filter = ""
        if slot_names:
            placeholders = ",".join("?" for _ in slot_names)
            slot_filter = f" AND slot_name IN ({placeholders})"
            params.extend(slot_names)
        params.append(limit)
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM worker_jobs
                WHERE status IN ('queued', 'retry_wait') AND attempts < max_attempts{type_filter}{slot_filter}
                ORDER BY priority ASC, id ASC
                LIMIT ?
                """,
                params,
            ).fetchall()
            claimed: list[dict[str, Any]] = []
            for row in rows:
                conn.execute(
                    """
                    UPDATE worker_jobs SET status='running', attempts=attempts+1, started_at=CURRENT_TIMESTAMP
                    WHERE id=? AND status IN ('queued', 'retry_wait')
                    """,
                    (row["id"],),
                )
                claimed.append(dict(row))
        return claimed

    def get_worker_job(self, job_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM worker_jobs WHERE id=?", (job_id,)).fetchone()
        return dict(row) if row else None

    def mark_worker_job_running(self, job_id: int, worker_id: str = "") -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM worker_jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                return None
            if str(row["status"]) not in {"queued", "retry_wait", "running"}:
                return dict(row)
            if str(row["status"]) != "running":
                conn.execute(
                    """
                    UPDATE worker_jobs
                    SET status='running', attempts=attempts+1, started_at=COALESCE(started_at, CURRENT_TIMESTAMP),
                        worker_id=?, heartbeat_at=CURRENT_TIMESTAMP, progress_percent=5, progress_message='Worker claimed job'
                    WHERE id=? AND status IN ('queued', 'retry_wait')
                    """,
                    (worker_id, job_id),
                )
            else:
                conn.execute(
                    """
                    UPDATE worker_jobs
                    SET worker_id=CASE WHEN ?<>'' THEN ? ELSE worker_id END,
                        heartbeat_at=CURRENT_TIMESTAMP,
                        progress_message=CASE WHEN progress_message='' THEN 'Worker resumed running job' ELSE progress_message END
                    WHERE id=? AND status='running'
                    """,
                    (worker_id, worker_id, job_id),
                )
            row = conn.execute("SELECT * FROM worker_jobs WHERE id=?", (job_id,)).fetchone()
        return dict(row) if row else None

    def is_worker_job_canceled(self, job_id: int) -> bool:
        with self.connect() as conn:
            row = conn.execute("SELECT status FROM worker_jobs WHERE id=?", (job_id,)).fetchone()
        return bool(row and str(row["status"]) == "canceled")

    def cancel_worker_jobs(self, slot_names: list[str] | None = None, job_types: list[str] | None = None, message: str = "사용자 중지 요청") -> int:
        where = ["status IN ('queued', 'retry_wait', 'running')"]
        params: list[Any] = [message]
        if slot_names:
            placeholders = ",".join("?" for _ in slot_names)
            where.append(f"slot_name IN ({placeholders})")
            params.extend(slot_names)
        if job_types:
            placeholders = ",".join("?" for _ in job_types)
            where.append(f"job_type IN ({placeholders})")
            params.extend(job_types)
        sql = f"UPDATE worker_jobs SET status='canceled', error_message=?, finished_at=CURRENT_TIMESTAMP WHERE {' AND '.join(where)}"
        with self.connect() as conn:
            cur = conn.execute(sql, params)
            affected = int(cur.rowcount or 0)
        self.log_audit("admin", "cancel", "worker_job", ",".join(slot_names or ["all"]), f"{message}: {affected}개")
        return affected

    def reset_stale_worker_jobs(self, slot_names: list[str] | None = None, job_types: list[str] | None = None, message: str = "manual reset: stale running job cleared") -> int:
        where = ["status='running'"]
        params: list[Any] = [message]
        if slot_names:
            placeholders = ",".join("?" for _ in slot_names)
            where.append(f"slot_name IN ({placeholders})")
            params.extend(slot_names)
        if job_types:
            placeholders = ",".join("?" for _ in job_types)
            where.append(f"job_type IN ({placeholders})")
            params.extend(job_types)
        sql = f"UPDATE worker_jobs SET status='failed', error_message=?, finished_at=CURRENT_TIMESTAMP WHERE {' AND '.join(where)}"
        with self.connect() as conn:
            cur = conn.execute(sql, params)
            affected = int(cur.rowcount or 0)
        self.log_audit("admin", "reset", "worker_job", ",".join(slot_names or ["all"]), f"{message}: {affected}개")
        return affected

    def add_worker_event(
        self,
        job_id: int | None,
        event_type: str,
        message: str = "",
        slot_name: str = "",
        profile_name: str = "",
        worker_id: str = "",
        progress_percent: int = 0,
        payload: dict[str, Any] | None = None,
    ) -> int:
        payload_text = json.dumps(payload or {}, ensure_ascii=False)
        with self.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO worker_events (worker_job_id, event_type, slot_name, profile_name, worker_id, progress_percent, message, payload)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (job_id, event_type, slot_name, profile_name, worker_id, progress_percent, message, payload_text),
            )
            return int(cur.lastrowid or 0)

    def list_worker_events(self, limit: int = 100, job_id: int | None = None, slot_name: str = "") -> list[dict[str, Any]]:
        query = "SELECT * FROM worker_events WHERE 1=1"
        params: list[Any] = []
        if job_id is not None:
            query += " AND worker_job_id=?"
            params.append(job_id)
        if slot_name:
            query += " AND slot_name=?"
            params.append(slot_name)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        try:
            with self.connect() as conn:
                rows = conn.execute(query, params).fetchall()
        except sqlite3.OperationalError as exc:
            if "worker_events" not in str(exc):
                raise
            self.init_db()
            with self.connect() as conn:
                rows = conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def heartbeat_worker_job(self, job_id: int, worker_id: str, progress_percent: int | None = None, message: str = "") -> None:
        updates = ["heartbeat_at=CURRENT_TIMESTAMP", "worker_id=?"]
        params: list[Any] = [worker_id]
        if progress_percent is not None:
            updates.append("progress_percent=?")
            params.append(max(0, min(100, int(progress_percent))))
        if message:
            updates.append("progress_message=?")
            params.append(message)
        params.append(job_id)
        with self.connect() as conn:
            conn.execute(f"UPDATE worker_jobs SET {', '.join(updates)} WHERE id=? AND status='running'", params)
        if message:
            row = self.get_worker_job(job_id) or {}
            self.add_worker_event(
                job_id,
                "heartbeat",
                message,
                slot_name=str(row.get("slot_name") or ""),
                profile_name=str(row.get("profile_name") or ""),
                worker_id=worker_id,
                progress_percent=int(progress_percent or row.get("progress_percent") or 0),
            )

    def retry_worker_job(self, job_id: int, message: str = "자동 재시도 대기", delay_seconds: int = 0) -> bool:
        retry_after = self._utc_text(delay_seconds)
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM worker_jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                return False
            if int(row["attempts"] or 0) >= int(row["max_attempts"] or 0):
                return False
            conn.execute(
                """
                UPDATE worker_jobs
                SET status='retry_wait', error_message=?, progress_message=?, progress_percent=0,
                    heartbeat_at=NULL, worker_id='', retry_after=?, finished_at=NULL
                WHERE id=?
                """,
                (message, message, retry_after, job_id),
            )
        self.add_worker_event(job_id, "retry_wait", message, slot_name=str(row["slot_name"] or ""), profile_name=str(row["profile_name"] or ""))
        return True

    def retry_failed_worker_jobs(self, slot_names: list[str] | None = None, job_types: list[str] | None = None, message: str = "GUI/API 실패 작업 재시도") -> int:
        where = ["status='failed'", "attempts < max_attempts"]
        params: list[Any] = []
        if slot_names:
            placeholders = ",".join("?" for _ in slot_names)
            where.append(f"slot_name IN ({placeholders})")
            params.extend(slot_names)
        if job_types:
            placeholders = ",".join("?" for _ in job_types)
            where.append(f"job_type IN ({placeholders})")
            params.extend(job_types)
        with self.connect() as conn:
            rows = conn.execute(f"SELECT * FROM worker_jobs WHERE {' AND '.join(where)}", params).fetchall()
            for row in rows:
                conn.execute(
                    """
                    UPDATE worker_jobs
                    SET status='retry_wait', error_message=?, progress_message=?, progress_percent=0,
                        heartbeat_at=NULL, worker_id='', retry_after=CURRENT_TIMESTAMP, finished_at=NULL
                    WHERE id=?
                    """,
                    (message, message, row["id"]),
                )
        for row in rows:
            self.add_worker_event(int(row["id"]), "retry_wait", message, slot_name=str(row["slot_name"] or ""), profile_name=str(row["profile_name"] or ""))
        self.log_audit("admin", "retry", "worker_job", ",".join(slot_names or ["all"]), f"{message}: {len(rows)}개")
        return len(rows)

    def recover_stale_worker_jobs(
        self,
        stale_after_seconds: int = 180,
        slot_names: list[str] | None = None,
        job_types: list[str] | None = None,
        message: str = "stale heartbeat recovered",
        auto_retry: bool = True,
    ) -> dict[str, int]:
        cutoff = self._utc_text(-max(30, int(stale_after_seconds or 180)))
        where = ["status='running'", "(heartbeat_at IS NULL OR heartbeat_at < ?)"]
        params: list[Any] = [cutoff]
        if slot_names:
            placeholders = ",".join("?" for _ in slot_names)
            where.append(f"slot_name IN ({placeholders})")
            params.extend(slot_names)
        if job_types:
            placeholders = ",".join("?" for _ in job_types)
            where.append(f"job_type IN ({placeholders})")
            params.extend(job_types)
        events: list[tuple[int, str, str, str, str]] = []
        with self.connect() as conn:
            rows = conn.execute(f"SELECT * FROM worker_jobs WHERE {' AND '.join(where)}", params).fetchall()
            retried = 0
            failed = 0
            for row in rows:
                if auto_retry and int(row["attempts"] or 0) < int(row["max_attempts"] or 0):
                    conn.execute(
                        """
                        UPDATE worker_jobs
                        SET status='retry_wait', error_message=?, progress_message=?, progress_percent=0,
                            heartbeat_at=NULL, worker_id='', retry_after=CURRENT_TIMESTAMP, finished_at=NULL
                        WHERE id=?
                        """,
                        (message, message, row["id"]),
                    )
                    retried += 1
                    event_type = "stale_retry"
                else:
                    conn.execute(
                        """
                        UPDATE worker_jobs
                        SET status='failed', error_message=?, progress_message=?, heartbeat_at=NULL, worker_id='', finished_at=CURRENT_TIMESTAMP
                        WHERE id=?
                        """,
                        (message, message, row["id"]),
                    )
                    failed += 1
                    event_type = "stale_failed"
                events.append((int(row["id"]), event_type, str(row["slot_name"] or ""), str(row["profile_name"] or ""), str(row["worker_id"] or "")))
        for event_job_id, event_type, event_slot, event_profile, event_worker_id in events:
            self.add_worker_event(event_job_id, event_type, message, slot_name=event_slot, profile_name=event_profile, worker_id=event_worker_id)
        total = len(rows)
        if total:
            self.log_audit("worker", "recover_stale", "worker_job", ",".join(slot_names or ["all"]), f"{message}: total={total} retry={retried} failed={failed}")
        return {"total": total, "retried": retried, "failed": failed}

    def finish_worker_job(self, job_id: int, status: str, result: dict[str, Any] | None = None, error_message: str = "") -> None:
        result_text = json.dumps(result or {}, ensure_ascii=False)
        progress = 100 if status in {"completed", "success"} else 0 if status in {"failed", "canceled"} else None
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE worker_jobs
                SET status=?, result=?, error_message=?, finished_at=CURRENT_TIMESTAMP,
                    heartbeat_at=CURRENT_TIMESTAMP,
                    progress_percent=COALESCE(?, progress_percent),
                    progress_message=?
                WHERE id=?
                """,
                (status, result_text, error_message, progress, error_message or f"작업 완료: {status}", job_id),
            )
        row = self.get_worker_job(job_id) or {}
        self.add_worker_event(job_id, status, error_message or f"작업 완료: {status}", slot_name=str(row.get("slot_name") or ""), profile_name=str(row.get("profile_name") or ""), worker_id=str(row.get("worker_id") or ""), progress_percent=int(row.get("progress_percent") or progress or 0), payload=result or {})
        level = "ERROR" if status == "failed" else "INFO"
        self.log_audit("worker", "finish", "worker_job", str(job_id), error_message or f"작업 완료: {status}", level=level)

    def create_ocr_result(
        self,
        session_id: int | None,
        slot_name: str,
        image_path: str,
        text: str,
        status: str = "created",
        error_message: str = "",
        engine: str = "tesseract",
    ) -> int:
        with self.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO ocr_results (session_id, slot_name, image_path, engine, text, status, error_message)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (session_id, slot_name, image_path, engine, text, status, error_message),
            )
            row_id = int(cur.lastrowid)
        self.log_audit("worker", "ocr", "ocr_result", str(row_id), f"OCR 저장: {slot_name} {status}")
        return row_id

    def list_ocr_results(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM ocr_results ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]

    def _row_to_rule(self, row: Any) -> AlertRule:
        return AlertRule(
            id=int(row["id"]),
            name=row["name"],
            enabled=int(row["enabled"]),
            keywords=self._json_list(row["keywords"]),
            exclude_keywords=self._json_list(row["exclude_keywords"]),
            locations=self._json_list(row["locations"]),
            job_categories=self._json_list(row["job_categories"]),
            education=self._json_list(row["education"]),
            experience=self._json_list(row["experience"]),
            min_salary=row["min_salary"] or "",
            notification_channel=row["notification_channel"] or "console",
        )

    def _row_to_profile(self, row: Any) -> SearchProfile:
        return SearchProfile(
            id=int(row["id"]),
            campaign_name=row["campaign_name"] or "기본 캠페인",
            name=row["name"],
            keyword=row["keyword"],
            target_url=row["target_url"],
            enabled=int(row["enabled"]),
            priority=int(row["priority"]),
            max_items=int(row["max_items"]),
            scroll_times=int(row["scroll_times"]),
        )
