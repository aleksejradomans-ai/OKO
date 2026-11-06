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

import tv_commands

logger = logging.getLogger("oko.router")

SELF, GARMIN, DEVICES, TV, BRAIN = "self", "garmin", "devices", "tv", "brain"
HINT = "hint"   # R1 (02.11): «как …» про ТВ -> готовые фразы, без модели
CLARIFY = "clarify"   # CLARIFY (03.11): неясно, к какому устройству просьба -> переспросить, без модели
TVSTATE = "tvstate"   # TV_STATE (03.11): «в каком режиме ТВ» -> последнее известное из журнала событий, без модели
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


def _tv_route(t: str, intent: Optional[Callable[[str], Optional[str]]]) -> Optional[Route]:
    """t -- нормализованный текст (tv_commands.normalize). Route(tv) или None."""
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
        return Route(TV, t, reason=f"tv_rule:{cmd.name}") if _tv_class_ok(cmd.name, t) else None
    if not (AMB_RE.search(t) or VOL_RE.search(t) or LIGHT_RE.search(t)):
        return None          # модель смысла -- только при явном предмете
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
        return ("📺 Состояние ТВ не знаю: через меня команд подсветке и звуку не было, а сам телевизор я не опрашиваю.\n"
                "Проверь на экране. Управление: «подсветка музыка», «громкость 20».")

    def when(ts: str) -> str:
        try:
            d = _dt.datetime.fromisoformat(ts)
        except (TypeError, ValueError):
            return "время неизвестно"
        return d.strftime("%d.%m %H:%M")

    lines = ["📺 Сам телевизор сейчас не опрашивал. Последнее, что он сообщил после моих команд:"]
    if light:
        lines.append(f"• {when(light[0])} — {light[1]}")
    if level:
        lines.append(f"• {when(level[0])} — {level[1]}")
    if sound:
        lines.append(f"• {when(sound[0])} — {sound[1]}")
    lines.append("Если на экране другое — меняли пультом или в меню: это я не вижу.")
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


def route(text: str, raw: Optional[str] = None,
          intent: Optional[Callable[[str], Optional[str]]] = default_intent,
          ctx: Optional[tuple] = None) -> Route:
    """ctx=(предмет, прошлая фраза) последней исполненной команды ТВ или None (CTX_FOLLOW)."""
    r = _route_typo(text, raw, intent)
    if r.kind != BRAIN:
        return r
    variants = [text or ""]
    tr = _translit(raw or text or "")
    if tr and tr not in variants:
        variants.append(tr)
    if ctx:
        for v in variants:
            ph = _follow_phrase(tv_commands.normalize(v), ctx)
            if not ph:
                continue
            r2 = _route_typo(ph, None, None)
            if r2.kind == TV:
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
    for v in variants:
        r = _tv_route(tv_commands.normalize(v), intent)
        if r is not None:
            return r
    return Route(BRAIN, reason="brain")


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
    r"включите|выключите|сделайте|поставьте)" + _E)
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
    r"изменен|сменен|отправлен|напечатан|увеличен|уменьшен)[аоы]?(?![a-zа-я])")


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
    return "?" in q or (bool(_QWORD_RE.search(q)) and not _REQUEST_RE.search(q))


def state_snapshot(user_text: str, answer: str) -> Optional[str]:
    """RZ2 (03.11, решение Оператора «показать, не писать»): ответ мозга на ВОПРОС с подписью модуля
    («подсветка: …», «hue 20», «громкость: 30») -- пересказ состояния из блока фактов. Показывается, но в историю
    не пишется: факты свежие на каждый запрос, а копия в истории устаревает и выдаётся потом за текущее
    (живой случай 23:39:48 / 23:42:18: «подсветка музыка включена, … hue 20»). -> фрагмент подписи или None.
    Без «?» подпись модуля -- подделка выполнения (fake_done), не сюда."""
    if not is_question(user_text):
        return None
    m = _MODULE_SIG_RE.search((answer or "").lower().replace("ё", "е"))
    return m.group(0).strip() if m else None


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
    if not _REQUEST_RE.search(q):
        return None
    for m in _DONE_RE.finditer(a):
        word = m.group(1)
        if word.endswith("а") and word not in ("готово",):     # «включила» -- о технике, не о себе
            continue
        return word
    m = _DONE_PART_RE.search(a)
    return m.group(0) if m else None
