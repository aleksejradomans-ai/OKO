"""test_menu_syntax.py — тесты разбора человеческих имён меню ТВ (MENU_SYNTAX, 05.11.2026).

Проверяет:
1. Дрейф карты: все 80 целей из tv_menu_aliases.json существуют в src/tv_menu_map.json
   и строят маршрут через tv_menu_map.route().
2. Решение Оператора 05.11 17:50: «выключи экран», «погаси экран», «чёрный экран» ведут в
   settings.general.power:Выкл. экран (press=True, 3 нажатия Confirm), а «выключи телевизор» / «выключи тв»
   НЕ перехватываются меню.
3. Регрессия 18 транслит-фраз (мягкий знак «j» после согласной в конце слога/слова + «dolby atmos naushniki»).
4. Позитивные фразы (400 фраз = 80 целей × 5, включая транслит и опечатку) -> ровно своя цель.
5. Негативные фразы (240 фраз = 80 целей × 3 соседей) -> НЕ попадают в эту цель.
6. Блок must_not_steal (30 живых фраз из test_tv_commands.py, test_tv_commands_ambi.py,
   test_amb_music.py, test_tv_nav.py) -> menu_syntax.parse возвращает None, прежние парсеры работают.
7. Блок ambiguous (13 фраз) -> menu_clarify (kind="local", список целей и вопрос уточнения).
8. Порядок слоёв: tv_nav.parse (пользовательские макросы MacroStore и серии кнопок) -> menu_syntax.parse.
"""
from __future__ import annotations

import pytest

import menu_syntax as MS
import tv_menu_map as TM
import tv_nav as TN

DATA = MS.load_aliases()
MAP = TM.load()

POSITIVE_CASES = [
    (entry["target"], phrase)
    for entry in DATA["aliases"]
    for phrase in entry["positive"]
]

NEGATIVE_CASES = [
    (entry["target"], phrase)
    for entry in DATA["aliases"]
    for phrase in entry["negative"]
]

MUST_NOT_STEAL_CASES = [
    (item["phrase"], item["file"], item["test"])
    for item in DATA["must_not_steal"]
]

AMBIGUOUS_CASES = [
    (item["phrase"], tuple(item["targets"]), item["ask"])
    for item in DATA["ambiguous"]
]

# 18 транслит-фраз, упавших в первом облачном прогоне (17 с мягким знаком «j» после согласной + «dolby atmos naushniki»):
TRANSLIT_18_CASES = [
    ("settings.sound:Стиль звука", "stilj zvuka"),
    ("settings.sound:Пользовательские нас...", "poljzovateljskij zvuk"),
    ("settings.sound:Дополнительные", "dopolniteljnije v zvuke"),
    ("settings.sound:Профиль звука", "profilj zvuka"),
    ("settings.sound:Dolby Atmos для науш...", "dolby atmos naushniki"),
    ("settings.ambilight:Стиль Ambilight", "stilj ambilight"),
    ("settings.ambilight:Дополнительные", "dopolniteljnije v podsvetke"),
    ("settings.picture:Стиль изображ.", "stilj izobrazhenija"),
    ("settings.picture:Автоматический стил...", "avtomaticheskij stilj"),
    ("settings.picture:Четкость", "chetkostj"),
    ("settings.picture:Восстановить стиль из...", "vosstanovitj stilj"),
    ("settings.channels:Профиль операт...", "profilj operatora"),
    ("settings.general:Изменить источ...", "izmenitj istochniki"),
    ("settings.access", "razdel universaljnij dostup"),
    ("settings.access:Универсальный доступ", "tumbler universaljnij dostup"),
    ("settings.parental:Родительский контроль", "roditeljskij kontrolj"),
    ("settings.parental:Установить код", "ustanovitj kod"),
    ("settings.update", "obnovitj po"),
]


def test_vocabulary_returns_set_of_strings():
    vocab = MS.vocabulary()
    assert isinstance(vocab, set)
    assert "питание" in vocab and "таймер" in vocab and "наушники" in vocab


def test_aliases_count_and_drift_against_live_map():
    """Все 80 целей существуют в живой карте tv_menu_map.json и строят маршрут без MapError."""
    assert len(DATA["aliases"]) == 80
    for entry in DATA["aliases"]:
        target = entry["target"]
        press = target == MS.SCREEN_OFF_TARGET
        keys = TM.route(target, MAP, press=press)
        assert isinstance(keys, list) and len(keys) > 0, target


@pytest.mark.parametrize("phrase", [
    "выключи экран",
    "погаси экран",
    "чёрный экран",
    "экран выключи, звук оставь",
    "vikljuci ekran",
])
def test_operator_decision_screen_off(phrase):
    """Решение Оператора 05.11 17:50: гашение экрана ведёт в Выкл. экран с финальным Confirm."""
    cmd = MS.parse(phrase, data=DATA, menu_map=MAP)
    assert cmd is not None
    assert cmd.name == "screen_off"
    assert cmd.target == "settings.general.power:Выкл. экран"
    assert cmd.needs_confirm is False
    assert cmd.keys == TM.route("settings.general.power:Выкл. экран", MAP, press=True)
    assert cmd.keys.count("Confirm") == 3


