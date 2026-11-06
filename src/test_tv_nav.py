"""tv_nav (28.10.2026): серии кнопок, числа словами, названные пункты меню, отказ от Standby в серии,
и связка с tv_bot_handler (навигация разбирается ДО tv_commands, одиночная кнопка остаётся за ним)."""
import pytest

import tv_nav as N


class FakeTV:
    def __init__(self):
        self.sent: list[list[str]] = []

    def send_keys(self, keys, delay=None):
        self.sent.append(list(keys))

    def send_key(self, key):
        self.sent.append([key])


@pytest.fixture
def store(tmp_path):
    return N.MacroStore(tmp_path / "tv_macros.json")


@pytest.mark.parametrize("phrase,keys", [
    ("вниз три", ["CursorDown"] * 3),
    ("Вниз 3.", ["CursorDown"] * 3),
    ("три раза вниз", ["CursorDown"] * 3),
    ("вниз два раза", ["CursorDown"] * 2),
    ("вправо вправо ок", ["CursorRight", "CursorRight", "Confirm"]),
    ("назад дважды", ["Back", "Back"]),
    ("домой, вниз 2 и ок", ["Home", "CursorDown", "CursorDown", "Confirm"]),
    ("направо потом вниз", ["CursorRight", "CursorDown"]),
])
def test_sequences(phrase, keys, store):
    c = N.parse(phrase, store)
    assert c is not None and c.keys == keys and c.needs_confirm is False
    tv = FakeTV()
    out = c.run(tv)
    assert tv.sent == [keys] and out.startswith("Отправил")


@pytest.mark.parametrize("phrase", ["вниз", "ок", "назад"])
def test_single_key_left_to_tv_commands(phrase, store):
    assert N.parse(phrase, store) is None


@pytest.mark.parametrize("phrase", [
    "подсветка вниз", "открой окно", "меню дня в столовой", "громче на 5", "выключи телевизор вниз",
    "standby вниз", "", "три",
])
def test_not_navigation_returns_none(phrase, store):
    assert N.parse(phrase, store) is None


def test_standby_never_in_navigation():
    assert "Standby" not in N.NAV_WORDS.values()


def test_too_many_presses_refused(store):
    with pytest.raises(N.NavError):
        N.parse("вниз 15 вправо 10", store)
    with pytest.raises(N.NavError):
        N.parse("вниз 50", store)


def test_say_groups_repeats():
    assert N.say(["Home", "CursorDown", "CursorDown", "Confirm"]) == "домой, вниз ×2, ок"


def test_macro_save_open_forget(store, tmp_path):
    r = N.parse("Запомни настройки: домой, вниз два, ок.", store).run(FakeTV())
    assert "Запомнил «настройки»" in r and "домой, вниз ×2, ок" in r
    # хранится на диске — новый объект видит
    again = N.MacroStore(tmp_path / "tv_macros.json")
    for phrase in ("открой настройки", "зайди в настройки", "настройки"):
        c = N.parse(phrase, again)
        assert c is not None and c.keys == ["Home", "CursorDown", "CursorDown", "Confirm"]
        tv = FakeTV()
        assert "пункт «настройки»" in c.run(tv) and tv.sent == [c.keys]
    assert "Помню" in N.parse("что помнишь", again).run(FakeTV())
    assert "Забыл" in N.parse("забудь настройки", again).run(FakeTV())
    assert N.parse("открой настройки", again) is None


def test_macro_voice_style_without_colon(store):
    N.parse("запомни ютуб плитка домой вправо три ок", store)
    c = N.parse("открой ютуб плитка", store)
    assert c.keys == ["Home", "CursorRight", "CursorRight", "CursorRight", "Confirm"]


def test_macro_overwrite_reports_old(store):
    N.parse("запомни звук домой вниз ок", store)
    r = N.parse("запомни звук домой вниз два ок", store).run(FakeTV())
    assert "было: домой, вниз, ок" in r


@pytest.mark.parametrize("phrase", ["запомни настройки", "запомни домой вниз", "запомни настройки вниз погода"])
def test_bad_macro_definitions_refused(phrase, store):
    with pytest.raises(N.NavError):
        N.parse(phrase, store)
    assert store.load() == {}


