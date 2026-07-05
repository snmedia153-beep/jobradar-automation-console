from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

DEFAULT_TARGET_URL = (
    "https://m.saramin.co.kr/location-job/recently-list"
    "?is_detail_search=y&list_type=domestic&loc_cd=102020%2C102030%2C102040%2C102050"
)

MOBILE_USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
    "Mobile/15E148 Safari/604.1"
)

# Windows/Hyper-V often reserves blocks around 5556~5655.  The default below
# keeps the first emulator on the conventional 5554/5555 pair and moves the
# remaining emulators above that common reserved range.
DEFAULT_EMULATOR_PORT_PAIRS = "5554:5555,5656:5657,5658:5659,5660:5661"


def bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def int_list_env(name: str, default: list[int] | None = None) -> list[int]:
    value = os.getenv(name)
    if not value:
        return list(default or [])
    items: list[int] = []
    for raw in value.replace(";", ",").split(","):
        raw = raw.strip()
        if not raw:
            continue
        try:
            number = int(raw)
        except ValueError:
            continue
        if number > 0 and number not in items:
            items.append(number)
    return items


def parse_port_pairs(value: str, slots: int = 4) -> list[tuple[int, int]]:
    """Parse EMULATOR_PORT_PAIRS like '5554:5555,5656:5657'."""
    pairs: list[tuple[int, int]] = []
    for item in (value or "").replace(";", ",").split(","):
        item = item.strip()
        if not item:
            continue
        sep = ":" if ":" in item else "-" if "-" in item else None
        if not sep:
            continue
        left, right = item.split(sep, 1)
        try:
            console_port = int(left.strip())
            adb_port = int(right.strip())
        except ValueError:
            continue
        if console_port > 0 and adb_port > 0:
            pairs.append((console_port, adb_port))
    if not pairs:
        pairs = [(5554, 5555)]
    while len(pairs) < max(1, slots):
        last_console, _ = pairs[-1]
        next_console = last_console + 2
        pairs.append((next_console, next_console + 1))
    return pairs[: max(1, slots)]


def parse_int_list(value: str) -> list[int]:
    """Parse comma/semicolon separated integer ports."""
    ports: list[int] = []
    for item in (value or "").replace(";", ",").split(","):
        item = item.strip()
        if not item:
            continue
        try:
            port = int(item)
        except ValueError:
            continue
        if port > 0 and port not in ports:
            ports.append(port)
    return ports


def default_appium_ports(base_port: int, step: int, slots: int, explicit_ports: str = "", usb_port: int = 4731) -> list[int]:
    """Return Appium ports for Emulator A-D plus optional USB device.

    APPIUM_PORTS is authoritative when provided. Otherwise derive ports from
    base/step and ensure the USB port is present for 5-slot operation. This
    prevents dashboards from showing 4/4 when ADB has 4 emulators + 1 USB device.
    """
    parsed = parse_int_list(explicit_ports)
    if parsed:
        return parsed
    count = max(1, int(slots or 1))
    ports = [int(base_port) + i * int(step) for i in range(count)]
    if count >= 5 and usb_port > 0 and usb_port not in ports:
        ports.append(int(usb_port))
    return ports


