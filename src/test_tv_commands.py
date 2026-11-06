"""test_tv_commands.py — разбор текстовых команд, без сети.

Сеть запрещена так же, как в test_tv_control.py: любой реальный запрос —
провал теста. Телевизор заменён FakeTV, который записывает вызовы и
отдаёт правдоподобное состояние.

Что проверяется:
  1. Таблица «фраза -> команда» с синонимами, регистром и «ё».
  2. Границы слов: «включи тв» не должно ловиться на «телевизор» и т.п.
  3. Класс команды: verified у того, что читается обратно; blind у кнопок.
  4. Честные отказы: включение по сети, запуск приложений.
  5. Подтверждение на Standby.
  6. Числа: шаг громкости, уровень, обрезка по диапазону устройства.
  7. Связность словарей: каждое имя кнопки существует в tv_control.KEYS,
     каждый алгоритм — в ambilight_supportedstyles живого телевизора.
"""
from __future__ import annotations

import pytest
import requests


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def _boom(*_a, **_k):
        raise AssertionError("реальный сетевой вызов в тесте команд")

    monkeypatch.setattr(requests, "request", _boom, raising=False)
    monkeypatch.setattr(requests, "post", _boom, raising=False)
    monkeypatch.setattr(requests, "get", _boom, raising=False)
    monkeypatch.setattr(requests, "Session", _boom)


@pytest.fixture(autouse=True)
def no_readback_delay(monkeypatch):
    """Пауза перед подтверждающим чтением выключается: 0.6 с на команду
    превратили бы сюиту в минутную. Сама величина проверяется отдельно."""
    monkeypatch.setenv("TV_READBACK_DELAY_S", "0")


@pytest.fixture
def cmds(monkeypatch):
    monkeypatch.setenv("TELEGRAM_TOKEN", "fake_token_for_tests")
    import tv_commands as mod
    return mod


class FakeTV:
    """Подражает TVClient в части, которую используют команды."""

    def __init__(self, volume=10, maximum=60, muted=False, power="On",
                 style="FOLLOW_VIDEO", setting="STANDARD"):
        self.calls: list[tuple] = []
        self._v = {"muted": muted, "current": volume, "min": 0, "max": maximum}
        self._power = power
        self._style = {"styleName": style, "isExpert": False, "menuSetting": setting}
        self._lounge = {"color": {"hue": 110, "saturation": 38, "brightness": 255}}

    # чтение
    def get_volume(self):
        self.calls.append(("get_volume",))
        return dict(self._v)

    def get_ambilight_power(self):
        self.calls.append(("get_ambilight_power",))
        return {"power": self._power}

    def get_ambilight_config(self):
        self.calls.append(("get_ambilight_config",))
        return dict(self._style)

    def get_lounge_color(self):
        self.calls.append(("get_lounge_color",))
        return {"color": dict(self._lounge["color"])}

    def get_current_activity(self):
        self.calls.append(("get_current_activity",))
        return {"id": "browser"}

    # запись
    def set_volume(self, level, muted=False):
        """Повторяет контракт настоящего клиента: обрезает по пределу
        контура и ВОЗВРАЩАЕТ фактическое значение. Двойник, который
        возвращает None, скрыл бы отчёт об обрезке."""
        import tv_control
        self.calls.append(("set_volume", level))
        ceiling = tv_control.volume_ceiling(self._v["min"], self._v["max"])
        self._v["current"] = min(int(level), ceiling)
        return self._v["current"]

    def volume_step(self, delta):
        import tv_control
        self.calls.append(("volume_step", delta))
        ceiling = tv_control.volume_ceiling(self._v["min"], self._v["max"])
        now = self._v["current"]
        target = now + delta
        if delta >= 0:
            target = min(target, max(ceiling, now))
        self._v["current"] = max(self._v["min"], min(self._v["max"], target))
        return self._v["current"]

    def set_muted(self, muted):
        self.calls.append(("set_muted", muted))
        self._v["muted"] = muted

    def set_ambilight_power(self, on):
        self.calls.append(("set_ambilight_power", on))
        self._power = "On" if on else "Off"

    def set_ambilight_style(self, style, setting=None):
        self.calls.append(("set_ambilight_style", style, setting))
        self._style = {"styleName": style, "isExpert": False, "menuSetting": setting}

    def set_lounge_color(self, hue, saturation, brightness):
        self.calls.append(("set_lounge_color", hue, saturation, brightness))
        self._lounge = {"color": {"hue": hue, "saturation": saturation,
                                  "brightness": brightness}}

    def send_key(self, key):
        self.calls.append(("send_key", key))

    def names(self) -> list[str]:
        return [c[0] for c in self.calls]


