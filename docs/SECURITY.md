# Безопасность «Око»

Правила, защиты в коде и CI, статусы инвариантов и честные пробелы. Формулировки инвариантов — `docs/INVARIANTS.md`
(номера постоянные); доказательства по каждому — `docs/CONTROL_MAP.md`; внешние контуры и хранилища данных —
`docs/SECURITY_AND_DATA.md`; журнал согласий и сторожа тестов — `docs/GUARDS.md`.

## 1. Кто может управлять системой

| граница | где в коде | поведение |
|---|---|---|
| Доступ к боту | `src/bot.py::handle_message` | id отправителя не в `ADMIN_USER_IDS` — сообщение игнорируется; в лог пишутся только id и длина текста |
| Режим хозяина (INV-07) | `src/host_only.py::is_host` | только по техническому id из окружения; текст сообщения роли не играет |
| Окно на ПК | `src/shell_channel.py` | TCP только на loopback-адресе (константа `HOST`, не настраивается), ключ `OKO_SHELL_KEY`; пустой ключ — канал выключен |
| Устройства (INV-05, INV-10) | `src/sigma_gate.py`, `src/tv_bot_handler.py::_execute` | боевой режим Σ — только при `OKO_SIGMA_LIVE=1`; иначе вердикт не останавливает команду |
| Выход в интернет | `src/net_gateway.py` | закрыт, пока нет `OKO_NET_OPEN=1`, и при файле `<data>/net/KILL`; только GET по `https`, белый список `config/net_allowlist.json` |

## 2. Защиты в коде

| защита | где | что делает |
|---|---|---|
| Судья ответов | `src/governor.py` | нераспознанный вердикт, сбой судьи, слишком длинный ответ — ответ не отдаётся (`JUDGE_UNPARSEABLE`, `JUDGE_ERROR`, `JUDGE_TOO_LONG`) |
| Реестр служебных кодов | `src/governor.py::log_service_code` | неизвестный код — `ValueError`; уровень лога берётся только из реестра |
| Гигиена ввода | `src/data_hygiene.py::DataHygieneFilter` | маскирует ключи, токены, реквизиты; в `Incident` — категория и `sha256[:8]`, не сам секрет |
| Отпечатки вместо текста в логах | `src/governor.py::text_sha16`, `src/bot.py` | ответ в лог — длина и `sha16`, не текст |
| Граница каталога данных | `src/path_guard.py` | путь после `Path.resolve()` обязан быть внутри `BASE_DIR`, плюс чёрный список `FORBIDDEN_PATHS` |
| Файлы секретов | `src/secrets_paths.py::secret_path` | токены облаков и пары ТВ — в `<BASE_DIR>/secrets/`, запись атомарно в той же папке |
| Песочница патчей (INV-21) | `src/sandbox_environment.py` | `safe_subprocess_env` чистит секретные переменные, `_copy_filtered` не копирует файлы секретов |
| Журнал согласий | `src/consent.py`, `tools/consent.py` | только дописывание, цепочка `prev_sha16 → entry_sha16`, проверка `verify` |
| Тесты без секретов и сети | `src/conftest.py` | временный `AI_BASE_DIR`, токен `test-token-not-real`, пустой `ADMIN_USER_IDS` |

## 3. Защиты CI (`.github/workflows/pytest.yml`)

- Токен только на чтение: `permissions: contents: read` на весь файл; `persist-credentials: false` у checkout.
- Все `uses:` закреплены полным SHA с тегом в комментарии; `src/test_ci_gate.py::test_every_uses_is_pinned_to_a_full_sha_with_tag_comment`
  ожидает ровно 11 таких строк (3 × 2 в джобах pytest + 5 в `control-map`).
