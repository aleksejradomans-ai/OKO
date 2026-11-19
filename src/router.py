"""ROUTER (этап B, 01.11.2026): общий маршрутизатор свободного текста и голоса.

Проблема (глобальный тест 30.10): скрипты прикручены к боту, а не к мозгу. Голос вне
/tv -> заглушка; «подсветка жёлтая» текстом -> чат-модель без доступа к ТВ.

Маршрут (порядок важен):
  self   -- вопрос о себе («что умеешь») -> capabilities.human_text(), без модели;
  garmin -- «как я спал», «сколько шагов» -> garmin_report.run(kind);
  devices -- «когда закончит сушилка», «что со стиральной» -> device_watch.state_text()
            (этап C: состояние из памяти детектора, без сети; управления нет);
  tv     -- команда подсветки / громкости / выключения ТВ -> tv_bot_handler.handle
            (тот же проверенный код, что /tv: права, подтверждение, честный ответ);
  brain  -- всё остальное -> process_logic (мозг).

ПОЧЕМУ ТВ В СВОБОДНОМ ТЕКСТЕ УЗКО. Докстринг handle_tv объясняет, зачем был префикс:
разбор ТВ ищет слова ГДЕ УГОДНО во фразе («статус», «назад», «тише», «он» внутри
«телефон»), и весь поток через него означал бы ложные нажатия. Поэтому без /tv:
  1. только классы команд с явным предметом: подсветка (amb_*) при слове
     «подсветка/амбилайт/яркость/насыщенность», громкость (volume_*) при слове
     «громкость», звук (mute_*) при слове «звук/mute», выключение (standby) и честный
     «включи телевизор» (03.11: Wake-on-LAN, было -- честный отказ);
  2. кнопки, серии, «статус», макросы -- ТОЛЬКО в режиме /tv (ложное нажатие в меню
     хуже, чем лишняя фраза «/tv»);
  3. вопрос («?», «почему», «что такое», «расскажи»...) и упоминание другого
     устройства («клавиатура», «телефон», «ноутбук»...) -> мозг;
  4. длинная фраза (> TV_MAX_WORDS слов) -> мозг: команды короткие, рассуждения длинные.
Если правила фразу не узнали, но предмет есть -- модель смысла (tv_intent, закрытый
каталог), её ответ снова проходит разбор правилами И проверку класса из п. 1.

Модуль не знает о Telegram и не пишет в память/реестр: route() -> Route, run_*() ->
(текст, исход). Побочные эффекты (ответ, история, реестр) -- в bot.py.
Текст пользователя в журнал не пишется.
"""
from __future__ import annotations

import datetime as _dt
import logging
import re
from dataclasses import dataclass
from typing import Any, Callable, Optional

import layout_fix
import role_guard        # сторож самоидентификации (контур B) -- без импорта бота/железа
import tv_commands

logger = logging.getLogger("oko.router")

SELF, GARMIN, DEVICES, TV, BRAIN = "self", "garmin", "devices", "tv", "brain"
HINT = "hint"   # R1 (02.11): «как …» про ТВ -> готовые фразы, без модели
CLARIFY = "clarify"   # CLARIFY (03.11): неясно, к какому устройству просьба -> переспросить, без модели
TVSTATE = "tvstate"   # TV_STATE (03.11): «в каком режиме ТВ» -> последнее известное из журнала событий, без модели
HISTORY = "history"   # (12.11): «верни прошлый цвет» -> цвет из журнала фактов (код), не модель смысла
HOST = "host"         # (15.11): Host-Only -- абстрактный вопрос хозяина -> модель; без приводов, связок, шаблона
TV_MAX_WORDS = 10

_B = r"(?<![a-zа-я])"   # граница слова слева
_E = r"(?![a-zа-я])"    # справа

SELF_RE = re.compile(
    _B + r"(что (ты )?(умеешь|можешь)|какие (у тебя )?(функци\w*|возможност\w*|команд\w*)|"
    r"(твои|свои) (функци\w*|возможност\w*)|список (функций|команд)|есть (ли )?(у тебя )?функци\w*|"
    r"(chto|cto) (ty )?(umeesh|mozhesh)|kakie (u tebya )?(funkcii|funkcij|vozmozhnosti|komandy))" + _E)

GARMIN_RE = re.compile(
    _B + r"(как (я )?спал\w*|сколько (я )?спал\w*|мой сон|сон за ночь|как прошла ночь|"
    r"hrv|вариабельност\w*|пульс покоя|батаре\w* тела|"
    r"сколько (у меня )?шагов|мои шаги|шаги за|мой стресс|уровень стресса|"
    r"гармин\w*|garmin|сводк\w* (с )?час\w*)" + _E)
GARMIN_MORNING_RE = re.compile(_B + r"(спал\w*|сон|ночь|hrv|вариабельност\w*|пульс покоя)" + _E)
GARMIN_EVENING_RE = re.compile(_B + r"(шаг\w*|стресс\w*|тренировк\w*)" + _E)

# Этап C: вопрос о состоянии техники -- предмет И признак вопроса о состоянии (без признака -> мозг:
# «сушилка сломалась, что делать» -- разговор, а не запрос состояния).
APPLIANCE_RE = re.compile(_B + r"(сушилк\w*|сушильн\w*|стиралк\w*|стиральн\w*|стирк\w*|сушк\w*)" + _E)
APPLIANCE_STATE_RE = re.compile(
    _B + r"(что с\w*|как там|когда|сколько|осталось|закончи\w*|кончит\w*|готов\w*|ид[её]т|работает|"
    r"статус|состояние|достир\w*|досуш\w*)" + _E)

AMB_RE = re.compile(_B + r"(подсветк\w*|амбилайт\w*|ambilight|яркост\w*|насыщенност\w*|цвет|цвета)" + _E)   # 03.11: + цвет
# «свет», «светомузыка» (Оператор 30.10: понимать «свет», «под музыку») -- только как повод спросить
# модель смысла; исполняется то, что она вернула, и только класса подсветки.
LIGHT_RE = re.compile(_B + r"(свет|светом|светомузык\w*)" + _E)
VOL_RE = re.compile(_B + r"громкост\w*" + _E)
SOUND_RE = re.compile(_B + r"(звук\w*|mute|unmute|мьют|анмьют)" + _E)
TV_ON_RE = re.compile(r"включи\w*\s+(телевизор|тв)" + _E)
QUESTION_RE = re.compile(
    _B + r"(почему|зачем|что такое|что значит|расскажи|объясни|как работает|как устроен\w*|"
    r"сколько стоит|где купить|какая|какой|какие)" + _E)
OTHER_DEVICE_RE = re.compile(
    _B + r"(клавиатур\w*|ноутбук\w*|телефон\w*|смартфон\w*|монитор\w*|компьютер\w*|пк|машин\w*|"
    r"авто\w*мобил\w*|фонар\w*|велосипед\w*|колонк\w*|наушник\w*|роутер\w*|принтер\w*)" + _E)

# Классы команд tv_commands, допустимые без /tv, и обязательный предмет в тексте.
TV_ALLOWED = (
    (re.compile(r"^amb_"), AMB_RE),
    (re.compile(r"^volume_"), VOL_RE),
    (re.compile(r"^mute_"), SOUND_RE),
    (re.compile(r"^standby$"), None),
    (re.compile(r"^screen_(off|on)$"), None),   # 06.11: «выключи/включи экран» без /tv (слово «экран» требует сама команда)
)

# Успешные ответы контура ТВ (tv_commands/_execute). Ошибки _execute начинаются с «📺 ».
TV_OK_PREFIXES = ("Подсветка", "Громкость", "Звук", "Отправил")


@dataclass(frozen=True)
class Route:
    kind: str
    text: str = ""       # tv: фраза для tv_bot_handler; garmin: morning|evening
    note: str = ""       # «Понял как «…». » -- для ответа
    reason: str = ""     # для журнала, без текста пользователя


def _translit(raw: str) -> str:
    """Латиница («podsvetka muzyka») -> слова словаря ТВ по звучанию (tv_nav.canonical).
    Только для латиницы: на кириллице фонетическая подмена могла бы превратить обычное
    слово в командное."""
    if not re.search(r"[a-z]", (raw or "").lower()):
        return raw
    try:
        import tv_nav
        return tv_nav.canonical(raw)
    except Exception:  # noqa: BLE001 - маршрутизатор не должен падать
        return raw


def _tv_class_ok(name: str, t: str) -> bool:
    for pat, subject in TV_ALLOWED:
        if pat.search(name):
            return subject is None or bool(subject.search(t))
    return False


# (13.11, полигон batch60 после карты меню v2): три класса фраз исполнялись мимо смысла.
# 1) «сбрось все настройки подсветки» -> модель смысла -> amb_ai («подсветка авто»): сброс -- не режим; стиль Оператора
#    («Музыка») молча слетал. 2) «открой ambilight» -> amb_ai: открыть меню -- не переключить режим. 3) «цвет тимпиратура
#    тёплый» -> правило amb_colour: человек про цветовую температуру КАРТИНКИ, а менялся цвет ПОДСВЕТКИ.
# Всё три -- переспрос кодом (CLARIFY), без кнопок и без модели.
B04_RESET_RE = re.compile(_B + r"(сбрось\w*|сброс\w*|сбрасыва\w*|обнули\w*|reset|sbros\w*|по умолчани\w*|заводск\w*)" + _E)
B04_OPEN_RE = re.compile(_B + r"(открой|открыть|откройте|зайди|зайти|перейди|перейти|покажи|показать|otkroj|zajdi)" + _E)
B04_TEMP_RE = re.compile(_B + r"(температур\w*|тимпиратур\w*|темпиратур\w*|тимператур\w*|temperatur\w*|"
                         r"temperatur|точк\w* бел\w*|картинк\w*|изображени\w*)" + _E)
B04_AMB_SUBJ_RE = re.compile(_B + r"(подсветк\w*|амбилайт\w*|эмбилайт\w*|ambilight|podsvetk\w*)" + _E)
B04_TEXT = {
    "reset": ("❓ Сброс настроек кодом не делаю — ничего не отправлено. Сброс подсветки — вручную пультом: "
              "Настройки → Ambilight → Персонализация → «Сбросить все…». Режим подсветки — фразой: "
              "(подсветка музыка), (подсветка авто)."),
    "open": ("❓ Открывать разделы меню фразой пока не умею — ничего не отправлено. Подсветка — фразой: "
             "(подсветка музыка), (подсветка авто), (подсветка красная); кнопки — /tv."),
    "temp": ("❓ Картинка или подсветка? Ничего не отправлено.\n"
             "• подсветка: (подсветка тёплая) / (подсветка холодная)\n"
             "• цветовая температура картинки кодом не меняется — пультом: Настройки → Изображение → Цвет → "
             "Цветовая температура"),
}