# --------------------------------------------------------------------------
# 1. Нормализация
# --------------------------------------------------------------------------

@pytest.mark.parametrize(("raw", "expected"), [
    ("Громче!", "громче"),
    ("  ПОДСВЕТКА   Тёплый  ", "подсветка теплый"),
    ("Громкость: 20.", "громкость: 20"),
    ("жёлтый", "желтый"),
])
def test_normalize(cmds, raw, expected):
    assert cmds.normalize(raw) == expected


def test_empty_is_unknown(cmds):
    for raw in ("", "   ", "!!!"):
        with pytest.raises(cmds.Unknown):
            cmds.parse(raw)


# --------------------------------------------------------------------------
# 2. Громкость — ВКЛЮЧЕНА 27.10 в 23:13, с пределом
# --------------------------------------------------------------------------
# Днём команда была отключена отказом: звук шёл по оптике на внешние
# колонки, `POST audio/volume` отвечал 200 и менял число, а на слух не
# менялось ничего. Оператор переключил вывод на динамики телевизора и
# проверил на слух — громкость меняется. Дефекта в API не было, был другой
# маршрут звука.
#
# Поэтому здесь тесты ПОВЕДЕНИЯ, а не отказа. Главное, что они сторожат, —
# предел контура: автоматика получила право менять звук сама.

def test_volume_up_default_step(cmds):
    tv = FakeTV(volume=10)
    cmd = cmds.parse("громче")
    assert cmd.name == "volume_up" and cmd.kind == cmds.VERIFIED
    cmd.run(tv)
    assert ("volume_step", 3) in tv.calls


@pytest.mark.parametrize(("phrase", "delta"), [
    ("громче на 7", 7), ("погромче на 2", 2), ("прибавь на 4", 4),
    ("тише на 5", -5), ("потише на 1", -1), ("убавь на 6", -6),
])
def test_volume_step_number(cmds, phrase, delta):
    tv = FakeTV(volume=20)
    cmds.parse(phrase).run(tv)
    assert ("volume_step", delta) in tv.calls


def test_volume_get_does_not_write(cmds):
    tv = FakeTV(volume=12)
    out = cmds.parse("громкость").run(tv)
    assert "12" in out
    assert not [c for c in tv.calls if c[0].startswith("set_") or c[0] == "volume_step"]


def test_volume_set_level(cmds):
    tv = FakeTV(volume=5)
    out = cmds.parse("громкость 20").run(tv)
    assert ("set_volume", 20) in tv.calls
    assert "20" in out


def test_volume_set_above_ceiling_is_clamped_and_said(cmds):
    """Обрезка до предела обязана быть ВИДНА в ответе.

    Молча выставить 30 вместо запрошенных 50 — это отчёт о выполнении
    команды, которая не выполнена. Человек должен узнать про предел.
    """
    tv = FakeTV(volume=10, maximum=60)
    out = cmds.parse("громкость 50").run(tv)
    assert tv._v["current"] == 30
    assert "запрошено 50" in out and "предел" in out


def test_volume_up_at_ceiling_says_so(cmds):
    tv = FakeTV(volume=30, maximum=60)
    out = cmds.parse("громче").run(tv)
    assert tv._v["current"] == 30
    assert "предел" in out


def test_volume_down_allowed_above_ceiling(cmds):
    """Выше предела (выставлено пультом) понижать можно."""
    tv = FakeTV(volume=50, maximum=60)
    cmds.parse("тише на 5").run(tv)
    assert tv._v["current"] == 45


