from __future__ import annotations

POSTGRES_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS job_postings (
    id SERIAL PRIMARY KEY,
    source TEXT NOT NULL,
    job_id TEXT NOT NULL,
    title TEXT NOT NULL,
    company TEXT,
    location TEXT,
    job_category TEXT,
    experience TEXT,
    education TEXT,
    employment_type TEXT,
    salary TEXT,
    deadline TEXT,
    posted_at TEXT,
    detail_url TEXT NOT NULL,
    description_text TEXT,
    tech_keywords TEXT,
    raw_text TEXT,
    content_hash TEXT,
    first_seen_at TEXT,
    last_seen_at TEXT,
    is_active INTEGER DEFAULT 1,
    source_site TEXT DEFAULT 'saramin',
    campaign_name TEXT DEFAULT '',
    profile_name TEXT DEFAULT '',
    emulator_slot TEXT DEFAULT '',
    collected_session_id INTEGER,
    created_at TEXT DEFAULT (CURRENT_TIMESTAMP::text),
    updated_at TEXT DEFAULT (CURRENT_TIMESTAMP::text),
    UNIQUE(source, job_id)
);

CREATE INDEX IF NOT EXISTS idx_job_postings_location ON job_postings(location);
CREATE INDEX IF NOT EXISTS idx_job_postings_title ON job_postings(title);
CREATE INDEX IF NOT EXISTS idx_job_postings_last_seen ON job_postings(last_seen_at);
CREATE INDEX IF NOT EXISTS idx_job_postings_emulator_slot ON job_postings(emulator_slot);
CREATE INDEX IF NOT EXISTS idx_job_postings_profile_name ON job_postings(profile_name);

CREATE TABLE IF NOT EXISTS campaigns (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    description TEXT DEFAULT '',
    enabled INTEGER DEFAULT 1,
    schedule_note TEXT DEFAULT '',
    created_at TEXT DEFAULT (CURRENT_TIMESTAMP::text),
    updated_at TEXT DEFAULT (CURRENT_TIMESTAMP::text)
);

CREATE TABLE IF NOT EXISTS search_profiles (
    id SERIAL PRIMARY KEY,
    campaign_name TEXT DEFAULT '기본 캠페인',
    name TEXT NOT NULL UNIQUE,
    keyword TEXT NOT NULL,
    target_url TEXT NOT NULL,
    enabled INTEGER DEFAULT 1,
    priority INTEGER DEFAULT 100,
    max_items INTEGER DEFAULT 20,
    scroll_times INTEGER DEFAULT 3,
    created_at TEXT DEFAULT (CURRENT_TIMESTAMP::text),
    updated_at TEXT DEFAULT (CURRENT_TIMESTAMP::text)
);

CREATE INDEX IF NOT EXISTS idx_search_profiles_enabled_priority ON search_profiles(enabled, priority);

CREATE TABLE IF NOT EXISTS alert_rules (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    enabled INTEGER DEFAULT 1,
    keywords TEXT,
    exclude_keywords TEXT,
    locations TEXT,
    job_categories TEXT,
    min_salary TEXT,
    education TEXT,
    experience TEXT,
    notification_channel TEXT DEFAULT 'console',
    created_at TEXT DEFAULT (CURRENT_TIMESTAMP::text),
    updated_at TEXT DEFAULT (CURRENT_TIMESTAMP::text)
);

CREATE TABLE IF NOT EXISTS alert_events (
    id SERIAL PRIMARY KEY,
    rule_id INTEGER NOT NULL,
    job_posting_id INTEGER NOT NULL,
    channel TEXT,
    message TEXT,
    status TEXT DEFAULT 'created',
    created_at TEXT DEFAULT (CURRENT_TIMESTAMP::text),
    UNIQUE(rule_id, job_posting_id),
    FOREIGN KEY(rule_id) REFERENCES alert_rules(id),
    FOREIGN KEY(job_posting_id) REFERENCES job_postings(id)
);

