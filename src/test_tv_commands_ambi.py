"""tv_commands: яркость/насыщенность/«авто» подсветки через меню настроек (30.10.2026). Сеть не трогается."""
import pytest

import tv_commands as C


class TV:
    def __init__(self, b=4, s=-2):
        self.vals = {710: b, 720: s}
        self.writes = []
        self.enums = []
        self.power = []

    def get_setting_value(self, node):
        return self.vals[node]

    def set_setting_value(self, node, value, lo, hi):
        v = max(lo, min(hi, int(value)))
        self.writes.append((node, v))
        self.vals[node] = v
        return v

    def set_setting_enum(self, node, enum_id):
        self.enums.append((node, enum_id))
        return enum_id

    def set_ambilight_power(self, on):
        self.power.append(on)


def run(phrase, tv):
    c = C.parse(phrase)
    assert c.kind == C.VERIFIED
    return c.name, c.run(tv)


@pytest.mark.parametrize("phrase,name,val,text", [
    ("подсветка ярче", "amb_brighter", 5, "Подсветка: яркость 5 из 10"),
    ("подсветка ярче на 3", "amb_brighter", 7, "Подсветка: яркость 7 из 10"),
    ("подсветка тусклее", "amb_dimmer", 3, "Подсветка: яркость 3 из 10"),
    ("Подсветку темнее.", "amb_dimmer", 3, "Подсветка: яркость 3 из 10"),
    ("подсветка 7", "amb_brightness_set", 7, "Подсветка: яркость 7 из 10"),
    ("яркость подсветки 2", "amb_brightness_set", 2, "Подсветка: яркость 2 из 10"),
])
def test_brightness(phrase, name, val, text):
    tv = TV()
    n, out = run(phrase, tv)
    assert n == name and tv.vals[710] == val and out == text


def test_brightness_at_limit_no_write():
    tv = TV(b=10)
    _, out = run("подсветка ярче", tv)
    assert tv.writes == [] and out == "Подсветка: яркость 10 из 10 — уже предел"


def test_brightness_over_range_clamped_and_said():
    tv = TV(b=4)
    _, out = run("подсветка 15", tv)
    assert tv.vals[710] == 10 and "просили 15" in out


def test_same_value_no_write():
    tv = TV(b=4)
    _, out = run("подсветка 4", tv)
    assert tv.writes == [] and out == "Подсветка: яркость 4 из 10"


def test_saturation():
    tv = TV(s=-2)
    n, out = run("подсветка насыщеннее", tv)
    assert n == "amb_sat_up" and tv.vals[720] == -1 and out == "Подсветка: насыщенность -1 (от -2 до +2)"
    _, out = run("подсветка бледнее", tv)
    assert tv.vals[720] == -2
    _, out = run("подсветка бледнее", tv)
    assert "уже предел" in out


def test_ai_mode_turns_power_on_and_sets_enum():
    tv = TV()
    n, out = run("подсветка авто", tv)
    assert n == "amb_ai" and tv.power == [True] and tv.enums == [(320, 321)] and "ИИ" in out


@pytest.mark.parametrize("phrase,name", [
    ("подсветка выключи", "amb_off"), ("подсветка музыка", "amb_audio"), ("подсветка кино", "amb_video"),
    ("подсветка красная 100", "amb_colour"), ("подсветка включи", "amb_on"),
])
def test_old_ambilight_commands_unchanged(phrase, name):
    assert C.parse(phrase).name == name


def test_help_mentions_brightness():
    assert "подсветка ярче" in C.help_text()


def test_no_brightness_without_ambilight_word():
    with pytest.raises(C.Unknown):
        C.parse("ярче")


@pytest.mark.parametrize("phrase,val", [("Яркость на три.", 3), ("яркость 8", 8), ("яркость на 3", 3)])
def test_brightness_word_alone_sets_level(phrase, val):
    """30.10 голосом: «Яркость на три.» -> «не понял команду». Слово «яркость» ведёт в подсветку."""
    tv = TV()
    n, out = run(phrase, tv)
    assert n == "amb_brightness_set" and tv.vals[710] == val and out.startswith("Подсветка: яркость")


def test_brightness_word_without_number_reads():
    tv = TV(b=6)
    n, out = run("какая яркость", tv)
    assert n == "amb_brightness_get" and out == "Подсветка: яркость 6 из 10" and tv.writes == []


def test_volume_not_caught_by_brightness():
    assert C.parse("громкость 10").name == "volume_set"


def test_word_numbers_in_brighter():
    tv = TV(b=4)
    n, out = run("Подсветка ярче на три.", tv)
    assert n == "amb_brighter" and tv.vals[710] == 7


def test_word_number_does_not_hit_inside_words():
    assert C._num("подсветка тристан") is None