def test_volume_up_above_ceiling_does_not_crash_down(cmds):
    """Запрос «громче» при громкости выше предела НЕ убавляет звук.

    Насильно уронить громкость человеку, попросившему громче, — худший
    отклик из возможных: он выглядит как поломка.
    """
    tv = FakeTV(volume=50, maximum=60)
    cmds.parse("громче").run(tv)
    assert tv._v["current"] == 50


@pytest.mark.parametrize("phrase", ["без звука", "выключи звук", "заглуши", "mute"])
def test_mute_on(cmds, phrase):
    tv = FakeTV()
    cmds.parse(phrase).run(tv)
    assert ("set_muted", True) in tv.calls


@pytest.mark.parametrize("phrase", ["включи звук", "верни звук", "со звуком", "unmute"])
def test_mute_off(cmds, phrase):
    tv = FakeTV(muted=True)
    cmds.parse(phrase).run(tv)
    assert ("set_muted", False) in tv.calls


def test_unmute_not_swallowed_by_mute_substring(cmds):
    """«unmute» содержит «mute»: порядок правил проверяется явно."""
    tv = FakeTV(muted=True)
    cmds.parse("unmute").run(tv)
    assert ("set_muted", False) in tv.calls
    assert ("set_muted", True) not in tv.calls


def test_volume_answer_claims_nothing_about_audibility(cmds):
    """Ответ сообщает число, а не слышимость.

    Условие слышимости — что звук воспроизводит сам телевизор, и по API
    это не определяется: в снятой карте menuitems/settings узла выбора
    аудиовыхода нет. Поэтому обещать слышимый результат нельзя.
    """
    tv = FakeTV(volume=10)
    out = cmds.parse("громче").run(tv)
    for lie in ("слышно", "громче стало", "вы услышите"):
        assert lie not in out.lower()


# --------------------------------------------------------------------------
# 3. Подсветка
# --------------------------------------------------------------------------

@pytest.mark.parametrize(("phrase", "algo"), [
    ("подсветка музыка", "ENERGY_ADAPTIVE_BRIGHTNESS"),
    ("подсветку под музыку", "ENERGY_ADAPTIVE_BRIGHTNESS"),
    ("подсветка вечеринка", "PARTY"),
    ("ambilight дискотека", "PARTY"),
    ("подсветка спектр", "SPECTRUM_ANALYZER"),
    ("подсветка эквалайзер", "VU_METER"),
    ("подсветка строб", "STROBE"),
])
def test_ambilight_audio(cmds, phrase, algo):
    tv = FakeTV()
    c = cmds.parse(phrase)
    assert c.kind == cmds.VERIFIED
    assert c.meta["algorithm"] == algo
    out = c.run(tv)
    assert ("set_ambilight_power", True) in tv.calls
    assert ("set_ambilight_style", "FOLLOW_AUDIO", algo) in tv.calls
    assert algo in out


@pytest.mark.parametrize(("phrase", "setting"), [
    ("подсветка видео", "STANDARD"),
    ("подсветка кино", "NATURAL"),
    ("подсветка игра", "GAME"),
])
def test_ambilight_video(cmds, phrase, setting):
    tv = FakeTV()
    out = cmds.parse(phrase).run(tv)
    assert ("set_ambilight_style", "FOLLOW_VIDEO", setting) in tv.calls
    assert setting in out


@pytest.mark.parametrize("phrase", ["подсветка выключи", "выключи подсветку", "ambilight off"])
def test_ambilight_off_dims_lounge_not_power(cmds, phrase):
    """03.11 (проба tools\\amb_off_probe.py, глаза Оператора): ambilight/power Off читается «Off», а лампы ГОРЯТ;
    OFF через currentconfiguration тоже не работает (27.10). Гаснут лампы только от цвета с яркостью 0 --
    поэтому выключение = FOLLOW_COLOR + lounge brightness 0, подтверждение -- чтением lounge."""
    tv = FakeTV()
    out = cmds.parse(phrase).run(tv)
    assert ("set_ambilight_power", False) not in tv.calls
    assert ("set_ambilight_style", "FOLLOW_COLOR", "MANUAL_HUE") in tv.calls
    assert tv.calls[-2][0] == "set_lounge_color" and tv.calls[-2][3] == 0
    assert out == "Подсветка: выключена (цвет погашен: яркость 0)"