CREATE TABLE IF NOT EXISTS crawler_runs (
    id SERIAL PRIMARY KEY,
    source TEXT NOT NULL,
    target_url TEXT NOT NULL,
    status TEXT NOT NULL,
    found_count INTEGER DEFAULT 0,
    new_count INTEGER DEFAULT 0,
    updated_count INTEGER DEFAULT 0,
    error_message TEXT,
    started_at TEXT DEFAULT (CURRENT_TIMESTAMP::text),
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS emulator_sessions (
    id SERIAL PRIMARY KEY,
    slot_name TEXT NOT NULL,
    device_id TEXT DEFAULT '',
    campaign_name TEXT DEFAULT '',
    profile_name TEXT DEFAULT '',
    keyword TEXT DEFAULT '',
    target_url TEXT DEFAULT '',
    status TEXT DEFAULT 'running',
    found_count INTEGER DEFAULT 0,
    new_count INTEGER DEFAULT 0,
    updated_count INTEGER DEFAULT 0,
    unchanged_count INTEGER DEFAULT 0,
    error_message TEXT DEFAULT '',
    screenshot_path TEXT DEFAULT '',
    health_percent INTEGER DEFAULT 100,
    started_at TEXT DEFAULT (CURRENT_TIMESTAMP::text),
    finished_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_emulator_sessions_started ON emulator_sessions(started_at);
CREATE INDEX IF NOT EXISTS idx_emulator_sessions_slot ON emulator_sessions(slot_name);

CREATE TABLE IF NOT EXISTS audit_logs (
    id SERIAL PRIMARY KEY,
    actor TEXT DEFAULT 'system',
    action TEXT NOT NULL,
    entity_type TEXT DEFAULT '',
    entity_id TEXT DEFAULT '',
    level TEXT DEFAULT 'INFO',
    message TEXT DEFAULT '',
    created_at TEXT DEFAULT (CURRENT_TIMESTAMP::text)
);

CREATE INDEX IF NOT EXISTS idx_audit_logs_created ON audit_logs(created_at);

CREATE TABLE IF NOT EXISTS operation_commands (
    id SERIAL PRIMARY KEY,
    command TEXT NOT NULL,
    status TEXT DEFAULT 'queued',
    actor TEXT DEFAULT 'admin',
    payload TEXT DEFAULT '{}',
    message TEXT DEFAULT '',
    created_at TEXT DEFAULT (CURRENT_TIMESTAMP::text),
    handled_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_operation_commands_created ON operation_commands(created_at);
CREATE INDEX IF NOT EXISTS idx_operation_commands_status ON operation_commands(status);

CREATE TABLE IF NOT EXISTS device_slots (
    id SERIAL PRIMARY KEY,
    slot_name TEXT NOT NULL UNIQUE,
    avd_name TEXT DEFAULT '',
    udid TEXT DEFAULT '',
    device_type TEXT DEFAULT 'emulator',
    enabled INTEGER DEFAULT 1,
    assigned_profile_name TEXT DEFAULT '',
    appium_url TEXT DEFAULT '',
    appium_port INTEGER DEFAULT 0,
    system_port INTEGER DEFAULT 0,
    mjpeg_server_port INTEGER DEFAULT 0,
    chromedriver_port INTEGER DEFAULT 0,
    proxy_name TEXT DEFAULT '',
    emulator_console_port INTEGER DEFAULT 0,
    emulator_adb_port INTEGER DEFAULT 0,
    status TEXT DEFAULT 'idle',
    last_seen_at TEXT DEFAULT '',
    notes TEXT DEFAULT '',
    created_at TEXT DEFAULT (CURRENT_TIMESTAMP::text),
    updated_at TEXT DEFAULT (CURRENT_TIMESTAMP::text)
);

CREATE INDEX IF NOT EXISTS idx_device_slots_status ON device_slots(status);

CREATE TABLE IF NOT EXISTS proxy_profiles (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    proxy_url TEXT DEFAULT '',
    enabled INTEGER DEFAULT 1,
    assigned_slot TEXT DEFAULT '',
    status TEXT DEFAULT 'ready',
    failure_count INTEGER DEFAULT 0,
    last_error TEXT DEFAULT '',
    created_at TEXT DEFAULT (CURRENT_TIMESTAMP::text),
    updated_at TEXT DEFAULT (CURRENT_TIMESTAMP::text)
);

CREATE INDEX IF NOT EXISTS idx_proxy_profiles_slot ON proxy_profiles(assigned_slot);

CREATE TABLE IF NOT EXISTS worker_jobs (
    id SERIAL PRIMARY KEY,
    job_type TEXT NOT NULL,
    slot_name TEXT DEFAULT '',
    profile_name TEXT DEFAULT '',
    priority INTEGER DEFAULT 100,
    status TEXT DEFAULT 'queued',
    attempts INTEGER DEFAULT 0,
    max_attempts INTEGER DEFAULT 3,
    payload TEXT DEFAULT '{}',
    result TEXT DEFAULT '{}',
    error_message TEXT DEFAULT '',
    worker_id TEXT DEFAULT '',
    heartbeat_at TEXT,
    progress_percent INTEGER DEFAULT 0,
    progress_message TEXT DEFAULT '',
    retry_after TEXT,
    created_at TEXT DEFAULT (CURRENT_TIMESTAMP::text),
    started_at TEXT,
    finished_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_worker_jobs_status_priority ON worker_jobs(status, priority, id);
CREATE INDEX IF NOT EXISTS idx_worker_jobs_slot ON worker_jobs(slot_name);

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
);

CREATE INDEX IF NOT EXISTS idx_worker_events_job ON worker_events(worker_job_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_worker_events_created ON worker_events(created_at);


CREATE TABLE IF NOT EXISTS ocr_results (
    id SERIAL PRIMARY KEY,
    session_id INTEGER,
    slot_name TEXT DEFAULT '',
    image_path TEXT DEFAULT '',
    engine TEXT DEFAULT 'tesseract',
    text TEXT DEFAULT '',
    status TEXT DEFAULT 'created',
    error_message TEXT DEFAULT '',
    created_at TEXT DEFAULT (CURRENT_TIMESTAMP::text)
);

CREATE INDEX IF NOT EXISTS idx_ocr_results_session ON ocr_results(session_id);
"""
