import json
from importlib.resources import files

MESSAGES = json.loads(files("echo.data").joinpath("locales.json").read_text("utf-8"))
MESSAGES.update(json.loads(files("echo.data").joinpath("modules-locales.json").read_text("utf-8")))
MESSAGES.update(json.loads(files("echo.data").joinpath("journeys-locales.json").read_text("utf-8")))


def tr(locale: str, key: str, **values) -> str:
    locale = locale if locale in {"ru", "en"} else "ru"
    return MESSAGES.get(key, {"ru": key, "en": key})[locale].format(**values)