def test_ambilight_on(cmds):
    tv = FakeTV(power="Off")
    out = cmds.parse("включи подсветку").run(tv)
    assert ("set_ambilight_power", True) in tv.calls
    assert "On" in out


def test_ambilight_on_after_off_restores_same_colour(cmds):
    tv = FakeTV()
    tv._lounge = {"color": {"hue": 85, "saturation": 255, "brightness": 255}}
    cmds.parse("подсветка выключи").run(tv)
    out = cmds.parse("подсветка включи").run(tv)
    assert tv._lounge["color"] == {"hue": 85, "saturation": 255, "brightness": 255}
    assert out.startswith("Подсветка: включена, режим один цвет (статичный), hue 85")


@pytest.mark.parametrize(("phrase", "hue"), [
    ("подсветка красная", 0),
    ("подсветка синяя", 170),
    ("подсветка зеленая", 85),
    ("подсветка зелёная", 85),
    ("подсветка теплый", 25),
])
def test_ambilight_colour(cmds, phrase, hue):
    tv = FakeTV()
    c = cmds.parse(phrase)
    assert c.meta["hue"] == hue
    out = c.run(tv)
    assert ("set_lounge_color", hue, 255, 255) in tv.calls
    assert ("set_ambilight_style", "FOLLOW_COLOR", "MANUAL_HUE") in tv.calls
    assert f"hue {hue}" in out
    # Порядок: стиль ДО цвета. Иначе запись цвета уходит в 200 и теряется
    # (проверено на живом телевизоре: hue остался 110).
    assert tv.names().index("set_ambilight_style") < tv.names().index("set_lounge_color")


def test_ambilight_colour_with_brightness(cmds):
    tv = FakeTV()
    cmds.parse("подсветка синяя 100").run(tv)
    assert ("set_lounge_color", 170, 255, 100) in tv.calls


def test_ambilight_unknown_mode(cmds):
    with pytest.raises(cmds.Unknown):
        cmds.parse("подсветка как-нибудь")


# --------------------------------------------------------------------------
# 4. Честные отказы
# --------------------------------------------------------------------------

@pytest.mark.parametrize("phrase", ["включи телевизор", "включи тв"])
def test_power_on_is_wake_on_lan_and_claims_nothing(cmds, phrase):
    """03.11: включение -- пакет Wake-on-LAN (проверено дважды по кадру камеры). Команда слепая: ответ говорит
    об ОТПРАВКЕ, не о результате -- результат сообщает проверка по кадру (bot.py)."""
    c = cmds.parse(phrase)
    assert c.name == "power_on" and c.kind == cmds.BLIND and not c.needs_confirm
    tv = FakeTV()
    tv.wake = lambda: tv.calls.append(("wake",))
    out = c.run(tv)
    assert ("wake",) in tv.calls and out.startswith("Отправил")
    for lie in ("включен", "включён", "готово", "выполнено"):
        assert lie not in out.lower()


@pytest.mark.parametrize("phrase", [
    "открой ютуб", "запусти youtube", "включи нетфликс", "netflix",
])
def test_apps_are_refused_with_reason(cmds, phrase):
    with pytest.raises(cmds.Unknown) as ei:
        cmds.parse(phrase)
    assert "403" in str(ei.value)


# --------------------------------------------------------------------------
# 5. Кнопки и подтверждение
# --------------------------------------------------------------------------

@pytest.mark.parametrize(("phrase", "key"), [
    ("вверх", "CursorUp"), ("вниз", "CursorDown"),
    ("влево", "CursorLeft"), ("вправо", "CursorRight"),
    ("ок", "Confirm"), ("назад", "Back"), ("инфо", "Info"),
    ("меню", "Home"), ("домой", "Home"), ("портал", "Online"),
    ("источник", "Source"), ("пауза", "PlayPause"), ("стоп", "Stop"),
])
def test_keys(cmds, phrase, key):
    tv = FakeTV()
    c = cmds.parse(phrase)
    assert c.kind == cmds.BLIND, "у кнопки нет обратной связи — класс blind"
    out = c.run(tv)
    assert ("send_key", key) in tv.calls
    assert out.startswith("Отправил")


