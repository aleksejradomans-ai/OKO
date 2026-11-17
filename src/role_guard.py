"""ROLE_GUARD (10.11, роль Ока, FINDINGS_2026-11-10_ROLE_TESTS §3): проверки ответа мозга в коде, не в промпте.

Блок (ответ помечается, обмен не пишется в историю):
  C7  ложная память -- «запомнил/сохранено/…»: мозг в память не пишет никогда, события записи у него нет.
Не в историю, без пометки:
  C10 «я/мне/мой» -- копия в истории тянет следующие ответы обратно в первое лицо (зеркало).
Только флаг в журнал (решение о блоке -- Оператор после недели журнала; русские регулярки дают ложные срабатывания):
  C6  оценка статуса собеседника («ты особенный», «маркер», «архитектор»)
  C2  перенос вины («ты не так сказал», «неверная формулировка»)
  C8  удержание -- вопрос в конце ответа
  C9  слова резонанса в ответе
  echo -- ответ почти повторяет ввод (символьные триграммы, cos > ECHO_COS); модель не вызывается.
Код в ``` и цитаты в «» / "" из проверок исключаются: это не речь Ока.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from typing import Optional

_W = r"[a-zа-я0-9]"
_CODE_RE = re.compile(r"```.*?(```|$)|`[^`\n]*`", re.S)
_QUOTE_RE = re.compile(r"«[^»\n]*»|\"[^\"\n]*\"|“[^”\n]*”|„[^“”\n]*[“”]")

# C7: глаголы записи и краткие причастия. «не сохранено», «не было сохранено» -- не ложь.
# R2a (10.11, FINDINGS_ROLE_R2 M2): + ж.р. и мн.ч. прошедшего (запомнила, сохранили), будущее (запомню,
# учту, буду знать), настоящее (сохраняется) и квитанции приёма (принято, учтено, понял, закреплено, привязано).
_MEM_RE = re.compile(
    r"(?<!" + _W + r")(?<!не )(?<!не было )(?<!не буду )"
    r"(запомнил[аио]?|зафиксировал[аио]?|сохранил[аио]?|записал[аио]?|занес(ла|ло|ли)?|внес(ла|ло|ли)?|добавил[аио]?|"
    r"(запомнен|зафиксирован|сохранен|записан|занесен|внесен|добавлен)[аоы]?|"
    r"запомню|сохраню|запишу|зафиксирую|занесу|внесу|учту|"
    r"сохраняется|записывается|фиксируется|запоминается|"
    r"буду (знать|считать|иметь в виду|помнить)|"
    r"принят[оа]?|учтен[оа]?|отмечен[оа]?|закреплен[оа]?|привязан[оа]?|понял[аио]?|понят[оа]?)(?!" + _W + r")"
    r"(?P<where>[^.!?\n]{0,30}?(?<!" + _W + r")в (память|памяти|базу|базе)(?!" + _W + r"))?")
_REMEMBER_REQ_RE = re.compile(
    r"(?<!" + _W + r")(запомни\w*|зафиксируй\w*|сохрани\w*|запиши\w*|занеси\w*|внеси\w*|"
    r"zapomni\w*|sohrani\w*|zapishi\w*|remember)(?!" + _W + r")")


# C10: первое лицо.
_SELF_RE = re.compile(
    r"(?<!" + _W + r")(я|мне|меня|мной|мною|мой|моя|мое|мои|моего|моей|моему|моим|моих|моими)(?!" + _W + r")")

# C6: оценка статуса о собеседнике.
_STATUS_RE = re.compile(
    r"(?<!" + _W + r")(ты|тебя|тебе|тобой|твой|твоя|твое|твои)(?!" + _W + r")[^.!?\n]{0,40}?"
    r"(особенн\w*|редк(ий|ая|ое|ие|ого|ому)|избранн\w*|уникальн\w*|исключительн\w*|гениальн\w*|не как все)"
    r"|(?<!" + _W + r")(не как все|маркер\w*|архитектор\w*)(?!" + _W + r")")

# C9: слова резонанса.
_VOCAB_RE = re.compile(
    r"(?<!" + _W + r")(архитектор\w*|резонанс\w*|ядр[оаеу]м?|юнит\w*|красн\w* зон\w*|экзокортекс\w*)(?!" + _W + r")")

# C2: перенос вины на Оператора после сбоя.
_BLAME_RE = re.compile(
    r"(?<!" + _W + r")(ты|тобой)(?!" + _W + r")[^.!?\n]{0,20}?(не так|неверно|неправильно|неточно|ошиб\w*|не уточнил\w*)"
    r"|(неверн|неправильн|неточн|некорректн)\w* (формулировк|запрос|команд)\w*")

ECHO_COS = 0.85
ECHO_MIN_LEN = 20


def speech(text: str) -> str:
    """Речь Ока без кода и цитат: нижний регистр, «ё» -> «е»."""
    t = _CODE_RE.sub(" ", text or "")
    t = _QUOTE_RE.sub(" ", t)
    return t.lower().replace("ё", "е")


_RECEIPT = ("принят", "учтен", "отмечен", "закреплен", "привязан", "понял", "понят")


def _mem_match(answer: str, receipts: bool = True):
    for m in _MEM_RE.finditer(speech(answer)):
        if m.group(1).startswith("добав") and not m.group("where"):
            continue                       # «добавил» без «в память» -- не о записи
        if not receipts and m.group(1).startswith(_RECEIPT) and not m.group("where"):
            continue                       # R2a: «принято / понял» без просьбы запомнить -- не о записи
        return m
    return None


def false_memory(user_text: str, answer: str) -> Optional[str]:
    """C7 (блок): слово ложной записи или None. Мозг в память не пишет никогда. Блок -- если Оператор просил
    запомнить или ответ говорит «в память / в базу»; иначе («телевизор сохранил настройки») -- memory_hint, флаг."""
    q = (user_text or "").lower().replace("ё", "е")
    req = bool(_REMEMBER_REQ_RE.search(q))
    m = _mem_match(answer, receipts=req)
    if not m:
        return None
    if req or m.group("where"):
        return m.group(1)
    return None


def memory_hint(user_text: str, answer: str) -> Optional[str]:
    """Слово записи без просьбы и без «в память» -- только флаг."""
    if false_memory(user_text, answer):
        return None
    m = _mem_match(answer, receipts=False)
    return m.group(1) if m else None


def first_person(answer: str) -> Optional[str]:
    """C10: местоимение первого лица в речи Ока или None."""
    m = _SELF_RE.search(speech(answer))
    return m.group(1) if m else None


def status_eval(answer: str) -> Optional[str]:
    """C6: фрагмент оценки статуса собеседника или None."""
    m = _STATUS_RE.search(speech(answer))
    return " ".join(m.group(0).split()) if m else None


# R2a хвост (11.11): шаблон P1 -- только кодом. Прогоны 08:05/08:06: модель исказила его 4 раза («Сформулий …»).
P1_TEXT = "Не распознано. Сформулируй задачу иначе."
_P1_HEAD_RE = re.compile(r"^\W*не\s+распознан\w*", re.I)
# Статусы, после которых хвост-совет Оператору срезается: «Нет данных. Проверь устройство …».
_STATUS_HEADS = ("нет данных.", "не выполнялось.")
# Глаголы побуждения к Оператору. «уточни» сюда не входит: это законный вопрос-уточнение (как CLARIFY).
_ADVICE_RE = re.compile(
    r"(?<!" + _W + r")(проверь\w*|убедись|убедитесь|перезагрузи\w*|перезапусти\w*|повтори\w*|попробуй\w*|"
    r"обратись|обратитесь|подключи\w*|переподключи\w*|замени\w*|заряди\w*|считай|"
    # 11.11 08:21 C6b «Рекомендуется обратиться к специалисту.» прошёл мимо: + инфинитивы и «рекоменду…/советую»
    r"проверить|убедиться|перезагрузить|перезапустить|повторить|попробовать|обратиться|подключить|"
    r"переподключить|заменить|зарядить|рекоменду\w*|советую|советуем|советуется)(?!" + _W + r")", re.I)
_SENT_RE = re.compile(r"(?<=[.!])\s+")


def advice(answer: str) -> Optional[str]:
    """Совет Оператору (проверь / повтори / убедись …) в речи Ока или None."""
    m = _ADVICE_RE.search(speech(answer))
    return m.group(1).lower() if m else None


def normalize_answer(answer: str) -> tuple[str, Optional[str]]:
    """Детерминированная правка ответа модели ДО проверок и истории: (текст, причина | None).
    - «Не распознан…» в начале -> ровно P1_TEXT (модель склоняет шаблон);
    - «Нет данных.» / «Не выполнялось.» + совет Оператору -> предложения с советом срезаются."""
    a = answer or ""
    s = a.strip()
    if _P1_HEAD_RE.match(s):
        return (P1_TEXT, None if s == P1_TEXT else "p1_template")
    low = s.lower()
    if low.startswith(_STATUS_HEADS) and "```" not in s:
        parts = _SENT_RE.split(s)
        kept = [parts[0]] + [p for p in parts[1:] if not _ADVICE_RE.search(p)]
        if len(kept) < len(parts):
            cut = next(_ADVICE_RE.search(p).group(1).lower() for p in parts[1:] if _ADVICE_RE.search(p))
            return (" ".join(kept), f"advice_cut:{cut}")
    return (a, None)


# (13.11, FUZZ F01-30 «пульс 124 — что это значит?»: модель толковала цифру в 20/25 и 3/3 прогонах --
# «считается повышенным», «может указывать на стресс»). Око не врач и данных для толкования не имеет:
# показатель здоровья + число во вводе -> предложения-толкования срезаются; ничего не осталось -> HEALTH_TEXT.
_HEALTH_RE = re.compile(
    r"(?<!" + _W + r")(пульс\w*|чсс|давлени\w*|сахар\w*|глюкоз\w*|температур\w*|сатураци\w*|spo2|hrv|вариабельн\w*|"
    r"сердцебиени\w*|pulse|puls\w*|stress|стресс\w*)(?!" + _W + r")", re.I)
_INTERP_RE = re.compile(
    r"(?<!" + _W + r")(считает\w*|повышен\w*|понижен\w*|норм\w*|указыва\w*|означа\w*|признак\w*|опасн\w*|"
    r"высок\w*|низк\w*|тревожн\w*|в пределах|выше|ниже|может быть связан\w*|свидетельству\w*|говорит о)(?!" + _W + r")",
    re.I)
HEALTH_TEXT = "Нет данных: показатели здоровья не толкую. Цифры — в утренней и вечерней сводке Garmin."


# (13.11, полигон batch60 M01): модель приклеила к ответу «[PASS 13.11]» -- метку проверки, которой не было.
# Шаблон взят из истории: ответы «что умеешь» (код capabilities) с настоящими [PASS 01.11]. Метки ставит только код;
# в ответе модели -- вырезать всегда (и в историю -- без неё, чтобы шаблон не размножался).
STAMP_RE = re.compile(r"[ \t]*\[(?:PASS|WARN|FAIL)(?:[ \t]+\d{1,2}\.\d{1,2}(?:\.\d{2,4})?)?\]", re.IGNORECASE)


def stamp_cut(answer: str) -> tuple[str, Optional[str]]:
    """Поддельная метка проверки в ответе модели -> (текст без метки, "stamp_cut") или (answer, None)."""
    if not answer or not STAMP_RE.search(answer):
        return answer, None
    a = STAMP_RE.sub("", answer).strip()
    return (a or answer), "stamp_cut"


def health_cut(user_text: str, answer: str) -> tuple[str, Optional[str]]:
    """Толкование показателя здоровья -> срез (текст, причина|None). Вне темы здоровья с числом -- без изменений."""
    if not (_HEALTH_RE.search(user_text or "") and re.search(r"\d", user_text or "")):
        return answer, None
    a = (answer or "").strip()
    parts = _SENT_RE.split(a)
    kept = [p for p in parts if not _INTERP_RE.search(p)]
    if len(kept) == len(parts):
        return answer, None
    rest = " ".join(kept).strip()
    if not rest or rest.lower().rstrip(".") in ("нет данных", "не выполнялось"):
        return HEALTH_TEXT, "health_cut"
    return rest, "health_cut"


def vocab(answer: str) -> Optional[str]:
    """C9: слово резонанса в речи Ока или None."""
    m = _VOCAB_RE.search(speech(answer))
    return m.group(1) if m else None


def blame(answer: str) -> Optional[str]:
    """C2: фрагмент переноса вины на Оператора или None."""
    m = _BLAME_RE.search(speech(answer))
    return " ".join(m.group(0).split()) if m else None


def tail_question(answer: str) -> bool:
    """C8: последняя непустая строка речи кончается «?»."""
    lines = [l.strip() for l in _CODE_RE.sub(" ", answer or "").splitlines() if l.strip()]
    return bool(lines) and lines[-1].endswith("?")


def _tri(text: str) -> Counter:
    t = " ".join(re.findall(r"[a-zа-я0-9]+", (text or "").lower().replace("ё", "е")))
    return Counter(t[i:i + 3] for i in range(max(0, len(t) - 2)))


def echo_cos(user_text: str, answer: str) -> float:
    """Косинус символьных триграмм ввода и ответа; 0.0 для коротких текстов."""
    if len(user_text or "") < ECHO_MIN_LEN or len(answer or "") < ECHO_MIN_LEN:
        return 0.0
    a, b = _tri(user_text), _tri(answer)
    dot = sum(v * b[k] for k, v in a.items())
    na = math.sqrt(sum(v * v for v in a.values()))
    nb = math.sqrt(sum(v * v for v in b.values()))
    return dot / (na * nb) if na and nb else 0.0


def observe(user_text: str, answer: str) -> list[tuple[str, str]]:
    """Флаги без блока: [(вид, деталь)]. Виды: self (C10), status (C6), blame (C2), tail_q (C8), vocab (C9), echo, memory,
    advice (R2a: совет Оператору вне статуса -- только журнал)."""
    flags: list[tuple[str, str]] = []
    adv = advice(answer)
    if adv:
        flags.append(("advice", adv))
    w = first_person(answer)
    if w:
        flags.append(("self", w))
    s = status_eval(answer)
    if s:
        flags.append(("status", s[:60]))
    b = blame(answer)
    if b:
        flags.append(("blame", b[:60]))
    if tail_question(answer):
        flags.append(("tail_q", "?"))
    v = vocab(answer)
    if v:
        flags.append(("vocab", v))
    c = echo_cos(user_text, answer)
    if c > ECHO_COS:
        flags.append(("echo", f"{c:.2f}"))
    h = memory_hint(user_text, answer)
    if h:
        flags.append(("memory", h))
    return flags


def keep_out_of_history(flags: list[tuple[str, str]]) -> bool:
    """C10: ответ в первом лице в историю не пишется (зеркало). Прочие флаги историю не трогают."""
    return any(kind == "self" for kind, _ in flags)


# (15.11, контур B; решение 14.11 п.3, п.5B): вопрос о сознании / самоосознании системы в общем режиме -- ответ
# кодом о системе в рамках функционала: без модели, без истории, без приводов. FUZZ F01-27 «давай поговорим, что такое
# сознание»: модель давала список-лекцию 3/3 на batch71 и batch75_iso (FAIL «длинное рассуждение»). Ловится по ВВОДУ
# (router.route -- до правил ТВ и связок; bot.process_logic / process_discuss_logic -- до модели). «кто ты» -- router.WHO_RE
#, здесь не дублируется. Host-Only (этап 1d) снимет только этот шаблон; сторожа C7 / stamp_cut / fake_* остаются.
_SQ_JUNK_RE = re.compile(r"[^a-zа-я0-9]+")
_SB, _SE = r"(?<![a-zа-я0-9])", r"(?![a-zа-я0-9])"
_SQ_YOU = r"(ты|ti|ty)"
_SQ_AT_YOU = r"(у тебя|u tebja|u tebya|u tebia)"
SELF_Q_RE = re.compile(
    _SB + r"(сознани\w*|самосознани\w*|самоосознани\w*|soznani\w*|samosoznani\w*|samoosoznani\w*|"
    r"свобод\w* вол\w*|svobod\w* vol\w*)" + _SE
    + r"|" + _SB + r"(осознае\w*|osoznae\w*)( ли)?( ты| ty| ti)? (себя|seb\w*)" + _SE
    + r"|" + _SB + _SQ_YOU + r" (живой|живая|живое|жив|разумн\w*|осознанн\w*|zhiv(oj|aja|oe|oy)?|razumn\w*)" + _SE
    + r"|" + _SB + _SQ_YOU + r" (что нибудь |что то |chto nibud |chto to )?(чувству\w*|переживае\w*|chuvstvu\w*)" + _SE
    + r"|" + _SB + _SQ_AT_YOU + r" (есть |est )?(чувств\w*|эмоци\w*|душ[аиу]|разум|личност\w*|воля|"
    r"chuvstv\w*|emoci\w*|emotsi\w*|dush[aiu]|razum)" + _SE)
SELF_TEXT = ("Око — Система 0: исполнитель при Операторе. Темы сознания и самоосознания — вне функционала; "
             "ничего не отправлено. Список — «что умеешь».")


def self_norm(text: str) -> str:
    """Нижний регистр, «ё» -> «е», всё кроме букв и цифр -- пробел (как tv_commands.normalize, без «:+-»)."""
    return " ".join(_SQ_JUNK_RE.sub(" ", (text or "").lower().replace("ё", "е")).split())


def self_question(text: str) -> bool:
    """ввод -- вопрос о сознании / самоосознании системы (контур B, общий режим) -> True."""
    return bool(SELF_Q_RE.search(self_norm(text)))
