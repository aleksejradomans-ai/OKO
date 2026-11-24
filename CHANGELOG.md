# Changelog

Формат — [Keep a Changelog](https://keepachangelog.com/ru/1.1.0/). Новые блоки сверху. Отсчёт истории — с нулевого
среза документации на `fdbb004`; более ранние изменения в этом файле не описаны.

## [B32h] — fdbb004

### Added
- CI: третья проверка — job `control-map` после `pytest-windows` и `pytest-linux`: `tools/verify_control_map.py` по
  отчётам JUnit обеих платформ и сверка `docs/CONTROL_MAP.md` со свежей копией; итог — аннотация
  `both_platforms=yes|no EXIT=<n>`, отчёты — артефакт `control-map`.
- Фикстура `src/control_map_junit/{windows,linux}.xml`; `src/test_verify_control_map.py` сверяет настоящую карту по
  ней и добавляет тесты-лжецы (испорченная карта, отчёт одной платформы, провал теста красной зоны на одной платформе).
- `src/test_ci_gate.py`: проверка состава job `control-map` и тест-лжец на испорченный job.

### Security
- Шапка `docs/CONTROL_MAP.md` больше не содержит SHA непубличного коммита: SHA снимка — в артефакте `control-map`.
