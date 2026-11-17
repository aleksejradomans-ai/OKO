"""menu_syntax.py — разбор человеческих названий пунктов меню ТВ (MENU_SYNTAX, 05.11.2026).

Место в контуре:
  tv_nav.canonical -> tv_nav.parse -> menu_syntax.parse -> tv_commands.parse

Правила срабатывания (по схеме tv_menu_aliases.json / gemini_menu_aliases_2026-11-05_v3.json):
1. Пользовательские макросы и серии кнопок (tv_nav.parse) идут ПЕРЕД этим модулем.
2. Фразы из блока ambiguous (или одновременное совпадение 2+ несводимых целей) возвращают
   локальную команду уточнения (name="menu_clarify", kind="local") без обращения к ТВ.
3. Цель срабатывает ТОЛЬКО если:
   - совпали ВСЕ основы из requires_all (с учётом частей речи и беглых гласных);
   - совпала хотя бы одна отличительная основа из key (с окончаниями её pos: noun/verb/adj)
     ИЛИ хотя бы одно целое слово из exact_words;
   - слова из context сами по себе цель не включают.
4. Решение Оператора (05.11 17:50):
   - «выключи экран», «погаси экран», «чёрный экран» -> settings.general.power:Выкл. экран
     (команда screen_off, route(..., press=True), 3 нажатия Confirm);
   - «выключи телевизор», «выключи тв» — НЕ меню (остаются за standby в tv_commands.py).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import tv_menu_map
import tv_nav

HERE = Path(__file__).resolve().parent
ALIASES_PATH = HERE / "tv_menu_aliases.json"
SCREEN_OFF_TARGET = "settings.general.power:Выкл. экран"
BLIND_KIND = "blind"
LOCAL_KIND = "local"

# Окончания по частям речи (pos: noun | adj | verb).
# Для noun включены падежные формы множественного числа и относительные прилагательные
# («антенн» -> «антенных», «спутник» -> «спутниковые»), но НЕ глагольные окончания,
# чтобы глагол «установ» («установить») не совпадал с существительным «установка».
POS_ENDS: dict[str, str] = {
    "noun": (
        r"(а|я|у|ю|о|е|ы|и|ь|й|ей|ой|ом|ем|ам|ям|ами|ями|ах|ях|ов|ев|"
        r"ий|ия|ии|ию|ием|иям|иями|иях|ью|ие|мя|мени|менем|мена|мен|"
        r"(ов|ев)?(ый|ий|ой|ая|яя|ое|ее|ые|ие|ого|его|ому|ему|ым|им|ом|ем|ых|их|ую|юю))?"
    ),
    "adj": (
        r"(ый|ий|ой|ая|яя|ое|ее|ую|юю|ого|его|ому|ему|ым|им|ом|ем|"
        r"ые|ие|ых|их|ыми|ими|ей|о|е|а|я|ы|и)?"
    ),
    "verb": (
        r"(и|й|ь|ить|ать|ять|еть|уть|ти|чь|ите|йте|ься|ться|аться|яться|иться|"
        r"л|ла|ло|ли|у|ю|ешь|ет|ем|ете|ут|ют|ишь|ит|им|ат|ят|"
        r"ление|ления|лений|лению|лением)?"
    ),
}
ANY_POS_END = (
    r"(а|я|у|ю|о|е|ы|и|ь|й|ей|ой|ом|ем|ам|ям|ами|ями|ах|ях|ов|ев|"
    r"ий|ия|ии|ию|ием|иям|иями|иях|ью|ие|"
    r"(ов|ев)?(ый|ий|ой|ая|яя|ое|ее|ую|юю|ого|его|ому|ему|ым|им|ом|ем|ые|ие|ых|их|ыми|ими)|"
    r"ить|ать|ять|еть|уть|ти|чь|ите|йте|ься|ться|ление|ления|лений|лению|лением)?"
)

# Набор стандартных окончаний для генерации опорных словоформ от основ (key/requires_all/context):
_STEM_SAMPLE_ENDS: dict[str, tuple[str, ...]] = {
    "noun": ("", "а", "я", "у", "ю", "е", "и", "ы", "о", "ь", "ов", "ев", "ей", "ам", "ами", "ах", "ия", "ии", "ию"),
    "adj": ("ый", "ий", "ой", "ая", "яя", "ое", "ее", "ые", "ие", "ого", "его", "ому", "ему", "ым", "им", "ую", "юю"),
    "verb": ("и", "й", "ь", "ить", "ать", "ять", "еть", "уть", "ти", "ите", "йте", "ение", "ения"),
}

# Общие контекстные основы: если фраза совпала и с общей целью (только из этого набора),
# и с узкой предметной целью (имеющей специфичную основу вне этого набора), побеждает узкая цель.
GLOBAL_CONTEXT_STEMS = frozenset({
    "настройк", "меню", "раздел", "установк", "открой",
    "экран", "стил", "звук", "звуков", "изображен", "картинк",
    "подсветк", "амбилайт", "ambilight",
})

# Технические английские токены, которые не транслитерируются в кириллицу.
TECH_LATIN = frozenset({
    "off", "on", "ambilight", "intellisound", "easylink", "dts", "play-fi", "playfi",
    "dolby", "atmos", "suite", "ambiscape", "wifi", "wi-fi", "bluetooth",
    "dvb-t", "dvb-c", "dvb-s", "hbbtv", "hbb", "cam", "pcmcia", "airplay",
    "homekit", "apple", "screensaver", "hdmi", "ultra", "4k", "hd", "uhd",
    "pin", "epg", "standby", "mute", "unmute", "home", "ok",
    "youtube", "netflix", "spotify", "prime",
})

# Цели, у которых основа из requires_all продублирована в key, но в key/exact_words
# есть обязательные предметные уточнители (без которых цель срабатывать не должна).
REQUIRED_DISTINCT_TARGETS: dict[str, frozenset[str]] = {
    "settings.parental:Установить код": frozenset({
        "пин-код", "пинкод", "парол", "код", "кода", "коду", "кодом", "пин", "pin", "kod",
    }),
    "settings.channels:Интерфейс CAM": frozenset({
        "pcmcia", "слот", "cam", "кам",
    }),
}


@dataclass
class MenuCommand:
    """Команда навигации по карте меню ТВ (совместима с tv_commands.Command и tv_nav.NavCommand)."""

    name: str
    kind: str
    target: str | None = None
    keys: list[str] = field(default_factory=list)
    reply: str = ""
    needs_confirm: bool = False
    targets: tuple[str, ...] = ()
    meta: dict[str, Any] = field(default_factory=dict)

    def run(self, tv: Any) -> str:
        if self.kind == LOCAL_KIND:
            return self.reply
        if hasattr(tv, "send_keys"):
            tv.send_keys(self.keys)
        else:
            for k in self.keys:
                tv.send_key(k)
        return f"Отправил: {tv_nav.say(self.keys)}" + (f" ({self.reply})" if self.reply else "")


def _default_aliases_path() -> Path:
    if ALIASES_PATH.is_file():
        return ALIASES_PATH
    fallback = HERE / "gemini_menu_aliases_2026-11-05_v3.json"
    return fallback if fallback.is_file() else ALIASES_PATH


_CACHE: dict[Path, dict[str, Any]] = {}


def load_aliases(path: Path | None = None) -> dict[str, Any]:
    p = (path or _default_aliases_path()).resolve()
    if p not in _CACHE:
        _CACHE[p] = json.loads(p.read_text(encoding="utf-8"))
    return _CACHE[p]


def normalize(text: str) -> str:
    """Нижний регистр, ё -> е, раскрытие экранного многоточия «приложе...», сохранение дефисов и 2.0."""
    t = (text or "").lower().replace("ё", "е")
    t = re.sub(r"приложе(\.\.\.|…)", "приложений", t)
    t = re.sub(r"[^a-zа-я0-9\s.-]", " ", t)
    t = re.sub(r"(?<!\d)\.(?!\d)", " ", t)
    t = re.sub(r"(?<![a-zа-я0-9])-|-(?![a-zа-я0-9])", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def tokens(text: str) -> list[str]:
    norm = normalize(text)
    return [w for w in norm.split() if w]


def _stem_variants(stem: str, pos: str | None) -> list[tuple[str, str]]:
    """Возвращает пары (основа, regex_окончаний) с учётом беглых гласных и сокращений."""
    s = stem.lower().replace("ё", "е")
    end_re = POS_ENDS.get(pos or "", ANY_POS_END)
    out = [(re.escape(s), end_re)]

    if s == "автовыкл":
        out.append((r"автовыключ(ен)?", POS_ENDS["noun"]))
    elif s == "детей":
        out.append((r"дет", r"(и|ей|ям|ьми|ск[а-я]*)"))
    elif s == "сеть":
        out.append((r"сет", r"(ь|и|ью|ей|ям|ями|ях)"))
    elif s == "мышь" or s == "мыши":
        out.append((r"мыш", r"(ь|и|ью|ей|ам|ами|ах|к[а-я]*)"))
    elif s == "речь" or s == "речи":
        out.append((r"реч", r"(ь|и|ью|ей)"))
    elif s == "часы":
        out.append((r"час", r"(ы|ов|ам|ами|ах)"))

    if pos in ("noun", None):
        if s.endswith("йк") and len(s) >= 4:
            out.append((re.escape(s[:-2] + "ек"), r""))
        elif len(s) >= 4 and s.endswith("к") and s[-2] not in "аеиоуыэюяй":
            out.append((re.escape(s[:-1] + "ок"), r""))
    return out


def word_matches_stem(word: str, stem: str, pos: str | None = None) -> bool:
    """Проверяет, совпадает ли отдельное слово с основой и допустимыми окончаниями её pos."""
    w = word.lower().replace("ё", "е")
    s = stem.lower().replace("ё", "е")
    if w == s:
        return True
    for base_pat, end_pat in _stem_variants(s, pos):
        if re.fullmatch(rf"{base_pat}{end_pat}", w):
            return True
    return False


def _pkey(word: str, c_as_ch: bool = False) -> str:
    """Фонетический ключ для сверки транслита.

    Правило для «j»:
    - перед гласной (ja/ju/je/jo) даёт йотированную гласную (я/ю/е);
    - после согласной (в конце слова или перед согласной: stilj, chetkostj,
      roditeljskij, obnovitj, poljzovateljskij, dopolniteljnije) даёт мягкий знак «ь»;
    - после гласной или в начале слова (otkroj, tajmer, nastrojki) даёт «й».
    """
    w = word.lower().replace("ё", "е")
    if re.search(r"[a-z]", w):
        if c_as_ch:
            w = re.sub(r"c(?!h)", "ch", w)
        for lat, cyr in (
            ("shch", "щ"), ("sch", "щ"), ("zh", "ж"), ("kh", "х"),
            ("ch", "ч"), ("sh", "ш"), ("ts", "ц"),
            ("ja", "я"), ("ya", "я"), ("ju", "ю"), ("yu", "ю"),
            ("je", "е"), ("ye", "е"), ("jo", "е"), ("yo", "е"),
        ):
            w = w.replace(lat, cyr)
        lat_map = {
            "a": "а", "b": "б", "v": "в", "g": "г", "d": "д", "e": "е",
            "z": "з", "i": "и", "k": "к", "l": "л", "m": "м",
            "n": "н", "o": "о", "p": "п", "r": "р", "s": "с", "t": "т",
            "u": "у", "f": "ф", "h": "х", "c": "ц", "w": "ш", "y": "и",
        }
        w = "".join(lat_map.get(ch, ch) for ch in w)
        w = re.sub(r"(?<=[бвгджзклмнпрстфхцчшщ])j", "ь", w)
        w = w.replace("j", "й")
    w = w.replace("щ", "ш").replace("ы", "и").replace("э", "е").replace("й", "и")
    w = w.replace("ь", "").replace("ъ", "")
    w = re.sub(r"(.)\1+", r"\1", w)
    return w


_VOCAB_CACHE: dict[Path, tuple[set[str], dict[str, str]]] = {}


def menu_vocabulary(data: dict[str, Any] | None = None, path: Path | None = None) -> tuple[set[str], dict[str, str]]:
    p = (path or _default_aliases_path()).resolve()
    if data is None and p in _VOCAB_CACHE:
        return _VOCAB_CACHE[p]
    d = data or load_aliases(p)
    cyr_words: set[str] = set()
    stem_forms: set[str] = set()

    for entry in d.get("aliases") or []:
        for field_name in ("positive", "negative", "exact_words"):
            for item in entry.get(field_name) or []:
                for w in tokens(item):
                    if re.search(r"[а-я]", w):
                        cyr_words.add(w)
        pos_map = _pos_map_for_entry(entry)
        for item in entry.get("key") or []:
            stem = str(item.get("stem") or "").lower().replace("ё", "е")
            pos = str(item.get("pos") or "noun")
            if re.fullmatch(r"[а-я]+", stem):
                for end in _STEM_SAMPLE_ENDS.get(pos, _STEM_SAMPLE_ENDS["noun"]):
                    stem_forms.add(stem + end)
        for field_name in ("requires_all", "context"):
            for raw_stem in entry.get(field_name) or []:
                stem = str(raw_stem).lower().replace("ё", "е")
                pos = pos_map.get(stem, "noun")
                if re.fullmatch(r"[а-я]+", stem):
                    for end in _STEM_SAMPLE_ENDS.get(pos, _STEM_SAMPLE_ENDS["noun"]):
                        stem_forms.add(stem + end)

    for amb in d.get("ambiguous") or []:
        for w in tokens(amb.get("phrase", "")):
            if re.search(r"[а-я]", w):
                cyr_words.add(w)

    cyr_words.update({
        "открой", "настройки", "процедура", "экран", "оставь",
        "питание", "питания", "таймер", "звука", "подсветки", "сеть", "сети",
        "наушники", "наушник", "наушников", "наушниках",
    })
    for typo in ("открйо", "настрйоки", "процедуар", "экрна", "отсавь"):
        cyr_words.discard(typo)

    pmap: dict[str, str] = {}
    for w in sorted(cyr_words):
        pk = _pkey(w)
        if pk and pk not in pmap:
            pmap[pk] = w
    for w in sorted(stem_forms):
        pk = _pkey(w)
        if pk and pk not in pmap:
            pmap[pk] = w

    all_vocab = cyr_words | stem_forms
    if p not in _VOCAB_CACHE:
        _VOCAB_CACHE[p] = (all_vocab, pmap)
    return all_vocab, pmap


_NAV_SAFE_VOCAB: set[str] | None = None


def vocabulary() -> set[str]:
    """Слова меню для tv_nav.canonical(extra=...). 06.11: без слов, чей фонетический ключ совпадает со словом
    базового словаря tv_nav («экрань» гасил «экран»: неоднозначный ключ tv_nav не использует)."""
    global _NAV_SAFE_VOCAB
    if _NAV_SAFE_VOCAB is None:
        base = tv_nav.vocabulary()
        keys = {tv_nav._pkey(w) for w in base}
        _NAV_SAFE_VOCAB = {w for w in menu_vocabulary()[0] if w in base or tv_nav._pkey(w) not in keys}
    return _NAV_SAFE_VOCAB


def _fix_transposition(word: str, vocab: set[str]) -> str | None:
    """Исправляет одиночную перестановку соседних букв («открйо» -> «открой», «настрйоки» -> «настройки»)."""
    if len(word) < 5 or word in vocab:
        return None
    chars = list(word)
    for i in range(len(chars) - 1):
        chars[i], chars[i + 1] = chars[i + 1], chars[i]
        cand = "".join(chars)
        if cand in vocab:
            return cand
        chars[i], chars[i + 1] = chars[i + 1], chars[i]
    return None


def canonical(text: str, data: dict[str, Any] | None = None) -> str:
    """Приводит транслит, раскладку и одиночные перестановки букв к словарю меню и команд."""
    vocab, pmap = menu_vocabulary(data)
    words = tokens(text)
    if not words:
        return ""
    out: list[str] = []
    for w in words:
        if w in vocab or w in TECH_LATIN or w.isdigit():
            out.append(w)
            continue
        if re.search(r"[а-я]", w):
            fixed = _fix_transposition(w, vocab)
            out.append(fixed or w)
            continue
        hit = pmap.get(_pkey(w, c_as_ch=False)) or pmap.get(_pkey(w, c_as_ch=True))
        if hit is None:
            nav_c = tv_nav.canonical(w, extra=vocab)
            if nav_c != w:
                hit = nav_c
        out.append(hit or w)
    return " ".join(out)


def _pos_map_for_entry(entry: dict[str, Any]) -> dict[str, str]:
    return {
        str(k["stem"]).lower().replace("ё", "е"): str(k.get("pos") or "noun")
        for k in (entry.get("key") or [])
        if isinstance(k, dict) and k.get("stem")
    }


def _match_entry(words: list[str], entry: dict[str, Any]) -> set[str] | None:
    """Возвращает множество совпавших ключевых/обязательных маркеров, если цель подошла, иначе None."""
    pos_map = _pos_map_for_entry(entry)
    exact_set = {str(x).lower().replace("ё", "е") for x in (entry.get("exact_words") or [])}
    reqs = [str(r).lower().replace("ё", "е") for r in (entry.get("requires_all") or [])]

    matched_markers: set[str] = set()

    # 1. Проверка requires_all (все обязаны присутствовать):
    for req in reqs:
        req_pos = pos_map.get(req)
        if req in exact_set and req in words:
            matched_markers.add(req)
            continue
        if any(word_matches_stem(w, req, req_pos) for w in words):
            matched_markers.add(req)
            continue
        return None

    # 2. Проверка key (>= 1) или exact_words (>= 1):
    key_hits: set[str] = set()
    for item in entry.get("key") or []:
        stem = str(item["stem"]).lower().replace("ё", "е")
        if entry.get("target") == SCREEN_OFF_TARGET and stem == "темн":
            continue
        pos = str(item.get("pos") or "noun")
        if any(word_matches_stem(w, stem, pos) for w in words):
            key_hits.add(stem)

    for ew in exact_set:
        if ew in words:
            key_hits.add(ew)

    if not key_hits:
        return None

    # 3. Защита целей с общим глаголом/существительным в requires_all (Установить код, Интерфейс CAM):
    target = str(entry.get("target") or "")
    distinct_needed = REQUIRED_DISTINCT_TARGETS.get(target)
    if distinct_needed and not (key_hits & distinct_needed):
        return None

    return matched_markers | key_hits


def _is_ancestor(anc: str, desc: str) -> bool:
    """Является ли узел anc родительским разделом для desc («settings.sound» -> «settings.sound:Стиль звука»)."""
    if anc == desc or ":" in anc:
        return False
    return desc.startswith(anc + ":") or desc.startswith(anc + ".")


def _disambiguate(hits: list[tuple[dict[str, Any], set[str]]]) -> list[tuple[dict[str, Any], set[str]]]:
    if len(hits) <= 1:
        return hits

    targets = [str(e["target"]) for e, _ in hits]

    # 1. Дочерний пункт внутри того же раздела побеждает родительский раздел:
    filtered = [
        (e, m) for e, m in hits
        if not any(_is_ancestor(str(e["target"]), other_t) for other_t in targets)
    ]
    if len(filtered) == 1:
        return filtered
    if filtered:
        hits = filtered

    # 2. Специфичная основа (вне GLOBAL_CONTEXT_STEMS) побеждает чисто контекстную цель:
    specific = [
        (e, m) for e, m in hits
        if any(stem not in GLOBAL_CONTEXT_STEMS for stem in m)
    ]
    if len(specific) == 1:
        return specific
    if specific:
        hits = specific

    # 3. Строгое надмножество совпавших маркеров побеждает подмножество:
    non_subset = [
        (e, m) for e, m in hits
        if not any(m < other_m for _, other_m in hits)
    ]
    if non_subset:
        hits = non_subset

    return hits


def _human_label(target: str) -> str:
    node_id, _, item = target.partition(":")
    if item:
        return f"меню «{item}»"
    return f"раздел «{node_id}»"


def _check_explicit_ambiguous(canon_text: str, words: list[str], data: dict[str, Any]) -> MenuCommand | None:
    """Проверяет явный список неоднозначных фраз из блока ambiguous."""
    stripped = list(words)
    if stripped and stripped[0] in ("открой", "покажи", "зайди", "перейди", "включи"):
        if stripped[0] == "включи" and stripped[1:] == ["экран"]:
            stripped_core = ""
        else:
            stripped_core = " ".join(stripped[1:])
    else:
        stripped_core = ""

    for amb in data.get("ambiguous") or []:
        amb_norm = normalize(amb.get("phrase", ""))
        if not amb_norm:
            continue
        if canon_text == amb_norm or (stripped_core and stripped_core == amb_norm and len(amb_norm.split()) == 1):
            t_list = tuple(str(x) for x in (amb.get("targets") or []))
            ask = str(amb.get("ask") or "")
            return MenuCommand(
                name="menu_clarify",
                kind=LOCAL_KIND,
                target=None,
                keys=[],
                reply=ask,
                targets=t_list,
                meta={"targets": list(t_list), "ask": ask},
            )
    return None


def parse(
    text: str,
    *,
    data: dict[str, Any] | None = None,
    menu_map: dict[str, Any] | None = None,
) -> MenuCommand | None:
    """Разбирает текст в команду меню (MenuCommand) или None (не меню — передать в tv_commands.parse)."""
    aliases_data = data or load_aliases()
    canon_text = canonical(text, aliases_data)
    words = tokens(canon_text)
    if not words:
        return None

    # 1. Явная неоднозначность из таблицы ambiguous:
    amb_cmd = _check_explicit_ambiguous(canon_text, words, aliases_data)
    if amb_cmd is not None:
        return amb_cmd

    # 2. Сопоставление со всеми 80 целями:
    raw_hits: list[tuple[dict[str, Any], set[str]]] = []
    for entry in aliases_data.get("aliases") or []:
        markers = _match_entry(words, entry)
        if markers is not None:
            raw_hits.append((entry, markers))

    if not raw_hits:
        return None

    resolved = _disambiguate(raw_hits)
    if len(resolved) > 1:
        t_list = tuple(str(e["target"]) for e, _ in resolved)
        labels = ", ".join(_human_label(t) for t in t_list)
        ask = f"Уточните пункт меню: {labels}?"
        return MenuCommand(
            name="menu_clarify",
            kind=LOCAL_KIND,
            target=None,
            keys=[],
            reply=ask,
            targets=t_list,
            meta={"targets": list(t_list), "ask": ask},
        )

    winner, _ = resolved[0]
    target = str(winner["target"])
    confidence = str(winner.get("confidence") or "high")
    m = menu_map or tv_menu_map.load()
    press = target == SCREEN_OFF_TARGET
    keys = tv_menu_map.route(target, m, press=press)
    cmd_name = "screen_off" if target == SCREEN_OFF_TARGET else "menu_route"
    return MenuCommand(
        name=cmd_name,
        kind=BLIND_KIND,
        target=target,
        keys=keys,
        reply=_human_label(target),
        needs_confirm=False,
        targets=(target,),
        meta={"target": target, "confidence": confidence, "press": press},
    )


def target_of(
    text: str,
    *,
    data: dict[str, Any] | None = None,
    menu_map: dict[str, Any] | None = None,
) -> str | None:
    """Удобная обёртка для тестов: возвращает id цели, если она определена однозначно, иначе None."""
    cmd = parse(text, data=data, menu_map=menu_map)
    return cmd.target if cmd is not None and cmd.name != "menu_clarify" else None
