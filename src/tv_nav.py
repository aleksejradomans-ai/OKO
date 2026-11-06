"""tv_nav.py — навигация по меню телевизора голосом/текстом: серии кнопок и названные пункты (28.10.2026).

Схема Оператора (human in the loop): он видит меню и называет, бот нажимает, он смотрит и подтверждает.
Телевизор НЕ сообщает, что на экране (input/key отвечает 200 на любое имя, замер 27.10), поэтому:
  - серии кнопок: «вниз три», «вправо вправо ок», «назад дважды», «три раза вниз» — с паузой между нажатиями
    (TVClient.send_keys: телевизор отвечает до перерисовки, без паузы кнопки теряются);
  - названные пункты: Оператор один раз показывает путь — «запомни настройки домой вниз два ок»
    (двоеточие не обязательно: голос его не произносит), потом «открой настройки» / просто «настройки».
    Хранение — JSON в data/ (tv_macros.json), переживает перезапуск.
Ответ всегда «отправил», не «сделал»: проверить экран боту нечем.

Модуль стоит ПЕРЕД tv_commands.parse в tv_bot_handler: parse() -> команда или None (не моё — разбирает
tv_commands). Одиночная кнопка («вниз») остаётся за tv_commands — там её проверенные тесты.
Standby в серию и в названный пункт не попадает никогда: выключение — только с подтверждением (tv_commands).
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

MAX_PRESSES = 20

# Только навигационные кнопки: то, что безопасно жать сериями. Имена — из tv_control.KEYS (тест сверяет).
NAV_WORDS: dict[str, str] = {
    "вверх": "CursorUp", "наверх": "CursorUp", "выше": "CursorUp",
    "вниз": "CursorDown", "ниже": "CursorDown",
    "влево": "CursorLeft", "налево": "CursorLeft", "левее": "CursorLeft",
    "вправо": "CursorRight", "направо": "CursorRight", "правее": "CursorRight",
    "ок": "Confirm", "окей": "Confirm", "ok": "Confirm", "ввод": "Confirm", "выбрать": "Confirm",
    "выбери": "Confirm", "enter": "Confirm",
    "назад": "Back", "back": "Back",
    "домой": "Home", "меню": "Home", "home": "Home",
    "инфо": "Info", "опции": "Options",
}
SAY = {"CursorUp": "вверх", "CursorDown": "вниз", "CursorLeft": "влево", "CursorRight": "вправо",
       "Confirm": "ок", "Back": "назад", "Home": "домой", "Info": "инфо", "Options": "опции"}
NUM_WORDS = {"один": 1, "одна": 1, "два": 2, "две": 2, "дважды": 2, "три": 3, "трижды": 3, "четыре": 4,
             "пять": 5, "шесть": 6, "семь": 7, "восемь": 8, "девять": 9, "десять": 10}
FILLER = {"и", "потом", "затем", "ещё", "еще", "раз", "раза", "нажми", "жми", "на", "кнопка", "кнопку", "пожалуйста"}
SAVE_WORDS = {"запомни", "сохрани", "запиши"}
OPEN_WORDS = {"открой", "зайди", "перейди", "покажи", "иди"}
FORGET_WORDS = {"забудь", "удали"}
LIST_PHRASES = {"что помнишь", "что ты помнишь", "список", "пункты", "названия", "макросы"}

BLIND_KIND = "blind"          # перекрывается tv_commands.BLIND при импорте (тот же смысл: без обратной связи)
LOCAL_KIND = "local"          # команда памяти: телевизор не трогается


try:                                   # tv_bot_handler ловит tv_commands.Unknown — NavError обязан быть им
    from tv_commands import Unknown as _UnknownBase
except Exception:                      # pragma: no cover - вне бота (песочница)
    class _UnknownBase(ValueError):
        pass


class NavError(_UnknownBase):
    """Понятная ошибка разбора навигации (текст для человека)."""


def _blind() -> str:
    try:
        import tv_commands
        return tv_commands.BLIND
    except Exception:
        return BLIND_KIND


def tokens(text: str) -> list[str]:
    t = (text or "").lower().replace("ё", "е")
    t = re.sub(r"[^\w\s-]", " ", t)
    return [w for w in t.split() if w]


def keys_from(words: list[str]) -> list[str] | None:
    """Слова -> кнопки. None — если есть хоть одно постороннее слово (это не навигация).
    Число относится к соседней кнопке: «вниз три» и «три раза вниз» — одно и то же."""
    out: list[str] = []
    pending: int | None = None
    last_key_idx: int | None = None
    for w in words:
        if w in FILLER:
            continue
        n = int(w) if w.isdigit() else NUM_WORDS.get(w)
        if n is not None:
            if n < 1 or n > MAX_PRESSES:
                raise NavError(f"число {n}: допустимо 1–{MAX_PRESSES}")
            if last_key_idx is not None and pending is None:
                out.extend([out[last_key_idx]] * (n - 1))        # «вниз три»: одна уже есть, добавить n-1
                last_key_idx = None
            else:
                pending = n                                     # «три вниз»
            continue
        key = NAV_WORDS.get(w)
        if key is None:
            return None
        out.extend([key] * (pending or 1))
        last_key_idx = None if pending else len(out) - 1
        pending = None
    if pending is not None or not out:
        return None
    if len(out) > MAX_PRESSES:
        raise NavError(f"{len(out)} нажатий — больше {MAX_PRESSES} за раз; разбей на части")
    return out


def say(keys: list[str]) -> str:
    """CursorDown×3, Confirm -> «вниз ×3, ок»."""
    parts: list[str] = []
    i = 0
    while i < len(keys):
        j = i
        while j < len(keys) and keys[j] == keys[i]:
            j += 1
        name = SAY.get(keys[i], keys[i])
        parts.append(f"{name} ×{j - i}" if j - i > 1 else name)
        i = j
    return ", ".join(parts)


class MacroStore:
    """Названные пункты меню: имя -> список кнопок, JSON на диске."""

    def __init__(self, path: Path | None = None):
        if path is None:
            env = os.getenv("TV_MACROS_PATH")
            if env:
                path = Path(env)
            else:
                import config
                path = Path(config.BASE_DIR) / "tv_macros.json"
        self.path = Path(path)

    def load(self) -> dict[str, list[str]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return {str(k): [str(x) for x in v] for k, v in data.items() if isinstance(v, list)}

    def save(self, macros: dict[str, list[str]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(macros, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.path)


@dataclass
class NavCommand:
    """Совместима с tv_commands.Command по полям, которые читает tv_bot_handler."""
    name: str
    kind: str
    keys: list[str] = field(default_factory=list)
    reply: str = ""
    needs_confirm: bool = False
    meta: dict = field(default_factory=dict)

    def run(self, tv) -> str:
        if self.kind == LOCAL_KIND:
            return self.reply
        tv.send_keys(self.keys)
        return f"Отправил: {say(self.keys)}" + (f" ({self.reply})" if self.reply else "")


def _norm_name(words: list[str]) -> str:
    return " ".join(words).strip()


def _find_macro(name: str, macros: dict) -> str | None:
    """Имя пункта: точное совпадение или то же по звучанию («nastrojki» == «настройки»)."""
    if name in macros:
        return name
    k = _pkey(name)
    hits = [m for m in macros if _pkey(m) == k]
    return hits[0] if len(hits) == 1 else None


def parse(text: str, store: MacroStore | None = None) -> NavCommand | None:
    """Текст -> NavCommand, либо None (не навигация — пусть разбирает tv_commands)."""
    words = tokens(text)
    if not words:
        return None
    joined = " ".join(words)

    if joined in LIST_PHRASES:
        macros = (store or MacroStore()).load()
        if not macros:
            body = "Названных пунктов пока нет. Пример: «запомни настройки домой вниз два ок»."
        else:
            body = "Помню:\n" + "\n".join(f"• {k}: {say(v)}" for k, v in sorted(macros.items()))
        return NavCommand("nav_list", LOCAL_KIND, reply=body)

    if words[0] in SAVE_WORDS:
        rest = words[1:]
        split = next((i for i, w in enumerate(rest) if w in NAV_WORDS or w.isdigit() or w in NUM_WORDS), None)
        if split is None or split == 0:
            raise NavError("скажи имя и путь: «запомни настройки домой вниз два ок»")
        name = _norm_name(rest[:split])
        if name in NAV_WORDS:
            raise NavError(f"«{name}» — это кнопка, назови пункт иначе")
        keys = keys_from(rest[split:])
        if keys is None:
            raise NavError("в пути есть слова, которые не кнопки; можно: вверх, вниз, влево, вправо, ок, назад, домой")
        st = store or MacroStore()
        macros = st.load()
        prev = _find_macro(name, macros)
        old = macros.pop(prev) if prev else None
        macros[name] = keys
        st.save(macros)
        note = f" (было: {say(old)})" if old else ""
        return NavCommand("nav_save", LOCAL_KIND, reply=f"Запомнил «{name}»: {say(keys)}{note}. "
                                                         f"Теперь: «открой {name}».")

    if words[0] in FORGET_WORDS and len(words) > 1:
        name = _norm_name(words[1:])
        st = store or MacroStore()
        macros = st.load()
        found = _find_macro(name, macros)
        if found is None:
            return None
        del macros[found]
        name = found
        st.save(macros)
        return NavCommand("nav_forget", LOCAL_KIND, reply=f"Забыл «{name}».")

    # «открой настройки», «зайди в настройки», или просто «настройки» — только если такое имя запомнено.
    rest = words[1:] if words[0] in OPEN_WORDS else words
    if rest and rest[0] in ("в", "во"):
        rest = rest[1:]
    if rest:
        name = _norm_name(rest)
        macros = (store or MacroStore()).load() if (words[0] in OPEN_WORDS or name not in NAV_WORDS) else {}
        found = _find_macro(name, macros) if macros else None
        if found:
            return NavCommand("nav_macro", _blind(), keys=list(macros[found]), reply=f"пункт «{found}»",
                              meta={"macro": found})

    keys = keys_from(words)
    if keys is None or len(keys) < 2:
        return None                    # одиночная кнопка и всё прочее — к tv_commands
    return NavCommand("nav_keys", _blind(), keys=keys)


# ---------------------------------------------------------------------------------------------------------
# Ввод в стиле Оператора (28.10, 21:56): «ok», «vpravo», «vniz tri», «hvatit», или кириллица в английской
# раскладке («dybp»). Живой замер: «ok» и «vpravo» -> «не понял команду». Нормализация бота (layout_fix.normalize)
# здесь не годится: «vverh» она превращает в «ммукр». Поэтому каждое слово сводится к слову словаря команд
# по звучанию (layout_fix.phonetic_key) или посимвольным сдвигом раскладки — и ТОЛЬКО если результат есть
# в словаре; незнакомые слова остаются как были (имена пунктов, текст для tv_commands).
# ---------------------------------------------------------------------------------------------------------
EXTRA_VOCAB = {"хватит", "выйти", "выход", "да", "нет", "подтверждаю", "громче", "тише", "погромче", "потише",
               "громкость", "звук", "без", "включи", "выключи", "верни", "подсветка", "подсветку", "статус",
               "телевизор", "помощь", "в", "во", "экран",   # 06.11: «vikljuci ekran» без /tv
               # 03.11 (живой случай 01:2x): «jarkostj na 6», «minus 4» уходили в мозг -- слова яркости и шага не сводились
               "яркость", "яркости", "насыщенность", "плюс", "минус", "на", "ярче", "тусклее", "текущая", "текущий",
               # 03.11 01:3x (живые фразы): «cvet zeltij», «jarkostj cutj viwe», «sdelaj tusklee», «podsvetki»
               "цвет", "цвета", "подсветки", "выше", "ниже", "чуть", "сделай", "сейчас",
               # 03.11 02:0x: продолжение без предмета -- «vikljuci ee», «sdelaj ewe»
               "ее", "его", "еще", "снова", "опять"}
_CYR_RE = re.compile(r"[а-я]")
_LAT_RE = re.compile(r"[a-z]")


def _pkey(text: str) -> str:
    try:
        import layout_fix
        return layout_fix.phonetic_key(text)
    except Exception:                  # pragma: no cover
        return text


def vocabulary() -> set[str]:
    words = {w for w in NAV_WORDS if _CYR_RE.search(w)} | set(NUM_WORDS) | FILLER | SAVE_WORDS | OPEN_WORDS \
        | FORGET_WORDS | EXTRA_VOCAB | {w for p in LIST_PHRASES for w in p.split()}
    try:
        import tv_commands
        for attr in ("KEY_WORDS", "AUDIO_ALGOS", "VIDEO_SETTINGS", "HUES"):
            for phrase in getattr(tv_commands, attr, {}) or {}:
                words |= set(tokens(phrase))
    except Exception:                  # pragma: no cover - вне бота
        pass
    return {w for w in words if _CYR_RE.search(w)}


def _phonetic_map(vocab: set[str]) -> dict[str, str]:
    seen: dict[str, set] = {}
    for w in vocab:
        seen.setdefault(_pkey(w), set()).add(w)
    return {k: next(iter(v)) for k, v in seen.items() if k and len(v) == 1}   # неоднозначные ключи не используются


def canonical(text: str, extra: set[str] | None = None) -> str:
    """«vniz tri» -> «вниз три», «ok» -> «ок», «dybp» -> «вниз»; прочие слова не меняются."""
    vocab = vocabulary() | set(extra or ())
    pm = _phonetic_map(vocab)
    out = []
    for t in tokens(text):
        if t in vocab or t.isdigit():
            out.append(t)
            continue
        hit = pm.get(_pkey(t))
        if hit is None and "w" in t:
            # 03.11: Оператор пишет «w» вместо «ш/щ» («viwe» = выше, «tekuwij» = текущий); layout_fix читает w как «в»
            hit = pm.get(_pkey(t.replace("w", "sh"))) or pm.get(_pkey(t.replace("w", "sch")))
        if hit is None and _LAT_RE.search(t):
            try:
                import layout_fix
                shifted = "".join(layout_fix.EN_TO_RU.get(ch, ch) for ch in t)
            except Exception:          # pragma: no cover
                shifted = t
            hit = shifted if shifted in vocab else pm.get(_pkey(shifted))
        out.append(hit or t)
    return " ".join(out)
