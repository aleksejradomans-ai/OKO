# Changelog

Формат — [Keep a Changelog](https://keepachangelog.com/ru/1.1.0/). Новые блоки сверху. Отсчёт истории — с нулевого
среза документации на `fdbb004`; более ранние изменения в этом файле не описаны.

## [B33] — ed10936

### Added
- `src/sigma_live.py`: единая точка записи в ТВ под шлюзом Σ (`tv_client`, `Guarded`, `refusal`); при `OKO_SIGMA_LIVE=1`
  пути мимо шлюза отказывают: `/cam calib auto`, повтор пробуждения, мосты `amb_music` и `audio_bridge`, `tv_control` (CLI),
  `tools/cam_latency.py`, `tools/amb_off_probe.py`, `src/tv_probe.py`, `src/tv_snapshot.py`, `src/tv_pair.py`, `tools/tv_wol_probe.py`.
- Служебный код `SIGMA_MODULE_MISSING` (красный, 15-й в реестре); `/status` первой строкой сообщает о боевом Σ без модуля.
- Охраняемая зона сторожа ослабления тестов: `.github/guarded_change_allow.txt`, виды `guarded_change`, `map_test_removed`,
  `map_new_known_gap`, `map_pass_drop`; `changed_param`, `--collect`; `tools/guard_mutation_smoke.py`, `tools/impl_mutation_smoke.py`.
- `tools/consent_lines.py` и `consent.find_entries`: сверка `entry_sha16` строк согласия с журналом (на ПК, не в CI).

### Changed
- CI: checkout `fetch-depth: 0`; база сторожа и дельты карты — опубликованный коммит (`merge-base HEAD origin/oko-live`), а не
  `HEAD~1`; сторож запускается кодом снимка и кодом базы; верификатор карты — `--base-rev auto --require-base`.
- `src/test_ci_gate.py`: job `control-map` сверяется с эталоном (sha256), любое `if:` кроме `always()` — находка.
- `CJK_GUARD`: повтор идёт через судью; не PASS — отдаётся `cjk_strip` исходного текста. Вывод верификатора карты — на английском.

### Security
- `tools/notify.py`: адресат только `OKO_HOST_USER_ID` из `ADMIN_USER_IDS`; расширенная маскировка путей, UNC, `ДОМЕН\пользователь`,
  длинных hex; файл-рубильник `<data>/net/KILL`.
- INV-05 в `docs/CONTROL_MAP.md` — GAP (новый `known_gap`); итог карты: PASS 9 · GAP 11 · MANUAL 1 · FAIL 0.

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
