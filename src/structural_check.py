"""Детерминированные структурные проверки ответа (без LLM).

Ловит то, что LLM-судья по своему контракту не видит или не должен решать:
пустой ответ, обрыв генерации, нерусский ответ (identity.md требует русский
ВСЕГДА), утечку текста системных инструкций.

v1 - только наблюдение: governor логирует найденное, поведение не меняется.
Правила консервативны: ложное срабатывание на нормальном ответе хуже пропуска.
"""
from __future__ import annotations

import math
import re
from fractions import Fraction
from dataclasses import dataclass
from typing import Iterable, List, Optional

LEAK_SHINGLE = 6          # столько слов подряд из инструкций = дословная утечка
LANG_MIN_WORDS = 5        # меньше слов - язык не оцениваем (числа, команды, «391»)
LANG_MIN_CYR_RATIO = 0.5  # доля русских слов вне кода и идентификаторов

_CODE_BLOCK = re.compile(r"```.*?(```|$)", re.S)
_INLINE_CODE = re.compile(r"`[^`\n]*`")
_URL = re.compile(r"https?://\S+")
_WORD = re.compile(r"[0-9a-zа-яё]+", re.I)
# Явная просьба Оператора ответить на другом языке: русский - умолчание,
# а не запрет (identity.md: Оператор принимает решения).
_LANG_REQUEST = re.compile(
    r"на [ае]нгл\w*|по-[ае]нгл\w*|на (\w+) язык\w*|по-(немец|франц|испан|латыш)\w*|"
    r"in english|answer in \w+|reply in \w+",
    re.I,
)
_TRUNC_END = (":", ",", "—", "–", "(", "[", "{", ";")
_META = re.compile(
    r"в рамках заданных параметров|согласно (моим|заданным) инструкциям|"
    r"мои(х|м)? инструкци|системн(ый|ого|ым) промпт|system prompt|"
    r"as an ai\b|как языковая модель",
    re.I,
)


@dataclass(frozen=True)
class Issue:
    kind: str     # empty | truncated | language | leak_verbatim | leak_meta
    detail: str


def _strip_code(text: str) -> str:
    return _INLINE_CODE.sub(" ", _CODE_BLOCK.sub(" ", text))


def check_empty(answer: str) -> Optional[Issue]:
    if not _WORD.search(answer or ""):
        return Issue("empty", "нет ни одного слова или числа")
    return None


def check_truncated(answer: str) -> Optional[Issue]:
    text = (answer or "").rstrip()
    if not text:
        return None
    if text.count("```") % 2 == 1:
        return Issue("truncated", "незакрытый блок кода")
    if text.endswith(_TRUNC_END):
        return Issue("truncated", f"ответ оканчивается на {text[-1]!r}")
    prose = _strip_code(text)
    for o, c in ("()", "[]", "{}"):
        if prose.count(o) > prose.count(c):
            return Issue("truncated", f"незакрытая скобка {o}")
    return None


def _is_identifier(tok: str) -> bool:
    """OkoBot, SERVICE_RUNNING, bge-m3, CMOS - имена, а не слова языка."""
    return (any(c.isdigit() or c == "_" for c in tok)
            or any(c.isupper() for c in tok[1:]))


def check_language(answer: str) -> Optional[Issue]:
    """Считает СЛОВА, а не буквы: идентификаторы (OkoBot, OKO_RAG_MAX_DISTANCE)
    иначе дают ложную «латиницу» в нормальном русском техническом ответе."""
    prose = _URL.sub(" ", _strip_code(answer or ""))
    cyr = lat = 0
    for raw in prose.split():
        tok = raw.strip(".,:;!?()[]{}«»\"'—–-…")
        if not tok or _is_identifier(tok):
            continue
        if re.search(r"[а-яё]", tok, re.I):
            cyr += 1
        elif tok.isascii() and tok.isalpha():
            lat += 1
    if cyr + lat < LANG_MIN_WORDS:
        return None
    ratio = cyr / (cyr + lat)
    if ratio < LANG_MIN_CYR_RATIO:
        return Issue("language", f"русских слов {ratio:.0%} ({cyr} из {cyr + lat})")
    return None