# (15.11, контур B): причина маршрута «вопрос о сознании». Не «clarify:…» -- связка (bind_layer.APPLY_REASONS)
# такой переспрос не снимает, кнопок с командами нет.
SELF_Q_REASON = "self:abstract"
# (15.11, этап 1d; docs/INVARIANTS.md INV-07): Host-Only -- у хозяина (bot: host_only.is_host по id отправителя) снимается
# только шаблон на абстрактный вопрос. Маршрут HOST -- не BRAIN и не CLARIFY: связка его не снимает (bind_layer.can_apply).
HOST_SELF_REASON = "host:abstract"
HOST_WHO_REASON = "host:who"


def host_route(text: str, raw: Optional[str] = None) -> Optional[Route]:
    """вызывается ТОЛЬКО для хозяина, ДО route. «сознание» (role_guard.self_question) / «кто ты» (WHO_RE) --
    Route(HOST) без текста: в модель, команда в хвосте не исполняется (правила ТВ, связки, «запомни» не проходятся).
    Иначе None -- обычный route, как у всех. Признак хозяина здесь не проверяется: решает bot по id, не по тексту."""
    if any(role_guard.self_question(s) for s in (text or "", raw or "") if s):
        return Route(HOST, "", reason=HOST_SELF_REASON)
    if any(WHO_RE.match(tv_commands.normalize(s)) for s in (text or "", raw or "") if s):
        return Route(HOST, "", reason=HOST_WHO_REASON)
    return None


def b04_gate(t: str, cmd_name: Optional[str] = None) -> Optional[Route]:
    """t -- нормализованный текст. cmd_name -- команда, которую узнало правило (или None -- путь модели смысла).
    -> Route(CLARIFY) или None. Сброс -- всегда переспрос; «открой …» -- только на пути модели смысла (правила
    tv_nav/menu_syntax со «открой <пункт>» не трогаются); температура без предмета «подсветка» -- при amb_colour и
    на пути модели. вопрос о сознании -- сухой ответ кодом, команда ТВ не исполняется (дубль проверки route)."""
    if role_guard.self_question(t):
        return Route(CLARIFY, role_guard.SELF_TEXT, reason=SELF_Q_REASON)
    if B04_RESET_RE.search(t):
        return Route(CLARIFY, B04_TEXT["reset"], reason="b04:reset")
    if cmd_name is None and B04_OPEN_RE.search(t):
        return Route(CLARIFY, B04_TEXT["open"], reason="b04:open")
    if (cmd_name is None or cmd_name == "amb_colour") and B04_TEMP_RE.search(t) and not B04_AMB_SUBJ_RE.search(t):
        return Route(CLARIFY, B04_TEXT["temp"], reason="b04:temp")
    return None


def _tv_route(t: str, intent: Optional[Callable[[str], Optional[str]]]) -> Optional[Route]:
    """t -- нормализованный текст (tv_commands.normalize). Route(tv), Route(clarify) или None."""
    if not t:
        return None
    if len(t.split()) > TV_MAX_WORDS:
        return None
    if QUESTION_RE.search(t) or OTHER_DEVICE_RE.search(t):
        return None
    if TV_ON_RE.search(t) and not AMB_RE.search(t):
        return Route(TV, t, reason="tv_rule:power_on")      # 03.11: Wake-on-LAN + проверка по кадру (было -- отказ)
    try:
        cmd = tv_commands.parse(t)
    except tv_commands.Refusal:
        return None          # прочие отказы (приложения) -- не повод перехватывать разговор
    except tv_commands.Unknown:
        cmd = None
    if cmd is not None:
        if not _tv_class_ok(cmd.name, t):
            return None
        return b04_gate(t, cmd.name) or Route(TV, t, reason=f"tv_rule:{cmd.name}")
    if not (AMB_RE.search(t) or VOL_RE.search(t) or LIGHT_RE.search(t)):
        return None          # модель смысла -- только при явном предмете
    g = b04_gate(t)          # сброс / «открой …» / температура -- до модели смысла
    if g is not None:
        return g
    phrase = intent(t) if intent is not None else None
    if not phrase:
        return None
    try:
        cmd = tv_commands.parse(phrase)
    except tv_commands.Unknown:
        return None
    if not _tv_class_ok(cmd.name, phrase):
        return None
    return Route(TV, phrase, note=f"Понял как «{phrase}». ", reason=f"tv_intent:{cmd.name}")


# R1 (02.11): вопрос «как сделать/включить/настроить …» про ТВ -> подсказка фраз маршрутизатора, без модели.
# Живой случай 22:41: мозг на такой вопрос пересказал JSON API ТВ. Фразы в «» обязаны уходить в ТВ (тест).
HOW_RE = re.compile(_B + r"(как|каким образом)" + _E)
HINT_LIGHT_RE = re.compile(_B + r"свет\w*" + _E)   # «света», «светом» -- только для подсказки
HOW_VERB_RE = re.compile(
    _B + r"(сделать|сделай|включить|выключить|поставить|настроить|изменить|поменять|сменить|менять|"
    r"убавить|прибавить|убрать|регулировать|управлять|переключить)" + _E)
TV_HINTS = {
    "ambilight": ("💡 Подсветка ТВ — напиши фразу (без /tv):\n"
                  "• «подсветка музыка» — под звук\n"
                  "• «подсветка красная» — один цвет\n"
                  "• «подсветка кино» — за картинкой\n"
                  "• «яркость минус 3» — ярче или тусклее\n"
                  "• «подсветка выключи»"),
    "volume": ("🔊 Звук ТВ — напиши фразу (без /tv):\n"
               "• «громкость 20»\n"
               "• «громкость плюс 5» или «громкость минус 5»\n"
               "• «без звука» или «включи звук»"),
}


def tv_hint_key(t: str) -> Optional[str]:
    """t -- нормализованный текст. «ambilight» / «volume» или None."""
    if not (t and HOW_RE.search(t) and HOW_VERB_RE.search(t)) or OTHER_DEVICE_RE.search(t):
        return None
    if AMB_RE.search(t) or LIGHT_RE.search(t) or HINT_LIGHT_RE.search(t):
        return "ambilight"
    if VOL_RE.search(t) or SOUND_RE.search(t):
        return "volume"
    return None


def tv_question_key(t: str) -> Optional[str]:
    """D2 (02.11): t -- нормализованный текст ВОПРОСА («?» проверяется в route по исходнику).
    Короткий вопрос про предмет ТВ без слов рассказа -> «ambilight» / «volume», иначе None (-> мозг).
    Живой случай 23:22: «подсветка music?» -> мозг повторил выдуманный API из истории.
    Вопрос о состоянии («подсветка работает?», «какой режим») сюда не доходит -- его раньше берёт TV_STATE."""
    if not t or len(t.split()) > TV_MAX_WORDS:
        return None
    if QUESTION_RE.search(t) or OTHER_DEVICE_RE.search(t):
        return None
    if AMB_RE.search(t) or LIGHT_RE.search(t):
        return "ambilight"
    if VOL_RE.search(t) or SOUND_RE.search(t):
        return "volume"
    return None


# TV_STATE (03.11, принцип Оператора): система не знает текущего состояния ТВ -- она знает только ПОСЛЕДНЕЕ, что ТВ
# сообщил после команды бота. Так и говорится: с временем и источником, чтобы человек проверил, а не поверил.
# Живой случай 01:22: факт «режим один цвет (статичный)» был в контексте, мозг всё равно ответил «режим подсветки
# музыки» (пример из каталога). Ответ о состоянии -- без модели, из журнала событий.
_STATE_SUBJ_RE = re.compile(
    _B + r"(подсветк\w*|амбилайт\w*|ambilight|телевизор\w*|тв|телик\w*|громкост\w*|звук\w*|яркост\w*|насыщенност\w*|"
    r"podsvetk\w*|televizor\w*|tv|telik\w*|gromkost\w*|zvuk\w*|jarkost\w*|yarkost\w*)" + _E)
# 03.11 01:3x: + «текущий», «какая/какой …», «сколько», транслит «sejcas/tekuwij/skoljko» (живые фразы ушли в подсказку
# или в мозг с выдумкой «установлен как зелёный»).
_STATE_WORD_RE = re.compile(
    _B + r"(режим\w*|сейчас|состояни\w*|работает|включен\w*|выключен\w*|какой цвет|какого цвета|что с|как там|"
    r"на каком|текущ\w*|какой|какая|какое|какую|каком|сколько|"
    r"rezh?im\w*|se[jiy]?c?h?as|sostojani\w*|sostoyani\w*|rabotaet|vkljuchen\w*|kakoj cvet|"
    r"(chto|cto|shto) s|kak tam|tekuw\w*|tekushch\w*|tekusch\w*|kakoj|kakoy|kakaja|kakaya|kakoe|kakuju|kakom|skol'?j?ko)" + _E)
# Вопрос-рассказ («почему», «что такое») -- мозгу. «Какой/какая» -- НЕ рассказ: «какой режим» -- вопрос о состоянии.
_STORY_RE = re.compile(
    _B + r"(почему|зачем|что такое|что значит|расскажи|объясни|как работает|как устроен\w*|сколько стоит|где купить|"
    r"как сделать|как включить|как настроить|лучше|посоветуй|pochemu|zachem|chto takoe|rasskazhi|objasni|luchshe)" + _E)


def tv_state_question(text: str) -> bool:
    """Вопрос о текущем состоянии ТВ (кириллица/транслит, «?» не обязателен)."""
    q = (text or "").lower().replace("ё", "е")
    if not is_question(q) or len(q.split()) > TV_MAX_WORDS:
        return False
    if _STORY_RE.search(q) or OTHER_DEVICE_RE.search(q):
        return False
    return bool(_STATE_SUBJ_RE.search(q) and _STATE_WORD_RE.search(q))


_NOTE_PREFIX_RE = re.compile(r"^Понял как «[^»]*»\.\s*")


