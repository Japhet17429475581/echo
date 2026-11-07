# Официальные источники и проверенные контракты

Проверка требований и версий: 2026-11-06. Python dependency metadata и sdist получены с PyPI; lock-файлы — результат реального разрешения зависимостей. Цены DeepSeek намеренно не копируются в код.

- [Sherlock: установка](https://sherlockproject.xyz/installation), [исходный код и лицензия](https://github.com/sherlock-project/sherlock), [PyPI](https://pypi.org/project/sherlock-project/0.16.2/). Контракты проверены по установленным `sherlock.py`, `result.py` и пакету resources.
- [Maigret: документация](https://maigret.readthedocs.io/), [исходный код](https://github.com/soxoj/maigret), [PyPI](https://pypi.org/project/maigret/0.6.6/). Контракты проверены по sdist `checking.py`, `sites.py`, `result.py`.
- [Telegram Bot API](https://core.telegram.org/bots/api), [CopyTextButton](https://core.telegram.org/bots/api#copytextbutton), [aiogram](https://docs.aiogram.dev/en/dev-3.x/). CopyTextButton принимает 1–256 символов; для длинного URL предусмотрен fallback. Обычный sendDocument: учитывать 50 MB, приложение ограничивает до 45 МиБ.
- [DeepSeek Chat Completions](https://api-docs.deepseek.com/api/create-chat-completion/), [JSON output](https://api-docs.deepseek.com/guides/json_mode/). Используются `json_object`, `max_tokens`, проверяются `finish_reason` и usage. Default model следует документированному примеру `deepseek-flash`; оператор может заменить модель.
- [SQLAlchemy async](https://docs.sqlalchemy.org/en/20/orm/extensions/asyncio.html), [Alembic](https://alembic.sqlalchemy.org/en/latest/tutorial.html), [PostgreSQL locking](https://www.postgresql.org/docs/17/explicit-locking.html), [Redis Lua atomicity](https://redis.io/docs/latest/develop/programmability/eval-intro/).
- [HTTPX async](https://www.python-httpx.org/async/), [aiohttp connector/resolver](https://docs.aiohttp.org/en/stable/client_reference.html), [Pydantic validation](https://docs.pydantic.dev/latest/).
- [Jinja autoescape](https://jinja.palletsprojects.com/en/stable/api/#autoescaping), [ReportLab paragraphs](https://docs.reportlab.com/reportlab/userguide/ch6_paragraphs/), [age](https://github.com/FiloSottile/age), [Public Suffix List](https://publicsuffix.org/).

Официальные источники восьми начальных описаний перечислены в `src/echo/data/sites.json`. Тексты кратко составлены человеком для объяснения назначения сервисов; они не описывают конкретного владельца профиля.

## Локальные инструменты и сетевые модули

- [ExifTool CLI](https://exiftool.org/exiftool_pod.html): `-config`, `-json`, `-G1`, `-a`, `-s`, `-n`; фактическая версия 12.57 проверена в образе. [Debian source/copyright](https://sources.debian.org/src/libimage-exiftool-perl/).
- [Tesseract CLI](https://tesseract-ocr.github.io/tessdoc/Command-Line-Usage.html), [FAQ/OpenMP](https://tesseract-ocr.github.io/tessdoc/FAQ.html), [tessdata_fast](https://github.com/tesseract-ocr/tessdata_fast). Проверены `--psm 3`, `-l rus+eng`, `OMP_THREAD_LIMIT=1` и фактические eng/rus.
- [ZXing-C++ Python bindings](https://github.com/zxing-cpp/zxing-cpp/tree/master/wrappers/python): реальные `read_barcodes(..., formats=BarcodeFormat.QRCode)` и создание собственных fixture-кодов.
- [WhatWeb README/исходники](https://github.com/urbanadventurer/WhatWeb), [Debian 0.5.5](https://sources.debian.org/src/whatweb/): реальный offline filename, `--aggression=1`, `--max-threads=1`, обычный `--log-json`. Актуальный upstream и формат verbose JSON не подставляются вместо установленной версии.
- [dnspython async resolver](https://dnspython.readthedocs.io/en/stable/async-resolver-class.html), [Python SSL](https://docs.python.org/3/library/ssl.html): `create_default_context`, SNI, chain/hostname verification.
- [Trafilatura core functions](https://trafilatura.readthedocs.io/en/latest/corefunctions.html): `bare_extraction(..., with_metadata=True)` проверена с установленной 2.3.1 и структурированным Document.
- [dnstwist source/license](https://github.com/elceef/dnstwist), [зафиксированный PyPI release](https://pypi.org/project/dnstwist/20250130/): `Fuzzer.generate()` и `permutations()` вместо запуска сетевого scanner.
- [Pillow image API и limits](https://pillow.readthedocs.io/en/stable/reference/Image.html), [EXIF transpose](https://pillow.readthedocs.io/en/stable/reference/ImageOps.html#PIL.ImageOps.exif_transpose).
- [Linux Landlock](https://docs.kernel.org/userspace-api/landlock.html), [seccomp](https://man7.org/linux/man-pages/man2/seccomp.2.html): проверены настоящие запреты socket и доступа вне scratch; изоляция отключает модули при невозможности установки.

Параметры проверялись по реальным установленным пакетам, CLI и локальным успешным smoke, не только документации. Версии, лицензии, алгоритм обновления и воспроизведение — в [modules.md](modules.md), [THIRD_PARTY_LICENSES.md](../THIRD_PARTY_LICENSES.md) и lock-файлах.
