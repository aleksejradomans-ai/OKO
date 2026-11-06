"""tv_commands.py — разбор текстовой команды в действие на телевизоре.

Слой между человеком и `tv_control.TVClient`. Нужен отдельно от бота,
потому что у него два потребителя: команды Telegram сейчас и голос позже
(распознавание речи даёт тот же русский текст, разбирать его должен тот
же код, иначе два набора правил разойдутся).

ПРИНЦИП ЧЕСТНОГО ОТВЕТА. Команды делятся на два класса, и класс виден в
ответе человеку:
- `verified` — результат читается обратно через GET. Бот говорит, что
  получилось: «громкость 14», «подсветка FOLLOW_AUDIO/VU_METER».
- `blind` — нажатие кнопки. `POST input/key` отдаёт 200 на ЛЮБОЕ имя
  (проверено на 47 кнопках 27.10), поэтому подтвердить выполнение
  нечем. Бот говорит «отправил», а не «сделал».
Смешивать их нельзя: «открыл YouTube» на слепом нажатии — это ложь
системы о собственном состоянии, и отлаживать её потом невозможно.

Что НЕ попало в словарь и почему:
- каналы: `channelLists/all` вернул пустой список, эфир не настроен;
- приложения (YouTube, Netflix): `applications` закрыт 403, DIAL на 8008
  мёртв (404 на всём). Остаётся макрос из нажатий, но его последователь-
  ность зависит от того, что реально открывают `Online`/`Home` — без
  визуальной проверки это гадание, поэтому макросов здесь пока нет;
- включение телевизора через API: `powerstate` 404, запись `screenstate` молча
  игнорируется (проверено 27.10). 03.11: включение -- пакетом Wake-on-LAN
  (в ТВ «Включение по сигналу Wi-Fi»), проверено дважды по кадру камеры.
  Ответа у пакета нет -- результат сообщает проверка по кадру (bot.py), не код.
"""
from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import amb_music   # AMB_MUSIC (04.11): разбор «под музыку <цвет>/переход» и пульт моста; без сети при импорте

VERIFIED = "verified"
BLIND = "blind"


@dataclass(frozen=True)
class Command:
    """Разобранная команда. `run(tv)` возвращает текст для человека."""

    kind: str                      # VERIFIED | BLIND
    name: str                      # для логов, без текста пользователя
    run: Callable[[Any], str]
    needs_confirm: bool = False
    meta: dict[str, Any] = field(default_factory=dict)


class Unknown(Exception):
    """Текст не распознан. Отдельный тип, чтобы бот мог ответить подсказкой."""


class Refusal(Unknown):
    """30.10: команда ПОНЯТА, но сознательно не выполняется (включение по сети, приложения).
    Отдельно от «не понял»: такой отказ не отдаётся модели смысла (tv_intent) — та «поняла» бы его
    как что-то выполнимое и обошла честный отказ."""


# --- нормализация -----------------------------------------------------------