@pytest.mark.parametrize("phrase", [
    "выключи телевизор", "выключи тв", "режим ожидания", "standby",
])
def test_standby_needs_confirm(cmds, phrase):
    c = cmds.parse(phrase)
    assert c.needs_confirm is True
    assert c.kind == cmds.BLIND
    tv = FakeTV()
    c.run(tv)
    assert ("send_key", "Standby") in tv.calls


def test_only_standby_needs_confirm(cmds):
    for phrase in ("подсветка кино", "вверх", "статус", "подсветка красная"):
        assert cmds.parse(phrase).needs_confirm is False


def test_status_still_reports_volume(cmds):
    """Управление громкостью отключено, но ЧТЕНИЕ остаётся: в статусе
    полезно видеть, что внутренний уровень стоит на нуле — это и есть
    признак вывода звука по оптике."""
    tv = FakeTV(volume=7)
    out = cmds.parse("статус").run(tv)
    assert "7/60" in out and "On" in out and "browser" in out
    assert "send_key" not in tv.names()
    assert "set_volume" not in tv.names() and "set_muted" not in tv.names()


# --------------------------------------------------------------------------
# 6. Границы слов
# --------------------------------------------------------------------------

def test_word_boundaries_not_substring(cmds):
    """«тв» внутри «телевизор» не должно превращаться в кнопку WatchTV —
    это тот же дефект подстрочного поиска, что в classify_content."""
    assert cmds.parse("включи телевизор").name == "power_on"  # -> пробуждение, а не кнопка WatchTV
    c = cmds.parse("выключи телевизор")
    assert c.name == "standby"


def test_ambilight_word_wins_over_key(cmds):
    """«подсветка вниз» — это не кнопка «вниз»: раз сказано про подсветку,
    команда должна разбираться в её ветке и падать в Unknown, а не
    нажимать стрелку."""
    with pytest.raises(cmds.Unknown):
        cmds.parse("подсветка вниз")


@pytest.mark.parametrize("phrase", [
    "около дома", "открой окно", "сокол", "стопка книг", "входит и выходит",
    "тверь", "меню дня в столовой",
])
def test_substring_does_not_trigger_key(cmds, phrase):
    """Разговорные имена кнопок — короткие слова («ок», «стоп», «вход»,
    «тв»), и они сидят внутри обычных слов. Подстрочный поиск превратил бы
    «открой окно» в нажатие Confirm. Исключение: «меню дня» содержит
    «меню» как отдельное слово — такую фразу ловить КАК кнопку допустимо,
    поэтому она проверяется отдельно ниже."""
    if phrase.startswith("меню"):
        c = cmds.parse(phrase)
        assert c.meta["key"] == "Home"
        return
    with pytest.raises(cmds.Unknown):
        cmds.parse(phrase)


def test_unknown_phrases(cmds):
    for phrase in ("свари кофе", "погода", "перезагрузи роутер"):
        with pytest.raises(cmds.Unknown):
            cmds.parse(phrase)


# --------------------------------------------------------------------------
# 7. Связность словарей с реальностью
# --------------------------------------------------------------------------

def test_every_key_word_exists_in_api_whitelist(cmds, monkeypatch):
    monkeypatch.setenv("TELEGRAM_TOKEN", "fake_token_for_tests")
    import tv_control
    unknown = sorted(set(cmds.KEY_WORDS.values()) - set(tv_control.KEYS))
    assert not unknown, f"нет в tv_control.KEYS: {unknown}"


def test_video_settings_are_only_confirmed_values(cmds):
    """`CINEMA` телевизор принял с кодом 200 и выставил NATURAL — значит
    непроверенные имена молча подменяются. В словаре допустимы только
    значения, прочитанные обратно с устройства."""
    confirmed = {"STANDARD", "NATURAL", "GAME"}
    assert set(cmds.VIDEO_SETTINGS.values()) <= confirmed


