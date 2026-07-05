from __future__ import annotations

from pathlib import Path

from appium import webdriver
from appium.options.android import UiAutomator2Options
from appium.webdriver.common.appiumby import AppiumBy

from jobradar.config import Settings


def open_android_settings_and_read_texts(settings: Settings, device_name: str | None = None) -> list[str]:
    options = UiAutomator2Options()
    options.platform_name = "Android"
    options.automation_name = "UiAutomator2"
    options.device_name = device_name or settings.android_device_name
    options.app_package = settings.android_settings_package
    options.app_activity = settings.android_settings_activity

    driver = webdriver.Remote(settings.appium_server_url, options=options)
    try:
        driver.implicitly_wait(5)
        elements = driver.find_elements(AppiumBy.CLASS_NAME, "android.widget.TextView")
        return [el.text for el in elements if el.text][:20]
    finally:
        driver.quit()


def capture_appium_screenshot(settings: Settings, output_path: Path, device_name: str | None = None) -> Path:
    options = UiAutomator2Options()
    options.platform_name = "Android"
    options.automation_name = "UiAutomator2"
    options.device_name = device_name or settings.android_device_name
    options.app_package = settings.android_settings_package
    options.app_activity = settings.android_settings_activity

    output_path.parent.mkdir(parents=True, exist_ok=True)
    driver = webdriver.Remote(settings.appium_server_url, options=options)
    try:
        driver.implicitly_wait(5)
        driver.get_screenshot_as_file(str(output_path))
        return output_path
    finally:
        driver.quit()