@pytest.mark.parametrize("phrase,n", [
    ("громкость двадцать", 20), ("Громкость двадцать.", 20), ("громкость двадцать пять", 25),
    ("громкость пятнадцать", 15), ("громкость сорок", 40), ("громкость десять", 10), ("громкость 20", 20),
])
def test_volume_word_numbers(phrase, n):
    """30.10 13:11 живьём: «Громкость двадцать.» -> бот прочитал громкость вместо установки."""
    c = C.parse(phrase)
    assert c.name == "volume_set" and c.meta["level"] == n


def test_words_number_edges():
    assert C._num("вниз раз") is None
    assert C._num("двадцать раз") == 20
    assert C._num("сто") == 100
    assert C._num("громкость") is None


class VolTV(TV):
    def __init__(self, cur=20):
        super().__init__()
        self.cur = cur
        self.steps = []

    def get_volume(self):
        return {"current": self.cur, "max": 60, "muted": False}

    def volume_step(self, delta):
        self.steps.append(delta)
        self.cur = max(0, min(60, self.cur + delta))
        return self.cur

    def set_volume(self, level):
        raise AssertionError("плюс/минус не должны ставить значение")


@pytest.mark.parametrize("phrase,step", [
    ("Громкость плюс пять.", 5), ("громкость минус двадцать", -20), ("громкость +5", 5),
    ("громкость -10", -10), ("громкость прибавь на 3", 3), ("громкость убавь на 2", -2),
])
def test_volume_plus_minus_is_step(phrase, step, monkeypatch):
    """30.10 13:37 живьём: «Громкость плюс пять» поставила 5, «минус двадцать» — 20."""
    monkeypatch.setattr(C, "_settle", lambda: None)
    c = C.parse(phrase)
    tv = VolTV(cur=20)
    out = c.run(tv)
    assert tv.steps == [step] and out.startswith("Громкость")


@pytest.mark.parametrize("phrase,val", [
    ("яркость плюс 2", 6), ("яркость минус 3", 1), ("подсветка +2", 6), ("подсветка -3", 1),
    ("подсветка плюс два", 6), ("яркость минус десять", 0),
])
def test_brightness_plus_minus_is_step(phrase, val):
    tv = TV(b=4)
    c = C.parse(phrase)
    c.run(tv)
    assert c.name == "amb_step" and tv.vals[710] == val


def test_saturation_plus_minus_and_set():
    tv = TV(s=-2)
    C.parse("насыщенность плюс 2").run(tv)
    assert tv.vals[720] == 0
    C.parse("насыщенность минус 1").run(tv)
    assert tv.vals[720] == -1
    C.parse("подсветка насыщенность 2").run(tv)
    assert tv.vals[720] == 2


def test_plain_value_still_sets():
    assert C.parse("громкость двадцать").name == "volume_set"
    assert C.parse("яркость 3").name == "amb_brightness_set"


def test_sign_helper():
    assert C._sign("громкость плюс 5") == 1 and C._sign("громкость минус 5") == -1
    assert C._sign("громкость 5") == 0 and C._sign("плюс минус 5") == 0
    assert C._sign("назад-быстро") == 0


@pytest.mark.parametrize("phrase,step", [
    ("Громкость чуть ниже.", -1), ("Громкость пониже.", -3), ("Громкость потише.", -3),
    ("громкость погромче", 3), ("громкость чуть-чуть выше", 1), ("громкость немного больше", 1),
])
def test_volume_direction_without_number(phrase, step, monkeypatch):
    """30.10 14:12 живьём: «Громкость пониже / чуть ниже / потише» -> бот только читал «25/60»."""
    monkeypatch.setattr(C, "_settle", lambda: None)
    tv = VolTV(cur=25)
    C.parse(phrase).run(tv)
    assert tv.steps == [step]


def test_volume_plain_still_reads():
    assert C.parse("громкость").name == "volume_get"
    assert C.parse("какая громкость").name == "volume_get"


@pytest.mark.parametrize("phrase,val", [("Яркость на максимум.", 10), ("яркость на минимум", 0),
                                        ("подсветка на полную", 10), ("яркость пониже", 3),
                                        ("яркость повыше", 5)])
def test_brightness_extremes_and_direction(phrase, val):
    tv = TV(b=4)
    C.parse(phrase).run(tv)
    assert tv.vals[710] == val


def test_brightness_word_number_two_words():
    tv = TV(b=4)
    c = C.parse("Подсветка десять.")
    assert c.name == "amb_brightness_set"
    c.run(tv)
    assert tv.vals[710] == 10


@pytest.mark.parametrize("phrase", ["подсветка стандартная", "Подсветка стандартный.", "подсветка обычная"])
def test_standard_forms_are_rules_not_model(phrase):
    c = C.parse(phrase)
    assert c.name == "amb_video" and c.meta["setting"] == "STANDARD"


def test_volume_max_min():
    assert C.parse("громкость на максимум").meta["level"] == 60
    assert C.parse("громкость на минимум").meta["level"] == 0