def test_audio_algorithms_match_live_supportedstyles(cmds):
    """Список снят с живого телевизора 27.10 (ambilight_supportedstyles.json).
    Опечатка в словаре дала бы 200 на POST и молча другой режим."""
    live = {
        "ENERGY_ADAPTIVE_BRIGHTNESS", "ENERGY_ADAPTIVE_COLORS", "VU_METER",
        "SPECTRUM_ANALYZER", "KNIGHT_RIDER_CLOCKWISE", "KNIGHT_RIDER_ALTERNATING",
        "RANDOM_PIXEL_FLASH", "STROBE", "PARTY",
    }
    assert set(cmds.AUDIO_ALGOS.values()) <= live


def test_hues_in_range(cmds):
    assert all(0 <= h <= 255 for h in cmds.HUES.values())


def test_help_lists_only_working_things(cmds):
    h = cmds.help_text()
    assert "канал" not in h.lower(), "каналов нет: список пуст"
    low = h.lower()
    assert "«громче»" not in low and "«тише" not in low, "громкость отключена"
    assert "оптике" in low, "подсказка обязана объяснять, почему нет громкости"
    assert "youtube" not in h.lower() and "ютуб" not in h.lower()
    assert "«включи телевизор»" in h and "пробуждения по сети" in h   # 03.11: Wake-on-LAN


# --------------------------------------------------------------------------
# 8. Сверка прочитанного с запрошенным (дефект 27.10, 23:10)
# --------------------------------------------------------------------------
# Живая команда «подсветка музыка» ответила «FOLLOW_VIDEO/NATURAL» — чужим
# режимом. Ответ строился чтением состояния сразу после записи, и
# телевизор отдал ещё старое значение. Повторный вызов ответил верно.
#
# Вывод дня в третий раз: на этой модели результат не доказывают ни код
# 200, ни чтение сразу после записи. Поэтому ответ обязан показывать
# расхождение, а не печатать прочитанное как результат.


