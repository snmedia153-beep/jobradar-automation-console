from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

from jobradar.config import Settings
from jobradar.device_farm.runtime import find_executable, start_detached


def appium_command(settings: Settings) -> str:
    return find_executable(settings, "appium", getattr(settings, "appium_command", ""))


def appium_url(host: str, port: int) -> str:
    return f"http://{host}:{port}"


def check_appium_status(url: str, timeout: int = 3) -> tuple[bool, str]:
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/status", timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="ignore")
        try:
            parsed = json.loads(raw)
            return True, parsed.get("value", {}).get("message") or "Appium status OK"
        except Exception:
            return True, raw[:200]
    except urllib.error.URLError as exc:
        return False, str(exc)
    except Exception as exc:
        return False, str(exc)


def start_appium(settings: Settings, port: int, host: str = "127.0.0.1", log_name: str | None = None) -> tuple[int | None, str]:
    cmd = [appium_command(settings), "--address", host, "--port", str(port)]
    allow_insecure = str(getattr(settings, "appium_allow_insecure", "") or "").strip()
    # Appium 3 requires a driver/wildcard prefix, e.g. *:chromedriver_autodownload.
    if allow_insecure == "chromedriver_autodownload":
        allow_insecure = "*:chromedriver_autodownload"
    if allow_insecure:
        cmd.extend(["--allow-insecure", allow_insecure])
    log_path = settings.output_dir / "logs" / (log_name or f"appium_{port}.log")
    return start_detached(cmd, stdout_path=log_path, stderr_path=log_path)
