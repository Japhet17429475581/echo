# Движки и обновление

Закреплены `sherlock-project==0.16.2` и `maigret==0.6.6`. Библиотеки и их манифесты берутся из установленных пакетов, не скачиваются при каждом поиске. Python 3.12 поддерживается обоими. Движки находятся в `/opt/sherlock` и `/opt/maigret`, отдельные lock-файлы содержат полное дерево с хэшами.

Sherlock вызывается через `sherlock_project.sherlock.sherlock` с `QueryNotify`; читаются реальные `QueryResult` и словари результатов. Response metadata извлекаются из уже завершённого `request_future`, сохранённого самой библиотекой. Maigret вызывается через `maigret.checking.maigret`, с `is_parsing_enabled=False`, `is_enrich_enabled=False`, `check_domains=False`, `retries=0`, без cookies и bypass. Небольшой транспортный subclass сохраняет HTTP metadata через aiohttp TraceConfig, оставляя правила обнаружения upstream; включает проверку TLS и отключает environment proxies/cookies. Этот version-sensitive участок покрыт локальными контрактными тестами.

Для каждой площадки JSONL мост отдаёт status/version/URL, HTTP-код, final URL, Retry-After, тип проверки и только безопасную категорию ошибки. Движки не передают подробное основание отдельным структурированным полем: `evidence=null`, в интерфейсе — соответствующее предупреждение. Ответы страниц не сохраняются. Расхождение движков показывается отдельно, без «процента уверенности».

Источники с Tor/I2P, mirrors, fuzzy search, activation и TLS impersonation не включаются. Встроенный транспорт Maigret имеет один дополнительный повтор для некоторых разрывов соединения/повреждений ответа; повтор движком полного списка ошибок отключён. Echo не повторяет блокировки. Пользовательский повтор временных ошибок создаёт отдельную задачу с точными upstream site keys. Она ожидает доступный Retry-After (секунды или HTTP-date, максимум час) и ограниченный exponential backoff с jitter; неизвестный Retry-After не выдумывается.

## Обновление

1. Изучите release notes, лицензии и изменения библиотечных контрактов/манифестов в официальных репозиториях.
2. Измените версии в `engines/*/requirements.in`.
3. Пересоздайте locks:

```sh
uv pip compile engines/sherlock/requirements.in --generate-hashes --universal --python-version 3.12 -o engines/sherlock/requirements.lock --quiet
uv pip compile engines/maigret/requirements.in --generate-hashes --universal --python-version 3.12 -o engines/maigret/requirements.lock --quiet
```

4. Обновите контрактные ожидания версий и проверьте `engines/runner.py`: форматы статусов, transports, параметры, правила исключения.
5. Выполните полный `compose.test.yaml`: обе библиотеки реально обращаются к локальному серверу для 200/404/403/429/503/redirect. Затем соберите runtime.
6. Проверьте лицензионные уведомления. Не удаляйте `.dist-info`/copyright файлов из образа.
7. Выполните небольшой ручной поиск только собственного/согласованного публичного ника после развёртывания; не заменяйте им автоматические локальные проверки.

После изменения манифестов точный retry может больше не содержать прежнюю площадку. Echo сообщает `targets_unavailable`; исходные наблюдения сохраняются. Обновление движков никогда не доказывает надёжность каждого сайта: они меняют свои ответы независимо от Echo.