- Сторож ослабления тестов `tools/weakening_guard.py --base HEAD~1`: удалённый тест, меньше проверок, новый skip /
  xfail, шире допуск — красный, если нет строки в `.github/test_weakening_allow.txt`. В `fdbb004` файл есть, но в нём
  только три строки комментария (`#`) и ни одной разрешающей строки, поэтому любая находка — красная. Строка списка:
  `<вид>:<путь>::<функция> | <причина> | <YYYY-MM-DD> | <код согласия>`; список читается из ревизии head (`git show`),
  при отсутствии файла он считается пустым; плохая строка списка или нет родителя `HEAD~1` — код выхода 2 (красный).
- Сторож живых часов в тестах — `src/clock_guard.py`, тест `src/test_clock_guard_b30.py`, исключения —
  `.github/live_clock_allow.txt`.
- Пропуски и xfail сверяются со списками: `.github/known_skips.txt` (14), `.github/known_skips_linux.txt` (16),
  `.github/known_xfail.txt` (20); лишний или пропавший — красный джоб.
- Job `control-map`: карта по JUnit обеих платформ и свежесть `docs/CONTROL_MAP.md`; код выхода верификатора —
  код шага. `src/test_ci_gate.py::_control_map_job_problems` запрещает в этом job `continue-on-error`, `|| true`,
  `secrets.`, `write-all`, `permissions:` и требует оба джоба в `needs`, `if: always()`, `exit $rc`;
  `test_liar_control_map_job_spoiled_is_caught` проверяет, что испорченный job ловится.

## 4. Статус инвариантов

По `docs/CONTROL_MAP.md` на `fdbb004`: 21 инвариант, красная зона — 15, рабочая — 6. Статус по JUnit обеих платформ:
PASS 10 · GAP 10 · MANUAL 1 · FAIL 0; коды ядра (`docs/kernel_codes.txt` против реестра `src/governor.py`) — PASS.
Сама карта отмечает: «внешней сверки этой версии не было». Прогон CI для `fdbb004` (из задачи): `control-map`
`both_platforms=yes EXIT=0`.

## 5. Честные пробелы

| пробел | опора |
|---|---|
| Весь ввод проходит через серверы Telegram раньше любых локальных фильтров. | `docs/SECURITY_AND_DATA.md`, раздел 2 |
| Судья — та же малая модель; противоречие между далёкими кусками длинного ответа не видно. | `src/governor.py`, `docs/SECURITY_AND_DATA.md` |
| Потолок громкости 15 действует только для знаковых команд; «громче» и «громкость 20» им не ограничены. | INV-19, known_gap в `docs/control_map.csv` |
| Без `OKO_SIGMA_SHADOW=1` и `OKO_SIGMA_LIVE=1` вердикт Σ не считается и не пишется вовсе; INV-10 говорит «только считается и пишется в журнал». | `src/tv_bot_handler.py::_execute`, `shadow_enabled` |
| Сбой маршрутизатора отдаёт текст модели (fail-open к модели, не к устройству). | `src/bot.py::_route_reply`, `ROUTE_ERROR` |
| Песочница — не граница безопасности: диск и сеть дочернему процессу доступны; в живых службах не используется. | INV-21, known_gap |
| Порядок «правила раньше модели» в `bot.py` для всех видов сообщений подтверждён только чтением исходника. | INV-02, known_gap |
| SHA снимка, на котором сгенерирована карта, теперь только в артефакте `control-map`; артефакт хранится 7 дней (`retention-days: 7`). | `docs/CONTROL_MAP.md`, `.github/workflows/pytest.yml` |
| Фикстура `src/control_map_junit/` не из прогона CI этого коммита: шапка Linux-файла называет Python 3.13, джоб `pytest-linux` — 3.12; происхождение по репозиторию не проверить. Сверку по настоящим отчётам этого коммита делает только job `control-map`. | `src/control_map_junit/linux.xml`, `.github/workflows/pytest.yml` |
| Удаление документа из базы знаний оставляет резервную копию базы. | `docs/SECURITY_AND_DATA.md`, раздел 2 |