class StaleTV(FakeTV):
    """Двойник телевизора, который на чтение отдаёт ПРЕДЫДУЩЕЕ состояние."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self._stale = dict(self._style)

    def set_ambilight_style(self, style, setting=None):
        super().set_ambilight_style(style, setting)
        self._style, self._stale = self._stale, dict(self._style)

    def set_ambilight_power(self, on):
        prev = self._power
        super().set_ambilight_power(on)
        self._power = prev


def test_stale_read_is_reported_not_hidden(cmds):
    tv = StaleTV(style="FOLLOW_VIDEO", setting="NATURAL")
    out = cmds.parse("подсветка музыка").run(tv)
    assert "FOLLOW_AUDIO" in out, "запрошенный режим обязан быть в ответе"
    assert "FOLLOW_VIDEO" in out, "прочитанное значение обязано быть в ответе"
    assert "запрошено" in out


def test_stale_power_read_is_reported(cmds):
    tv = StaleTV(power="Off")
    out = cmds.parse("подсветка включи").run(tv)
    assert "запрошено" in out and "On" in out


def test_consistent_read_has_no_mismatch_note(cmds):
    """Контроль: при согласованном чтении лишнего текста быть не должно.

    Без этого теста сверка могла бы сообщать о расхождении всегда, и
    предыдущие два теста проходили бы, ничего не доказывая.
    """
    tv = FakeTV()
    out = cmds.parse("подсветка музыка").run(tv)
    assert "запрошено" not in out
    assert "FOLLOW_AUDIO" in out


def test_verified_helper_shows_both_values(cmds):
    assert cmds._verified("X", "A", "B") == "X: запрошено B, телевизор сообщает A"
    assert cmds._verified("X", "A", "A") == "X: A"
    assert cmds._verified("X", "A") == "X: A"


def test_readback_delay_default_is_nonzero(monkeypatch, cmds):
    """Пауза по умолчанию обязана быть ненулевой.

    Нулевая пауза вернула бы ровно то поведение, на котором дефект и
    проявился, а тесты этого не заметили бы: они паузу отключают.
    """
    monkeypatch.delenv("TV_READBACK_DELAY_S", raising=False)
    assert cmds.readback_delay() > 0


@pytest.mark.parametrize("raw", ["мусор", "-1", ""])
def test_readback_delay_bad_value_falls_back(monkeypatch, cmds, raw):
    monkeypatch.setenv("TV_READBACK_DELAY_S", raw)
    assert cmds.readback_delay() == cmds.READBACK_DELAY_S


def test_settle_sleeps_when_delay_set(monkeypatch, cmds):
    """Пауза действительно вызывает ожидание, а не только считается."""
    monkeypatch.setenv("TV_READBACK_DELAY_S", "0.25")
    slept = []
    monkeypatch.setattr(cmds.time, "sleep", lambda s: slept.append(s))
    cmds._settle()
    assert slept == [0.25]


def test_settle_does_not_sleep_at_zero(monkeypatch, cmds):
    monkeypatch.setenv("TV_READBACK_DELAY_S", "0")
    slept = []
    monkeypatch.setattr(cmds.time, "sleep", lambda s: slept.append(s))
    cmds._settle()
    assert slept == []


# --------------------------------------------------------------------------
# 9. Громкость: подтверждающее чтение после паузы (дефект 27.10, 23:29)
# --------------------------------------------------------------------------
# Замер на живом телевизоре: volume_step(10) от 17 записал 27, чтение через
# миллисекунды дало 26, мгновением позже — 27. При шаге с 12 на +10 бот
# показал 16. Телевизор наезжает на значение не мгновенно, и немедленный
# GET попадает в середину перехода.
#
# Паузу я тогда добавил ТОЛЬКО в подсветку и не проверил путь громкости —
# второй за день случай, когда правка легла в одну ветку из двух.


class RampTV(FakeTV):
    """Двойник телевизора, который доезжает до значения не сразу.

    `get_volume` отдаёт промежуточное значение, пока не произошло
    ожидание. Так тест отличает «прочитал сразу» от «прочитал после
    паузы»: без паузы ответ будет промежуточным.
    """

    def __init__(self, **kw):
        super().__init__(**kw)
        self._target = self._v["current"]
        self._settled = True

    def _move(self, target):
        self._target = target
        self._settled = False
        # На полпути: именно это значение и видел живой бот.
        self._v["current"] = (self._v["current"] + target) // 2

    def arrive(self):
        self._settled = True
        self._v["current"] = self._target

    def volume_step(self, delta):
        import tv_control
        self.calls.append(("volume_step", delta))
        ceiling = tv_control.volume_ceiling(self._v["min"], self._v["max"])
        now = self._v["current"]
        target = now + delta
        if delta >= 0:
            target = min(target, max(ceiling, now))
        self._move(max(self._v["min"], min(self._v["max"], target)))
        return self._target

    def set_volume(self, level, muted=False):
        import tv_control
        self.calls.append(("set_volume", level))
        ceiling = tv_control.volume_ceiling(self._v["min"], self._v["max"])
        self._move(min(int(level), ceiling))
        return self._target

    def set_muted(self, muted):
        self.calls.append(("set_muted", muted))
        self._v["muted"] = muted


@pytest.fixture
def ramping(monkeypatch, cmds):
    """Пауза включена, а ожидание подменено прибытием телевизора."""
    monkeypatch.setenv("TV_READBACK_DELAY_S", "0.5")
    tv = RampTV(volume=17, maximum=60)
    monkeypatch.setattr(cmds.time, "sleep", lambda _s: tv.arrive())
    return tv


def test_volume_step_reports_settled_value(cmds, ramping):
    """Ответ показывает конечное значение, а не середину перехода."""
    out = cmds.parse("громче на 10").run(ramping)
    assert "27/60" in out
    assert "22/60" not in out


def test_volume_set_reports_settled_value(cmds, ramping):
    out = cmds.parse("громкость 25").run(ramping)
    assert "25/60" in out


def test_mute_reports_after_settle(cmds, ramping):
    out = cmds.parse("без звука").run(ramping)
    assert "Звук выключен" in out and "без звука" in out


def test_without_settle_the_answer_would_be_wrong(cmds, monkeypatch):
    """Контроль: без паузы ответ действительно промежуточный.

    Этот тест доказывает, что предыдущие три проверяют именно паузу, а не
    случайное совпадение чисел. Если убрать `_settle` из `_vol_confirm`,
    падут они, а этот — пройдёт.
    """
    monkeypatch.setenv("TV_READBACK_DELAY_S", "0")
    tv = RampTV(volume=17, maximum=60)
    out = cmds.parse("громче на 10").run(tv)
    assert "22/60" in out


def test_mismatch_after_settle_shows_both_numbers(cmds, monkeypatch):
    """Если телевизор и после паузы сообщает своё — видно оба числа."""
    monkeypatch.setenv("TV_READBACK_DELAY_S", "0")

    class Stubborn(FakeTV):
        def volume_step(self, delta):
            self.calls.append(("volume_step", delta))
            return 27          # отправили 27

        def get_volume(self):
            self.calls.append(("get_volume",))
            return {"muted": False, "current": 24, "min": 0, "max": 60}

    out = cmds.parse("громче на 10").run(Stubborn())
    assert "отправлено 27" in out and "24" in out


def test_volume_path_uses_settle(cmds, monkeypatch):
    """Прямая проверка: путь громкости обязан вызывать ожидание.

    Сверка числами может совпасть случайно; это — факт вызова.
    """
    monkeypatch.setenv("TV_READBACK_DELAY_S", "0.3")
    waited = []
    monkeypatch.setattr(cmds.time, "sleep", lambda s: waited.append(s))
    cmds.parse("громче").run(FakeTV(volume=10))
    assert waited == [0.3]


# --------------------------------------------------------------------------
# 03.11: Wake-on-LAN (tv_control.send_wol) -- без сети, сокет подменён
# --------------------------------------------------------------------------

def test_wol_packet_format():
    import tv_control
    p = tv_control.wol_packet("D4-0A-DC-4D-F6-2C")
    assert len(p) == 102 and p[:6] == b"\xff" * 6 and p[6:12] == bytes.fromhex("d40adc4df62c")
    with pytest.raises(ValueError):
        tv_control.wol_packet("12:34")


def test_send_wol_targets_and_repeats(monkeypatch):
    import tv_control
    sent = []

    class Sock:
        def __init__(self, *a): pass
        def setsockopt(self, *a): pass
        def sendto(self, pkt, addr): sent.append(addr)
        def close(self): pass

    monkeypatch.setattr(tv_control.socket, "socket", Sock)
    monkeypatch.setattr(tv_control.time, "sleep", lambda s: None)
    out = tv_control.send_wol("d4:0a:dc:4d:f6:2c", "10.0.0.41", repeats=2)
    assert len(out) == 2 * 3 * 2                                  # 2 повтора x 3 адреса x порты 9/7
    assert ("255.255.255.255", 9) in sent and ("10.0.0.255", 9) in sent and ("10.0.0.41", 7) in sent


def test_send_wol_default_five_series_one_second_apart(monkeypatch):
    """03.11 17:24: 3 серии за 1 с не разбудили ТВ; проба с сериями через 1 с -- будила."""
    import tv_control
    slept = []

    class Sock:
        def __init__(self, *a): pass
        def setsockopt(self, *a): pass
        def sendto(self, pkt, addr): pass
        def close(self): pass

    monkeypatch.setattr(tv_control.socket, "socket", Sock)
    monkeypatch.setattr(tv_control.time, "sleep", slept.append)
    out = tv_control.send_wol("d4:0a:dc:4d:f6:2c", "10.0.0.41")
    assert len(out) == 5 * 3 * 2 and slept == [1.0] * 4


def test_wake_without_mac_is_explicit():
    import tv_control
    tv = tv_control.TVClient(host="10.0.0.41", username="u", password="p", session=object())
    with pytest.raises(ValueError, match="TV_MAC"):
        tv.wake()