@pytest.mark.parametrize(("raw", "expected"), [
    ("stilj", "стиль"),
    ("chetkostj", "четкость"),
    ("kontrastnostj", "контрастность"),
    ("roditeljskij", "родительский"),
    ("obnovitj", "обновить"),
    ("poljzovateljskij", "пользовательский"),
    ("vosstanovitj", "восстановить"),
    ("ustanovitj", "установить"),
    ("izmenitj", "изменить"),
    ("profilj", "профиль"),
    ("kontrolj", "контроль"),
    ("universaljnij", "универсальный"),
    ("dopolniteljnije", "дополнительные"),
    ("naushniki", "наушники"),
])
def test_pkey_and_canonical_soft_sign_and_naushniki(raw, expected):
    """«j» после согласной в конце слова или перед согласной даёт «ь», а не «и»; «naushniki» -> «наушники»."""
    assert MS._pkey(raw) == MS._pkey(expected)
    assert MS.canonical(raw, DATA) == expected


@pytest.mark.parametrize(("target", "phrase"), TRANSLIT_18_CASES)
def test_translit_18_regressions(target, phrase):
    """Отдельный прогон 18 транслит-фраз, упавших в первом облачном прогоне."""
    assert len(TRANSLIT_18_CASES) == 18
    cmd = MS.parse(phrase, data=DATA, menu_map=MAP)
    assert cmd is not None, f"транслит-фраза «{phrase}» не распознана (ожидалась {target})"
    assert cmd.name != "menu_clarify", f"транслит-фраза «{phrase}» дала уточнение {cmd.targets} вместо {target}"
    assert cmd.target == target, f"транслит-фраза «{phrase}» дала {cmd.target} вместо {target}"


@pytest.mark.parametrize(("target", "phrase"), POSITIVE_CASES)
def test_positive_phrases_match_exact_target(target, phrase):
    """Каждая из 400 позитивных фраз (включая транслит и опечатку) ведёт ровно в свою цель."""
    cmd = MS.parse(phrase, data=DATA, menu_map=MAP)
    assert cmd is not None, f"фраза «{phrase}» не распознана (ожидалась {target})"
    assert cmd.name != "menu_clarify", f"фраза «{phrase}» дала уточнение {cmd.targets} вместо {target}"
    assert cmd.target == target, f"фраза «{phrase}» дала {cmd.target} вместо {target}"


@pytest.mark.parametrize(("target", "phrase"), NEGATIVE_CASES)
def test_negative_phrases_do_not_match_target(target, phrase):
    """Каждая из 240 негативных фраз соседних пунктов НЕ попадает в проверяемую цель."""
    got = MS.target_of(phrase, data=DATA, menu_map=MAP)
    assert got != target, f"негативная фраза «{phrase}» ошибочно попала в {target}"


@pytest.mark.parametrize(("phrase", "src_file", "test_name"), MUST_NOT_STEAL_CASES)
def test_must_not_steal_existing_commands(phrase, src_file, test_name, monkeypatch):
    """30 существующих команд из тестовой сюиты НЕ перехватываются слоем menu_syntax."""
    assert MS.parse(phrase, data=DATA, menu_map=MAP) is None, (
        f"фраза «{phrase}» ({src_file}::{test_name}) перехвачена menu_syntax"
    )
    if TN.parse(phrase) is not None:
        return
    monkeypatch.setenv("TELEGRAM_TOKEN", "fake_token_for_tests")
    import tv_commands as TC
    assert TC.parse(phrase) is not None


@pytest.mark.parametrize(("phrase", "expected_targets", "expected_ask"), AMBIGUOUS_CASES)
def test_ambiguous_phrases_return_clarify(phrase, expected_targets, expected_ask):
    """Неоднозначные фразы возвращают локальный переспрос без нажатия кнопок ТВ."""
    cmd = MS.parse(phrase, data=DATA, menu_map=MAP)
    assert cmd is not None
    assert cmd.name == "menu_clarify"
    assert cmd.kind == MS.LOCAL_KIND
    assert cmd.keys == []
    assert set(cmd.targets) == set(expected_targets)
    assert cmd.reply == expected_ask


