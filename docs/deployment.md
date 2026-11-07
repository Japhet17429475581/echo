# Самостоятельное развёртывание

## Подготовка

Docker с Linux containers и Compose v2.20+; ориентир для одного worker — 4 ГБ RAM и несколько ГБ диска. Нужен исходящий HTTPS к Telegram, публичным площадкам и, при включении AI, DeepSeek, а также доступ к вашему серверу PostgreSQL. Входящие порты не нужны. Не запускайте несколько polling-копий `bot` с одним токеном; масштабируйте только worker.

Скопируйте `.env.example` в `.env`. Обязательные значения: `ECHO_BOT_TOKEN` и готовая строка PostgreSQL `ECHO_DATABASE_URL` от вашего провайдера. Поддерживаются схемы `postgres://`, `postgresql://`, `postgresql+asyncpg://`; параметры `sslmode` и другие поддержанные asyncpg DSN-опции передаются драйверу без преобразования в неизвестные аргументы. Вставляйте закодированный URL провайдера, а не составляйте его из отдельных паролей. Настройте `ECHO_ADMINS` списком числовых Telegram ID через запятую. Для DeepSeek достаточно `ECHO_AI_KEY`; адрес и модель заданы по умолчанию, а разрешение AI включается отдельным флагом. Бренд по умолчанию Echo; Telegram-имя и username бота меняются отдельно через BotFather.

```sh
docker compose config --quiet
docker compose up -d --build
docker compose ps
docker compose logs --tail=50 migrate worker bot
docker compose exec worker python -m echo.main health --worker
docker compose exec bot python -m echo.main admin
```

Не публикуйте вывод `docker compose config`: он содержит подставленные секреты. Полная конфигурация проверяется через `--quiet`.

Compose запускает Redis во внутренней сети без `ports`, затем миграции в вашей PostgreSQL и приложения. Отдельная локальная БД по умолчанию не запускается. Redis AOF и `noeviction` сохраняют лимиты; при нехватке памяти запросы останавливаются, лимиты не обходятся. Для Redis нужна отдельная защищённая сеть; при внешнем Redis настройте TLS/аутентификацию через URI и сетевые правила.

### Необязательная локальная PostgreSQL

Для собственной PostgreSQL из этого Compose включите профиль `local-db`. Только для её первоначальной инициализации задайте `LOCAL_POSTGRES_PASSWORD` и используйте тот же пароль в `ECHO_DATABASE_URL=postgresql://echo:ENCODED_PASSWORD@postgres:5432/echo`. `LOCAL_POSTGRES_PASSWORD` содержит обычный пароль, а в URL спецсимволы кодируются. Запуск: `docker compose --profile local-db up -d --build`. Миграции дождутся здоровья этой БД. После инициализации изменение переменной не меняет пароль существующей БД автоматически.

При переходе со старой конфигурации замените отдельный `POSTGRES_PASSWORD` на `ECHO_DATABASE_URL`. Если продолжаете использовать прежний локальный volume, включите `local-db` и укажите прежний пароль. Существующие volumes не удаляются и сами во внешнюю БД не переносятся.

## Управление

```sh
docker compose -f compose.yaml -f compose.standard.yaml up -d --scale worker=2
docker compose restart bot worker
docker compose exec worker python -m echo.main cleanup
docker compose exec worker python -m echo.main catalog-list
docker compose down
```

Остановка даёт worker время завершить subprocess. При принудительном обрыве задача возобновляется после истечения lease. `down` сохраняет именованные volumes. Для обновления сначала сделайте резервную копию, соберите новый образ и выполните миграции; не запускайте старый код с несовместимой новой схемой.

## Безопасные источники каталога

Администратор импортирует массив JSON командой `catalog-import` (путь внутри контейнера, файл можно скопировать через `docker compose cp`). Поля описаны в `CatalogEntry` в `main.py`: `id`, `language`, `name`, `domains`, `category`, `description`, `activities`, `sources`, `human_reviewed`. `human_reviewed=false` разрешает генерацию по официальному источнику. Домены источников, включая корпоративные About-домены, должны присутствовать в `domains`. Не добавляйте в aliases общие хостинги или пользовательские страницы.

`catalog-refresh ID --language ru` обновляет доступную AI-запись; ручная запись защищена. Импорт — доверенное административное действие; проверяйте источник и права на текст. AI-каталог загружает только одобренные источники. Сетевые инструменты обрабатывают явно подтверждённый публичный URL через общий защищённый fetch.

## Контейнеры и сеть

Приложения работают как UID 10001, без Linux capabilities, с `no-new-privileges`, `read_only`, tmpfs `/tmp` и файловым volume `/data/echo`. PDF и ZIP используют ограниченный scratch, остальные простые отчёты — память. Для HTTP aiohttp проверяет DNS, передаёт проверенные IP непосредственно connector, проверяет каждый redirect и ограничивает адреса/порты/размер/время; cookies, proxies, JS и дополнительные ресурсы отключены. Локальные новые обработчики и WhatWeb дополнительно закрыты Landlock/seccomp; без поддержки ядра модули не включаются. Для установки на недоверенной инфраструктуре дополнительно применяйте egress firewall к Интернету, запрещающий RFC1918, metadata и служебные сети; разрешения на PostgreSQL/Redis оставляйте только приложению. Не предоставляйте движкам Docker socket.

Базовые образы Python 3.12, PostgreSQL 17, Redis 8 и uv закреплены проверенными SHA256 digests. Обновляйте их вместе с проверками; при использовании собственного registry сохраняйте эту фиксацию. Python-зависимости закреплены полностью. При изменении системного образа проверяйте `age --version`, шрифт PDF и полный smoke.

Профиль eco включён по умолчанию. Подробные бюджеты RAM, CPU, диска, ограничения каждого инструмента, проверка kernel isolation и обновление зависимостей — в [modules.md](modules.md). Standard с двумя workers ориентирован на 8 ГБ RAM; один дополнительный worker не обходит распределённый лимит eco. Не смешивайте настройки профилей между репликами.
