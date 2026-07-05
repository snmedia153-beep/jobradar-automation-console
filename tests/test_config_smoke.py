from jobradar.config import Settings


def test_settings_loads_with_defaults():
    settings = Settings()
    assert settings.max_items > 0
    assert settings.output_dir is not None