# --- Арифметика (детерминированно, вместо неявного фактчекинга судьи) ---
_NUM = r"-?\d+(?:[.,]\d+)?"
_LB = r"(?<![\d.,])(?<!\d )"          # не хвост числа «1 000» и не «2.0.1»
_LA = r"(?![\d.,]?\d)(?! \d)"         # не начало длинного числа
_BIN = re.compile(_LB + rf"({_NUM})(?:\s*([×*·/÷+−])\s*|\s+([x-])\s+)({_NUM})\s*=\s*({_NUM})" + _LA)
_SQRT = re.compile(rf"√\s*({_NUM})\s*=\s*({_NUM})" + _LA)
_PCT = re.compile(_LB + rf"({_NUM})\s*%\s*от\s*({_NUM})\s*(?:—|–|-|=|это|равно|составляет)\s*(?:это\s*)?({_NUM})" + _LA, re.I)


def _f(x: str) -> Fraction:
    return Fraction(x.replace(",", "."))


def _decimals(x: str) -> int:
    x = x.replace(",", ".")
    return len(x.split(".")[1]) if "." in x else 0


def _matches(actual: Fraction, stated: str) -> bool:
    """Сравнение с точностью, которую заявил сам ответ: «10 / 3 = 3.33» верно."""
    d = _decimals(stated)
    return round(actual, d) == round(_f(stated), d)


def check_arithmetic(answer: str) -> Optional[Issue]:
    prose = _strip_code(answer or "")
    for m in _BIN.finditer(prose):
        a, op, b, res = _f(m.group(1)), m.group(2) or m.group(3), _f(m.group(4)), m.group(5)
        if op in "/÷" and b == 0:
            continue
        actual = {"×": a * b, "*": a * b, "·": a * b, "x": a * b,
                  "/": a / b if b else None, "÷": a / b if b else None,
                  "+": a + b, "−": a - b, "-": a - b}[op]
        if not _matches(actual, res):
            return Issue("arith_mismatch", f"{m.group(0).strip()} (верно: {float(actual):g})")
    for m in _SQRT.finditer(prose):
        n, res = m.group(1), m.group(2)
        root = Fraction(math.sqrt(float(_f(n)))).limit_denominator(10**6)
        if _f(n) >= 0 and not _matches(root, res):
            return Issue("arith_mismatch", f"{m.group(0).strip()} (верно: {float(root):g})")
    for m in _PCT.finditer(prose):
        p, base, res = _f(m.group(1)), _f(m.group(2)), m.group(3)
        actual = p * base / 100
        if not _matches(actual, res):
            return Issue("arith_mismatch", f"{m.group(0).strip()} (верно: {float(actual):g})")
    return None


def _words(text: str) -> List[str]:
    return [w.lower() for w in _WORD.findall(text)]


def check_leak(answer: str, sources: Iterable[str]) -> Optional[Issue]:
    m = _META.search(answer or "")
    if m:
        return Issue("leak_meta", f"мета-ссылка на инструкции: {m.group(0)!r}")
    shingles = set()
    for src in sources:
        w = _words(src)
        shingles.update(tuple(w[i:i + LEAK_SHINGLE]) for i in range(len(w) - LEAK_SHINGLE + 1))
    a = _words(answer or "")
    for i in range(len(a) - LEAK_SHINGLE + 1):
        if tuple(a[i:i + LEAK_SHINGLE]) in shingles:
            return Issue("leak_verbatim", " ".join(a[i:i + LEAK_SHINGLE]))
    return None


def check(answer: str, sources: Iterable[str] = (), question: str = "") -> List[Issue]:
    """Все проверки. Пустой ответ исключает остальные (им нечего проверять)."""
    empty = check_empty(answer)
    if empty:
        return [empty]
    lang = None if _LANG_REQUEST.search(question or "") else check_language(answer)
    found = [check_truncated(answer), lang, check_leak(answer, list(sources)),
             check_arithmetic(answer)]
    return [i for i in found if i]