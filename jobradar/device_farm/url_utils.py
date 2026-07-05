from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse


def _running_inside_container() -> bool:
    return Path("/.dockerenv").exists() or os.getenv("JOBRADAR_FORCE_DOCKER_URLS", "").strip().lower() in {"1", "true", "yes", "on"}


def resolve_appium_url(settings: Any, raw_url: str) -> str:
    """Resolve an Appium URL for local Windows or Docker worker execution.

    Local Windows run:
      http://127.0.0.1:4723 stays local.

    Docker run:
      localhost/127.0.0.1 URLs are rewritten to APPIUM_CONNECT_HOST, usually
      host.docker.internal, so container workers reach Appium on Windows host.

    Safety guard:
      If .env accidentally keeps APPIUM_CONNECT_HOST=host.docker.internal on
      the Windows host, local CLI health checks are rewritten back to 127.0.0.1
      instead of timing out.
    """
    url = (raw_url or getattr(settings, "appium_server_url", "") or "http://127.0.0.1:4723").rstrip("/")
    connect_host = (getattr(settings, "appium_connect_host", "") or "").strip()
    in_container = _running_inside_container()

    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()

        # Host-side PowerShell/Python should not call host.docker.internal for
        # Appium. On Windows host, Appium is bound to 127.0.0.1.
        if not in_container and host == "host.docker.internal":
            netloc = "127.0.0.1"
            if parsed.port:
                netloc = f"127.0.0.1:{parsed.port}"
            return urlunparse((parsed.scheme or "http", netloc, parsed.path or "", parsed.params, parsed.query, parsed.fragment)).rstrip("/")

        if not in_container or not connect_host:
            return url

        if host not in {"127.0.0.1", "localhost", "0.0.0.0"}:
            return url

        netloc = connect_host
        if parsed.port:
            netloc = f"{connect_host}:{parsed.port}"
        return urlunparse((parsed.scheme or "http", netloc, parsed.path or "", parsed.params, parsed.query, parsed.fragment)).rstrip("/")
    except Exception:
        return url