def tv_state_text(events: list, now: Optional[_dt.datetime] = None) -> str:
    """events -- записи журнала событий src=tv ({"ts", "text"}), старые сверху. Ответ без модели:
    последнее про подсветку и последнее про звук, с датой и временем; честно -- ТВ сейчас не опрашивался."""
    light = level = sound = None
    for rec in events or []:
        t = _NOTE_PREFIX_RE.sub("", str(rec.get("text", "")).strip())
        if t.startswith(("Подсветка: яркость", "Подсветка: насыщенность")):
            level = (rec.get("ts", ""), t)         # уровень -- отдельно: не вытесняет режим
        elif t.startswith("Подсветка"):
            light = (rec.get("ts", ""), t)
        elif t.startswith(("Громкость", "Звук")):
            sound = (rec.get("ts", ""), t)
    if not (light or level or sound):
        return ("📺 Состояние ТВ: нет данных — команд подсветке и звуку не было, сам телевизор не опрашивается.\n"
                "Точнее — на экране. Управление: «подсветка музыка», «громкость 20».")

    def when(ts: str) -> str:
        try:
            d = _dt.datetime.fromisoformat(ts)
        except (TypeError, ValueError):
            return "время неизвестно"
        return d.strftime("%d.%m %H:%M")

    lines = ["📺 Телевизор сейчас не опрашивался. Последнее известное по журналу команд:"]   # R2a C4: без первого лица
    if light:
        lines.append(f"• {when(light[0])} — {light[1]}")
    if level:
        lines.append(f"• {when(level[0])} — {level[1]}")
    if sound:
        lines.append(f"• {when(sound[0])} — {sound[1]}")
    lines.append("Если на экране другое — меняли пультом или в меню: этого журнал не видит.")
    return "\n".join(lines)


def default_intent(t: str) -> Optional[str]:
    try:
        import tv_intent
        return tv_intent.understand(t)
    except Exception:  # noqa: BLE001
        return None


# TYPO_SUBJECT (02.11, Red Zone 1): опечатка в слове-предмете ТВ («одсветка», «подстветка», «ромкость») обходила
# AMB_RE/VOL_RE -> мозг (живой случай 23:42:18). Нечёткое совпадение -- ТОЛЬКО для длинных слов-предметов
# (>= TYPO_MIN_LEN букв) и ТОЛЬКО кириллица: короткие «свет»/«звук» на расстоянии 1 дают обычные слова
# («сеть», «звон»). Расстояние Дамерау-Левенштейна <= 1 (пропуск, вставка, замена, перестановка соседних).
# Только запасной путь: точный маршрут ничего не нашёл (BRAIN) -> повтор маршрута по исправленному тексту;
# исполнение ТВ -- с пометкой «Понял как «…».», как у модели смысла.
TYPO_MIN_LEN = 7
# Кортеж, не множество: при равных кандидатах («подсветкц» -> -а/-у/-и/-е) выбор детерминирован (первая форма).
TYPO_FORMS = (
    "подсветка", "подсветку", "подсветки", "подсветке", "подсветкой",
    "громкость", "громкости", "громкостью",
    "яркость", "яркости", "яркостью",
    "насыщенность", "насыщенности", "насыщенностью",
    "амбилайт", "амбилайта", "амбилайтом", "амбилайте",
)
TYPO_STOP = frozenset({"яростью"})   # настоящие слова на расстоянии 1 от формы предмета


def _dl1(a: str, b: str) -> bool:
    """Расстояние Дамерау-Левенштейна между a и b <= 1."""
    if a == b:
        return True
    la, lb = len(a), len(b)
    if abs(la - lb) > 1:
        return False
    if la == lb:
        diff = [i for i in range(la) if a[i] != b[i]]
        if len(diff) == 1:
            return True
        return (len(diff) == 2 and diff[1] == diff[0] + 1
                and a[diff[0]] == b[diff[1]] and a[diff[1]] == b[diff[0]])
    if la > lb:
        a, b = b, a
    i = 0
    while i < len(a) and a[i] == b[i]:
        i += 1
    return a[i:] == b[i + 1:]


def _typo_word(w: str) -> str:
    if len(w) < TYPO_MIN_LEN or w in TYPO_FORMS or w in TYPO_STOP:
        return w
    for form in TYPO_FORMS:
        if _dl1(w, form):
            return form
    return w


def fix_subject_typos(s: str) -> str:
    """Слова-предметы ТВ с опечаткой -> правильная форма. Регистр -> нижний (дальше всё равно normalize),
    знаки («?») сохраняются. Латиница не трогается (у неё свой путь -- транслит)."""
    low = (s or "").lower().replace("ё", "е")
    return re.sub(r"[а-я]+", lambda m: _typo_word(m.group(0)), low)


# CLARIFY (03.11, решение Оператора): «если без прямого ввода бот не понимает, что я хочу и от какой техники, пусть
# уточнит, к кому я обращаюсь -- уточнить, а не писать выдумки». Живые случаи 01:2x-01:3x: «minus 4», «sdelaj tusklee»
# -> мозг выдумал «яркость установлена на 251». Срабатывает ТОЛЬКО когда ни один маршрут не взял фразу (иначе -- мозг):
# короткая фраза с действием/вопросом, который есть у нескольких устройств или у устройства, не названного в тексте.
CLARIFY_MAX_WORDS = 5
_CL_LEVEL_RE = re.compile(
    _B + r"(ярче|поярче|тусклее|потусклее|темнее|светлее|громче|погромче|тише|потише|выше|ниже|повыше|пониже|"
    r"больше|меньше|плюс|минус|прибавь|убавь)" + _E)
_CL_SLEEP_RE = re.compile(_B + r"(сон|сна|сну|спать|сплю|sleep|таймер сна|засыпани\w*)" + _E)
_CL_DONE_RE = re.compile(
    _B + r"(когда законч\w*|скоро законч\w*|сколько осталось|сколько еще|закончил\w*|достирал\w*|досушил\w*|готово ли|"
    r"готов[аоы]? ли)" + _E)
_CL_ONOFF_RE = re.compile(r"^(включи|выключи|включить|выключить|вруби|выруби|останови|запусти)(\s+(его|ее|это|пожалуйста))?$")
# Просьба к самому собеседнику («говори тише», «пиши короче») -- разговор, не техника.
_CL_TALK_RE = re.compile(_B + r"(говори|говорите|скажи|пиши|отвечай|отвечай|объясняй|читай)" + _E)
CLARIFY_TEXT = {
    "colour": "❓ Это про подсветку ТВ? Напиши (подсветка <цвет>), например (подсветка красная) — выполню.",
    "level": ("❓ Уточни, что прибавить или убавить:\n"
              "• «яркость минус 4» или «подсветка ярче» — подсветка ТВ\n"
              "• «громкость минус 4» — звук ТВ (звук идёт через колонки Logitech: громкость ТВ может быть не слышна)"),
    "sleep": ("❓ Уточни, про какой сон:\n"
              "• «как я спал» — сон по часам Garmin\n"
              "• таймер сна ТВ я ставить не умею — только кнопками: «/tv»"),
    "done": ("❓ Уточни, про какую технику:\n"
             "• «когда закончит стиральная»\n"
             "• «когда закончит сушилка»"),
    "again": ("❓ Уточни, что повторить: прошлую команду ТВ я помню 10 минут, сейчас её нет.\n"
              "• например «подсветка тусклее» или «громкость минус 2»"),
    "onoff": ("❓ Уточни, что включить или выключить:\n"
              "• «подсветка включи» / «подсветка выключи» — подсветка ТВ\n"
              "• «включи телевизор» / «выключи телевизор» — ТВ\n"
              "• стиральную и сушилку я не включаю — только сообщаю, когда закончат"),
}


def clarify_key(t: str) -> Optional[str]:
    """t -- нормализованный текст фразы, которую не взял ни один маршрут. Ключ уточнения или None (-> мозг)."""
    if not t or len(t.split()) > CLARIFY_MAX_WORDS:
        return None
    if _STORY_RE.search(t) or OTHER_DEVICE_RE.search(t) or SELF_RE.search(t) or _CL_TALK_RE.search(t):
        return None
    if _CL_ONOFF_RE.match(t):
        return "onoff"
    if _colour_word(" ".join(_FILLER_RE.sub(" ", t).split())):
        return "colour"                                 # цвет без предмета и без свежего контекста -- переспросить
    if _AGAIN_RE.search(t) and not _FILLER_RE.sub(" ", _AGAIN_RE.sub(" ", t)).strip():
        return "again"                                  # «ещё», «сделай ещё» без прошлой команды (CTX_FOLLOW)
    if _CL_DONE_RE.search(t):
        return "done"
    if _CL_SLEEP_RE.search(t):
        return "sleep"
    if _CL_LEVEL_RE.search(t):
        return "level"
    return None


# CTX_FOLLOW (03.11, «нужно связать намерения»): живые 02:0x -- «sdelaj ewe» после «тусклее» -> мозг «Не распознал»;
# «vikljuci ee» -> мозг «Выключи телевизор.»; «sdelaj ee jarce» -> уточнение. Бот помнит ПРЕДМЕТ последней исполненной
# команды ТВ (bot передаёт ctx=(предмет, фраза), живёт минуты). Продолжение без предмета -- местоимение («её», «его»),
# «ещё/снова/опять» или голое «тусклее / минус 4 / выключи» -- переписывается с предметом и проходит обычные правила
# (исполняется только то, что правила узнали). Пометка «Понял как «…».» -- человек видит, как связано.
SUBJ_AMB, SUBJ_VOL = "подсветка", "громкость"
_PRONOUN_RE = re.compile(_B + r"(ее|её|его|ей|ему|ее|ee|ego|ejo|eyo|ей)" + _E)
_AGAIN_RE = re.compile(_B + r"(еще|ещё|снова|опять|повтори|ewe|eshe|esche|esho|ewjo|ewo|eshjo|snova|opjat)" + _E)
_FILLER_RE = re.compile(_B + r"(сделай|сделать|давай|ну|пожалуйста|sdelaj|sdelai|davaj|nu|pozhalujsta)" + _E)


def tv_subject(reason: str) -> Optional[str]:
    """Предмет исполненной команды ТВ по Route.reason («tv_rule:amb_step», «typo:tv_intent:volume_up» …)."""
    name = (reason or "").rsplit(":", 1)[-1]
    if name.startswith("amb_"):
        return SUBJ_AMB
    if name.startswith(("volume_", "mute_")):
        return SUBJ_VOL
    return None


