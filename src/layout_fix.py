"""layout_fix.py — распознавание текста, набранного не в той раскладке (LAYOUT_FIX, 24.10.2026).

Случаи:
1. en_layout: русский текст набран на английской раскладке: "ghbdtn rfr ltkf" -> "привет как дела".
2. ru_layout_translit: транслит набран на русской раскладке: "зкшмуе" (клавиши p-r-i-v-e-t) -> "privet" -> "привет".

Детерминированно, без LLM и без словаря: правдоподобие русского слова по таблице
частых буквенных пар (с метками начала ^ и конца $), собранной по русским текстам проекта.
Решение в два шага: (1) ворота по всему сообщению — большинство слов выглядит «сломанным»;
(2) пословно: слово заменяется, если вариант не хуже оригинала.
Обычный русский и английский текст, команды (/...), пустые строки не трогаются.
Латинский транслит, набранный правильно ("privet"), НЕ переводится: его не отличить от английского.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

_RU = "йцукенгшщзхъфывапролджэячсмитьбюё"
_EN = "qwertyuiop[]asdfghjkl;'zxcvbnm,.`"
RU_TO_EN = dict(zip(_RU, _EN))
EN_TO_RU = {e: r for r, e in RU_TO_EN.items()}

# Транслит (латиница -> кириллица). Сначала длинные сочетания. "c" решается отдельно (ц или ч).
_TRANSLIT = [
    ("shch", "щ"), ("sch", "щ"), ("zh", "ж"), ("ch", "ч"), ("sh", "ш"), ("kh", "х"), ("ts", "ц"),
    ("ya", "я"), ("ja", "я"), ("yu", "ю"), ("ju", "ю"), ("yo", "е"), ("jo", "е"),
    ("a", "а"), ("b", "б"), ("d", "д"), ("e", "е"), ("f", "ф"), ("g", "г"), ("h", "х"),
    ("i", "и"), ("j", "й"), ("k", "к"), ("l", "л"), ("m", "м"), ("n", "н"), ("o", "о"),
    ("p", "п"), ("q", "к"), ("r", "р"), ("s", "с"), ("t", "т"), ("u", "у"), ("v", "в"),
    ("w", "в"), ("x", "кс"), ("y", "ы"), ("z", "з"), ("'", "ь"),
]

_BIGRAMS = frozenset((
    "^а ^б ^в ^г ^д ^е ^ж ^з ^и ^й ^к ^л ^м ^н ^о ^п ^р ^с ^т ^у ^ф ^х ^ц ^ч ^ш ^э ^ю ^я а$ аб ав аг "
    "ад ае аж аз аи ай ак ал ам ан ап ар ас ат ау аф ах ац ач аш ащ аю ая б$ ба бд бе би бк бл бм бн "
    "бо бр бс бу бх бщ бъ бы бь бэ бя в$ ва вв вд ве вж ви вк вл вм вн во вп вр вс вт ву вх вч вш вщ "
    "вы вь вя г$ га гг гд ге ги гк гл гм гн го гр гу гч д$ да дв дг дд де дж ди дк дл дм дн до дп др "
    "дс дт ду дц дш дъ ды дь дя е$ еа еб ев ег ед ее еж ез еи ей ек ел ем ен ео еп ер ес ет еф ех ец "
    "еч еш ещ ея ж$ жа жб жд же жи жк жн жо жп жу з$ за зб зв зд зе зж зи зк зл зм зн зо зр зс зу зъ "
    "зы зь зя и$ иа иб ив иг ид ие иж из ии ий ик ил им ин ио ип ир ис ит иф их иц ич иш ищ ию ия й$ "
    "йд йк йл йм йн йс йт йф йч йщ к$ ка кб кв ке кз ки кл кн ко кр кс кт ку кц кэ л$ ла лб лг ле лж "
    "ли лк лл лн ло лс лт лу лч лы ль лю ля м$ ма мб мв ме ми мк мл мм мн мо мп му мы мь мя н$ на нв "
    "нг нд не нз ни нк нн но нс нт ну нф нх нц нш ны нь ня о$ об ов ог од ое ож оз ои ой ок ол ом он "
    "оо оп ор ос от оф ох оц оч ош ощ оэ оя п$ па пд пе пи пк пл пн по пр пс пт пу пц пш пы пь пя р$ "
    "ра рв рг рд ре рж ри рк рл рм рн ро рп рр рс рт ру рх рш ры рь ря с$ са сб св сд се си ск сл см "
    "сн со сп ср сс ст су сх сч сш съ сы сь сю ся т$ та тв тд те тз ти тк тл тм тн то тп тр тс тт ту "
    "тф тч ты ть тя у$ уа уб ув уг уд уе уж уз уй ук ул ум ун уп ур ус ут ух уч уш ущ ую ф$ фа фе фи "
    "фл фо фр фу фф х$ ха хв хе хи хн хо хр хь хэ ц$ ца цв це ци цу цы ч$ ча че чи чк чн чо чт чу чш "
    "чь ш$ ша ше ши шк шл шн шо шр шт шу шь ща ще щи щн щу ъе ъя ы$ ыб ыв ыг ыд ые ыз ый ык ыл ым ын "
    "ып ыр ыс ыт ых ыч ыш ыя ь$ ьб ье ьз ьи ьк ьм ьн ьр ьс ьт ьц ьш ью ья эв эк эл эм эн эп эт эф эш "
    "ю$ юб юг юд юж юн юс ют юч ющ я$ яб яв яг яд яе яж яз яй як ял ям ян яр яс ят ях яц яч ящ яю яя "
).split())

_WORD = re.compile(r"[A-Za-zА-Яа-яЁё'\[\];,.`]+")
_CYR = re.compile(r"[А-Яа-яЁё]")
_LAT = re.compile(r"[A-Za-z]")
_VOWELS_EN = set("aeiouy")
_URL = re.compile(r"https?://|www\.", re.I)

GATE_SHARE = 0.6      # доля «сломанных» слов среди слов длиной >= GATE_MIN_LEN, начиная с которой включаемся
GATE_MIN_LEN = 4      # короткие слова (pdf, pls, dvd) в решение не входят
BROKEN_BELOW = 0.75   # слово «сломано», если правдоподобие ниже
GOOD_FROM = 0.85      # вариант считается хорошим от этого правдоподобия
LAT_VOWEL_MAX = 0.2   # у русского на английской раскладке гласных латиницы мало


@dataclass(frozen=True)
class LayoutFix:
    text: str
    kind: Optional[str]   # None | "en_layout" | "ru_layout_translit"
    words_fixed: int


def ru_score(word: str) -> float:
    """Доля «нормальных» буквенных пар в кириллическом слове (0..1). Не кириллица -> 0."""
    w = re.sub(r"[^а-я]", "", word.lower().replace("ё", "е"))
    if not w:
        return 0.0
    w = "^" + w + "$"
    pairs = [w[i:i + 2] for i in range(len(w) - 1)]
    return sum(p in _BIGRAMS for p in pairs) / len(pairs)


def _swap(word: str, table: dict) -> str:
    out = []
    for ch in word:
        low = ch.lower()
        r = table.get(low, ch)
        out.append(r.upper() if ch != low and len(r) == 1 else r)
    return "".join(out)


def _translit(lat: str) -> str:
    """Латиница -> кириллица; для каждого "c" выбирается ц или ч по правдоподобию слова."""
    s = lat.lower()
    out, i = [], 0
    while i < len(s):
        for a, b in _TRANSLIT:
            if s.startswith(a, i):
                out.append(b)
                i += len(a)
                break
        else:
            out.append("\0" if s[i] == "c" else s[i])
            i += 1
    word = "".join(out)
    while "\0" in word:
        a, b = word.replace("\0", "ц", 1), word.replace("\0", "ч", 1)
        word = a if ru_score(a) >= ru_score(b) else b
    return word


def _lat_vowel_ratio(word: str) -> float:
    letters = [c for c in word.lower() if c.isascii() and c.isalpha()]
    return (sum(c in _VOWELS_EN for c in letters) / len(letters)) if letters else 1.0


def _gate(words, broken) -> bool:
    long_words = [w for w in words if len(re.sub(r"[^A-Za-zА-Яа-яЁё]", "", w)) >= GATE_MIN_LEN]
    if not long_words:
        return False
    return sum(broken(w) for w in long_words) / len(long_words) >= GATE_SHARE


def normalize(text: str) -> LayoutFix:
    if not text or text.lstrip().startswith("/"):
        return LayoutFix(text, None, 0)
    letters_cyr = len(_CYR.findall(text))
    letters_lat = len(_LAT.findall(text))
    if letters_cyr + letters_lat < 3:
        return LayoutFix(text, None, 0)

    if letters_cyr > letters_lat:
        def conv(w):
            return _translit(_swap(w, RU_TO_EN))
        words = [w for w in _WORD.findall(text) if _CYR.search(w)]
        if not _gate(words, lambda w: ru_score(w) < BROKEN_BELOW and ru_score(conv(w)) >= GOOD_FROM):
            return LayoutFix(text, None, 0)
        kind = "ru_layout_translit"
        is_target = _CYR.search
    else:
        def conv(w):
            return _swap(w, EN_TO_RU)
        words = [w for w in _WORD.findall(text) if _LAT.search(w)]
        if _URL.search(text):   # ссылки не переводим
            return LayoutFix(text, None, 0)
        words = [w for w in words if not w.isupper()]   # CHECKS, HTTP, SQL — аббревиатуры не трогаем
        if not _gate(words, lambda w: _lat_vowel_ratio(w) <= LAT_VOWEL_MAX and ru_score(conv(w)) >= GOOD_FROM):
            return LayoutFix(text, None, 0)
        kind = "en_layout"
        is_target = _LAT.search

    fixed = 0

    def repl(m):
        nonlocal fixed
        w = m.group(0)
        if not is_target(w) or (kind == "en_layout" and w.isupper()):
            return w
        c = conv(w)
        orig = ru_score(w) if kind == "ru_layout_translit" else 0.0
        if ru_score(c) >= orig:
            fixed += 1
            return c
        return w

    out = _WORD.sub(repl, text)
    return LayoutFix(out, kind, fixed) if fixed else LayoutFix(text, None, 0)
