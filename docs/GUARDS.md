# Предохранители: журнал согласий, сторож ослабления тестов, сторож живых часов

## Журнал согласий (`src/consent.py`, `tools/consent.py`)
- Решения владельца ([1] / [0]) — машинные строки в отдельной базе `data/state/consent.db` (переменная
  `OKO_CONSENT_DB`): время ответа, источник (`telegram` / `chat` / `window`), id вопроса, ответ, sha16 формулировки
  вопроса (сам текст не хранится), короткий код вопроса, связь с задачей (`B30`), происхождение (`live` / `retro_summary`).
- Отдельная база, а не таблица метрик: разрешение на действие — не метрика; только дописывание, своя копия и права.
- Цепочка `prev_sha16 → entry_sha16`: правка или удаление строки задним числом видны в `verify`.
- `verify --require-task T --require-code C`: GO (код 0) только при целой цепочке и последней записи (T, C) с ответом 1;
  иначе ASK (код 3) — блок не действует.

## Сторож ослабления тестов (`tools/weakening_guard.py`, шаг CI)
- Снимок сравнивается с опубликованной базой (B33 v2: `--base auto` = `git merge-base HEAD origin/oko-live`, checkout с
  `fetch-depth: 0`; базы нет — красный, без молчаливого `HEAD~1`) по AST тестовых файлов: удалённый файл,
  удалённая функция `test_*`, меньше проверок в функции, новый skip / xfail / importorskip, шире допуск
  (`pytest.approx` rel / abs, `math.isclose`, `timeout`), исчезнувшее значение `@pytest.mark.parametrize`
  (`changed_param`: значение элемента, у `pytest.param` — аргументы и `marks=`, без `id=`; так ловится подмена
  `"111"` -> `pytest.param("abc", id="111")` при том же node ID); с `--collect` — исчезнувший собранный node ID.
  `assert` с константой (`assert True`, `assert 1`,
  `assert "x"`, `assert x or True`) проверкой не считается: замена проверки на него — «меньше проверок» (B33).
- Охраняемая зона (B33): `conftest.py` (любой), `pytest.ini`, `docs/control_map.csv`, `config/net_direct_registry.json`,
  `.github/workflows/`, `src/weakening_guard.py`, `tools/weakening_guard.py`, `tools/verify_control_map.py`,
  `src/test_ci_gate.py`, `tools/guard_mutation_smoke.py` — любая правка = находка `guarded_change`; в карте ещё
  `map_test_removed` (исчез node ID из `tests`), `map_new_known_gap` (новый `known_gap`), `map_pass_drop` (меньше PASS в
  `docs/CONTROL_MAP.md`). Ключ находки зоны несёт blob файла (`...@<blob12>`): разрешение действует только на эту версию.
- Любая находка — красный, если нет строки согласия: ослабления тестов — в `.github/test_weakening_allow.txt`, зона —
  в `.github/guarded_change_allow.txt`. Строка: `<ключ> | <причина> | <дата> | <код согласия> | <entry_sha16>`
  (`entry_sha16` — поле записи журнала согласий). Нет опубликованной базы, плохая строка, повтор ключа или `entry_sha16` — красный.
- **Что проверяется где — честно.** CI проверяет ТОЛЬКО формат и уникальность строки согласия: журнала согласий в CI
  нет, и строку может дописать тот же коммит. Сверка с журналом — на ПК: `push_*_stage` перед пушем вызывает
  `tools/consent_lines.py` (строка, добавленная в блоке, без записи [1] с этим `entry_sha16` в `consent.db` — STOP, пуша нет).
- Сторож в CI запускается дважды: кодом снимка и кодом опубликованной базы (`git show <merge-base>:...` во временную папку) —
  правка сторожа не разрешает сама себя и действует со следующего снимка.
- Мутационный smoke `tools/guard_mutation_smoke.py` (шаг CI): известные порчи красной зоны (три аудитные: `assert True`
  в тесте INV-19, 1 тест из 5 у INV-19, произвольный `known_gap` у INV-05; плюс правки зоны) обязаны дать находку.

## Сторож живых часов (`src/clock_guard.py`, тест `src/test_clock_guard_b30.py`)
- Вызов `datetime.now / today / utcnow`, `date.today`, `time.time / monotonic / perf_counter` в тестовом файле без
  заморозки (monkeypatch / mock.patch целей часов, фикстура заморозки) — красный.
- Исключения — `.github/live_clock_allow.txt`: `<путь>::<функция> | <число вызовов> | <причина> | <дата>`; больше
  вызовов, чем в строке, — красный; строка без нарушения — устаревшая (красный).