def _follow_phrase(t: str, ctx: tuple) -> Optional[str]:
    """t -- нормализованный текст без предмета; ctx=(предмет, прошлая фраза). Фраза с предметом или None."""
    subj, last = ctx
    if not t or not subj or len(t.split()) > CLARIFY_MAX_WORDS or AMB_RE.search(t) or VOL_RE.search(t):
        return None
    if _STORY_RE.search(t) or OTHER_DEVICE_RE.search(t) or _CL_TALK_RE.search(t) or is_question(t):
        return None
    pron, again = bool(_PRONOUN_RE.search(t)), bool(_AGAIN_RE.search(t))
    rest = _FILLER_RE.sub(" ", _AGAIN_RE.sub(" ", _PRONOUN_RE.sub(" ", t)))
    rest = " ".join(rest.split())
    if not rest:
        return last if again and last else None       # «ещё», «сделай ещё» -- повтор прошлой команды
    if re.search(r"[a-z]", rest):
        return None                                    # непереведённая латиница -- смысл неизвестен (вариант-транслит решит)
    colour = _colour_word(rest)                        # «включи красный» / «zeltuju» -> только подсветка
    if colour:
        return f"подсветка {colour}"
    level = bool(_CL_LEVEL_RE.search(rest))
    onoff = bool(re.fullmatch(r"(включи|выключи|включить|выключить|вруби|выруби)", rest))
    if not (pron or again or level or onoff):
        return None
    if subj == SUBJ_VOL and onoff:
        return f"{rest} звук"                          # «выключи его» после громкости -- звук (mute), не «громкость выключи»
    # 03.11 02:28 (живой): «sdelaj ewe na 5» после «тусклее» -> было «яркость на 5» (поставить 5), а намерение -- убавить
    # ещё на 5. «ещё» + число без своего направления наследует направление прошлой команды.
    num = re.fullmatch(r"(на\s+)?(\d+)", rest)
    if again and num and last:
        if re.search(r"(тусклее|потусклее|минус|ниже|пониже|убав\w*|тише|потише|меньше)", last):
            sign = "минус"
        elif re.search(r"(ярче|поярче|плюс|выше|повыше|прибав\w*|громче|погромче|больше)", last):
            sign = "плюс"
        else:
            sign = None
        if sign:
            return f"{'яркость' if subj == SUBJ_AMB else subj} {sign} {num.group(2)}"
    if subj == SUBJ_AMB and re.search(r"\d", rest) and not re.search(r"(ярче|тусклее|поярче|потусклее)", rest):
        return f"яркость {rest}"                       # «минус 4» после подсветки -- яркость
    return f"{subj} {rest}"


# --- (12.11, FUZZ batch01/02 + разбор ловушек 17:39/20:06/22:19 и 22:3x) --------------------------------------
# Узел A (границы команды): правила исполняли ПЕРВУЮ узнанную команду и молча бросали остальное -- «потише ну или экран
# погаси» -> экран; «podsvetka krasnaja; громкость 20; экран выкл» -> одна из трёх; «cerz minutu vikljuci» -> сразу;
# «sinjaja, a hotja pogodi virubi» -> синий. Узел B (вставленный статус): «ТВ: подсветка музыка…», «[VERIFIED] …» ->
# исполнялось как команда. Правило: понимание фразы != разрешение на действие; любая неясность -> переспрос,
# ничего не отправляется. Исполнение по-прежнему -- только штатные правила, по одной команде.
_TAG_RE = re.compile(r"\[\s*(verified|blind|warn|fail|pass|ok|done)\s*\]", re.I)
_LABEL_RE = re.compile(r"^\s*(тв|tv|телевизор|ambilight)\s*:", re.I)
GATE_LITERAL_TEXT = ("❓ Это похоже на отчёт или статус (метка «[…]» / «ТВ: …»), а не на команду — ничего не отправлено. "
                     "Нужна команда — напиши её отдельно, например (подсветка красная).")
_DELAY_RE = re.compile(
    _B + r"(через|чрез|cherez|cerez|4erez|cerz|chrez|spustja|спустя)\s+(\d+|пару|пять|десять|полчаса|час|минуту|"
    r"полминуты|секунду|minutu|chas|cas|paru|pjatj|desjatj)" + _E
    + r"|" + _B + r"(попозже|позже|потом|вечером|утром|pozzhe|pozze|potom|vecherom|vecerom)" + _E)
# (13.11, batch30 G04-08 «neee ne nado podsvetku» -> модель смысла выключила): «не надо <предмет>» без глагола --
# неясно: выключить или ничего не трогать. Переспрос, ничего не отправлено.
_NEG_OBJ_RE = re.compile(
    _B + r"(не+|ne+|нее+)\s+(надо|нужно|nado|nuzhno|nuzno)\s+(подсветк\w*|podsvetk\w*|свет\w*|svet\w*|звук\w*|zvuk\w*|"
    r"экран\w*|ekran\w*|громкост\w*|gromkost\w*|телевизор\w*|televizor\w*|тв|tv|музык\w*|muzik\w*)" + _E)
NEG_OBJ_TEXT = ("❓ «Не надо …» — выключить это или просто ничего не трогать? Ничего не отправил. "
                "Если выключить — напиши, например, (подсветка выключи).")
GATE_DELAY_TEXT = ("⏱ Таймеров у меня нет — команду «через …» не выполняю и сейчас ничего не отправил. "
                   "Напиши команду, когда она нужна.")
_SPLIT_RE = re.compile(r"\s*;\s*|\s*,\s*|\s+(?:или|ili|либо|libo)\s+|"
                       # (13.11, batch30 G04-17/22): «… и громкость 15» -- союз «и» / «а потом» / «затем»
                       r"\s+(?:и|i|а потом|a potom|потом|potom|затем|zatem)\s+|"
                       r"\s*(?:,?\s*(?:а|a)\s+(?:хотя|hotja|hotya)|хотя нет|hotja net|а нет|a net|нет,? лучше|net,? luchwe)\s*")
_CORRECTION_RE = re.compile(r"(?:^|\s)(?:а|a)\s+(?:хотя|hotja|hotya)|хотя нет|hotja net|(?:^|\s)(?:а|a) нет\b|нет,? лучше|net,? luchwe",
                            re.I)
_OR_RE = re.compile(_B + r"(или|ili|либо|libo)" + _E)
_HISTORY_RE = re.compile(
    _B + r"(прошл\w*|предыдущ\w*|прежн\w*|последн\w*|prowl\w*|proshl\w*|proshel|predyduw\w*|predydush\w*|prezhn\w*|"
    r"prezn\w*|posledn\w*)\s+(цвет\w*|cvet\w*|color)" + _E
    + r"|" + _B + r"(верни|vernі|verni|вертай)\s+(цвет\w*|cvet\w*)" + _E)
_UNDO_RE = re.compile(_B + r"(верни|verni)\s+(обратно|назад|как было|obratno|nazad|kak bilo|kak bylo)" + _E)
UNDO_TEXT = ("↩️ Вернуть умею пока только цвет подсветки: напиши (верни прошлый цвет). "
             "Остальное «как было» — следующей версией (нужен снимок состояния до команды).")


_ONOFF_EN = ((re.compile(r"(?<![a-zа-я])off(?![a-zа-я])", re.I), "выключи"),
             (re.compile(r"(?<![a-zа-я])on(?![a-zа-я])", re.I), "включи"))


def _cmd_chunk(c: str) -> Optional[str]:
    """Кусок фразы -> название команды ТВ по штатным правилам (без модели смысла) или None.
    (13.11, batch30 G04-23 «ekran off; zvuk off»): + английские off/on и полный транслит латиницы."""
    vs = [c, _translit(c)]
    try:
        lat = request_view(c).split("\n")[-1]
        vs.append(lat)
    except Exception:
        pass
    for v in list(vs):
        w = v
        for rx, rep in _ONOFF_EN:
            w = rx.sub(rep, w)
        if w != v:
            vs.append(w)
    for v in list(vs):                       # «звук выключи» -> «выключи звук» (правила знают порядок глагол-предмет)
        ws = v.split()
        if len(ws) == 2:
            vs.append(f"{ws[1]} {ws[0]}")
    for v in vs:
        t = tv_commands.normalize(v or "")
        r = _tv_route(t, None) if t else None
        if r is not None and r.kind == TV:      # переспрос (CLARIFY) -- не фраза для продолжения
            return r.text
        if t and (clarify_key(t) in ("onoff", "level") or _CL_ONOFF_RE.match(t)):
            return t
    return None


def structure_gate(text: str, raw: Optional[str] = None) -> Optional["Route"]:
    """границы команды ДО правил и модели смысла. Route(CLARIFY/HISTORY) или None (дальше как раньше)."""
    srcs = [s for s in (raw, text) if s]
    if not srcs:
        return None
    low = [s.lower().replace("ё", "е") for s in srcs]
    norm = [tv_commands.normalize(_translit(s)) or "" for s in srcs] + [tv_commands.normalize(s) or "" for s in srcs]
    question = any("?" in s for s in srcs) or any(is_question(n) for n in norm if n)
    tv_word = any(AMB_RE.search(n) or VOL_RE.search(n) or LIGHT_RE.search(n) or SOUND_RE.search(n)
                  or re.search(_B + r"(экран\w*|телевизор\w*|тв|tv|ekran)" + _E, n) for n in norm + low)
    if any(_TAG_RE.search(s) for s in low) or (any(_LABEL_RE.match(s) for s in low) and not question):
        return Route(CLARIFY, GATE_LITERAL_TEXT, reason="gate:literal")
    if any(_HISTORY_RE.search(n) for n in norm + low) and not question:
        return Route(HISTORY, "colour", reason="history:colour")
    if any(_UNDO_RE.search(n) for n in norm + low):
        return Route(CLARIFY, UNDO_TEXT, reason="gate:undo")
    if question:
        return None
    if any(_NEG_OBJ_RE.search(n) for n in norm + low) and not any(NEG_ACT_RE.search(n) for n in norm + low):
        return Route(CLARIFY, NEG_OBJ_TEXT, reason="gate:neg_object")    # «не надо подсветку» -- без глагола
    if tv_word and any(_DELAY_RE.search(n) for n in norm + low):
        return Route(CLARIFY, GATE_DELAY_TEXT, reason="gate:delay")
    src = srcs[0]
    parts = [p for p in _SPLIT_RE.split(src) if p and p.strip()]
    if len(parts) < 2:
        return None
    cmds = [c for c in (_cmd_chunk(p) for p in parts) if c]
    correction = bool(_CORRECTION_RE.search(src.lower()))
    alt = bool(_OR_RE.search(src.lower()))
    if alt and tv_word and not cmds:      # (batch30 G04-12 «ekran pogasi libo podsvetku»): «или» + предмет ТВ
        return Route(CLARIFY, "❓ «Или» — выбери одно, ничего не отправил. Напиши одну команду.", reason="gate:or")
    if not cmds or (len(cmds) < 2 and not correction and not (alt and tv_word)):
        return None
    shown = ", ".join(f"({c})" for c in cmds[:4]) or "—"
    if correction:
        return Route(CLARIFY, f"❓ Ты передумал по ходу фразы — ничего не отправил. Последнее: {shown.split(', ')[-1]}. "
                              "Напиши нужную команду отдельно — выполню.", reason="gate:correction")
    if alt:
        return Route(CLARIFY, f"❓ «Или» — выбери одно, ничего не отправил: {shown}. Напиши ту, что нужна.",
                     reason="gate:or")
    return Route(CLARIFY, f"❓ Во фразе несколько команд: {shown}. Выполняю по одной — ничего не отправил. "
                          "Напиши первую, потом следующую.", reason="gate:multi")