def test_order_tv_nav_macro_wins_over_menu_syntax(tmp_path):
    """Порядок слоёв: tv_nav.parse -> menu_syntax.parse.
    Запомненный пользователем макрос «настройки» срабатывает раньше ambiguous в menu_syntax,
    а после удаления макроса «открой настройки» уходит в menu_syntax (menu_clarify)."""
    store = TN.MacroStore(tmp_path / "tv_macros.json")
    TN.parse("Запомни настройки: домой, вниз два, ок.", store)

    # 1. Пока макрос есть в MacroStore, tv_nav.parse перехватывает первым:
    nav_cmd = TN.parse("открой настройки", store)
    assert nav_cmd is not None and nav_cmd.name == "nav_macro"
    pipeline_cmd = TN.parse("открой настройки", store) or MS.parse("открой настройки", data=DATA, menu_map=MAP)
    assert pipeline_cmd.name == "nav_macro"
    assert pipeline_cmd.keys == ["Home", "CursorDown", "CursorDown", "Confirm"]

    # 2. После удаления макроса tv_nav.parse возвращает None, и срабатывает menu_syntax:
    TN.parse("забудь настройки", store)
    assert TN.parse("открой настройки", store) is None
    after_cmd = TN.parse("открой настройки", store) or MS.parse("открой настройки", data=DATA, menu_map=MAP)
    assert after_cmd is not None and after_cmd.name == "menu_clarify"


@pytest.mark.parametrize("phrase", [
    "сначала подумай",
    "снаружи темно",
    "кодировка файла",
    "шумный сосед",
    "microsoft office",
    "оффлайн режим",
    "камера наблюдения",
    "погода на завтра",
    "выключи свет в спальне",
    "тёмный экран",
])
def test_short_exact_words_do_not_match_substrings(phrase):
    """Короткие слова из exact_words (сна, код, шум, off, офф, cam, по) и исключённый «тёмный экран» не срабатывают."""
    assert MS.parse(phrase, data=DATA, menu_map=MAP) is None


# --- 05.11 Claude: врезка в tv_commands / tv_bot_handler ---
def test_tv_commands_screen_off_not_standby():
    import tv_commands as TC
    for p in ("выключи экран", "погаси экран", "чёрный экран"):
        assert TC.parse(p).name == "screen_off", p
    for p in ("выключи телевизор", "выключи тв", "режим ожидания", "standby"):
        c = TC.parse(p)
        assert c.name == "standby" and c.needs_confirm is True, p
    try:
        assert TC.parse("тёмный экран").name != "screen_off"
    except TC.Unknown:
        pass


def test_execute_local_does_not_touch_tv_factory():
    H = pytest.importorskip("tv_bot_handler")
    cmd = MS.parse("экран", data=DATA, menu_map=MAP)
    assert cmd is not None and cmd.kind == MS.LOCAL_KIND

    def boom():
        raise ValueError("TV_HOST не задан в .env")
    out = H._execute(cmd, boom)
    assert "TV_HOST" not in out and out == cmd.reply


# --- 06.11 SCREEN_ON: «включи экран» -> CursorDown (не menu_clarify) ---
def test_screen_on_not_captured_by_menu_clarify():
    import tv_commands as TC

    class _TV:
        def __init__(self):
            self.keys = []

        def send_key(self, k):
            self.keys.append(k)

    for raw in ("включи экран", "vkljuci ekran", "верни экран", "разбуди экран", "зажги экран", "экран включи"):
        canon = TN.canonical(raw, extra=MS.vocabulary())
        assert MS.parse(canon, data=DATA, menu_map=MAP) is None, f"{raw!r} не должна перехватываться menu_syntax"
        cmd = TC.parse(canon)
        assert cmd.name == "screen_on" and cmd.kind == TC.BLIND and cmd.needs_confirm is False, raw
        tv = _TV()
        out = cmd.run(tv)
        assert tv.keys == ["CursorDown"] and "Отправил" in out

    # «экран» само по себе по-прежнему возвращает уточнение:
    amb = MS.parse("экран", data=DATA, menu_map=MAP)
    assert amb is not None and amb.name == "menu_clarify" and amb.kind == MS.LOCAL_KIND

    # «выключи экран» по-прежнему возвращает screen_off:
    off_cmd = MS.parse("выключи экран", data=DATA, menu_map=MAP)
    assert off_cmd is not None and off_cmd.name == "screen_off"


# --- 06.11 Claude: роутер без /tv и калибровка камеры по карте ---
@pytest.mark.parametrize(("phrase", "name"), [("выключи экран", "screen_off"), ("включи экран", "screen_on")])
def test_router_allows_screen_commands_without_tv_mode(phrase, name):
    R = pytest.importorskip("router")
    r = R.route(phrase, phrase, None, None)
    assert r.kind == R.TV and r.reason == f"tv_rule:{name}"


def test_router_translit_ekran():
    R = pytest.importorskip("router")
    assert R._translit("vikljuci ekran") == "выключи экран"


def test_calibrate_auto_refuses_without_map_route(monkeypatch):
    cw = pytest.importorskip("camera_watcher")
    monkeypatch.setattr(cw, "enabled", lambda: True)

    def bad_route(*a, **kw):
        raise TM.MapError("нет узла")
    monkeypatch.setattr(TM, "route", bad_route)
    sent = []

    class _TV:
        def send_key(self, k):
            sent.append(k)
    text, path = cw.calibrate_auto(_TV(), grab=lambda *_: None, sleep=lambda _s: None)
    assert sent == [] and path is None and "маршрут в карте меню не найден" in text