def test_memory_commands_do_not_touch_tv(store):
    tv = FakeTV()
    N.parse("запомни икс домой ок", store).run(tv)
    N.parse("что помнишь", store).run(tv)
    assert tv.sent == []


def test_keys_exist_in_tv_control():
    tv_control = pytest.importorskip("tv_control")
    assert set(N.NAV_WORDS.values()) <= set(tv_control.KEYS)


def test_nav_error_is_handler_unknown():
    """tv_bot_handler ловит tv_commands.Unknown — иначе ошибка навигации уронила бы обработчик."""
    tv_commands = pytest.importorskip("tv_commands")
    assert issubclass(N.NavError, tv_commands.Unknown)


def test_handler_routes_navigation_first(store, monkeypatch):
    H = pytest.importorskip("tv_bot_handler")
    monkeypatch.setattr(N, "MacroStore", lambda path=None: store)
    tv = FakeTV()
    out = H.handle(1, "вниз три", lambda: tv, admin_ids=[1], pending=H.PendingStore())
    assert tv.sent == [["CursorDown"] * 3] and "Отправил: вниз ×3" in out
    out = H.handle(1, "запомни звук домой вниз ок", lambda: tv, admin_ids=[1], pending=H.PendingStore())
    assert "Запомнил" in out and "подтверждения от телевизора нет" not in out.lower()
    out = H.handle(1, "вниз 99", lambda: tv, admin_ids=[1], pending=H.PendingStore())
    assert out.startswith("Не выполнено")


# --- ввод в стиле Оператора (21:56: «ok», «vpravo» -> «не понял команду») ---
@pytest.mark.parametrize("raw,canon", [
    ("ok", "ок"), ("vpravo", "вправо"), ("vniz tri", "вниз три"), ("vverh dva", "вверх два"),
    ("hvatit", "хватит"), ("dybp", "вниз"), ("da", "да"), ("cto pomnisj", "что помнишь"),
    ("Вправо, вправо.", "вправо вправо"), ("privet mir", "privet mir"), ("открой окно", "открой окно"),
])
def test_canonical(raw, canon):
    assert N.canonical(raw) == canon


def test_latin_sequence_and_macro_names_match_cyrillic(store):
    c = N.parse(N.canonical("vniz tri"), store)
    assert c.keys == ["CursorDown"] * 3
    N.parse(N.canonical("zapomni nastrojki domoj vniz dva ok"), store)
    for phrase in ("открой настройки", "otkroj nastrojki", "настройки"):
        c = N.parse(N.canonical(phrase), store)
        assert c is not None and c.keys == ["Home", "CursorDown", "CursorDown", "Confirm"], phrase
    assert "Забыл" in N.parse(N.canonical("zabudj настройки"), store).run(FakeTV())


def test_handler_understands_latin(store, monkeypatch):
    H = pytest.importorskip("tv_bot_handler")
    if "tv_nav.canonical(" not in open(H.__file__, encoding="utf-8-sig").read():
        pytest.fail("tv_bot_handler не приводит ввод через tv_nav.canonical")
    monkeypatch.setattr(N, "MacroStore", lambda path=None: store)
    tv = FakeTV()
    H.handle(1, "vniz tri", lambda: tv, admin_ids=[1], pending=H.PendingStore())
    assert tv.sent == [["CursorDown"] * 3]


def test_handler_single_latin_key_reaches_tv_commands(store, monkeypatch):
    """«ok» одиночной кнопкой разбирает tv_commands — нужен настоящий словарь (на машине Оператора)."""
    tv_commands = pytest.importorskip("tv_commands")
    if not getattr(tv_commands, "KEY_WORDS", None):
        pytest.skip("заглушка tv_commands (песочница)")
    H = pytest.importorskip("tv_bot_handler")
    monkeypatch.setattr(N, "MacroStore", lambda path=None: store)
    tv = FakeTV()
    out = H.handle(1, "ok", lambda: tv, admin_ids=[1], pending=H.PendingStore())
    assert tv.sent == [["Confirm"]] and out.startswith("Отправил")