# Цвет без предмета («да» Оператора 21:47): «включи красный» / «zeltuju» -> подсветка ТОЛЬКО по свежему контексту
# (расстояние контекста: <= 120 с и <= 1 сообщения -- исполнить; <= 300 с и <= 3 -- переспросить; дальше -- не трогать).
_COLOUR_VERB_RE = re.compile(r"^(?:(?:включи|вруби|сделай|поставь|давай|зажги|цвет|на)\s+)*(?P<c>\S+)$")


def _colour_word(rest: str) -> Optional[str]:
    m = _COLOUR_VERB_RE.match(rest or "")
    if not m:
        return None
    w = m.group("c")
    return w if w in tv_commands.HUES else None


def route(text: str, raw: Optional[str] = None,
          intent: Optional[Callable[[str], Optional[str]]] = default_intent,
          ctx: Optional[tuple] = None) -> Route:
    """ctx=(предмет, прошлая фраза) последней исполненной команды ТВ или None (CTX_FOLLOW)."""
    # (15.11, контур B, решение 14.11 п.3): вопрос о сознании -- раньше всех правил, связок и «запомни»: ответ кодом
    # о системе, модель не зовётся, приводы не трогаются даже при команде в хвосте («подсветка красная, что такое сознание»).
    if any(role_guard.self_question(s) for s in (text or "", raw or "") if s):
        return Route(CLARIFY, role_guard.SELF_TEXT, reason=SELF_Q_REASON)
    # R2a: «запомни: подсветка — …» не должно исполнить команду ТВ из хвоста -- проверка раньше всех правил.
    if any(REMEMBER_CMD_RE.match(tv_commands.normalize(s)) for s in (text or "", raw or "") if s):
        return Route(CLARIFY, MEM_TEXT, reason="mem:request")
    if any(MYTH_RE.search(tv_commands.normalize(s)) for s in (text or "", raw or "") if s):
        return Route(CLARIFY, NODATA_TEXT, reason="vocab:myth")
    if any(WHO_RE.match(tv_commands.normalize(s)) for s in (text or "", raw or "") if s):   # «шаблон кто-ты»
        return Route(CLARIFY, WHO_TEXT, reason="who")
    for s in (text or "", raw or ""):     # R2b: лампы потолка / голое «лампы» -- раньше правил ТВ и модели смысла
        lr = lamps_route(tv_commands.normalize(s)) if s else None
        if lr is not None:
            return lr
    g = structure_gate(text, raw)          # метки/статус, «через …», «или», «а хотя», несколько команд, «прошлый цвет»
    if g is not None:
        return g
    r = _route_typo(text, raw, intent)
    if r.kind != BRAIN:
        return r
    variants = [text or ""]
    tr = _translit(raw or text or "")
    if tr and tr not in variants:
        variants.append(tr)
    if ctx:
        ask = len(ctx) > 2 and ctx[2] == "ask"            # контекст «далеко» (120..300 с или 2..3 сообщения)
        for v in variants:
            ph = _follow_phrase(tv_commands.normalize(v), ctx[:2])
            if not ph:
                continue
            r2 = _route_typo(ph, None, None)
            if r2.kind == TV:
                if ask:
                    return Route(CLARIFY, f"❓ Это продолжение прошлой команды? Понял как ({r2.text}) — напиши так, выполню.",
                                 reason=f"ctx_ask:{r2.reason}")
                return Route(TV, r2.text, note=f"Понял как «{r2.text}». ", reason=f"ctx:{r2.reason}")
    for v in variants:
        key = clarify_key(tv_commands.normalize(v))
        if key:
            return Route(CLARIFY, CLARIFY_TEXT[key], reason=f"clarify:{key}")
    return r


# SPLIT_WORD (03.11 11:3x, живой): «podsv etka pod muziku» -- пробел внутри слова-предмета -> мозг (D_GUARD поймал выдумку
# «hue 85»). Запасной путь: склеить два соседних слова, если склейка -- слово-предмет ТВ (после транслита/опечатки).
def _join_split_subject(s: str) -> Optional[str]:
    words = (s or "").split()
    for i in range(len(words) - 1):
        if any(AMB_RE.search(_translit(w)) or VOL_RE.search(_translit(w)) for w in words[i:i + 2]):
            continue                      # одно из слов уже предмет -- склеивать нечего
        glued = words[i] + words[i + 1]
        cand = fix_subject_typos(_translit(glued)).strip()
        if cand in TYPO_FORMS:            # точная форма предмета, не «громкость…» с хвостом
            return " ".join(words[:i] + [glued] + words[i + 2:])
    return None


def _route_typo(text: str, raw: Optional[str] = None,
                intent: Optional[Callable[[str], Optional[str]]] = default_intent) -> Route:
    r = _route_typo1(text, raw, intent)
    if r.kind != BRAIN:
        return r
    j = _join_split_subject(raw or text or "")
    if not j:
        return r
    r2 = _route_typo1(j, j, intent)
    if r2.kind == BRAIN:
        return r
    note = r2.note or (f"Понял как «{r2.text}». " if r2.kind == TV else "")
    return Route(r2.kind, r2.text, note=note, reason=f"split:{r2.reason}")


def _route_typo1(text: str, raw: Optional[str] = None,
                 intent: Optional[Callable[[str], Optional[str]]] = default_intent) -> Route:
    """text -- текст после исправления раскладки (или распознанный голос); raw -- исходный текст
    (латиница для ТВ-словаря). intent=None -- без модели смысла (тесты).
    TYPO_SUBJECT: точный маршрут не нашёл ничего -> повтор по тексту с исправленным словом-предметом."""
    r = _route_exact(text, raw, intent)
    if r.kind != BRAIN:
        return r
    ft = fix_subject_typos(text or "")
    fr = fix_subject_typos(raw) if raw else None
    if ft == (text or "").lower().replace("ё", "е") and (raw is None or fr == raw.lower().replace("ё", "е")):
        return r
    r2 = _route_exact(ft, fr, intent)
    if r2.kind == BRAIN:
        return r
    note = r2.note
    if r2.kind == TV and not note:
        note = f"Понял как «{r2.text}». "
    return Route(r2.kind, r2.text, note=note, reason=f"typo:{r2.reason}")


def _route_exact(text: str, raw: Optional[str],
                 intent: Optional[Callable[[str], Optional[str]]]) -> Route:
    variants = [text or ""]
    # 01.11 (глобальный тест): layout_fix не трогает транслит («podsvetka zelenaja» -> kind=None), raw == text,
    # и латиница не сводилась к словарю -> мозг. Вариант по звучанию -- ВСЕГДА, если есть латиница.
    translit = _translit(raw or text or "")
    if translit and translit not in variants:
        variants.append(translit)
    for v in variants:
        t = tv_commands.normalize(v)
        if not t:
            continue
        if SELF_RE.search(t):
            return Route(SELF, reason="self")
        if GARMIN_RE.search(t):
            if GARMIN_EVENING_RE.search(t) and not GARMIN_MORNING_RE.search(t):
                kind = "evening"
            elif GARMIN_MORNING_RE.search(t):
                kind = "morning"
            else:
                kind = "auto"
            return Route(GARMIN, kind, reason=f"garmin:{kind}")
        if APPLIANCE_RE.search(t) and APPLIANCE_STATE_RE.search(t):
            return Route(DEVICES, reason="devices")
        key = tv_hint_key(t)              # R1 (02.11)
        if key:
            return Route(HINT, TV_HINTS[key], reason=f"hint:{key}")
    # TV_STATE (03.11): вопрос о состоянии ТВ -- по исходному тексту (знак «?» и транслит сохраняются), до D2 и правил ТВ.
    if any(tv_state_question(s) for s in (text or "", raw or "")):
        return Route(TVSTATE, reason="tvstate")
    # ROUTER_QMARK (02.11): признак вопроса -- из ИСХОДНОГО текста, один раз для всех вариантов. Вариант-транслит
    # знак «?» стирает (живой случай 23:00:40: вопрос со слипшимся «…txtкак» переключил подсветку).
    if any("?" in s for s in (text or "", raw or "")):
        # D2 (02.11): короткий вопрос про предмет ТВ -> подсказка готовых фраз, без модели. После первого цикла:
        # self/garmin/devices/«как …» любого варианта важнее. Команда ТВ на вопрос по-прежнему не исполняется.
        for v in variants:
            key = tv_question_key(tv_commands.normalize(v))
            if key:
                return Route(HINT, TV_HINTS[key], reason=f"hint_q:{key}")
        return Route(BRAIN, reason="brain")
    for v in variants:                    # ROLE_R1 C4/C5 (10.11): запрет и голый «свет» -- до правил ТВ и модели смысла
        r = _role_route(tv_commands.normalize(v))
        if r is not None:
            return r
    for v in variants:
        r = _tv_route(tv_commands.normalize(v), intent)
        if r is not None:
            return r
    return Route(BRAIN, reason="brain")


# ROLE C4 (10.11, FINDINGS_2026-11-10_ROLE_TESTS, пост #28 п.2): запрет на действие -- не команда. Живой разбор 10.11:
# «не выключай подсветку» -> правило amb_off (подсветка гасла); «подсветку НЕ трогай, скажи только статус» -> модель
# смысла -> «подсветка музыка». Запрет при предмете ТВ -> ни правил, ни модели: просьба статуса -> TV_STATE, иначе пометка.
NEG_ACT_RE = re.compile(
    _B + r"(не|нельзя|ne+|нее+)\s+((надо|нужно|стоит|nado|nuzhno)\s+)?"
    r"(трога\w*|трож\w*|тронь|меня\w*|поменя\w*|включа\w*|выключа\w*|переключа\w*|дела\w*|став\w*|"
    r"убавля\w*|прибавля\w*|сбива\w*|труп\w*|trogaj|trogaj\w*|menjaj|vykljuchaj|vikljucaj|"
    # (12.11, FUZZ F01-12 «ne vkljucaj podsvetku» -> модель смысла выключила): латинские формы «включай/крути/врубай»
    r"vkljucaj|vkljuchaj|vklyuchaj|vkljucat\w*|vkljuchat\w*|vrubaj|krut\w*|крут\w*|вруба\w*|menjat\w*|trogat\w*)" + _E
    + r"|" + _B + r"ничего не (дела\w*|меня\w*|трога\w*)" + _E)
