from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from jobradar.config import Settings


# 명령 결과 클래스는 처리 결과와 상태 정보를 한곳에 담아 전달합니다.
@dataclass
class CommandResult:
    code: int
    stdout: str
    stderr: str
    command: list[str]

    # 명령 실행 결과가 성공인지 쉽게 확인할 수 있게 합니다.
    @property
    def ok(self) -> bool:
        return self.code == 0

    # 긴 실행 결과에서 핵심 메시지만 뽑아 보기 쉽게 정리합니다.
    def summary(self) -> str:
        out = (self.stdout or self.stderr or "").strip()
        return out if out else f"exit={self.code}"


def _candidate_roots(settings: Settings) -> list[Path]:
    roots: list[Path] = []
    for value in [
        getattr(settings, "android_sdk_root", ""),
        os.getenv("ANDROID_SDK_ROOT", ""),
        os.getenv("ANDROID_HOME", ""),
    ]:
        if value:
            roots.append(Path(value).expanduser())
    if sys.platform.startswith("win"):
        local = os.getenv("LOCALAPPDATA", "")
        if local:
            roots.append(Path(local) / "Android" / "Sdk")
    else:
        roots.extend([Path.home() / "Library" / "Android" / "sdk", Path.home() / "Android" / "Sdk"])
    # preserve order, drop duplicates
    unique: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        key = str(root)
        if key not in seen:
            unique.append(root)
            seen.add(key)
    return unique


def find_executable(settings: Settings, name: str, explicit: str = "") -> str:
    """Find adb/emulator/appium while supporting Windows and non-PATH SDK installs."""
    if explicit:
        path = Path(explicit).expanduser()
        if path.exists():
            return str(path)
    found = shutil.which(name)
    if found:
        return found
    suffixes = [name]
    if sys.platform.startswith("win") and not name.lower().endswith(".exe"):
        suffixes.insert(0, f"{name}.exe")
        suffixes.append(f"{name}.cmd")
    for root in _candidate_roots(settings):
        candidates = []
        if name.startswith("adb"):
            candidates = [root / "platform-tools" / suffix for suffix in suffixes]
        elif name.startswith("emulator"):
            candidates = [root / "emulator" / suffix for suffix in suffixes]
        elif name.startswith("appium"):
            candidates = [Path(suffix) for suffix in suffixes]
        for candidate in candidates:
            if candidate.exists():
                return str(candidate)
    return explicit or name


def run_command(command: Sequence[str], timeout: int = 30, cwd: str | Path | None = None) -> CommandResult:
    cmd = [str(part) for part in command]
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return CommandResult(proc.returncode, proc.stdout or "", proc.stderr or "", cmd)
    except FileNotFoundError as exc:
        return CommandResult(127, "", str(exc), cmd)
    except subprocess.TimeoutExpired as exc:
        return CommandResult(124, exc.stdout or "", exc.stderr or "timeout", cmd)


def start_detached(command: Sequence[str], stdout_path: Path | None = None, stderr_path: Path | None = None) -> tuple[int | None, str]:
    cmd = [str(part) for part in command]
    try:
        stdout_handle = open(stdout_path, "a", encoding="utf-8") if stdout_path else subprocess.DEVNULL
        stderr_handle = open(stderr_path, "a", encoding="utf-8") if stderr_path else subprocess.DEVNULL
        flags = 0
        if sys.platform.startswith("win"):
            flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS  # type: ignore[attr-defined]
        proc = subprocess.Popen(
            cmd,
            stdout=stdout_handle,
            stderr=stderr_handle,
            stdin=subprocess.DEVNULL,
            creationflags=flags,
            close_fds=not sys.platform.startswith("win"),
        )
        return proc.pid, "started"
    except Exception as exc:
        return None, str(exc)
