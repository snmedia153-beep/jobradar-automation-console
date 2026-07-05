from __future__ import annotations

import argparse
from pathlib import Path

from jobradar.appium_agent.adb_manager import list_adb_devices, take_adb_screenshot
from jobradar.appium_agent.appium_probe import capture_appium_screenshot, open_android_settings_and_read_texts
from jobradar.config import Settings


# 명령줄에서 프로그램이 시작될 때 실행되는 진입점입니다.
def main() -> None:
    parser = argparse.ArgumentParser(description="Local Android Emulator/Appium helper")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("devices", help="List adb devices")

    adb_ss = sub.add_parser("adb-screenshot", help="Take screenshot through adb")
    adb_ss.add_argument("--device", required=True)
    adb_ss.add_argument("--output", default="output/screenshots/adb_screenshot.png")

    appium_text = sub.add_parser("settings-text", help="Open Android settings through Appium and print TextViews")
    appium_text.add_argument("--device-name", default=None)

    appium_ss = sub.add_parser("settings-screenshot", help="Open Android settings and save screenshot through Appium")
    appium_ss.add_argument("--device-name", default=None)
    appium_ss.add_argument("--output", default="output/screenshots/appium_settings.png")

    args = parser.parse_args()
    settings = Settings()
    settings.ensure_dirs()

    if args.command == "devices":
        for device in list_adb_devices():
            print(f"{device.device_id}\t{device.status}")
    elif args.command == "adb-screenshot":
        ok, message = take_adb_screenshot(args.device, args.output)
        print("OK" if ok else "FAIL", message)
    elif args.command == "settings-text":
        texts = open_android_settings_and_read_texts(settings, device_name=args.device_name)
        for text in texts:
            print(text)
    elif args.command == "settings-screenshot":
        path = capture_appium_screenshot(settings, Path(args.output), device_name=args.device_name)
        print(path)


if __name__ == "__main__":
    main()