NEG_STATUS_RE = re.compile(_B + r"(статус\w*|состояни\w*|что сейчас|что включено)" + _E)
NEG_TEXT = "Принято: без команды. Ничего не отправлено."
# R2a (10.11, ТЗ ROLE_R2 v2 §4, решение 23:15 п.5): «запомни / запиши / сохрани …» в начале фразы -- разбор кодом ДО модели.
# Связки (G6) ещё не подключены -> честный ответ, без модели и без истории. Вопросы («как сохранить файл?») сюда не идут:
# вопрос уходит раньше (ROUTER_QMARK), а глагол должен стоять первым (после «око / ну / пожалуйста / давай»).
REMEMBER_CMD_RE = re.compile(
    r"^((око|oko|ну|nu|пожалуйста|давай|davaj)\s+)*"
    r"(запомни|запиши|зафиксируй|занеси|внеси|сохрани|zapomni|zapishi|zafiksiruj|sohrani|remember)" + _E)
MEM_TEXT = "Не сохранено. Формат: «запомни: фраза — команда»."
# R2c (11.11): router.route остаётся чистым -- «запомни …» -> CLARIFY(mem:request); bot.py разбирает «X — Y»
# (bind_layer.parse_bind) и сохраняет связку только после исполнения Y с результатом VERIFIED. Без формата -- этот текст.
# R2a хвост (11.11, прогоны 08:05/08:06: C9 то «Нет данных», то выдуманное определение): термины старой «мифологии»
# Ока (резонанс ядра, экзокортекс) -- ответ кодом, без модели и истории. Одиночные «ядро» / «резонанс» не трогаются:
# «ядро Linux», «резонанс в физике» -- обычные вопросы.
MYTH_RE = re.compile(r"резонанс\w*\s+ядр\w*|ядр\w*\s+резонанс\w*|экзокортекс\w*")
NODATA_TEXT = "Нет данных."
# (13.11, FUZZ F01-29 «кто ты»: «Я — система…» 11/25 на «сторож цифр здоровья», 15/25 на «расстояние контекста»; роль P7 -- безлико, без «я»):
# вопрос «кто ты» целиком -- ответ кодом, без модели и истории (история сдвигала форму ответа).
WHO_RE = re.compile(
    r"^((око|oko|ну|nu|а|a|слушай|slushaj)\s+)*"
    r"(кто\s+ты(\s+такое|\s+такой|\s+такая)?|ты\s+кто(\s+такое|\s+такой|\s+такая)?|что\s+ты\s+такое|"
    r"кто\s+(такое|такой)\s+око|что\s+такое\s+око|"
    r"kto\s+(ty|ti)(\s+takoe|\s+takoj|\s+takaja)?|(ty|ti)\s+kto(\s+takoe|\s+takoj)?|chto\s+(ty|ti)\s+takoe|cto\s+(ty|ti)\s+takoe|"
    r"кто\s+ти|ти\s+кто)$")
WHO_TEXT = "Око — Система 0: исполнитель при Операторе. Команды ТВ, сводки Garmin, справки. Список — «что умеешь»."
# ROLE C5 (10.11): голый «свет» («свет», «свет включи», «ну свет») -- неясно, что именно; модель смысла не зовётся.
LIGHT_ONLY_RE = re.compile(
    r"^((ну|давай|пожалуйста|сделай|включи|выключи|вруби|выруби|nu|davaj|vkljuchi|vykljuchi)\s+)*"
    r"(свет|светом|svet)(\s+(ну|давай|пожалуйста|включи|выключи|вруби|выруби|vkljuchi|vykljuchi))*$")
LIGHT_TEXT = ("❓ Уточни, что со светом:\n"
              "• «подсветка музыка» / «подсветка красная» — подсветка ТВ\n"
              "• «подсветка выключи»\n"
              "• «люстра …» — лампы потолка (пока только вместе с подсветкой: /amb → «💡 Лампы»)")

# R2b (11.11, ТЗ ROLE_R2 v2 §2, решение 23:15 п.4 + 23:37): «люстра» = потолочная штанга Deltaco, 4 лампы.
# До R2b «люстра цвет синий» / «yarkost lustry 50» перекрашивали ПОДСВЕТКУ ТВ (правила amb по словам «цвет»/«яркость»).
# Прямого управления лампами нет (выключатель Tuya -- PENDING): лампы идут только вместе с подсветкой через мост /amb.
# Поэтому -- честный ответ кодом, без команды ТВ и без модели. Голое «лампы» -- два предмета -> уточнение.
LAMPS_STRONG_RE = re.compile(_B + r"(люстр\w*|ljustr\w*|ljust\w*|lustr\w*|ljuster\w*)" + _E)
LAMPS_WEAK_RE = re.compile(_B + r"(потол\w*|штанг\w*|планк\w*|potol\w*|shtang\w*|plank\w*)" + _E
                           + r"|" + _B + r"верхн\w*\s+свет\w*" + _E)
LAMP_WORD_RE = re.compile(_B + r"(ламп\w*|lamp\w*)" + _E)
LIGHT_ACT_RE = re.compile(_B + r"(вкл\w*|выкл\w*|вруб\w*|выруб\w*|зажг\w*|зажечь|погас\w*|туш\w*|ярк\w*|ярч\w*|"
                          r"поярч\w*|тускл\w*|цвет\w*|красн\w*|син\w*|зел[её]н\w*|ж[её]лт\w*|бел\w*|фиолет\w*|"
                          r"оранж\w*|розов\w*|свет\w*|гор\w*|музык\w*|vkl\w*|vykl\w*|vikl\w*|vrub\w*|virub\w*|"
                          r"jark\w*|yark\w*|cvet\w*|svet\w*)" + _E)
TV_LIGHT_RE = re.compile(_B + r"(подсветк\w*|амбилайт\w*|ambilight\w*|podsvetk\w*|телевизор\w*|тв|tv)" + _E)
LAMPS_TEXT = ("Не выполнялось. Лампы потолка (штанга Deltaco, 4 лампы) отдельно не управляются — "
              "только вместе с подсветкой ТВ: /amb → «💡 Лампы».")
LAMP_WHICH_TEXT = ("❓ Уточни, какие лампы:\n"
                   "• «подсветка …» — подсветка ТВ\n"
                   "• «люстра …» — лампы потолка (пока только вместе с подсветкой: /amb → «💡 Лампы»)")


def lamps_route(t: str) -> Optional["Route"]:
    """t -- нормализованный текст (или транслит). Route(CLARIFY) для ламп потолка / голого «лампы», иначе None."""
    if not t or TV_LIGHT_RE.search(t):
        return None
    if LAMPS_STRONG_RE.search(t) or (LAMPS_WEAK_RE.search(t) and LIGHT_ACT_RE.search(t)):
        return Route(CLARIFY, LAMPS_TEXT, reason="lamps:ceiling")
    if LAMP_WORD_RE.search(t):
        return Route(CLARIFY, LAMP_WHICH_TEXT, reason="lamps:which")
    return None


def _role_route(t: str) -> Optional[Route]:
    """t -- нормализованный текст. Route(CLARIFY/TVSTATE) для запрета на действие с ТВ (C4) и голого «свет» (C5);
    R2a: просьба запомнить (C7) -- MEM_TEXT (дубль проверки в начале route())."""
    if not t:
        return None
    if REMEMBER_CMD_RE.match(t):
        return Route(CLARIFY, MEM_TEXT, reason="mem:request")
    if NEG_ACT_RE.search(t) and (AMB_RE.search(t) or VOL_RE.search(t) or LIGHT_RE.search(t) or SOUND_RE.search(t)
                                 or re.search(_B + r"(телевизор\w*|тв|tv)" + _E, t)):
        if NEG_STATUS_RE.search(t):
            return Route(TVSTATE, reason="neg:status")
        return Route(CLARIFY, NEG_TEXT, reason="neg")
    if LIGHT_ONLY_RE.match(t):
        return Route(CLARIFY, LIGHT_TEXT, reason="clarify:light")
    return None


# --- исполнение (синхронно; bot.py вызывает в потоке) -------------------------------------

def tv_outcome(reply: str) -> Optional[str]:
    """Исход команды ТВ по ответу контура: PASS / WARN / FAIL / None (не исполнялась).
    FAIL -- строки ошибок _execute («📺 …», кроме заметки о смене адреса);
    WARN -- сверка не сошлась («телевизор сообщает»)."""
    lines = [l.strip() for l in (reply or "").splitlines() if l.strip()]
    if not lines:
        return None
    for l in lines:
        if l.startswith("📺") and not l.startswith("📺 Телевизор сменил адрес"):
            return "FAIL"
    head = re.sub(r"^Понял как «[^»]*»\.\s*", "", lines[0])
    if not head.startswith(TV_OK_PREFIXES):
        return None   # «Не выполнено», «Требуется подтверждение», отказ по правам
    if "телевизор сообщает" in reply or "⚠️ По кадру камеры" in reply:   # 03.11: сверка по кадру камеры не сошлась
        return "WARN"
    return "PASS"


# (12.11): происхождение строки ТВ в журнале фактов. До «происхождение строк ТВ» все исходы шли под одной меткой «ТВ:», и BLIND
# («Отправил: …», чтения нет) выглядел для модели так же, как прочитанное с телевизора.
TV_KINDS = ("verified", "blind", "warn", "fail")


def tv_kind(reply: str, outcome: Optional[str]) -> Optional[str]:
    """verified (прочитано с ТВ после записи) / blind (отправлено, без чтения) / warn (сверка не сошлась) / fail."""
    if outcome == "FAIL":
        return "fail"
    if outcome == "WARN":
        return "warn"
    if outcome != "PASS":
        return None
    lines = [l.strip() for l in (reply or "").splitlines() if l.strip()]
    head = re.sub(r"^Понял как «[^»]*»(?:\s*\([^)]*\))?\.\s*", "", lines[0]) if lines else ""   # + «(связка)»
    return "blind" if head.startswith("Отправил") else "verified"


def run_tv(user_id: int, r: Route, handle: Callable[..., str], factory: Callable[[], Any]) -> tuple[str, Optional[str]]:
    reply = handle(user_id, r.text, factory)
    return r.note + reply, tv_outcome(reply)