def normalize(text: str) -> str:
    """Нижний регистр, `ё` -> `е`, схлопывание пробелов и мусора.

    `ё` приводится намеренно: распознавание речи и клавиатура дают разные
    варианты одного слова («тёплый»/«теплый»), и без этого один и тот же
    запрос попадал бы то в правило, то в Unknown.
    """
    t = (text or "").lower().replace("ё", "е")
    t = re.sub(r"[^a-zа-я0-9\s:+-]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


# --- словари ---------------------------------------------------------------

# Кнопки: разговорное имя -> имя в API. Значения обязаны существовать в
# tv_control.KEYS, это проверяется тестом — иначе опечатка здесь всплыла
# бы только в момент живой команды.
KEY_WORDS: dict[str, str] = {
    "вверх": "CursorUp", "вниз": "CursorDown",
    "влево": "CursorLeft", "вправо": "CursorRight",
    "ок": "Confirm", "выбрать": "Confirm", "подтверди": "Confirm",
    "назад": "Back", "инфо": "Info", "информация": "Info",
    "меню": "Home", "домой": "Home", "home": "Home",
    "опции": "Options", "настройки": "Options",
    "поиск": "Find", "найти": "Find",
    "портал": "Online", "приложения": "Online", "смарт": "Online",
    "тв": "WatchTV", "телевидение": "WatchTV",
    "источник": "Source", "вход": "Source",
    "субтитры": "Subtitle", "телетекст": "Teletext",
    "пауза": "PlayPause", "играй": "PlayPause", "плей": "PlayPause",
    "стоп": "Stop", "вперед": "FastForward", "назад-быстро": "Rewind",
    "дальше": "Next", "предыдущий": "Previous",
}

# Как кнопка называется в ответе человеку (29.10, Оператор: «понятные значения выводов»).
# Ответ говорит «отправил», а не «сделал» — класс blind сохраняется, меняется только имя.
KEY_SAY: dict[str, str] = {
    "CursorUp": "вверх", "CursorDown": "вниз", "CursorLeft": "влево", "CursorRight": "вправо",
    "Confirm": "ОК", "Back": "назад", "Info": "инфо", "Home": "домой", "Options": "опции",
    "Find": "поиск", "Online": "портал", "WatchTV": "ТВ", "Source": "источник",
    "Subtitle": "субтитры", "Teletext": "телетекст", "PlayPause": "пауза/плей", "Stop": "стоп",
    "FastForward": "перемотка вперёд", "Rewind": "перемотка назад", "Next": "дальше",
    "Previous": "предыдущий", "Standby": "выключение (режим ожидания)",
}


def key_say(key: str) -> str:
    return KEY_SAY.get(key, key)


# Алгоритмы FOLLOW_AUDIO из ambilight_supportedstyles.json. Ключ — слово,
# которым человек это называет.
AUDIO_ALGOS: dict[str, str] = {
    "музыка": "ENERGY_ADAPTIVE_BRIGHTNESS",
    "музыку": "ENERGY_ADAPTIVE_BRIGHTNESS",
    "эквалайзер": "VU_METER",
    "индикатор": "VU_METER",
    "спектр": "SPECTRUM_ANALYZER",
    "анализатор": "SPECTRUM_ANALYZER",
    "вечеринка": "PARTY",
    "вечеринку": "PARTY",
    "дискотека": "PARTY",
    "строб": "STROBE",
    "стробоскоп": "STROBE",
    "всполохи": "RANDOM_PIXEL_FLASH",
    "рыцарь": "KNIGHT_RIDER_CLOCKWISE",
    "бегущий": "KNIGHT_RIDER_ALTERNATING",
}

# Пресеты FOLLOW_VIDEO. ВНИМАНИЕ: телевизор не проверяет значение
# menuSetting — на `CINEMA` он ответил 200 и выставил NATURAL (живая
# проверка 27.10). То есть неверное имя не вызывает ошибку, а молча даёт
# другой режим. Здесь оставлены только значения, прочитанные обратно с
# устройства; остальные слова ведут на STANDARD, пока не подтверждены
# пробой `tv_probe.py --amb-style FOLLOW_VIDEO:<ИМЯ>`.
VIDEO_SETTINGS: dict[str, str] = {
    "видео": "STANDARD",
    "обычный": "STANDARD",
    "стандарт": "STANDARD",
    "стандартная": "STANDARD", "стандартный": "STANDARD", "стандартную": "STANDARD",   # 30.10 голосом
    "обычная": "STANDARD", "обычную": "STANDARD",
    "кино": "NATURAL",
    "фильм": "NATURAL",
    "натуральный": "NATURAL",
    "игра": "GAME",
    "игры": "GAME",
}

# Оттенок 0..255. Значения приблизительные: прямого соответствия «слово ->
# hue» телевизор не отдаёт, проверен только факт записи (0,100,255).
# Поэтому это удобная таблица, а не подтверждённый факт — помечено в
# ответе словом «примерно» для цветов, кроме красного (hue=0 проверен).
# 02.11 (TV_HUES_FORMS): все формы (род/падеж). «podsvetka zeltaja» в игре уходила в мозг: не было «желтая»,
# а словарь транслита tv_nav.vocabulary() строится из КЛЮЧЕЙ этой таблицы -- поэтому формы ключами, не регуляркой.
# Модель смысла (tv_intent) в игровом режиме отключена, цвет обязан узнаваться правилами.
def _hue_forms(hard: str, soft: bool = False) -> tuple[str, ...]:
    """«желт» -> желтый/желтая/желтую/желтое; soft («син») -> синий/синяя/синюю/синее; «голуб» -- -ой."""
    if soft:
        return (hard + "ий", hard + "яя", hard + "юю", hard + "ее")
    first = hard + ("ой" if hard == "голуб" else "ый")
    return (first, hard + "ая", hard + "ую", hard + "ое")


_HUE_STEMS: tuple[tuple[str, bool, int], ...] = (
    ("красн", False, 0), ("оранжев", False, 20), ("желт", False, 40), ("зелен", False, 85),
    ("бирюзов", False, 128), ("голуб", False, 150), ("син", True, 170), ("фиолетов", False, 200),
    ("сиренев", False, 210), ("розов", False, 230), ("бел", False, 0), ("тепл", False, 25),
    ("холодн", False, 160),
)
HUES: dict[str, int] = {w: hue for stem, soft, hue in _HUE_STEMS for w in _hue_forms(stem, soft)}


# 30.10: голос даёт числа словами («ярче на три») — без этого число терялось.
NUM_WORDS: dict[str, int] = {
    "ноль": 0, "один": 1, "одну": 1, "одна": 1, "раз": 1, "два": 2, "две": 2, "три": 3, "четыре": 4, "пять": 5,
    "шесть": 6, "семь": 7, "восемь": 8, "девять": 9, "десять": 10,
}
# 30.10 13:11: «Громкость двадцать» -> число не найдено -> команда стала ЧТЕНИЕМ громкости («10/60»).
TEENS: dict[str, int] = {
    "одиннадцать": 11, "двенадцать": 12, "тринадцать": 13, "четырнадцать": 14, "пятнадцать": 15,
    "шестнадцать": 16, "семнадцать": 17, "восемнадцать": 18, "девятнадцать": 19,
}
TENS: dict[str, int] = {
    "двадцать": 20, "тридцать": 30, "сорок": 40, "пятьдесят": 50, "шестьдесят": 60,
    "семьдесят": 70, "восемьдесят": 80, "девяносто": 90, "сто": 100,
}


def _words_number(text: str) -> int | None:
    """Первое число словами: «двадцать пять» -> 25, «пятнадцать» -> 15, «три» -> 3. «раз» числом не считается."""
    words = re.findall(r"[a-zа-я]+", text)
    for i, w in enumerate(words):
        if w in TENS:
            nxt = words[i + 1] if i + 1 < len(words) else ""
            unit = NUM_WORDS.get(nxt) if nxt != "раз" else None
            return TENS[w] + (unit if unit is not None and 1 <= unit <= 9 and TENS[w] < 100 else 0)
        if w in TEENS:
            return TEENS[w]
        if w in NUM_WORDS and w != "раз":
            return NUM_WORDS[w]
    return None


# 30.10 14:12: «громкость пониже / чуть ниже / потише», «яркость на максимум» -> бот только ЧИТАЛ значение.
_UP_WORDS = re.compile(r"(?<![a-zа-я])(выше|повыше|громче|погромче|ярче|поярче|больше|побольше|сильнее|"
                       r"прибав\w*|добав\w*|подним\w*|повыс\w*|плюс)(?![a-zа-я])")
_DOWN_WORDS = re.compile(r"(?<![a-zа-я])(ниже|пониже|тише|потише|тусклее|темнее|меньше|поменьше|слабее|"
                         r"убав\w*|опуст\w*|пониз\w*|сниз\w*|минус)(?![a-zа-я])")
_LITTLE = re.compile(r"(?<![a-zа-я])(чуть|чуть-чуть|немного|немножко|слегка)(?![a-zа-я])")
_MAX = re.compile(r"(?<![a-zа-я])(максимум\w*|на полную|до упора|максимальн\w*)(?![a-zа-я])")
_MIN = re.compile(r"(?<![a-zа-я])(минимум\w*|минимальн\w*)(?![a-zа-я])")


def _direction(text: str) -> int:
    """+1 / -1 по словам направления без числа («пониже», «чуть ниже», «погромче»); 0 — нет или оба."""
    up, down = bool(_UP_WORDS.search(text)), bool(_DOWN_WORDS.search(text))
    return 0 if up == down else (1 if up else -1)


_PLUS = re.compile(r"(?<![a-zа-я])(плюс|plus|прибав\w*|добав\w*|повыс\w*|подним\w*|выше)(?![a-zа-я])|\+\s*\d")
_MINUS = re.compile(r"(?<![a-zа-я])(минус|minus|убав\w*|пониз\w*|опуст\w*|сниз\w*|ниже)(?![a-zа-я])|(?<![\w])-\s*\d")


def _sign(text: str) -> int:
    """+1 / -1, если фраза просит ИЗМЕНИТЬ на величину («плюс 5», «минус три», «+2», «-3», «убавь на 2»); иначе 0."""
    plus, minus = bool(_PLUS.search(text)), bool(_MINUS.search(text))
    if plus == minus:
        return 0
    return 1 if plus else -1


def _num(text: str) -> int | None:
    m = re.search(r"(-?\d+)", text)
    if m:
        return int(m.group(1))
    return _words_number(text)


def _word_in(text: str, table: dict[str, Any]) -> tuple[str, Any] | None:
    """Ищет слово из таблицы как ОТДЕЛЬНОЕ слово, не подстроку.

    Подстрочный поиск — та самая ошибка, из-за которой классификатор
    knowledge_indexer относит «планету» к проектным заметкам, а любой
    .py с `except ValueError` — к траблшутингу. Здесь она дала бы
    «включи тв» -> совпадение внутри «телевизор», поэтому границы слов
    обязательны.
    """
    for word, value in table.items():
        if re.search(rf"(?<![a-zа-я]){re.escape(word)}(?![a-zа-я])", text):
            return word, value
    return None


# --- разбор ---------------------------------------------------------------

def _screen_off(tv: Any) -> str:
    import tv_menu_map
    import tv_nav
    keys = tv_menu_map.route("settings.general.power:Выкл. экран", press=True)
    if hasattr(tv, "send_keys"):
        tv.send_keys(keys)
    else:
        for k in keys:
            tv.send_key(k)
    return f"Отправил: {tv_nav.say(keys)} (меню «Выкл. экран»)"


def parse(text: str) -> Command:
    """Текст -> Command. Unknown, если правило не нашлось."""
    t = normalize(text)
    if not t:
        raise Unknown("пусто")

    # 30.10: «яркость на три» голосом — без слова «подсветка». Управляемая яркость у этой модели одна —
    # у подсветки (у «Изображения» узлов нет), поэтому «яркость» ведёт в подсветку; ответ это называет явно.
    # 03.11: «включи красный цвет» -- без слова «подсветка» (живая фраза 01:3x); цвет есть только у подсветки,
    # другие устройства маршрутизатор отсекает раньше (OTHER_DEVICE_RE). «ярче/тусклее» без предмета -- НЕ сюда:
    # неоднозначно (экран? подсветка?) -- маршрутизатор переспрашивает (CLARIFY).
    amb = bool(re.search(r"(?<![a-zа-я])(подсветк\w*|амбилайт|ambilight|яркост\w*|насыщенност\w*|цвет|цвета)(?![a-zа-я])", t))
    sign = _sign(t)

    # --- выключение телевизора: единственная команда с подтверждением ---
    if re.search(r"выключи\w*\s+(телевизор|тв)(?![a-zа-я])|режим ожидания|стендбай|standby", t):
        return Command(
            kind=BLIND, name="standby", needs_confirm=True,
            run=lambda tv: (tv.send_key("Standby"), f"Отправил: {key_say('Standby')}")[1],
        )

    if re.search(r"включи\w*\s+(телевизор|тв)(?![a-zа-я])", t) and not amb:
        # 03.11: Wake-on-LAN (кнопки питания в API нет). Слепая команда: ТВ не подтверждает -- смотрит камера.
        return Command(
            kind=BLIND, name="power_on",
            run=lambda tv: (tv.wake(), "Отправил: пробуждение телевизора по сети")[1],
        )

    # 05.11 (решение Оператора 17:50): «выключи экран» / «погаси экран» / «чёрный экран» -> только экран через меню.
    if re.search(
        r"(?<![a-zа-я])(((выключ|отключ|погас|потуш)\w*|черн\w+|выкл|офф|off)\s+экран\w*|"
        r"экран\w*\s+((выключ|отключ|погас|потуш)\w*|выкл|офф|off))(?![a-zа-я])",
        t,
    ) and not amb:
        return Command(
            kind=BLIND, name="screen_off",
            run=_screen_off,
        )

    # 06.11 (живой замер Оператора): экран после «Выкл. экран» будит 1 нажатие «вниз» (CursorDown), подсветку не трогает.
    if re.search(
        r"(?<![a-zа-я])(((включ|верн|разбуд|зажг|зажеч)\w*|вкл)\s+экран\w*|"
        r"экран\w*\s+((включ|верн|разбуд|зажг|зажеч)\w*|вкл))(?![a-zа-я])",
        t,
    ) and not amb:
        return Command(
            kind=BLIND, name="screen_on",
            run=lambda tv: (tv.send_key("CursorDown"), f"Отправил: {key_say('CursorDown')} (пробуждение экрана)")[1],
        )

    # --- подсветка ---
    if amb:
        if re.search(r"(выключ\w*|убери|офф|off)", t):
            return Command(
                kind=VERIFIED, name="amb_off", run=_amb_off,
            )
        # AMB_MUSIC (04.11, Оператор: «под музыку статичный цвет / переход, ползунки RGB»): «музык*» + цвет/RGB или
        # «переход/градиент» -> мост микрофон -> лампы поштучно (amb_music). «подсветка музыка» без цвета -- как было
        # (встроенный FOLLOW_AUDIO ниже). Ответ -- по статусу моста (кадры, режим ТВ прочитан), не по записи пульта.
        upd = amb_music.parse_music(t)
        if upd is not None:
            return Command(kind=VERIFIED, name="amb_music", meta=dict(upd), run=lambda tv: amb_music.start(upd))
        hit = _word_in(t, AUDIO_ALGOS)
        if hit:
            algo = hit[1]
            return Command(
                kind=VERIFIED, name="amb_audio", meta={"algorithm": algo},
                run=lambda tv: (tv.set_ambilight_power(True),
                                tv.set_ambilight_style("FOLLOW_AUDIO", algo),
                                _settle(),
                                _mode(_verified("Подсветка", _amb_style(tv), "FOLLOW_AUDIO"), "под музыку"))[3],
            )
        hit = _word_in(t, VIDEO_SETTINGS)
        if hit:
            setting = hit[1]
            return Command(
                kind=VERIFIED, name="amb_video", meta={"setting": setting},
                run=lambda tv: (tv.set_ambilight_power(True),
                                tv.set_ambilight_style("FOLLOW_VIDEO", setting),
                                _settle(),
                                _mode(_verified("Подсветка", _amb_style(tv), "FOLLOW_VIDEO"), "за картинкой"))[3],
            )
        hit = _word_in(t, HUES)
        if hit:
            word, hue = hit
            bri = _num(t)
            return Command(
                kind=VERIFIED, name="amb_colour", meta={"hue": hue, "word": word},
                # Порядок важен: СНАЧАЛА стиль FOLLOW_COLOR, потом цвет.
                # В обратном порядке (цвет -> стиль) живой телевизор 27.10
                # оставил hue 110/sat 38 — запись цвета вне режима lounge
                # отдаёт 200 и теряется. Ещё один случай, когда код ответа
                # ничего не значит; проверяется чтением после установки.
                run=lambda tv: (tv.set_ambilight_power(True),
                                tv.set_ambilight_style("FOLLOW_COLOR", "MANUAL_HUE"),
                                tv.set_lounge_color(hue, 0 if word.startswith("бел") else 255,
                                                    255 if bri is None else max(0, min(255, bri))),
                                _settle(),
                                f"Подсветка: {MODE_PREFIX}{MODE_COLOUR}, цвет {word}, {_lounge(tv)}")[4],
            )
        # 30.10: яркость/насыщенность/«авто» — через меню настроек (menuitems/settings, проверено
        # записью 710: 4->3->4 с чтением). Ответ — значение, прочитанное ПОСЛЕ записи (set_setting_*).
        n = _num(t)
        if re.search(r"(насыщеннее|ярче цвет|сочнее)", t):
            return Command(kind=VERIFIED, name="amb_sat_up", run=lambda tv: _amb_level(tv, SATURATION, +1))
        if re.search(r"(бледнее|блеклее|спокойнее)", t):
            return Command(kind=VERIFIED, name="amb_sat_down", run=lambda tv: _amb_level(tv, SATURATION, -1))
        if re.search(r"(ярче|поярче)", t):
            return Command(kind=VERIFIED, name="amb_brighter", meta={"step": abs(n or 1)},
                           run=lambda tv: _amb_level(tv, BRIGHTNESS, +abs(n or 1)))
        if re.search(r"(тусклее|темнее|потемнее|слабее|приглуши)", t):
            return Command(kind=VERIFIED, name="amb_dimmer", meta={"step": abs(n or 1)},
                           run=lambda tv: _amb_level(tv, BRIGHTNESS, -abs(n or 1)))
        if re.search(r"(?<![a-zа-я])(авто|автоматически|умная|ии|ai)(?![a-zа-я])", t):
            return Command(kind=VERIFIED, name="amb_ai", run=_amb_ai)
        # 30.10 13:37 (Оператор): «плюс 2 / минус 3» — ИЗМЕНЕНИЕ, а не значение. Раньше «плюс пять» ставило 5.
        if n is not None and sign:
            spec = SATURATION if re.search(r"насыщенн", t) else BRIGHTNESS
            delta = sign * abs(n)
            return Command(kind=VERIFIED, name="amb_sat_step" if spec is SATURATION else "amb_step",
                           meta={"delta": delta}, run=lambda tv: _amb_level(tv, spec, delta))
        if n is not None and re.search(r"насыщенност", t):
            return Command(kind=VERIFIED, name="amb_sat_set", meta={"level": n},
                           run=lambda tv: _amb_level(tv, SATURATION, None, n))
        if n is None and (_MAX.search(t) or _MIN.search(t)):
            lvl = BRIGHTNESS[3] if _MAX.search(t) else BRIGHTNESS[2]
            return Command(kind=VERIFIED, name="amb_brightness_set", meta={"level": lvl},
                           run=lambda tv: _amb_level(tv, BRIGHTNESS, None, lvl))
        if n is None and re.search(r"яркост", t) and _direction(t):
            step = _direction(t)
            return Command(kind=VERIFIED, name="amb_step", meta={"delta": step},
                           run=lambda tv: _amb_level(tv, BRIGHTNESS, step))
        if n is None and re.search(r"яркост", t):
            return Command(kind=VERIFIED, name="amb_brightness_get",
                           run=lambda tv: f"Подсветка: яркость {tv.get_setting_value(BRIGHTNESS[1])} из {BRIGHTNESS[3]}")
        # «подсветка 7» и «подсветка десять» (число словами — 30.10 уходило в модель)
        # 03.11: «подсветка на 6» (живой случай 01:2x -- уходило в модель смысла)
        if n is not None and (re.search(r"ярк", t) or re.fullmatch(r"\S+\s+(на\s+)?-?\d+", t) or len(t.split()) == 2):
            return Command(kind=VERIFIED, name="amb_brightness_set", meta={"level": n},
                           run=lambda tv: _amb_level(tv, BRIGHTNESS, None, n))
        if re.search(r"(включ\w*|он|on)", t):
            return Command(
                kind=VERIFIED, name="amb_on", run=_amb_on,
            )
        raise Unknown("подсветка: не понял режим")

    # --- звук ---
    # ГРОМКОСТЬ ВКЛЮЧЕНА 27.10 в 23:13 после живого замера Оператора.
    #
    # История важна, потому что она меняет смысл кода ответа. Днём 27.10
    # `POST audio/volume` отвечал 200 и менял число в API, а на слух не
    # менялось НИЧЕГО — звук шёл по оптике (S/PDIF) на внешние колонки
    # фиксированным уровнем. Тогда команда была отключена с отказом: бот,
    # уверенно отчитывающийся о результате, которого нет, хуже бота,
    # который честно отказывает.
    #
    # Оператор переключил вывод на динамики телевизора и проверил на слух:
    # громкость меняется. То есть дефекта в API не было вовсе — был другой
    # маршрут звука. Условие работоспособности: звук воспроизводит САМ
    # телевизор. При возврате вывода на оптику команда снова станет
    # бессмысленной, и обнаружить это по API нельзя: снятая 27.10 карта
    # `menuitems/settings` содержит только ветку подсветки, узла выбора
    # аудиовыхода в ней нет. Поэтому ответ сообщает ЧИСЛО, прочитанное из
    # телевизора, и не утверждает ничего о слышимости.
    #
    # Предел: контур не поднимает громкость выше половины диапазона
    # (`tv_control.volume_limit_fraction`). Автоматика получила право
    # менять звук сама, и ошибка в ней не должна бить по человеку.
    # Снятие mute проверяется ПЕРВЫМ и с границами слова: иначе «unmute»
    # ловится правилом включения тишины по подстроке «mute» и звук
    # выключается вместо включения. Найдено тестом, не теорией.
    if re.search(r"(включи звук|верни звук|со звуком|анмьют|(?<![a-zа-я])unmute(?![a-zа-я]))", t):
        return Command(
            kind=VERIFIED, name="mute_off",
            run=lambda tv: _mute(tv, False),
        )
    if re.search(r"(без звука|выключи звук|заглуши|мьют|(?<![a-zа-я])mute(?![a-zа-я]))", t):
        return Command(
            kind=VERIFIED, name="mute_on",
            run=lambda tv: _mute(tv, True),
        )
    if re.search(r"громкость", t):
        n = _num(t)
        if n is not None and sign:          # «громкость плюс пять» / «минус двадцать» / «+5» — шаг, не значение
            step = sign * abs(n)
            return Command(kind=VERIFIED, name="volume_up" if step > 0 else "volume_down",
                           meta={"step": abs(step)}, run=lambda tv: _vol_step(tv, step))
        if n is None and _MAX.search(t):
            return Command(kind=VERIFIED, name="volume_set", meta={"level": 60},   # предел контура обрежет
                           run=lambda tv: _vol_set(tv, 60))
        if n is None and _MIN.search(t):
            return Command(kind=VERIFIED, name="volume_set", meta={"level": 0}, run=lambda tv: _vol_set(tv, 0))
        d = _direction(t)
        if n is None and d:
            step = d * (1 if _LITTLE.search(t) else 3)
            return Command(kind=VERIFIED, name="volume_up" if step > 0 else "volume_down",
                           meta={"step": abs(step)}, run=lambda tv: _vol_step(tv, step))
        if n is None:
            return Command(kind=VERIFIED, name="volume_get",
                           run=lambda tv: f"Громкость: {_vol(tv)}")
        return Command(
            kind=VERIFIED, name="volume_set", meta={"level": n},
            run=lambda tv: _vol_set(tv, n),
        )
    if re.search(r"(громче|погромче|прибав\w+)", t):
        step = _num(t) or 3
        return Command(
            kind=VERIFIED, name="volume_up", meta={"step": step},
            run=lambda tv: _vol_step(tv, abs(step)),
        )
    if re.search(r"(тише|потише|убав\w+|сделай тихо)", t):
        step = _num(t) or 3
        return Command(
            kind=VERIFIED, name="volume_down", meta={"step": step},
            run=lambda tv: _vol_step(tv, -abs(step)),
        )

    # --- состояние ---
    if re.search(r"(статус|состояние|что там|что сейчас)", t):
        return Command(kind=VERIFIED, name="status", run=_status)

    # --- приложения: честный отказ, пока нет проверенного макроса ---
    if re.search(r"(ютуб|youtube|нетфликс|netflix|спотифай|spotify|прайм|prime)", t):
        raise Refusal(
            "запуск приложений по API недоступен: applications отдаёт 403, "
            "DIAL на 8008 не отвечает. Нужен макрос нажатий — он появится, "
            "когда будет известно, что открывает кнопка «портал»"
        )

    # --- кнопки ---
    hit = _word_in(t, KEY_WORDS)
    if hit:
        key = hit[1]
        return Command(
            kind=BLIND, name=f"key_{key}", meta={"key": key},
            run=lambda tv: (tv.send_key(key), f"Отправил: {key_say(key)}")[1],
        )

    raise Unknown("не понял команду")


# --- чтение состояния для ответов -----------------------------------------

READBACK_DELAY_S = 0.6
READBACK_DELAY_ENV = "TV_READBACK_DELAY_S"


def readback_delay() -> float:
    """Пауза перед подтверждающим чтением.

    ЗАЧЕМ. 27.10 в 23:10 живая команда «подсветка музыка» ответила
    «FOLLOW_VIDEO/NATURAL» — чужим режимом. Ответ строится чтением
    состояния сразу после записи, и телевизор отдал ещё старое значение;
    повторный вызов той же команды ответил верно. То есть на этой модели
    НЕ доказывает результат ни код 200, ни чтение сразу после записи.
    Пауза уменьшает частоту таких ответов, но не устраняет причину —
    поэтому рядом стоит сверка запрошенного с прочитанным.
    """
    raw = os.getenv(READBACK_DELAY_ENV, "")
    if not raw:
        return READBACK_DELAY_S
    try:
        value = float(raw)
    except ValueError:
        return READBACK_DELAY_S
    return value if value >= 0.0 else READBACK_DELAY_S


def _settle() -> None:
    delay = readback_delay()
    if delay > 0:
        time.sleep(delay)


# MODE_TEXT (03.11): режим подсветки -- словами в начале ответа. Живой случай 00:35: после «подсветка зелёная»
# ответ «Подсветка: цвет зеленая, hue 85…» режима не называл; на «в каком режиме телевизор» мозг (layers=0, kn=31)
# достроил «режим подсветки музыки» из примеров каталога. Ответ модуля идёт и в Telegram, и в факты (event_log) --
# режим, названный явно, закрывает пробел. Коды вендора остаются после режима (сверка, тесты).
MODE_PREFIX = "режим "
MODE_COLOUR = "один цвет (статичный)"


def _mode(text: str, human: str) -> str:
    """«Подсветка: X» -> «Подсветка: режим <human>, X». При расхождении сверки («запрошено …») режим НЕ
    добавляется: телевизор не подтвердил его -- называть его установленным было бы ложью."""
    head = "Подсветка: "
    if not text.startswith(head) or "запрошено" in text:
        return text
    return f"{head}{MODE_PREFIX}{human}, {text[len(head):]}"


# AMB_OFF (03.11, проба tools\amb_off_probe.py 11:24-11:25, глаза Оператора, «тв амби» 50PUS9010):
#   POST ambilight/power Off  -> чтение «Off», лампы ГОРЯТ (в режиме «один цвет» и в «за картинкой»);
#   стиль FOLLOW_COLOR + ambilight/lounge brightness 0 -> лампы ПОГАСЛИ; brightness 255 -> зелёный вернулся.
# Поэтому «выключить» = цвет с яркостью 0 (проверяется чтением lounge), «включить» = вернуть яркость тому же цвету.
# power Off больше не используется: его чтение -- ложное подтверждение (как громкость 27.10).
def _amb_off(tv: Any) -> str:
    c = (tv.get_lounge_color() or {}).get("color", {})
    hue, sat = int(c.get("hue") or 0), int(c.get("saturation") or 255)
    tv.set_ambilight_power(True)
    tv.set_ambilight_style("FOLLOW_COLOR", "MANUAL_HUE")
    tv.set_lounge_color(hue, sat, 0)
    _settle()
    got = (tv.get_lounge_color() or {}).get("color", {}).get("brightness")
    if got != 0:
        return f"Подсветка: запрошено погасить (яркость цвета 0), телевизор сообщает яркость {got}"
    return "Подсветка: выключена (цвет погашен: яркость 0)"


def _amb_on(tv: Any) -> str:
    c = (tv.get_lounge_color() or {}).get("color", {})
    tv.set_ambilight_power(True)
    if c.get("brightness") == 0:        # погашена через AMB_OFF -- вернуть яркость тому же цвету
        tv.set_ambilight_style("FOLLOW_COLOR", "MANUAL_HUE")
        tv.set_lounge_color(int(c.get("hue") or 0), int(c.get("saturation") or 255), 255)
    _settle()
    got = (tv.get_lounge_color() or {}).get("color", {})
    if c.get("brightness") == 0 and got.get("brightness") != 255:
        return f"Подсветка: запрошено включить (яркость цвета 255), телевизор сообщает {_lounge(tv)}"
    if c.get("brightness") == 0:
        return f"Подсветка: включена, {MODE_PREFIX}{MODE_COLOUR}, {_lounge(tv)}"
    return _onoff(_verified("Подсветка", _amb_power(tv), "On"))


def _onoff(text: str) -> str:
    """03.11 02:0x (Оператор): «Подсветка: режим выключена, Off» / «Подсветка: On» -- коды вместо слов.
    -> «Подсветка: выключена (Off)» / «Подсветка: включена (On)» (двоеточие -- подпись модуля для D_GUARD). Расхождение сверки -- как есть."""
    if "запрошено" in text:
        return text
    if text == "Подсветка: Off":
        return "Подсветка: выключена (Off)"
    if text == "Подсветка: On":
        return "Подсветка: включена (On)"
    return text


def _verified(label: str, actual: str, expected: str | None = None) -> str:
    """Текст ответа со сверкой. Расхождение показывается, а не скрывается.

    Прочитанное значение НЕ выдаётся за результат: если телевизор сообщает
    не то, что просили, человек видит оба значения и знает, что доверять
    ответу нельзя. Молча печатать прочитанное — та же ложь, что рапорт по
    коду 200.
    """
    if expected and expected not in actual:
        return f"{label}: запрошено {expected}, телевизор сообщает {actual}"
    return f"{label}: {actual}"


def _vol_confirm(tv: Any, sent: int | None, requested: int | None = None,
                 label: str = "Громкость") -> str:
    """Подтверждающее чтение громкости ПОСЛЕ паузы, со сверкой.

    ЗАЧЕМ ПАУЗА ИМЕННО ЗДЕСЬ. 27.10 в 23:29 замер показал: телевизор
    наезжает на новое значение не мгновенно. `volume_step(10)` от 17
    записал 27, чтение через миллисекунды дало 26, чтение мгновением
    позже — 27. При большом шаге расхождение больше: от 12 на +10 бот
    показал 16. То есть немедленный GET попадает в середину перехода.
    Пауза 0.5 с в замере возвращала уже установившееся значение.

    МОЙ ДЕФЕКТ, КОТОРЫЙ ЭТО ВСКРЫЛО. Паузу я добавил только в команды
    подсветки и не проверил второй путь — громкость. Это второй случай
    за день, когда правка применилась в одной ветке из двух (первый —
    NameError в калибровке). Поэтому пауза и сверка собраны здесь, в
    одном месте, через которое проходят ВСЕ команды звука.

    Прочитанное не выдаётся за результат: если оно расходится с
    отправленным, в ответе оба числа.
    """
    _settle()
    v = tv.get_volume() or {}
    now = v.get("current")
    text = f"{label}: {now}/{v.get('max')}"
    if v.get("muted"):
        text += ", без звука"
    if requested is not None and sent is not None and int(sent) < int(requested):
        text += f" (запрошено {requested}, предел контура {sent})"
    if sent is not None and now is not None and int(now) != int(sent):
        text += f" (отправлено {sent}, телевизор сообщает {now})"
    return text


def _vol_set(tv: Any, level: int) -> str:
    applied = tv.set_volume(level)
    return _vol_confirm(tv, applied, requested=level)


def _vol_step(tv: Any, delta: int) -> str:
    before = (tv.get_volume() or {}).get("current")
    after = tv.volume_step(delta)
    text = _vol_confirm(tv, after)
    if delta > 0 and before is not None and after is not None and int(after) <= int(before):
        text += " — предел контура, выше не поднимаю"
    return text


def _mute(tv: Any, muted: bool) -> str:
    """Mute тоже читается после паузы: тот же путь, тот же дефект."""
    tv.set_muted(muted)
    return _vol_confirm(tv, None, label="Звук выключен" if muted else "Звук включен")


def _vol(tv: Any) -> str:
    v = tv.get_volume() or {}
    s = f"{v.get('current')}/{v.get('max')}"
    return s + (", без звука" if v.get("muted") else "")


def _amb_power(tv: Any) -> str:
    return str((tv.get_ambilight_power() or {}).get("power"))


def _amb_style(tv: Any) -> str:
    c = tv.get_ambilight_config() or {}
    name = c.get("styleName")
    setting = c.get("menuSetting")
    return f"{name}/{setting}" if setting else str(name)


# Узлы меню настроек (номера — из tv_control; модельные, «тв амби» 50PUS9010).
BRIGHTNESS = ("яркость", 710, 0, 10)
SATURATION = ("насыщенность", 720, -2, 2)
FOLLOW_VIDEO_NODE, FOLLOW_VIDEO_AI = 320, 321


def _amb_level(tv: Any, spec: tuple, delta: int | None, absolute: int | None = None) -> str:
    """Ползунок подсветки: +/-шаг или точное значение. Ответ — факт после записи; у края — так и сказано."""
    label, node, lo, hi = spec
    cur = tv.get_setting_value(node)
    want = int(absolute) if absolute is not None else cur + int(delta)
    got = tv.set_setting_value(node, want, lo, hi) if max(lo, min(hi, want)) != cur else cur
    scale = f"{got} из {hi}" if lo == 0 else f"{got:+d} (от {lo} до +{hi})"
    text = f"Подсветка: {label} {scale}"
    if got == cur and want != cur:
        text += " — уже предел" if want > hi or want < lo else ""
    elif want != got:
        text += f" (просили {want}, предел {lo}…{hi})"
    return text


def _amb_ai(tv: Any) -> str:
    tv.set_ambilight_power(True)
    tv.set_setting_enum(FOLLOW_VIDEO_NODE, FOLLOW_VIDEO_AI)
    return "Подсветка: следует за видео, режим ИИ"


def _lounge(tv: Any) -> str:
    c = (tv.get_lounge_color() or {}).get("color", {})
    return f"hue {c.get('hue')}, sat {c.get('saturation')}, bri {c.get('brightness')}"


def _status(tv: Any) -> str:
    parts = [f"громкость {_vol(tv)}"]
    try:
        parts.append(f"подсветка {_amb_power(tv)} {_amb_style(tv)}")
    except Exception:
        parts.append("подсветка недоступна")
    try:
        parts.append(f"активность {(tv.get_current_activity() or {}).get('id')}")
    except Exception:
        pass
    return "; ".join(parts)


def help_text() -> str:
    """Подсказка для бота. Перечисляет только то, что реально работает."""
    return (
        "Подсветка: «подсветка выключи», «подсветка музыка», «подсветка вечеринка», "
        "«подсветка спектр», «подсветка кино», «подсветка игра», «подсветка красная».\n"
        "Под музыку своим цветом (микрофон ПК): «подсветка под музыку зелёная», «подсветка под музыку 0 120 255», "
        "«подсветка переход», «подсветка под музыку от синего до жёлтого»; ползунки RGB — /amb.\n"
        "Яркость подсветки: «подсветка ярче», «подсветка тусклее», «подсветка 7», «яркость плюс 2», "
        "«яркость минус 3», «подсветка насыщеннее/бледнее», «подсветка авто».\n"
        "Громкость: «громкость 20», «громкость плюс 5», «громкость минус 10».\n"
        "Кнопки: «вверх», «вниз», «влево», «вправо», «ок», «назад», «инфо», «меню», "
        "«портал», «источник», «пауза», «стоп».\n"
        "Состояние: «статус».\n"
        "Выключить: «выключи телевизор» (спросит подтверждение).\n"
        "Включить: «включи телевизор» — пакет пробуждения по сети (в ТВ должно быть «Включение по сигналу Wi-Fi»).\n"
        "Громкость тоже нельзя: звук идёт по оптике, регулировка на пульте колонок."
    )