# 프로그램 전체에서 쓰는 환경 변수와 기본 설정을 한곳에 모아 관리합니다.
@dataclass(frozen=True)
class Settings:
    target_url: str = os.getenv("TARGET_URL", DEFAULT_TARGET_URL)
    headless: bool = bool_env("HEADLESS", False)
    max_items: int = int_env("MAX_ITEMS", 50)
    scroll_times: int = int_env("SCROLL_TIMES", 6)
    request_delay_ms: int = int_env("REQUEST_DELAY_MS", 1200)
    detail_delay_ms: int = int_env("DETAIL_DELAY_MS", 800)
    output_dir: Path = Path(os.getenv("OUTPUT_DIR", "output"))
    database_url: str = os.getenv("DATABASE_URL", "output/jobradar.sqlite3")
    user_agent: str = os.getenv("USER_AGENT", MOBILE_USER_AGENT)
    viewport_width: int = int_env("VIEWPORT_WIDTH", 390)
    viewport_height: int = int_env("VIEWPORT_HEIGHT", 844)
    telegram_bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    telegram_chat_id: str = os.getenv("TELEGRAM_CHAT_ID", "")
    discord_webhook_url: str = os.getenv("DISCORD_WEBHOOK_URL", "")
    appium_server_url: str = os.getenv("APPIUM_SERVER_URL", "http://127.0.0.1:4723")
    # When Worker runs inside Docker, 127.0.0.1 means the container itself.
    # Set APPIUM_CONNECT_HOST=host.docker.internal so container workers can reach
    # Appium servers running on the Windows host. Keep blank for normal local runs.
    appium_connect_host: str = os.getenv("APPIUM_CONNECT_HOST", "")
    redis_url: str = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    redis_queue_enabled: bool = bool_env("REDIS_QUEUE_ENABLED", bool_env("JOBRADAR_DOCKER_MODE", False))
    redis_key_prefix: str = os.getenv("REDIS_KEY_PREFIX", "jobradar")
    redis_events_maxlen: int = int_env("REDIS_EVENTS_MAXLEN", 500)
    api_url: str = os.getenv("JOBRADAR_API_URL", "http://127.0.0.1:8000")
    api_enabled: bool = bool_env("JOBRADAR_API_ENABLED", bool_env("JOBRADAR_DOCKER_MODE", False))
    host_agent_url: str = os.getenv(
        "JOBRADAR_HOST_AGENT_URL",
        "http://host.docker.internal:8767" if bool_env("JOBRADAR_DOCKER_MODE", False) else "http://127.0.0.1:8767",
    )
    host_agent_enabled: bool = bool_env("JOBRADAR_HOST_AGENT_ENABLED", True)
    host_agent_timeout_seconds: int = int_env("JOBRADAR_HOST_AGENT_TIMEOUT_SECONDS", 5)
    database_backend: str = os.getenv("DATABASE_BACKEND", "auto")
    docker_mode: bool = bool_env("JOBRADAR_DOCKER_MODE", False)
    android_sdk_root: str = os.getenv("ANDROID_SDK_ROOT", os.getenv("ANDROID_HOME", ""))
    adb_path: str = os.getenv("ADB_PATH", "")
    emulator_path: str = os.getenv("EMULATOR_PATH", "")
    appium_command: str = os.getenv("APPIUM_COMMAND", "appium")
    appium_host: str = os.getenv("APPIUM_HOST", "127.0.0.1")
    appium_base_port: int = int_env("APPIUM_BASE_PORT", 4723)
    appium_port_step: int = int_env("APPIUM_PORT_STEP", 2)
    appium_ports: str = os.getenv("APPIUM_PORTS", "")
    usb_appium_port: int = int_env("USB_APPIUM_PORT", 4731)
    appium_system_port_base: int = int_env("APPIUM_SYSTEM_PORT_BASE", 8201)
    appium_mjpeg_port_base: int = int_env("APPIUM_MJPEG_PORT_BASE", 9201)
    appium_chromedriver_port_base: int = int_env("APPIUM_CHROMEDRIVER_PORT_BASE", 9515)
    appium_browser_name: str = os.getenv("APPIUM_BROWSER_NAME", "Chrome")
    appium_browser_package: str = os.getenv("APPIUM_BROWSER_PACKAGE", "com.android.chrome")
    appium_new_command_timeout: int = int_env("APPIUM_NEW_COMMAND_TIMEOUT", 180)
    appium_page_load_timeout: int = int_env("APPIUM_PAGE_LOAD_TIMEOUT", 60)
    appium_step_delay_ms: int = int_env("APPIUM_STEP_DELAY_MS", 1200)
    appium_auto_accept_alerts: bool = bool_env("APPIUM_AUTO_ACCEPT_ALERTS", True)
    appium_chromedriver_autodownload: bool = bool_env("APPIUM_CHROMEDRIVER_AUTODOWNLOAD", True)
    appium_allow_insecure: str = os.getenv("APPIUM_ALLOW_INSECURE", "*:chromedriver_autodownload")
    emulator_console_port_base: int = int_env("EMULATOR_CONSOLE_PORT_BASE", 5554)
    emulator_port_pairs: str = os.getenv("EMULATOR_PORT_PAIRS", DEFAULT_EMULATOR_PORT_PAIRS)
    emulator_gpu_mode: str = os.getenv("EMULATOR_GPU_MODE", "swiftshader_indirect")
    emulator_launch_delay_seconds: int = int_env("EMULATOR_LAUNCH_DELAY_SECONDS", 8)
    emulator_boot_timeout_seconds: int = int_env("EMULATOR_BOOT_TIMEOUT_SECONDS", 240)
    tesseract_cmd: str = os.getenv("TESSERACT_CMD", "")
    worker_poll_seconds: int = int_env("WORKER_POLL_SECONDS", 5)
    worker_id: str = os.getenv("WORKER_ID", "")
    worker_heartbeat_seconds: int = int_env("WORKER_HEARTBEAT_SECONDS", 10)
    worker_stale_after_seconds: int = int_env("WORKER_STALE_AFTER_SECONDS", 180)
    worker_retry_delay_seconds: int = int_env("WORKER_RETRY_DELAY_SECONDS", 30)
    worker_auto_retry: bool = bool_env("WORKER_AUTO_RETRY", True)
    slot_log_tail_lines: int = int_env("SLOT_LOG_TAIL_LINES", 200)
    android_device_name: str = os.getenv("ANDROID_DEVICE_NAME", "Android Emulator")
    android_settings_package: str = os.getenv("ANDROID_SETTINGS_PACKAGE", "com.android.settings")
    android_settings_activity: str = os.getenv("ANDROID_SETTINGS_ACTIVITY", ".Settings")
    emulator_slots: int = int_env("EMULATOR_SLOTS", 5)
    default_campaign_name: str = os.getenv("DEFAULT_CAMPAIGN_NAME", "IT 신입/경력 채용 모니터링")


    def all_appium_ports(self) -> list[int]:
        """Return Appium ports to manage/status-check: Emulator A-D + USB.

        APPIUM_STATUS_PORTS can override the list, for example
        `4723,4725,4727,4729,4731`.  Without override we always include
        the four emulator ports and USB_APPIUM_PORT, even when EMULATOR_SLOTS
        is accidentally set to 4.
        """
        explicit = int_list_env("APPIUM_STATUS_PORTS", [])
        if explicit:
            return explicit
        emulator_count = max(4, min(int(self.emulator_slots or 5), 4))
        ports = [int(self.appium_base_port) + i * int(self.appium_port_step) for i in range(emulator_count)]
        usb_port = int(self.usb_appium_port or (int(self.appium_base_port) + 4 * int(self.appium_port_step)))
        if usb_port > 0 and usb_port not in ports:
            ports.append(usb_port)
        return ports

    def appium_port_slot_name(self, port: int) -> str:
        ports = self.all_appium_ports()
        try:
            index = ports.index(int(port))
        except ValueError:
            return f"Appium {int(port)}"
        if index == 4:
            return "USB Device"
        return f"Emulator {chr(65 + index)}"

    def appium_status_target_count(self) -> int:
        return len(self.all_appium_ports())

    def parsed_appium_ports(self) -> list[int]:
        explicit = parse_int_list(self.appium_ports)
        return explicit or self.all_appium_ports()

    def parsed_emulator_port_pairs(self) -> list[tuple[int, int]]:
        return parse_port_pairs(self.emulator_port_pairs, slots=max(4, min(self.emulator_slots, 4)))

    def ensure_dirs(self) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        (self.output_dir / "logs").mkdir(parents=True, exist_ok=True)
        (self.output_dir / "screenshots").mkdir(parents=True, exist_ok=True)
        (self.output_dir / "sessions").mkdir(parents=True, exist_ok=True)
        db_url = str(self.database_url)
        if not db_url.startswith(("postgresql://", "postgres://")):
            Path(db_url).parent.mkdir(parents=True, exist_ok=True)