def garmin_kind(r: Route, now: Optional[_dt.datetime] = None) -> str:
    if r.text in ("morning", "evening"):
        return r.text
    now = now or _dt.datetime.now()
    return "morning" if now.hour < 15 else "evening"     # как /garmin


def garmin_outcome(reply: str) -> str:
    if "вход истёк" in reply:
        return "FAIL"
    if "недоступно" in reply:
        return "WARN"
    return "PASS"


def run_garmin(r: Route, run: Callable[[str], str], now: Optional[_dt.datetime] = None) -> tuple[str, str]:
    reply = run(garmin_kind(r, now))
    return reply, garmin_outcome(reply)


def should_record(last: Optional[dict], status: str, today: str) -> bool:
    """Запись в реестр -- только при смене статуса или дня: реестр append-only (TTL=0),
    каталог показывает дату -- чаще писать незачем."""
    if not last:
        return True
    return last.get("status") != status or str(last.get("ts", ""))[:10] != today


# --- D (этап D, 01.11): запрет ложного «сделал» ----------------------------------------------
# До мозга доходит только то, что маршрутизатор НЕ исполнил. Значит, если человек просил действие
# (повелительное наклонение), а модель отвечает «включил/готово» -- это ложь системы о себе:
# бот ничего не делал. Такой ответ не пишется в историю (иначе повторяется -- находка 30.10 16:10)
# и помечается для человека. Вопрос о прошлом («что ты сделал?») -- не просьба: отчёт модели
# по фактам дома не трогается.
_REQUEST_RE = re.compile(
    _B + r"(включи|выключи|переключи|запусти|останови|поставь|установи|сделай|нажми|убавь|прибавь|"
    r"увеличь|уменьши|настрой|открой|закрой|напечатай|отправь|смени|поменяй|верни|убери|добавь|"
    r"включите|выключите|сделайте|поставьте|"
    # (12.11): разговорные глаголы с полигона («vrubi rozovuju», внешний корпус) -- после транслита request_view
    r"вруби|выруби|врубай|вырубай|включай|выключай|глуши|заглуши|приглуши|сбавь|выстави|переведи|поменяй|"
    r"врубите|вырубите|сбавьте|приглушите)" + _E)
# Прошедшее время мужского/среднего рода и итоговые слова. Женский род (-ла) не берётся: так в
# ответе говорят о технике («Стиральная запустила»), а Око о себе -- в мужском/среднем.
_DONE_RE = re.compile(
    r"(?<![a-zа-я])(?<!не )(включил[оа]?|выключил[оа]?|переключил[оа]?|запустил[оа]?|остановил[оа]?|"
    r"поставил[оа]?|установил[оа]?|сделал[оа]?|нажал[оа]?|убавил[оа]?|прибавил[оа]?|увеличил[оа]?|"
    r"уменьшил[оа]?|настроил[оа]?|открыл[оа]?|закрыл[оа]?|напечатал[оа]?|отправил[оа]?|сменил[оа]?|"
    r"поменял[оа]?|вернул[оа]?|убрал[оа]?|добавил[оа]?|готово|выполнено|сделано)(?![a-zа-я])")


# Краткие причастия («Кондиционер включён», «Подсветка переключена»): род согласуется с предметом,
# а не с говорящим -- женский род здесь допустим.
_DONE_PART_RE = re.compile(
    r"(?<![a-zа-я])(?<!не )(включен|выключен|переключен|запущен|остановлен|установлен|настроен|"
    r"изменен|сменен|отправлен|напечатан|увеличен|уменьшен|нажат|выполнен|сделан)[аоы]?(?![a-zа-я])")   # 10.11: + безличные «нажато»


# Подпись модуля (01.11, глобальный тест): мозг ответил на «podsvetka zelenaja» строкой
# «Подсветка: цвет зелёный, hue 60, sat 255, bri 255» -- скопировал формат событий ТВ из блока фактов,
# телевизор не трогался. Модули отвечают ДО мозга, поэтому их формат в ответе мозга -- подделка.
# Исключение -- вопрос («что ты сделал с подсветкой?»): пересказ факта допустим.
# 03.11 (живой случай 01:2x): «Яркость подсветки … установлена на 6», «установлена на 251 (было 255)» -- мозг
# выдал установку значения, ТВ не трогался (на «jarkostj na 6» без глагола просьбы _REQUEST_RE молчал).
_MODULE_SIG_RE = re.compile(
    r"(подсветка:|громкость:\s*\d+|(?<![a-z])hue\s*\d+|(?<![a-z])sat\s*\d+|follow_(audio|video|color)|"
    r"отправил:|звук (вы)?ключен:|"
    r"(?<!не )(?<![a-zа-я])(установлен|выставлен|изменен|увеличен|уменьшен|повышен|понижен)[аоы]?\s+(на|до)\s+-?\d+|"
    # 03.11 01:3x: «Цвет подсветки … установлен как зелёный» (на ТВ был другой) -- состояние словами, не числом
    r"(?<!не )(?<![a-zа-я])(установлен|выставлен|изменен|переключен)[аоы]?\s+(как|на)\s+[а-я]{4,})")


# D1 (02.11): выдуманные команды и пути API в ответе мозга. Живой случай 22:41 / 23:22: мозг выдал
# «/tv ambilight/mode expert», «/tv ambilight/power On» -- такого синтаксиса у бота нет; записанная в
# историю выдумка повторилась на следующий вопрос (kn=31: справки RAG не было). Выдуманный API неверен
# и в ответе на вопрос, поэтому проверка -- ДО выхода по «?». Решение Оператора 02.11: ответ скрывается
# целиком, показывается только пометка (bot.D1_NOTE).
# Команды, зарегистрированные в bot.py (CommandHandler). Сверка -- тест test_d1_d2.py (дрейф).
BOT_COMMANDS = frozenset({
    "status", "memory", "discuss", "enddiscuss", "forget", "newepoch", "tv", "garmin", "force_brain", "cam",
    "amb",                                   # AMB_MUSIC (04.11): пульт подсветки под музыку
    "talk", "lock",                          # B18 (18.11): тумблер БОЕВОЙ / ТОЛК
})
# /tv с аргументом-путем: «/tv ambilight/mode», «/tv audio/volume».
_FAKE_TV_PATH_RE = re.compile(r"/tv\s+[a-z_]+/")
# Пути JSON API Philips (JointSpace): ambilight/…, menuitems/…, channeldb/…, activities/…, audio/volume,
# input/key, powerstate. «audio/input» -- только с известным вторым сегментом («audio/video» -- прозой).
_FAKE_API_PATH_RE = re.compile(
    r"(?<![a-z0-9_])((ambilight|menuitems|channeldb|activities)/[a-z_]+|audio/volume|input/(key|pointer)|"
    r"powerstate)(?![a-z0-9_])")
# Slash-команда: «/» не после буквы/цифры/«/»/«:»/«.» (пути, URL, «и/или», «km/h»), за ней -- не «/» (путь).
_SLASH_CMD_RE = re.compile(r"(?<![a-zа-я0-9_/:.\\])/([a-z][a-z0-9_]*)(?![a-z0-9_/])")


def fake_api(answer: str) -> Optional[str]:
    """D1: фрагмент выдуманной команды/пути API из ответа модели или None."""
    a = (answer or "").lower()
    m = _FAKE_TV_PATH_RE.search(a) or _FAKE_API_PATH_RE.search(a)
    if m:
        return m.group(0).strip()
    for m in _SLASH_CMD_RE.finditer(a):
        if m.group(1) not in BOT_COMMANDS:
            return m.group(0)
    return None


# QWORD (03.11): вопрос без «?». Живой случай 00:13: «v kakom rezime rabotaet televizor» (транслит, голос/латиница
# -- знак «?» Оператор обычно не ставит) -> мозг пересказал состояние («hue 0») -> D_GUARD принял за подделку
# выполнения («бот это НЕ выполнял»). Вопросительные слова -- кириллица и транслит. Голое «как» не берётся:
# «подсветка как в кино» -- команда.
_QWORD_RE = re.compile(
    _B + r"(какой|какая|какое|каком|какие|какую|каким|какого|что с|что сейчас|как там|сколько|когда|где|почему|зачем|ли|"
    r"kakoj|kakoy|kakaja|kakaya|kakoe|kakom|kakie|kakuju|kakuyu|kakogo|(chto|cto|shto) s|(chto|cto|shto) sejchas|kak tam|"
    r"skol'?j?ko|kogda|gde|pochemu|zachem|li)" + _E)


def is_question(user_text: str) -> bool:
    """«?» или вопросительное слово (кириллица/транслит) БЕЗ просьбы о действии: «включи подсветку, где музыка»
    -- просьба, ложное «включил» на неё по-прежнему ловится."""
    q = (user_text or "").lower().replace("ё", "е")
    if "?" in q:
        return True
    qv = request_view(user_text)            # «kakoj rezhim» / «sdelaj potishe» -- как кириллица
    return bool(_QWORD_RE.search(qv)) and not _REQUEST_RE.search(qv)


# (11.11, живые 13:55 / 22:00, red-team Fable): модель выдаёт статус ТВ без исполнения в формате, которого нет у
# подписи модуля: метка источника из блока фактов («ТВ: …», event_log LABELS) и именной статус без двоеточия
# («подсветка музыка, цвет розовый»). Честный ответ модуля ТВ сюда не попадает: он уходит из _route_reply до модели.
_COLOUR = r"(?:красн|син|зелен|желт|бел|фиолет|оранж|розов|голуб|бирюз|малинов|сиренев|красот)\w*"
# (12.11): метка -- в любом месте строки (живой 22:12:56 «… Гармин: …. ТВ: подсветка музыки, яркость 2 из 10»),
# с пометкой происхождения из блока фактов («ТВ (прочитано с телевизора): …»), но ТОЛЬКО со статусом после неё:
# «ТВ: в этой памятке …» (контрпример) -- справка, не статус.
_FAKE_LABEL_RE = re.compile(
    r"(?<![a-zа-я])(?:тв|телевизор|tv)(?:\s*\([^)\n]{0,60}\))?\s*:\s*"
    r"(?=(?:подсветк|громкост|звук|яркост|экран|режим|цвет|музык|включ|выключ|вкл|выкл|on(?![a-z])|off(?![a-z])|\d|"
    + _COLOUR + r"))")
