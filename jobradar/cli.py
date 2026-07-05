from __future__ import annotations

import argparse
import time
from dataclasses import replace
from pathlib import Path

from jobradar.config import Settings
from jobradar.crawler.saramin_crawler import SaraminCrawler
from jobradar.db.repository import JobRadarRepository
from jobradar.db.migrate_sqlite_to_postgres import migrate_sqlite_to_postgres
from jobradar.models import AlertRule, SearchProfile
from jobradar.orchestrator import build_search_url, run_multi_emulator_collection
from jobradar.services.alert_service import AlertService
from jobradar.services.exporter import export_csv, export_json
from jobradar.device_farm.adb import list_devices, stop_device
from jobradar.device_farm.appium_server import check_appium_status, start_appium
from jobradar.device_farm.diagnostics import run_diagnostics
from jobradar.device_farm.emulator_launcher import launch_avd, launch_avd_checked, list_avds
from jobradar.device_farm.worker import queue_default_collection, run_worker_daemon, run_worker_once, sync_detected_devices
from jobradar.device_farm.url_utils import resolve_appium_url
from jobradar.device_farm.device_actions import control_slots
from jobradar.integrations.redis_health import check_redis
from jobradar.integrations.redis_queue import RedisJobQueue
from jobradar.integrations.host_agent_client import HostAgentClient
from jobradar.deploy.doctor import run_deploy_diagnostics


def split_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def worker_types_for_cli(mode: str) -> list[str] | None:
    mode = (mode or "playwright").strip().lower()
    if mode == "all":
        return None
    if mode == "appium":
        return ["appium_collect_profile"]
    return ["collect_profile"]


