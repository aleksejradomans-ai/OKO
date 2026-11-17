"""N = f_norm(S): Unicode -> транслит -> регистр и пунктуация -> токены -> морфология -> нечёткий поиск.

Морфология — закрытая таблица словоформ встроенных словарей (без внешних библиотек):
слово вне словарей остаётся как есть. Нечёткое сопоставление только порождает кандидатов:
единственный кандидат становится леммой с пометкой fuzzy, несколько — остаются альтернативами,
и автоматическое исполнение по ним запрещено.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Optional

_TOKEN = re.compile(r"\d+(?:[.,]\d+)?|[a-z]+|[а-я]+")
STOPWORDS = frozenset({"пожалуйста", "плиз", "ну", "ка", "же", "а", "и", "в", "во", "на", "по", "до", "к", "с",
                       "мне", "нам", "там", "тут", "ты", "бы", "ли", "уже", "еще"})

# Транслит. Неоднозначная латинская буква даёт несколько кандидатов; выбирается тот, что есть
# в словарях, иначе — первый по таблице. Диграфы (ch, sh, ju…) пробуются раньше одиночных букв.
_DIGRAPHS = {"shch": "щ", "sch": "щ", "zh": "ж", "kh": "х", "ts": "ц", "ch": "ч", "sh": "ш",
             "ju": "ю", "yu": "ю", "ja": "я", "ya": "я", "jo": "е", "yo": "е", "je": "е", "ye": "е"}
_LETTERS = {"a": "а", "b": "б", "c": "цч", "d": "д", "e": "е", "f": "ф", "g": "г", "h": "х", "i": "иы",
            "k": "к", "l": "л", "m": "м", "n": "н", "o": "о", "p": "п", "q": "к", "r": "р", "s": "с",
            "t": "т", "u": "у", "v": "в", "w": "шщ", "y": "ыи", "z": "з"}
_LATIN_VOWELS = frozenset("aeiouy")
_MAX_CANDIDATES = 512


@dataclass(frozen=True)
class Normalized:
    text: str                  # все токены, включая незначащие
    tokens: tuple[str, ...]    # значащие токены
    variants: dict = field(default_factory=dict)   # токен -> прочие кандидаты транслита


@dataclass(frozen=True)
class Tok:
    text: str
    lemma: str                       # лемма словаря либо сам токен
    known: bool = False
    fuzzy: bool = False
    alternatives: tuple[str, ...] = ()   # несколько нечётких кандидатов -> неоднозначность

    @property
    def is_number(self) -> bool:
        return self.text[0].isdigit()


def translit_candidates(word: str) -> list[str]:
    """Все прочтения латинского слова кириллицей, в порядке таблицы."""
    out: list = []

    def walk(i: int, acc: str) -> None:
        if len(out) >= _MAX_CANDIDATES:
            return
        if i == len(word):
            out.append(acc)
            return
        for n in (4, 3, 2):
            seg = word[i:i + n]
            cyr = _DIGRAPHS.get(seg) if len(seg) == n else None
            if cyr:
                walk(i + n, acc + cyr)
        ch = word[i]
        after_vowel = i > 0 and word[i - 1] in _LATIN_VOWELS
        if ch == "j":
            options = ("й",) if i == 0 or after_vowel else ("ь",)
        elif ch == "x":
            options = ("кс",)
        elif ch == "y" and after_vowel:
            options = ("й", "ы", "и")
        else:
            options = _LETTERS.get(ch, ch)
        for cyr in options:
            walk(i + 1, acc + cyr)

    walk(0, "")
    return out


def _near(word: str, known, fuzzy_min_len: int) -> bool:
    return len(word) >= fuzzy_min_len and any(within1(word, form) for form in known)


def detransliterate(word: str, known=frozenset(), fuzzy_min_len: int = 4) -> tuple[str, tuple]:
    """(выбранное прочтение, остальные кандидаты). Точное словарное совпадение побеждает;
    затем — прочтение в одной правке от словаря; иначе первое по таблице."""
    cands = translit_candidates(word)
    for c in cands:
        if c in known:
            return c, ()
    best = next((c for c in cands if _near(c, known, fuzzy_min_len)), cands[0])
    return best, tuple(c for c in cands if c != best)


def normalize(raw: str, known=frozenset(), fuzzy_min_len: int = 4) -> Normalized:
    """known — словоформы словарей: латинское слово из словаря (tv) не транслитерируется."""
    text = unicodedata.normalize("NFKC", str(raw or "")).casefold().replace("ё", "е")
    tokens, variants = [], {}
    for tok in _TOKEN.findall(text):
        if tok[0].isdigit():
            tok = tok.replace(",", ".")
        elif tok[0] <= "z" and tok not in known:
            tok, others = detransliterate(tok, known, fuzzy_min_len)
            if others:
                variants[tok] = others
        tokens.append(tok)
    return Normalized(" ".join(tokens), tuple(t for t in tokens if t not in STOPWORDS), variants)


def within1(a: str, b: str) -> bool:
    """d_L(a, b) <= 1."""
    if a == b:
        return True
    la, lb = len(a), len(b)
    if abs(la - lb) > 1:
        return False
    if la > lb:
        a, b, la, lb = b, a, lb, la
    i = 0
    while i < la and a[i] == b[i]:
        i += 1
    return a[i + 1:] == b[i + 1:] if la == lb else a[i:] == b[i + 1:]


def morph(tokens, forms: dict, fuzzy_min_len: int = 4, variants: Optional[dict] = None) -> list[Tok]:
    """forms: словоформа -> лемма. Нечёткий поиск идёт после выбора кандидата транслита и
    учитывает остальные его прочтения: разные леммы у разных прочтений — это неоднозначность."""
    out = []
    for t in tokens:
        if t in forms:
            out.append(Tok(t, forms[t], known=True))
            continue
        cands: tuple = ()
        if not t[0].isdigit():
            readings = [r for r in (t, *(variants or {}).get(t, ())) if len(r) >= fuzzy_min_len]
            cands = tuple(sorted({lemma for form, lemma in forms.items()
                                  if any(within1(r, form) for r in readings)}))
        if len(cands) == 1:
            out.append(Tok(t, cands[0], known=True, fuzzy=True))
        else:
            out.append(Tok(t, t, alternatives=cands))
    return out
