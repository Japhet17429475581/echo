# Разработка Echo

Не добавляйте обход CAPTCHA/авторизации, утечки, идентификацию человека по совпадениям, извлечение новых поисковых идентификаторов, Tor или отправку профилей/заметок в AI.

Python 3.12. `uv sync --frozen --extra dev`. Все изменения контрактов движков проверяйте с их зафиксированными отдельными окружениями. Тесты используют синтетический ник, локальный HTTP-сервер, подмену Telegram/DeepSeek и изолированные схемы PostgreSQL. Не используйте реальные персональные поиски или платные ключи в CI.

```sh
uv run --frozen ruff check main.py src engines tools tests scripts migrations
uv run --frozen ruff format --check main.py src engines tools tests scripts migrations
uv run --frozen pytest -q
docker compose -f compose.test.yaml up --build --abort-on-container-exit --exit-code-from tests
```

После изменения `pyproject.toml` обновите `uv.lock`; после изменения engine/tools requirements — соответствующие хэшированные locks. Миграции должны быть самостоятельными, не импортировать изменяемую ORM-схему для создания таблиц. Пользовательские строки редактируются в `src/echo/data/locales.json` и `modules-locales.json`; оба языка обязательны.

Новые native-сценарии проверяет `tests/test_tools_smoke.py` в тестовом Docker с `ECHO_TEST_TOOLS=1`: собственные OCR/QR/EXIF, локальные HTTP/TLS, mock DNS, реальная изоляция/отмена/таймаут, папки, manifest и age. Не убирайте kernel isolation для тестов; тестовый HTTP resolver разрешён только локальному fixture. Профили, версии и измерения описаны в `docs/modules.md` и `docs/verification.md`.

Обязательно покрывайте изменения в нормализации, владении, lease fencing, удалении, SSRF, escaping и бюджетах содержательными тестами. Не записывайте токены и реальные результаты в fixtures, issues или логи. Не публикуйте проект и не запускайте публичного бота без поручения владельца.

Проверяйте лицензии новых зависимостей и обновляйте уведомления. Echo не связан официально с upstream-проектами.