def parse_slot_args(values: list[str] | None) -> list[str]:
    slots: list[str] = []
    for value in values or []:
        for item in str(value).split(","):
            item = item.strip()
            if item:
                slots.append(item)
    return slots


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="JobRadar CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="Create database tables")
    sub.add_parser("db-check", help="Check configured database backend and connection")
    migrate_db = sub.add_parser("db-migrate-sqlite-to-postgres", help="Copy output/jobradar.sqlite3 into Postgres")
    migrate_db.add_argument("--sqlite", default="output/jobradar.sqlite3")
    migrate_db.add_argument("--postgres-url", default=None)
    migrate_db.add_argument("--dry-run", action="store_true")
    sub.add_parser("seed-defaults", help="Create default campaign and search profiles")

    crawl = sub.add_parser("crawl", help="Crawl Saramin mobile jobs and save to SQLite")
    crawl.add_argument("--url", default=None)
    crawl.add_argument("--keyword", default="")
    crawl.add_argument("--headless", action="store_true")
    crawl.add_argument("--max-items", type=int, default=None)
    crawl.add_argument("--scroll-times", type=int, default=None)

    multi = sub.add_parser("multi-crawl", help="Run up to 4 profile crawlers as emulator slots")
    multi.add_argument("--concurrency", type=int, default=4)
    multi.add_argument("--headed", action="store_true", help="Run visible browser windows instead of headless")
    multi.add_argument("--seed", action="store_true", help="Seed default profiles before running")

    add_profile = sub.add_parser("add-profile", help="Create or update a search profile")
    add_profile.add_argument("--name", required=True)
    add_profile.add_argument("--keyword", required=True)
    add_profile.add_argument("--url", default=None)
    add_profile.add_argument("--campaign", default="IT 신입/경력 채용 모니터링")
    add_profile.add_argument("--priority", type=int, default=100)
    add_profile.add_argument("--max-items", type=int, default=20)
    add_profile.add_argument("--scroll-times", type=int, default=3)

    sub.add_parser("list-profiles", help="List search profiles")
    sub.add_parser("sessions", help="List latest emulator sessions")

    export = sub.add_parser("export", help="Export saved jobs to JSON/CSV")
    export.add_argument("--keyword", default="")
    export.add_argument("--limit", type=int, default=1000)
    export.add_argument("--json", default="output/jobs_export.json")
    export.add_argument("--csv", default="output/jobs_export.csv")

    add_rule = sub.add_parser("add-rule", help="Add an alert rule")
    add_rule.add_argument("--name", required=True)
    add_rule.add_argument("--keywords", default="")
    add_rule.add_argument("--exclude-keywords", default="")
    add_rule.add_argument("--locations", default="")
    add_rule.add_argument("--job-categories", default="")
    add_rule.add_argument("--education", default="")
    add_rule.add_argument("--experience", default="")
    add_rule.add_argument("--channel", default="console", choices=["console", "telegram", "discord"])

    sub.add_parser("list-rules", help="List alert rules")

    alert = sub.add_parser("alert", help="Evaluate alert rules against saved jobs")
    alert.add_argument("--limit", type=int, default=200)

    jobs = sub.add_parser("jobs", help="List saved jobs")
    jobs.add_argument("--keyword", default="")
    jobs.add_argument("--limit", type=int, default=30)


    sub.add_parser("doctor", help="Check Python/Playwright/ADB/Emulator/Appium/OCR environment")
    sub.add_parser("deploy-doctor", help="Check release stack: DB, Redis, API, GUI, and Appium slots")
    sub.add_parser("avd-list", help="List Android Virtual Devices from Android SDK")
    sub.add_parser("device-list", help="List connected ADB devices with boot/model info")

    slot_init = sub.add_parser("slot-init", help="Create or refresh practical device-farm slots: Emulator A-D + USB Device")
    slot_init.add_argument("--slots", type=int, default=None)

    slot_assign = sub.add_parser("slot-assign", help="Assign an AVD/UDID/proxy to a slot")
    slot_assign.add_argument("--slot", required=True, help="Example: Emulator A")
    slot_assign.add_argument("--avd", default="")
    slot_assign.add_argument("--udid", default="")
    slot_assign.add_argument("--proxy", default="")
    slot_assign.add_argument("--console-port", type=int, default=0)
    slot_assign.add_argument("--adb-port", type=int, default=0)
    slot_assign.add_argument("--notes", default="")

    launch_slot = sub.add_parser("launch-slot", help="Launch the assigned AVD for one slot")
    launch_slot.add_argument("--slot", required=True)
    launch_slot.add_argument("--headless", action="store_true")
    launch_slot.add_argument("--no-wait", action="store_true", help="Do not wait for ADB boot_completed")
    launch_slot.add_argument("--timeout", type=int, default=None, help="Boot wait timeout seconds")
    launch_slot.add_argument("--gpu", default="", help="Emulator gpu mode, e.g. swiftshader_indirect or host")
    launch_slot.add_argument("--snapshot", action="store_true", help="Allow snapshot load/save behavior")

    launch_all = sub.add_parser("launch-all", help="Launch assigned AVDs for all slots sequentially")
    launch_all.add_argument("--headless", action="store_true")
    launch_all.add_argument("--no-wait", action="store_true", help="Do not wait for ADB boot_completed")
    launch_all.add_argument("--timeout", type=int, default=None, help="Boot wait timeout seconds per slot")
    launch_all.add_argument("--delay", type=int, default=None, help="Delay seconds between slots")
    launch_all.add_argument("--gpu", default="", help="Emulator gpu mode, e.g. swiftshader_indirect or host")
    launch_all.add_argument("--snapshot", action="store_true", help="Allow snapshot load/save behavior")

    sub.add_parser("slot-ports", help="Show configured emulator/Appium port assignments")

    stop = sub.add_parser("stop-device", help="Stop an emulator/device by ADB serial")
    stop.add_argument("--udid", required=True)

    appium_slot = sub.add_parser("appium-start", help="Start a local Appium server for one slot or every slot")
    appium_slot.add_argument("--slot", default="all")

    device_action = sub.add_parser("device-action", help="Control selected Appium slots: immediate-stop, resume, home, close-all-home, launch-package")
    device_action.add_argument("--action", required=True, choices=["immediate-stop", "resume", "home", "close-all-home", "launch-package"])
    device_action.add_argument("--slot", action="append", default=[], help="Target slot. Can repeat or pass comma-separated values. Omit for all slots.")
    device_action.add_argument("--package", default="", help="Android package for launch-package, e.g. com.android.chrome")
    device_action.add_argument("--activity", default="", help="Optional Android activity for launch-package fallback")
    device_action.add_argument("--run-now", action="store_true", help="For resume, run worker once immediately after queueing")

    proxy = sub.add_parser("proxy-add", help="Create/update a proxy profile and optionally assign it to a slot")
    proxy.add_argument("--name", required=True)
    proxy.add_argument("--url", required=True, help="Example: http://user:pass@host:port")
    proxy.add_argument("--slot", default="")

    sub.add_parser("proxy-list", help="List proxy profiles")

    queue = sub.add_parser("queue-collection", help="Queue one collection worker job per enabled profile/slot")
    queue.add_argument("--slots", type=int, default=None)
    queue.add_argument("--slot", action="append", default=[], help="Queue only selected slot. Can repeat or pass comma-separated values, e.g. --slot 'Emulator B' or --slot 'Emulator B,USB Device'")
    queue.add_argument("--mode", default="appium", choices=["playwright", "appium", "both"], help="Queue PC Playwright jobs, Emulator Appium jobs, or both")

    worker = sub.add_parser("worker", help="Run queued worker jobs")
    worker.add_argument("--once", action="store_true", help="Run one polling iteration and exit")
    worker.add_argument("--max-jobs", type=int, default=4)
    worker.add_argument("--headed", action="store_true")
    worker.add_argument("--slot", action="append", default=[], help="Process only selected slot. Can repeat or pass comma-separated values.")
    worker.add_argument("--type", default="playwright", choices=["playwright", "appium", "all"], help="Worker job type to process")

    appium_worker = sub.add_parser("appium-worker", help="Run queued Appium mobile-web jobs inside Android emulators or USB devices")
    appium_worker.add_argument("--once", action="store_true", help="Run one polling iteration and exit")
    appium_worker.add_argument("--max-jobs", type=int, default=4)
    appium_worker.add_argument("--slot", action="append", default=[], help="Process only selected slot. Can repeat or pass comma-separated values.")

    reset_worker = sub.add_parser("worker-reset", help="Mark stale running worker jobs as failed")
    reset_worker.add_argument("--slot", action="append", default=[], help="Reset only selected slot(s)")
    reset_worker.add_argument("--type", default="all", choices=["playwright", "appium", "all"])

    recover_worker = sub.add_parser("worker-recover-stale", help="Recover running jobs with expired heartbeat")
    recover_worker.add_argument("--slot", action="append", default=[], help="Recover only selected slot(s)")
    recover_worker.add_argument("--type", default="all", choices=["playwright", "appium", "all"])
    recover_worker.add_argument("--stale-after", type=int, default=None, help="Heartbeat timeout seconds")

    retry_worker = sub.add_parser("worker-retry-failed", help="Move retryable failed jobs back to retry_wait and Redis queue")
    retry_worker.add_argument("--slot", action="append", default=[], help="Retry only selected slot(s)")
    retry_worker.add_argument("--type", default="all", choices=["playwright", "appium", "all"])

    worker_events = sub.add_parser("worker-events", help="Show worker heartbeat/progress events")
    worker_events.add_argument("--limit", type=int, default=50)
    worker_events.add_argument("--job-id", type=int, default=None)
    worker_events.add_argument("--slot", default="")

    cancel_worker = sub.add_parser("worker-cancel", help="Cancel queued/running worker jobs")
    cancel_worker.add_argument("--slot", action="append", default=[], help="Cancel only selected slot(s)")
    cancel_worker.add_argument("--type", default="all", choices=["playwright", "appium", "all"])

    slot_profile = sub.add_parser("slot-profile", help="Assign a search profile to one device slot")
    slot_profile.add_argument("--slot", required=True)
    slot_profile.add_argument("--profile", required=True, help="Search profile name. Use an empty string to go back to automatic mapping.")

    health = sub.add_parser("appium-health", help="Check Appium /status for all or selected slots")
    health.add_argument("--slot", action="append", default=[], help="Check only selected slot(s). Can repeat or pass comma-separated values.")

    sub.add_parser("api-check", help="Check FastAPI control-plane connectivity")
    host_agent = sub.add_parser("host-agent", help="Run Windows Host Agent for emulator window arrangement")
    host_agent.add_argument("--host", default="127.0.0.1")
    host_agent.add_argument("--port", type=int, default=8767)

    sub.add_parser("host-agent-check", help="Check Windows Host Agent connectivity")
    sub.add_parser("emulator-windows", help="List visible Android Emulator windows through Host Agent")

    arrange = sub.add_parser("arrange-emulators", help="Arrange visible Android Emulator windows on the Windows desktop")
    arrange.add_argument("--layout", default="grid2x2", choices=["grid2x2", "horizontal", "vertical"])
    arrange.add_argument("--x", type=int, default=20)
    arrange.add_argument("--y", type=int, default=40)
    arrange.add_argument("--width", type=int, default=430)
    arrange.add_argument("--height", type=int, default=780)
    arrange.add_argument("--gap", type=int, default=12)
    arrange.add_argument("--columns", type=int, default=2)
    arrange.add_argument("--dry-run", action="store_true")
    sub.add_parser("redis-check", help="Check Redis connectivity used by Docker Compose/future queue layer")
    sub.add_parser("redis-queue", help="Show Redis real-time queue status")

    redis_events = sub.add_parser("redis-events", help="Show recent Redis queue events")
    redis_events.add_argument("--limit", type=int, default=30)

    sub.add_parser("redis-drain", help="Clear pending Redis queue items only; SQLite history is kept")

    sub.add_parser("worker-jobs", help="List worker queue jobs")

    return parser


