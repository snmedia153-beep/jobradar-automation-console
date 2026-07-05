from types import SimpleNamespace

from jobradar.device_farm.url_utils import resolve_appium_url


def test_resolve_appium_url_keeps_local_url():
    settings = SimpleNamespace(appium_connect_host="")
    url = resolve_appium_url(settings, "http://127.0.0.1:4723")
    assert url == "http://127.0.0.1:4723"