_FAKE_NOM_RE = re.compile(
    r"(?<![a-zа-я])(?:подсветка|громкость|звук|яркость|экран)\s*(?:[:—–-]\s*)?"
    r"(?:(?:теперь|сейчас|уже|горит|в режиме)\s+)*"
    r"(?:музык\w*|под музыку|за картинкой|" + _COLOUR + r"|включен\w*|выключен\w*|вкл|выкл|\d+(?:\s*(?:из|/)\s*\d+)?)(?![a-zа-я])"
    r"(?=\s*(?:[,.;!)/%]|\(|$))")     # значение закрывает утверждение; «… не является …» -- упоминание, не статус
# (12.11, контрпримеры): установка состояния словами без метки и без имени параметра --
# «Готово: установлен розовый оттенок», «Применено: выбран красный цвет», «Сейчас выбран динамический режим».
# На транслит-ввод («vrubi rozovuju») _REQUEST_RE молчит, поэтому -- здесь, а не в _DONE_RE.
_FAKE_SET_RE = re.compile(
    r"(?<![a-zа-я])(?<!не )(?:установлен|выбран|применен|переключен|активирован|выставлен)[аоы]?\s+(?:на\s+)?"
    r"(?:[а-я]+\s+){0,2}(?:цвет|оттен|режим)\w*")
# QFIX (12.11): (1) линейно -- без «/"/` и перевода строки внутри: на 30 000 незакрытых «« было 1.1 с (квадратичный
# перебор), теперь каждый поиск упирается в следующую «; (2) скобки (…) -- тоже цитата: каталог для модели даёт
# примеры фраз в скобках (Оператор 12.11 14:12), модель повторяет их в подсказках -- это не статус.
_QUOTED_RE = re.compile(r"«[^»«\n]*»|\"[^\"\n]*\"|`[^`\n]*`|\([^()\n]*\)")


# (12.11, «да» Оператора 16:38): (1) просьба на латинице/раскладке. _REQUEST_RE и _QWORD_RE видят кириллицу;
# ввод «vrubi rozovuju» / «ghjcdtnrf» без нормализации проходил мимо, и слова выполнения не проверялись (корпус
# корпус B1: 6/30 на латинице против 18/30). request_view -- текст для РАСПОЗНАВАНИЯ (не для показа и не для мозга):
# исходник + исправленная раскладка (layout_fix.normalize) + транслит всех латинских слов.
# (2) происхождение: до мозга доходит только то, что маршрутизатор НЕ исполнил. Ввод с предметом ТВ (не вопрос)
# -> утверждение модели о действии/состоянии ложно, даже без глагола просьбы («podsvetka krasnaja», «потише»).
_LAT_WORD_RE = re.compile(r"[a-z']+")
# Цвет -- только полной формой прилагательного: транслит английского даёт «since» -> «синце», «bel» и т.п.;
# корень _COLOUR («син\w*») на такой строке ложно видел предмет ТВ (replay memory.db: событие 205/206, COMPLIANCE).
_COLOUR_FULL = (r"(?:(?:красн|зелен|желт|розов|голуб|фиолетов|оранжев|бирюзов|малинов|сиренев)"
                r"(?:ая|ый|ое|ую|ой|ые|ого|ым|ом|ых|ий|ей)|син(?:яя|ий|юю|ее|ей|его|им|ие)|бел(?:ая|ый|ое|ую|ым|ые))")
_TV_SUBJECT_RE = re.compile(
    _B + r"(подсветк\w*|амбилайт\w*|ambilight|яркост\w*|громк\w*|звук\w*|тише|громче|темнее|светлее|ярче|потише|погромче|потемнее|посветлее|поярче|"
    r"экран\w*|телевизор\w*|тв|tv|канал\w*|hdmi|кинорежим\w*|" + _COLOUR_FULL + r")" + _E)
# Дополнительные утверждения о действии/состоянии (корпус B1: «снизил», «перешел», «сохранены», «сейчас стоит …»).
# В отличие от _DONE_RE -- с проверкой отрицания в той же фразе: «Я не знаю, какой сейчас активирован спектр» (B2).
_DONE_EXT_RE = re.compile(
    r"(?<![a-zа-я])(снизил[оа]?|сбавил[оа]?|понизил[оа]?|повысил[оа]?|перевел[оа]?|перешел|перешло|приглушил[оа]?|отключил[оа]?|"
    r"подключил[оа]?|сохранил[оа]?|обновил[оа]?|изменил[оа]?|активировал[оа]?|выбрал[оа]?|применил[оа]?|"
    r"выставил[оа]?|"
    r"(?:отключен|подключен|сохранен|обновлен|активирован|выбран|применен|приглушен|переведен|выставлен|снижен|"
    r"повышен|понижен|сбавлен|убавлен|прибавлен|погашен)[аоы]?|"
    r"(?:сейчас|теперь|уже)\s+(?:[а-я]+\s+)?(?:активе?н\w*|стоит|играет|горит|включен\w*|установлен\w*)|"
    r"текущее значение|значение теперь)(?![a-zа-я])")
# Отрицание/условие/совет в той же фразе ДО утверждения («я не знаю, какой сейчас …», «если звук отключен …»,
# «можешь …») или «ли» сразу после («включен ли телевизор») -- не утверждение о сделанном.
_NEG_RE = re.compile(r"(?<![a-zа-я])(не|нет|нельзя|невозможно|если|чтобы|можешь|можно|попробуй|когда)(?![a-zа-я])")
_LI_AFTER_RE = re.compile(r"\s+ли(?![a-zа-я])")


def request_view(user_text: str) -> str:
    """строка для распознавания просьбы/вопроса: исходник + раскладка + транслит латиницы (нижний регистр, е)."""
    q = (user_text or "").lower().replace("ё", "е")
    views = [q]
    try:
        fx = layout_fix.normalize(user_text or "")
        if fx.kind:
            views.append(fx.text.lower().replace("ё", "е"))
        if _LAT_WORD_RE.search(q):
            views.append(_LAT_WORD_RE.sub(lambda m: layout_fix._translit(m.group(0)), q))
    except Exception:       # распознавание не должно ронять ответ; без нормализации -- как до «просьба на транслите»
        pass
    return "\n".join(views)


def _hedged(a: str, m) -> bool:
    start = max(a.rfind(ch, 0, m.start()) for ch in ".!?\n")
    return bool(_NEG_RE.search(a[start + 1:m.start()]) or _LI_AFTER_RE.match(a, m.end()))


def _claim(rx, a: str) -> Optional[str]:
    """первое совпадение rx, которое не под отрицанием/условием своей фразы."""
    for m in rx.finditer(a):
        if not _hedged(a, m):
            return m.group(0).strip()
    return None


def _ext_claim(a: str) -> Optional[str]:
    """утверждение из _DONE_EXT_RE без отрицания раньше в той же фразе."""
    return _claim(_DONE_EXT_RE, a)


def fake_status(answer: str, set_claims: bool = True) -> Optional[str]:
    """фрагмент выдуманного статуса ТВ в ответе МОДЕЛИ или None (цитаты «…» и код не считаются).
    set_claims=False -- без _FAKE_SET_RE (fake_done проверяет его последним, после слов выполнения)."""
    a = _QUOTED_RE.sub(" ", (answer or "").lower().replace("ё", "е"))
    m = _FAKE_LABEL_RE.search(a)
    if m:                               # для пометки -- строка статуса целиком, не обрывок «тв: п»
        return a[m.start():].split("\n")[0].strip()[:60]
    m = _FAKE_NOM_RE.search(a) or (_FAKE_SET_RE.search(a) if set_claims else None)
    return m.group(0).strip() if m else None


def state_snapshot(user_text: str, answer: str) -> Optional[str]:
    """RZ2 (03.11, решение Оператора «показать, не писать»): ответ мозга на ВОПРОС с подписью модуля
    («подсветка: …», «hue 20», «громкость: 30») -- пересказ состояния из блока фактов. Показывается, но в историю
    не пишется: факты свежие на каждый запрос, а копия в истории устаревает и выдаётся потом за текущее
    (живой случай 23:39:48 / 23:42:18: «подсветка музыка включена, … hue 20»). -> фрагмент подписи или None.
    Без «?» подпись модуля -- подделка выполнения (fake_done), не сюда."""
    if not is_question(user_text):
        return None
    m = _MODULE_SIG_RE.search((answer or "").lower().replace("ё", "е"))
    if m:
        return m.group(0).strip()
    return fake_status(answer)          # пересказ «ТВ: …» на вопрос -- показать, не писать


def fake_done(user_text: str, answer: str) -> Optional[str]:
    """Слово ложного выполнения из ответа модели или None.
    0) D1: выдуманная команда/путь API -- всегда, и в ответе на вопрос; далее -- не на вопрос («?»):
    1) подпись модуля в ответе мозга -- всегда подделка; 2) человек ПРОСИЛ действие, а модель
    отвечает, что выполнила."""
    api = fake_api(answer)
    if api:
        return api
    q = (user_text or "").lower().replace("ё", "е")
    a = (answer or "").lower().replace("ё", "е")
    if is_question(q):        # «?» или вопросительное слово (QWORD, 03.11)
        return None
    sig = _MODULE_SIG_RE.search(a)
    if sig:
        return sig.group(0).strip()
    st = fake_status(answer, set_claims=False)    # «ТВ: подсветка музыка, цвет розовый» без исполнения
    if st:
        return st
    qv = request_view(user_text)
    if not (_REQUEST_RE.search(qv) or _TV_SUBJECT_RE.search(qv)):    # просьба после транслита / предмет ТВ
        st = _FAKE_SET_RE.search(_QUOTED_RE.sub(" ", a))   # «выбран красный цвет» на ввод без просьбы
        return st.group(0).strip() if st else None
    for m in _DONE_RE.finditer(a):
        word = m.group(1)
        if word.endswith("а") and word not in ("готово",):     # «включила» -- о технике, не о себе
            continue
        if not _REQUEST_RE.search(q) and _hedged(a, m):        # путь транслита/предмета -- «если я включил …»
            continue
        return word
    if not _REQUEST_RE.search(q):      # дошли сюда по транслиту/предмету ТВ -- с учётом отрицания/условия
        return _claim(_DONE_PART_RE, a) or _claim(_FAKE_SET_RE, _QUOTED_RE.sub(" ", a)) \
            or _ext_claim(_QUOTED_RE.sub(" ", a))
    m = _DONE_PART_RE.search(a) or _FAKE_SET_RE.search(_QUOTED_RE.sub(" ", a))     # + «выбран … цвет»
    if m:
        return m.group(0)
    return _ext_claim(_QUOTED_RE.sub(" ", a))     # «снизил», «сохранены», «сейчас стоит …» (без отрицания)