# 명령줄에서 프로그램이 시작될 때 실행되는 진입점입니다.
def main() -> None:
    args = build_parser().parse_args()
    settings = Settings()

    # redis-check must not touch SQLite. In Docker, the DB bind mount can be
    # absent or still being created while Redis is already healthy. This command
    # is purely a network/service check, so run it before directory/DB init.
    if args.command == "api-check":
        try:
            import requests
            response = requests.get(settings.api_url.rstrip("/") + "/health", timeout=5)
            data = response.json() if response.headers.get("content-type", "").startswith("application/json") else {"text": response.text}
            print(f"{'OK' if response.ok and data.get('ok', True) else 'FAIL'}\t{settings.api_url}\tHTTP {response.status_code}\t{data.get('service', data.get('message', ''))}")
        except Exception as exc:
            print(f"FAIL\t{settings.api_url}\t{exc}")
        return

    if args.command == "redis-check":
        health = check_redis(settings.redis_url)
        print(f"{'OK' if health.ok else 'FAIL'}\t{health.url}\t{health.message}")
        return
    if args.command in {"redis-queue", "redis-events", "redis-drain"}:
        try:
            queue = RedisJobQueue.from_settings(settings)
            if args.command == "redis-queue":
                status = queue.status()
                print(f"{'OK' if status.ok else 'FAIL'}\tqueued={status.queued}\tprocessing={status.processing}\tevents={status.events}\t{status.message}")
            elif args.command == "redis-events":
                for item in queue.recent_events(limit=args.limit):
                    print(f"{item.get('event_id')}\t{item.get('event')}\t#{item.get('job_id')}\t{item.get('slot_name')}\t{item.get('profile_name')}\t{item.get('message')}")
            elif args.command == "redis-drain":
                count = queue.drain()
                print(f"Drained Redis queued jobs: {count}")
        except Exception as exc:
            print(f"FAIL\tRedis queue unavailable: {exc}")
        return

    if args.command == "host-agent":
        try:
            import uvicorn
        except Exception as exc:
            raise SystemExit(f"uvicorn 설치가 필요합니다: {exc}")
        uvicorn.run("jobradar.host_agent.main:app", host=args.host, port=int(args.port), reload=False)
        return

    if args.command in {"host-agent-check", "emulator-windows", "arrange-emulators"}:
        client = HostAgentClient(settings.host_agent_url, timeout=float(settings.host_agent_timeout_seconds))
        if args.command == "host-agent-check":
            result = client.health()
            print(f"{'OK' if result.ok else 'FAIL'}	{settings.host_agent_url}	{result.data.get('message') if result.ok else result.error}")
            return
        if args.command == "emulator-windows":
            result = client.emulator_windows()
            if not result.ok:
                print(f"FAIL	{settings.host_agent_url}	{result.error}")
                return
            for row in result.data.get("windows", []):
                print(f"hwnd={row.get('hwnd')}	pid={row.get('pid')}	{row.get('x')},{row.get('y')} {row.get('width')}x{row.get('height')}	{row.get('title')}")
            return
        if args.command == "arrange-emulators":
            result = client.arrange(
                layout=args.layout,
                x=args.x,
                y=args.y,
                width=args.width,
                height=args.height,
                gap=args.gap,
                columns=args.columns,
                dry_run=args.dry_run,
            )
            print(f"{'OK' if result.ok else 'FAIL'}	{settings.host_agent_url}	{result.data.get('message') if result.data else result.error}")
            for row in result.data.get("results", []):
                print(f"{'OK' if row.get('ok') else 'FAIL'}	{row.get('title')}	{row.get('x')},{row.get('y')} {row.get('width')}x{row.get('height')}	{row.get('message')}")
            return

    settings.ensure_dirs()
    repo = JobRadarRepository(settings.database_url)

    if args.command == "init-db":
        repo.init_db()
        print(f"Initialized database [{repo.backend_name}]: {settings.database_url}")
        return

    if args.command == "db-check":
        info = repo.check_connection()
        print(f"OK\tbackend={info['backend']}\tdatabase={info['database_url']}")
        return

    if args.command == "db-migrate-sqlite-to-postgres":
        postgres_url = args.postgres_url or settings.database_url
        summary = migrate_sqlite_to_postgres(args.sqlite, postgres_url, dry_run=args.dry_run)
        print(f"SQLite: {summary['sqlite']}")
        print(f"Postgres: {summary['postgres']}")
        for table, info in summary['tables'].items():
            if info.get('dry_run'):
                print(f"{table}: source_rows={info.get('source_rows', 0)} dry-run")
            elif info.get('skipped'):
                print(f"{table}: skipped={info.get('skipped')}")
            else:
                print(f"{table}: copied={info.get('copied', 0)} / source_rows={info.get('source_rows', 0)}")
        return

    repo.init_db()

    if args.command == "seed-defaults":
        count = repo.seed_default_profiles(settings.target_url)
        print(f"Seeded default search profiles: {count}")

    elif args.command == "crawl":
        crawl_settings = settings
        target_url = args.url or settings.target_url
        if args.keyword:
            target_url = build_search_url(target_url, args.keyword)
        crawl_settings = replace(crawl_settings, target_url=target_url)
        if args.headless:
            crawl_settings = replace(crawl_settings, headless=True)
        if args.max_items is not None:
            crawl_settings = replace(crawl_settings, max_items=args.max_items)
        if args.scroll_times is not None:
            crawl_settings = replace(crawl_settings, scroll_times=args.scroll_times)

        run_id = repo.create_run("saramin", crawl_settings.target_url)
        try:
            crawler = SaraminCrawler(crawl_settings)
            jobs = crawler.crawl()
            stats = repo.insert_jobs(jobs)
            rows = repo.list_jobs(limit=len(jobs) or 1)
            export_json(rows, crawl_settings.output_dir / "jobs_latest.json")
            export_csv(rows, crawl_settings.output_dir / "jobs_latest.csv")
            repo.finish_run(
                run_id,
                status="success",
                found_count=len(jobs),
                new_count=stats["new"],
                updated_count=stats["updated"],
            )
            print("Crawl completed")
            print(f"Found    : {len(jobs)}")
            print(f"New      : {stats['new']}")
            print(f"Updated  : {stats['updated']}")
            print(f"Unchanged: {stats['unchanged']}")
            print(f"Database : {settings.database_url}")
        except Exception as exc:
            repo.finish_run(run_id, status="failed", error_message=str(exc))
            raise

    elif args.command == "multi-crawl":
        if args.seed:
            repo.seed_default_profiles(settings.target_url)
        results = run_multi_emulator_collection(
            settings=settings,
            repo=repo,
            concurrency=max(1, min(args.concurrency, 8)),
            force_headless=not args.headed,
        )
        for item in results:
            print(
                f"{item.get('slot_name')} | {item.get('profile_name')} | {item.get('status')} | "
                f"found={item.get('found_count')} new={item.get('new')} updated={item.get('updated')}"
            )

    elif args.command == "add-profile":
        profile = SearchProfile(
            campaign_name=args.campaign,
            name=args.name,
            keyword=args.keyword,
            target_url=args.url or settings.target_url,
            priority=args.priority,
            max_items=args.max_items,
            scroll_times=args.scroll_times,
        )
        profile_id = repo.upsert_search_profile(profile)
        print(f"Saved search profile #{profile_id}: {profile.name}")

    elif args.command == "list-profiles":
        for profile in repo.list_search_profiles(enabled_only=False):
            print(
                f"#{profile.id} {profile.name} enabled={profile.enabled} keyword={profile.keyword} "
                f"priority={profile.priority} max_items={profile.max_items}"
            )

    elif args.command == "sessions":
        for row in repo.list_emulator_sessions(limit=30):
            print(
                f"#{row['id']} {row['slot_name']} {row['status']} {row['profile_name']} "
                f"found={row['found_count']} new={row['new_count']} error={row['error_message'] or '-'}"
            )

    elif args.command == "export":
        rows = repo.list_jobs(limit=args.limit, keyword=args.keyword)
        export_json(rows, Path(args.json))
        export_csv(rows, Path(args.csv))
        print(f"Exported {len(rows)} jobs")
        print(args.json)
        print(args.csv)

    elif args.command == "add-rule":
        rule = AlertRule(
            name=args.name,
            keywords=split_csv(args.keywords),
            exclude_keywords=split_csv(args.exclude_keywords),
            locations=split_csv(args.locations),
            job_categories=split_csv(args.job_categories),
            education=split_csv(args.education),
            experience=split_csv(args.experience),
            notification_channel=args.channel,
        )
        rule_id = repo.add_rule(rule)
        print(f"Added alert rule id={rule_id}: {rule.name}")

    elif args.command == "list-rules":
        for rule in repo.list_rules():
            print(
                f"#{rule.id} {rule.name} enabled={rule.enabled} "
                f"keywords={rule.keywords} locations={rule.locations} channel={rule.notification_channel}"
            )

    elif args.command == "alert":
        service = AlertService(repo, settings)
        events = service.evaluate_recent_jobs(limit=args.limit)
        print(f"Created alert events: {len(events)}")

    elif args.command == "jobs":
        rows = repo.list_jobs(limit=args.limit, keyword=args.keyword)
        for row in rows:
            print(
                f"#{row['id']} [{row.get('emulator_slot') or '-'}] {row['title']} | "
                f"{row['company']} | {row['location']} | {row['detail_url']}"
            )


    elif args.command == "doctor":
        for item in run_diagnostics(settings):
            print(f"{'OK' if item.ok else 'FAIL'}	{item.name}	{item.detail}")

    elif args.command == "deploy-doctor":
        for item in run_deploy_diagnostics(settings, repo):
            print(f"{'OK' if item.ok else 'FAIL'}	{item.name}	{item.detail}")

    elif args.command == "avd-list":
        avds, message = list_avds(settings)
        if avds:
            for avd in avds:
                print(avd)
        else:
            print(message)

    elif args.command == "device-list":
        for device in list_devices(settings):
            print(f"{device.udid}	{device.state}	model={device.model or '-'}	android={device.android_version or '-'}	boot={device.boot_completed}")

    elif args.command == "slot-init":
        slot_count = args.slots or settings.emulator_slots
        count = repo.seed_device_slots(
            slot_count=slot_count,
            appium_host=settings.appium_host,
            appium_base_port=settings.appium_base_port,
            appium_port_step=settings.appium_port_step,
            system_port_base=settings.appium_system_port_base,
            mjpeg_port_base=settings.appium_mjpeg_port_base,
            chromedriver_port_base=settings.appium_chromedriver_port_base,
            emulator_port_pairs=settings.parsed_emulator_port_pairs(),
        )
        print(f"Initialized device slots: {count}")

    elif args.command == "slot-assign":
        repo.upsert_device_slot(
            args.slot,
            avd_name=args.avd,
            udid=args.udid,
            proxy_name=args.proxy,
            notes=args.notes,
            emulator_console_port=args.console_port,
            emulator_adb_port=args.adb_port,
        )
        print(f"Saved slot: {args.slot}")

    elif args.command == "slot-ports":
        for slot in repo.list_device_slots():
            console_port = int(slot.get("emulator_console_port") or 0)
            adb_port = int(slot.get("emulator_adb_port") or 0)
            serial = f"emulator-{console_port}" if console_port else str(slot.get("udid") or "")
            print(
                f"{slot['slot_name']} avd={slot.get('avd_name') or '-'} "
                f"console={console_port or '-'} adb={adb_port or '-'} serial={serial or '-'} "
                f"appium={slot.get('appium_url') or '-'} systemPort={slot.get('system_port') or '-'}"
            )

    elif args.command == "launch-slot":
        slot = repo.get_device_slot(args.slot)
        if not slot:
            raise SystemExit(f"슬롯을 찾을 수 없습니다: {args.slot}")
        result = launch_avd_checked(
            settings,
            str(slot.get("avd_name") or ""),
            console_port=int(slot.get("emulator_console_port") or 0) or None,
            adb_port=int(slot.get("emulator_adb_port") or 0) or None,
            headless=args.headless,
            wait=not args.no_wait,
            boot_timeout=args.timeout,
            gpu_mode=args.gpu or None,
            snapshot=args.snapshot,
        )
        repo.update_device_slot_runtime(args.slot, result.status, udid=result.udid if result.ok else "", notes=result.summary())
        print(f"{args.slot}: avd={slot.get('avd_name') or '-'} {result.summary()}")

    elif args.command == "launch-all":
        delay = settings.emulator_launch_delay_seconds if args.delay is None else max(0, args.delay)
        for slot in repo.list_device_slots():
            avd = str(slot.get("avd_name") or "")
            if not avd:
                print(f"{slot['slot_name']}: skipped (AVD 미지정)")
                continue
            result = launch_avd_checked(
                settings,
                avd,
                console_port=int(slot.get("emulator_console_port") or 0) or None,
                adb_port=int(slot.get("emulator_adb_port") or 0) or None,
                headless=args.headless,
                wait=not args.no_wait,
                boot_timeout=args.timeout,
                gpu_mode=args.gpu or None,
                snapshot=args.snapshot,
            )
            repo.update_device_slot_runtime(slot["slot_name"], result.status, udid=result.udid if result.ok else "", notes=result.summary())
            print(f"{slot['slot_name']}: avd={avd} {result.summary()}")
            if not result.ok and not args.no_wait:
                print("실패한 슬롯이 있어 다음 슬롯 실행을 중단합니다. --no-wait 또는 문제 해결 후 다시 실행하세요.")
                break
            if delay:
                time.sleep(delay)

    elif args.command == "stop-device":
        ok, msg = stop_device(settings, args.udid)
        print("OK" if ok else "FAIL", msg)

    elif args.command == "appium-start":
        slots = repo.list_device_slots()
        targets = slots if args.slot == "all" else [s for s in slots if s["slot_name"] == args.slot]
        if not targets:
            raise SystemExit("대상 슬롯이 없습니다. 먼저 slot-init을 실행하세요.")
        for slot in targets:
            port = int(slot.get("appium_port") or settings.appium_base_port)
            pid, msg = start_appium(settings, port=port, host=settings.appium_host, log_name=f"appium_{slot['slot_name'].replace(' ', '_')}.log")
            url = str(slot.get("appium_url") or f"http://{settings.appium_host}:{port}")
            ok, health = check_appium_status(url)
            repo.update_device_slot_runtime(slot["slot_name"], "appium_starting" if pid else "appium_failed", notes=f"pid={pid} {msg} health={health}")
            print(f"{slot['slot_name']}: pid={pid} url={url} {msg}")

    elif args.command == "device-action":
        slot_names = parse_slot_args(args.slot)
        action = str(args.action).replace("-", "_")
        slots = repo.list_device_slots()
        if action == "immediate_stop":
            affected = repo.cancel_worker_jobs(slot_names=slot_names or None, job_types=["appium_collect_profile"], message="CLI 즉시 중지 요청")
            redis_affected = 0
            if settings.redis_queue_enabled:
                try:
                    redis_affected = RedisJobQueue.from_settings(settings).cancel_jobs(slot_names=slot_names or None, job_types=["appium_collect_profile"], message="CLI 즉시 중지 요청")
                except Exception as exc:
                    print(f"Redis cancel failed: {exc}")
            rows = control_slots(settings, slots, "immediate_stop", slot_names=slot_names or None)
            print(f"Immediate stop requested: SQLite={affected} Redis={redis_affected}")
            for row in rows:
                print(f"{'OK' if row.get('ok') else 'FAIL'}\t{row.get('slot_name')}\t{row.get('message')}")
        elif action == "resume":
            retried = repo.retry_failed_worker_jobs(slot_names=slot_names or None, job_types=["appium_collect_profile"], message="CLI 이어하기 요청")
            queued = queue_default_collection(settings, repo, slot_count=settings.emulator_slots, mode="appium", slot_names=slot_names or None)
            redis_count = 0
            if settings.redis_queue_enabled:
                try:
                    rows = repo.list_worker_jobs(limit=500, statuses=["queued", "retry_wait"])
                    if slot_names:
                        rows = [row for row in rows if str(row.get("slot_name") or "") in set(slot_names)]
                    redis_count = RedisJobQueue.from_settings(settings).enqueue_jobs(rows) if rows else 0
                except Exception as exc:
                    print(f"Redis enqueue failed: {exc}")
            print(f"Resume queued: retried={retried} queued={queued} redis={redis_count}")
            if args.run_now:
                results = run_worker_once(settings, repo, max_jobs=max(1, len(slot_names) or settings.emulator_slots), worker_types=["appium_collect_profile"], slot_names=slot_names or None)
                print(f"Worker processed: {len(results)}")
                for item in results:
                    print(item)
        else:
            rows = control_slots(settings, slots, action, slot_names=slot_names or None, package_name=args.package, activity_name=args.activity)
            for row in rows:
                print(f"{'OK' if row.get('ok') else 'FAIL'}\t{row.get('slot_name')}\t{row.get('message')}")

    elif args.command == "proxy-add":
        row_id = repo.upsert_proxy_profile(args.name, args.url, assigned_slot=args.slot)
        if args.slot:
            slot = repo.get_device_slot(args.slot)
            if slot:
                repo.upsert_device_slot(args.slot, proxy_name=args.name, udid=str(slot.get("udid") or ""), status=str(slot.get("status") or "idle"), notes=str(slot.get("notes") or ""))
        print(f"Saved proxy profile #{row_id}: {args.name}")

    elif args.command == "proxy-list":
        for item in repo.list_proxy_profiles():
            print(f"#{item['id']} {item['name']} enabled={item['enabled']} slot={item['assigned_slot'] or '-'} status={item['status']} url={item['proxy_url']}")

    elif args.command == "queue-collection":
        slot_names = parse_slot_args(args.slot)
        count = queue_default_collection(settings, repo, slot_count=args.slots or settings.emulator_slots, mode=args.mode, slot_names=slot_names or None)
        target = ", ".join(slot_names) if slot_names else f"{args.slots or settings.emulator_slots} slots"
        redis_note = " + Redis" if settings.redis_queue_enabled else ""
        print(f"Queued {args.mode} collection jobs: {count} ({target}){redis_note}")

    elif args.command == "worker":
        sync_detected_devices(settings, repo)
        worker_types = worker_types_for_cli(args.type)
        slot_names = parse_slot_args(args.slot)
        if args.once:
            results = run_worker_once(settings, repo, max_jobs=args.max_jobs, force_headless=not args.headed, worker_types=worker_types, slot_names=slot_names or None)
            print(f"Worker processed: {len(results)}")
            for item in results:
                print(item)
        else:
            run_worker_daemon(settings, repo, max_jobs=args.max_jobs, force_headless=not args.headed, once=False, worker_types=worker_types, slot_names=slot_names or None)

    elif args.command == "appium-worker":
        sync_detected_devices(settings, repo)
        slot_names = parse_slot_args(args.slot)
        if args.once:
            results = run_worker_once(settings, repo, max_jobs=args.max_jobs, worker_types=["appium_collect_profile"], slot_names=slot_names or None)
            print(f"Appium worker processed: {len(results)}")
            for item in results:
                print(item)
        else:
            run_worker_daemon(settings, repo, max_jobs=args.max_jobs, once=False, worker_types=["appium_collect_profile"], slot_names=slot_names or None)


    elif args.command == "worker-reset":
        slot_names = parse_slot_args(args.slot)
        worker_types = worker_types_for_cli(args.type)
        affected = repo.reset_stale_worker_jobs(slot_names=slot_names or None, job_types=worker_types)
        print(f"Reset stale running worker jobs: {affected}")

    elif args.command == "worker-recover-stale":
        slot_names = parse_slot_args(args.slot)
        worker_types = worker_types_for_cli(args.type)
        result = repo.recover_stale_worker_jobs(
            stale_after_seconds=args.stale_after or settings.worker_stale_after_seconds,
            slot_names=slot_names or None,
            job_types=worker_types,
            message="CLI heartbeat stale recovery",
            auto_retry=settings.worker_auto_retry,
        )
        redis_recovered = 0
        if settings.redis_queue_enabled:
            try:
                redis_recovered = RedisJobQueue.from_settings(settings).recover_stale_processing(args.stale_after or settings.worker_stale_after_seconds)
            except Exception as exc:
                print(f"Redis recover failed: {exc}")
        print(f"Recovered stale jobs: {result} Redis={redis_recovered}")

    elif args.command == "worker-retry-failed":
        slot_names = parse_slot_args(args.slot)
        worker_types = worker_types_for_cli(args.type)
        affected = repo.retry_failed_worker_jobs(slot_names=slot_names or None, job_types=worker_types, message="CLI failed job retry")
        redis_count = 0
        if settings.redis_queue_enabled:
            try:
                rows = repo.list_worker_jobs(limit=500, statuses=["retry_wait"])
                redis_count = RedisJobQueue.from_settings(settings).enqueue_jobs(rows) if rows else 0
            except Exception as exc:
                print(f"Redis retry enqueue failed: {exc}")
        print(f"Retry failed jobs: {affected} SQLite / {redis_count} Redis enqueued")

    elif args.command == "worker-events":
        rows = repo.list_worker_events(limit=args.limit, job_id=args.job_id, slot_name=args.slot)
        for row in rows:
            print(f"#{row['id']} job={row.get('worker_job_id') or '-'} {row.get('created_at')} {row.get('event_type')} {row.get('slot_name') or '-'} progress={row.get('progress_percent') or 0}% {row.get('message') or ''}")

    elif args.command == "worker-cancel":
        slot_names = parse_slot_args(args.slot)
        worker_types = worker_types_for_cli(args.type)
        affected = repo.cancel_worker_jobs(slot_names=slot_names or None, job_types=worker_types)
        redis_affected = 0
        if settings.redis_queue_enabled:
            try:
                redis_affected = RedisJobQueue.from_settings(settings).cancel_jobs(slot_names=slot_names or None, job_types=worker_types)
            except Exception as exc:
                print(f"Redis cancel failed: {exc}")
        print(f"Canceled worker jobs: {affected} SQLite / {redis_affected} Redis")

    elif args.command == "slot-profile":
        repo.set_device_slot_profile(args.slot, args.profile)
        print(f"Assigned {args.slot} -> {args.profile or 'auto'}")

    elif args.command == "appium-health":
        slot_names = parse_slot_args(args.slot)
        requested = set(slot_names)
        targets = [slot for slot in repo.list_device_slots() if not requested or slot["slot_name"] in requested]
        if not targets:
            print("No target slots. Run slot-init first or check --slot name.")
        for slot in targets:
            raw_url = str(slot.get("appium_url") or settings.appium_server_url)
            url = resolve_appium_url(settings, raw_url)
            ok, msg = check_appium_status(url)
            print(f"{'OK' if ok else 'FAIL'}	{slot['slot_name']}	{url}	{msg}")

    elif args.command == "redis-check":
        health = check_redis(settings.redis_url)
        print(f"{'OK' if health.ok else 'FAIL'}	{health.url}	{health.message}")

    elif args.command == "worker-jobs":
        for item in repo.list_worker_jobs(limit=100):
            progress = item.get("progress_percent") if item.get("progress_percent") is not None else 0
            heartbeat = item.get("heartbeat_at") or "-"
            worker_id = item.get("worker_id") or "-"
            print(
                f"#{item['id']} {item['status']} {item['job_type']} {item['slot_name']} {item['profile_name']} "
                f"attempts={item['attempts']}/{item['max_attempts']} progress={progress}% heartbeat={heartbeat} worker={worker_id} "
                f"error={item['error_message'] or '-'}"
            )


if __name__ == "__main__":
    main()
