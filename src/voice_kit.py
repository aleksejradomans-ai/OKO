"""voice_kit.py -- подготовка голоса бота (голос Despina, генерация вручную вне бота): тексты, каталоги, раскладка.

Без сети и без моделей; при импорте ничего не делает. Источник фраз -- утверждённый файл v4 (JSONL, 177 записей);
результат -- v5 (две фразы по решению 16.11 00:15), словарь {summary}, каталог {cmd}, manifest.json, пачки для
ручной генерации. Каталог {cmd} строится из кода маршрутов (tv_commands.py, tv_nav.py, menu_syntax.py) чтением AST,
не из ответа модели: каждое имя команды из кода обязано иметь формулировку здесь (иначе ValueError -- рассинхрон
кода и голоса виден сразу, а не в живой команде).

Раскладка файлов (manifest):
  статическая фраза  <key>__<field>__<v>.wav           key = p<NNN>_<slug>, NNN -- номер записи v5 (001..177)
  часть шаблона      <key>__<field>__<v>__<part>.wav   part: a, b, b_few, b_many, plain
  числа 0..100       num_<000..100>.wav                через носитель «Текущее значение: <число>.» (носитель отрезается)
  состояние ТВ       stat_<slug>.wav                   {summary} в «Телевизор: {summary}, на {time}.»
  команда            cmd_<NNN>_<slug>.wav              {cmd} в «Понято: {cmd}.»
"""
from __future__ import annotations

import ast
import copy
import json
import re
from pathlib import Path

VOICED_FIELDS = ("say", "ok", "fail", "limit", "ask")
EXPECTED_V4 = {"records": 177, "say": 355, "ok": 57, "fail": 56, "limit": 20, "ask": 3, "check": 45}
TUNING_LINE = "Система готова к работе."
CARRIER = "Текущее значение:"
BATCH_MIN, BATCH_MAX, BATCH_MAX_S, BATCH_HARD_S = 10, 15, 30.0, 35.0
CHARS_PER_S, PAUSE_S = 13.0, 0.7
STYLE_PROMPT = (
    "Говори на русском языке спокойным, ровным, безоценочным тоном диспетчера или бортового ассистента. "
    "Средняя теплота тембра: звучи естественно и по-человечески, не как механический синтезатор, но без "
    "заискивания, лести, радости и театральности. Строго избегай восклицательной и вопросительной интонации: "
    "каждая фраза и каждое отдельное число должны завершаться ровным, утвердительным, слегка нисходящим тоном "
    "в конце. Держи стабильный средний темп речи (1.0x), одинаковую громкость и чёткую артикуляцию без "
    "растягивания гласных и без драматических пауз."
)

# ---------------------------------------------------------------- текст
_TR = dict(zip("абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
               ["a", "b", "v", "g", "d", "e", "e", "zh", "z", "i", "y", "k", "l", "m", "n", "o", "p", "r", "s", "t",
                "u", "f", "h", "ts", "ch", "sh", "sch", "", "y", "", "e", "yu", "ya"]))


def slug(s: str, limit: int = 24) -> str:
    """ASCII-слаг: транслит кириллицы, нижний регистр, [a-z0-9_], без повторных «_», не длиннее limit."""
    t = "".join(_TR.get(c, c) for c in s.lower())
    t = re.sub(r"[^a-z0-9]+", "_", t).strip("_")
    return (t[:limit].rstrip("_")) or "x"


# Слова, где в текстах нужна «ё» (генератор не угадывает: только этот список; найденные -- в отчёт).
YO_WORDS = {
    "включен": "включён", "выключен": "выключен", "отключен": "отключён", "подключен": "подключён",
    "погашен": "погашен", "зеленый": "зелёный", "желтый": "жёлтый", "теплый": "тёплый", "еще": "ещё",
    "черный": "чёрный", "отправлен": "отправлен", "поднят": "поднят", "сохранен": "сохранён",
    "установлен": "установлен", "остановлен": "остановлен", "запущен": "запущен", "прочтен": "прочтён",
    "получен": "получен", "подтвержден": "подтверждён", "сброшен": "сброшен", "выполнен": "выполнен",
    "переведен": "переведён", "найден": "найден", "ее": "её", "ниже": "ниже",
}
_YO = {k: v for k, v in YO_WORDS.items() if k != v}


def yo(text: str) -> tuple[str, list[str]]:
    """Расставить «ё» по YO_WORDS (с сохранением заглавной буквы) -> (текст, список заменённых слов)."""
    changed = []

    def rep(m):
        w = m.group(0)
        low = w.lower()
        if low in _YO:
            new = _YO[low]
            changed.append(low)
            return new[0].upper() + new[1:] if w[0].isupper() else new
        return w
    return re.sub(r"[А-Яа-яЁё]+", rep, text), changed


def speakable(text: str) -> str:
    """Текст для генерации: «?»/«!» -> «.», «ё» по словарю, пробелы схлопнуты."""
    t = re.sub(r"[?!]+", ".", text)
    t = re.sub(r"\.\s*\.", ".", t)
    return re.sub(r"\s+", " ", yo(t)[0]).strip()


_UNITS = ["ноль", "один", "два", "три", "четыре", "пять", "шесть", "семь", "восемь", "девять", "десять",
          "одиннадцать", "двенадцать", "тринадцать", "четырнадцать", "пятнадцать", "шестнадцать", "семнадцать",
          "восемнадцать", "девятнадцать"]
_TENS = {20: "двадцать", 30: "тридцать", 40: "сорок", 50: "пятьдесят", 60: "шестьдесят", 70: "семьдесят",
         80: "восемьдесят", 90: "девяносто"}


def number_words(n: int) -> str:
    """0..100 количественным числительным (именительный падеж, мужской род): 25 -> «двадцать пять»."""
    if not 0 <= n <= 100:
        raise ValueError("число 0..100")
    if n < 20:
        return _UNITS[n]
    if n == 100:
        return "сто"
    t, u = (n // 10) * 10, n % 10
    return _TENS[t] + ("" if u == 0 else " " + _UNITS[u])


def loop_tail(n: int) -> str:
    """Хвост gen_loop: «раза подряд.» при n%10 in {2,3,4} и n%100 not in {12,13,14}, иначе «раз подряд.»."""
    return "b_few" if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14) else "b_many"


def time_files(hh: int, mm: int) -> list[str]:
    """{time} ЧЧ:ММ -> num-файлы: часы num_<ЧЧ>; :00 -> ноль ноль; :01..:09 -> ноль + цифра; :10..:59 -> num_<ММ>."""
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        raise ValueError("время ЧЧ:ММ")
    out = [f"num_{hh:03d}"]
    if mm == 0:
        out += ["num_000", "num_000"]
    elif mm < 10:
        out += ["num_000", f"num_{mm:03d}"]
    else:
        out += [f"num_{mm:03d}"]
    return out


# ---------------------------------------------------------------- v4 -> v5
def load_jsonl(path: Path) -> list[dict]:
    return [json.loads(ln) for ln in Path(path).read_text(encoding="utf-8-sig").splitlines() if ln.strip()]


def counts(recs: list[dict]) -> dict:
    c = {"records": len(recs)}
    for f in VOICED_FIELDS + ("check",):
        c[f] = sum(len(r.get(f) or []) for r in recs)
    c["voiced"] = sum(c[f] for f in VOICED_FIELDS)
    return c


V5_CHANGES = {
    "gen_series_stopped": {"say": ["Серия остановлена на шаге {i}, всего шагов: {m}."],
                           "glue": {"a": "Серия остановлена на шаге", "b": "всего шагов:"},
                           "v5": "решение 16.11 00:15: числа в одной форме -- «на шаге {i}, всего шагов: {m}»"},
    "gen_loop": {"glue": {"a": "Одна и та же команда", "b_few": "раза подряд.", "b_many": "раз подряд.",
                          "rule": "b_few: n % 10 in {2,3,4} и n % 100 not in {12,13,14}; иначе b_many"},
                 "v5": "решение 16.11 00:15: хвост по числу «раза подряд.» / «раз подряд.»"},
}


def make_v5(v4: list[dict]) -> tuple[list[dict], list[str]]:
    """-> (записи v5, строки diff). Остальные 175 записей -- без изменений."""
    ids = [r["id"] for r in v4]
    if len(set(ids)) != len(ids):
        raise ValueError("в v4 повторяются id")
    out, diff = [], []
    for r in v4:
        n = copy.deepcopy(r)
        ch = V5_CHANGES.get(r["id"])
        if ch:
            for k, v in ch.items():
                old = r.get(k)
                n[k] = copy.deepcopy(v)
                if k != "v5" and old != v:
                    diff.append(f"{r['id']}.{k}: {json.dumps(old, ensure_ascii=False)} -> {json.dumps(v, ensure_ascii=False)}")
        out.append(n)
    missing = sorted(set(V5_CHANGES) - set(ids))
    if missing:
        raise ValueError(f"в v4 нет записей {missing}")
    return out, diff


# ---------------------------------------------------------------- {summary}
# (текст состояния, файл, шаблон поиска строки-источника; None -- база решения 16.11 00:15)
SUMMARY = [
    ("включён", "src/tv_control.py", r'POWERSTATES\b.*"On"'),
    ("выключен", None, None),
    ("в режиме ожидания", "src/tv_control.py", r'POWERSTATES\b.*"Standby"'),
    ("на главном экране", "src/tv_nav.py", r'"домой": "Home"'),
    ("в меню настроек", "src/tv_commands.py", r'"настройки": "Options"'),
    ("нет сигнала", None, None),
    ("без звука", "src/tv_commands.py", r'", без звука"'),
    ("подсветка включена", "src/tv_commands.py", r'return "Подсветка: включена \(On\)"'),
    ("подсветка выключена", "src/tv_commands.py", r'return "Подсветка: выключена \(Off\)"'),
    ("подсветка следует за видео", "src/tv_commands.py", r'set_ambilight_style\("FOLLOW_VIDEO"'),
    ("подсветка под музыку", "src/tv_commands.py", r'set_ambilight_style\("FOLLOW_AUDIO"'),
    ("подсветка одним цветом", "src/tv_commands.py", r'^MODE_COLOUR = '),
    ("подсветка недоступна", "src/tv_commands.py", r'"подсветка недоступна"'),
]


def _find_line(root: Path, rel: str, pattern: str) -> int:
    rx = re.compile(pattern)
    for i, ln in enumerate((Path(root) / rel).read_text(encoding="utf-8").splitlines(), 1):
        if rx.search(ln):
            return i
    raise ValueError(f"источник состояния не найден: {rel} /{pattern}/")


def summary_dict(root: Path) -> list[dict]:
    out = []
    for text, rel, pat in SUMMARY:
        src = "решение 16.11 00:15 (база)" if rel is None else f"{rel}:{_find_line(root, rel, pat)}"
        out.append({"text": text, "slug": slug(text), "file": f"stat_{slug(text)}", "source": src})
    return out


# ---------------------------------------------------------------- {cmd}
CMD_SAY = {
    "standby": "выключение телевизора", "power_on": "включение телевизора", "screen_off": "выключение экрана",
    "screen_on": "включение экрана", "amb_off": "выключение подсветки", "amb_on": "включение подсветки",
    "amb_music": "подсветка под музыку", "amb_ai": "подсветка за видео, режим ИИ",
    "amb_sat_up": "насыщенность подсветки выше", "amb_sat_down": "насыщенность подсветки ниже",
    "amb_sat_step": "шаг насыщенности подсветки", "amb_sat_set": "насыщенность подсветки",
    "amb_brighter": "подсветка ярче", "amb_dimmer": "подсветка тусклее", "amb_step": "шаг яркости подсветки",
    "amb_brightness_set": "яркость подсветки", "amb_brightness_get": "текущая яркость подсветки",
    "mute_on": "выключение звука", "mute_off": "включение звука", "volume_up": "громче", "volume_down": "тише",
    "volume_set": "громкость", "volume_get": "текущая громкость", "status": "состояние телевизора",
    "menu_route": "переход по меню", "nav_keys": "серия кнопок", "nav_macro": "сохранённый маршрут",
}
# имена команд, которые отвечают сами и не подтверждаются «Понято» (локальный ответ без действия на ТВ)
CMD_LOCAL = {"menu_clarify": "локальное уточнение, ответ сам задаёт вопрос", "nav_list": "список маршрутов",
             "nav_save": "сохранение маршрута", "nav_forget": "удаление маршрута"}
HUE_SAY = {"красн": "красный", "оранжев": "оранжевый", "желт": "жёлтый", "зелен": "зелёный", "бирюзов": "бирюзовый",
           "голуб": "голубой", "син": "синий", "фиолетов": "фиолетовый", "сиренев": "сиреневый",
           "розов": "розовый", "бел": "белый", "тепл": "тёплый", "холодн": "холодный"}
ALGO_SAY = {"ENERGY_ADAPTIVE_BRIGHTNESS": "по громкости", "VU_METER": "индикатор уровня",
            "SPECTRUM_ANALYZER": "анализатор спектра", "PARTY": "вечеринка", "STROBE": "стробоскоп",
            "RANDOM_PIXEL_FLASH": "всполохи", "KNIGHT_RIDER_CLOCKWISE": "бегущий огонь по кругу",
            "KNIGHT_RIDER_ALTERNATING": "бегущий огонь навстречу"}
VIDEO_SAY = {"STANDARD": "стандартный", "NATURAL": "кино", "GAME": "игра"}


def _literal(tree: ast.Module, name: str):
    for n in tree.body:
        tgt = n.targets[0] if isinstance(n, ast.Assign) else (n.target if isinstance(n, ast.AnnAssign) else None)
        if isinstance(tgt, ast.Name) and tgt.id == name and n.value is not None:
            return ast.literal_eval(n.value), n.lineno
    raise ValueError(f"в коде нет {name}")


def _names(tree: ast.Module) -> dict[str, int]:
    """Имена команд из вызовов *Command(name=...) и NavCommand("<имя>", ...): константы, a if c else b, f"key_{...}"."""
    out: dict[str, int] = {}

    def add(v, line):
        if isinstance(v, ast.Constant) and isinstance(v.value, str):
            out.setdefault(v.value, line)
        elif isinstance(v, ast.IfExp):
            add(v.body, line)
            add(v.orelse, line)
        elif isinstance(v, ast.JoinedStr):
            lead = "".join(p.value for p in v.values if isinstance(p, ast.Constant))
            out.setdefault(lead + "*", line)
        elif isinstance(v, ast.Name):
            out.setdefault("$" + v.id, line)
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id.endswith("Command"):
            for kw in n.keywords:
                if kw.arg == "name":
                    add(kw.value, n.lineno)
            if n.func.id == "NavCommand" and n.args:
                add(n.args[0], n.lineno)
    return out


def cmd_catalog(root: Path) -> list[dict]:
    """Каталог {cmd}: id -> формулировка, источник файл:строка. Порядок детерминированный; ValueError при рассинхроне."""
    root = Path(root)
    src = {f: ast.parse((root / "src" / f).read_text(encoding="utf-8")) for f in ("tv_commands.py", "tv_nav.py", "menu_syntax.py")}
    found: dict[str, str] = {}
    for f, t in src.items():
        for nm, line in _names(t).items():
            found.setdefault(nm, f"src/{f}:{line}")
    ms = (root / "src" / "menu_syntax.py").read_text(encoding="utf-8")
    if '"menu_route"' in ms:
        line = _find_line(root, "src/menu_syntax.py", r'"menu_route"')
        found.setdefault("menu_route", f"src/menu_syntax.py:{line}")
    found.pop("$cmd_name", None)                     # menu_syntax: cmd_name = "screen_off" | "menu_route"
    rows = []
    for nm in sorted(found):
        where = found[nm]
        if nm in CMD_LOCAL:
            continue
        if nm == "key_*":
            kw, kl = _literal(src["tv_commands.py"], "KEY_WORDS")
            ks, _ = _literal(src["tv_commands.py"], "KEY_SAY")
            for key in sorted(set(kw.values())):
                if key not in ks:
                    raise ValueError(f"KEY_SAY без кнопки {key}")
                rows.append(("key_" + key, f"кнопка {ks[key].replace('/', ' ')}", where))
            continue
        if nm == "amb_colour":
            stems, _ = _literal(src["tv_commands.py"], "_HUE_STEMS")
            for stem, _soft, _hue in stems:
                if stem not in HUE_SAY:
                    raise ValueError(f"цвет без формулировки: {stem}")
                rows.append((f"amb_colour_{slug(HUE_SAY[stem])}", f"подсветка, {HUE_SAY[stem]} цвет", where))
            continue
        if nm == "amb_audio":
            al, _ = _literal(src["tv_commands.py"], "AUDIO_ALGOS")
            for a in sorted(set(al.values())):
                if a not in ALGO_SAY:
                    raise ValueError(f"режим подсветки под звук без формулировки: {a}")
                rows.append((f"amb_audio_{a.lower()}", f"подсветка под звук, {ALGO_SAY[a]}", where))
            continue
        if nm == "amb_video":
            vs, _ = _literal(src["tv_commands.py"], "VIDEO_SETTINGS")
            for s in sorted(set(vs.values())):
                if s not in VIDEO_SAY:
                    raise ValueError(f"режим подсветки за видео без формулировки: {s}")
                rows.append((f"amb_video_{s.lower()}", f"подсветка за видео, {VIDEO_SAY[s]}", where))
            continue
        if nm not in CMD_SAY:
            raise ValueError(f"команда из кода без формулировки голоса: {nm} ({where})")
        rows.append((nm, CMD_SAY[nm], where))
    rows.sort(key=lambda r: r[0])
    return [{"n": i, "id": cid, "text": txt, "source": w, "file": f"cmd_{i:03d}_{slug(cid)}"}
            for i, (cid, txt, w) in enumerate(rows, 1)]


# ---------------------------------------------------------------- manifest
def _template_parts(rec: dict, text: str) -> list[tuple[str, str]]:
    """Части шаблонной фразы (part, текст) или [] для статической."""
    rid = rec["id"]
    if "{" not in text:
        return []
    g = rec.get("glue") or {}
    if g.get("prefix"):
        return [("a", g["prefix"])]
    if rid == "status":
        return [("a", "Телевизор:"), ("b", "на")]
    if rid == "gen_understood":
        return [("a", "Понято:"), ("plain", "Понято.")]
    if rid == "gen_last_known":
        return [("a", "Последнее известное:")]
    if rid == "gen_loop":
        return [("a", g["a"]), ("b_few", g["b_few"]), ("b_many", g["b_many"])]
    if rid == "gen_series_stopped":
        return [("a", g["a"]), ("b", g["b"])]
    raise ValueError(f"шаблон без правила склейки: {rid}: {text}")


def build_manifest(v5: list[dict], summary: list[dict], catalog: list[dict]) -> dict:
    entries = []
    for i, r in enumerate(v5, 1):
        key = f"p{i:03d}_{slug(r['id'])}"
        for f in VOICED_FIELDS:
            for v, text in enumerate(r.get(f) or [], 1):
                parts = _template_parts(r, text)
                base = {"id": r["id"], "key": key, "field": f, "v": v}
                if not parts:
                    entries.append(dict(base, kind="phrase", part=None, text=speakable(text), source_text=text,
                                        file=f"{key}__{f}__{v}"))
                for p, pt in parts:
                    entries.append(dict(base, kind="part", part=p, text=speakable(pt), source_text=text,
                                        file=f"{key}__{f}__{v}__{p}", join_tail=p == "a"))
    for s in summary:
        entries.append({"id": "stat", "kind": "stat", "text": speakable(s["text"]), "file": s["file"],
                        "source": s["source"]})
    for c in catalog:
        entries.append({"id": c["id"], "kind": "cmd", "text": speakable(c["text"]), "file": c["file"],
                        "source": c["source"]})
    for n in range(101):
        entries.append({"id": "num", "kind": "num", "n": n, "text": f"{CARRIER} {number_words(n)}.",
                        "carrier": CARRIER, "file": f"num_{n:03d}"})
    files = [e["file"] for e in entries]
    if len(set(f.lower() for f in files)) != len(files):
        raise ValueError("повтор имени файла (без учёта регистра)")
    by = {}
    for e in entries:
        by[e["kind"]] = by.get(e["kind"], 0) + 1
    return {"version": 1, "voice": "Despina",
            "format": {"container": "WAV", "codec": "PCM 16 bit", "sample_rate": 24000, "channels": 1},
            "cut": {"silence_dbfs": -50.0, "head_ms": 40, "tail_ms": 120, "tail_join_ms": 40,
                    "batch_lufs": -18.0, "true_peak_dbtp": -1.5, "short_clip_s": 1.0, "short_clip_rms_dbfs": -20.0},
            "tuning_line": TUNING_LINE,
            "rules": {"loop_tail": "b_few: n % 10 in {2,3,4} и n % 100 not in {12,13,14}; иначе b_many",
                      "time": "часы num_<ЧЧ>; :00 -> num_000 + num_000; :01..:09 -> num_000 + num_00<М>; :10..:59 -> num_0<ММ>",
                      "prefix": "prefix (__a) + num_<n>; на прямой вопрос -- только num_<n>",
                      "status": "__a + stat_<summary> + __b + время",
                      "understood": "__a + cmd_<NNN>; неизвестная команда -- __plain"},
            "counts": dict(by, total=len(entries)), "entries": entries}


def estimate_s(text: str) -> float:
    return len(text) / CHARS_PER_S + PAUSE_S


def make_batches(manifest: dict) -> list[list[dict]]:
    """Пачки по порядку manifest: до 15 строк; от 10 строк пачка закрывается на оценке 30 с, при любом числе строк --
    на 35 с (длинные фразы: пачка короче 10 строк). Оценка: длина текста / 13 знаков в секунду + 0,7 с паузы.
    Номер пачки и позиция (1..) записываются в записи manifest (поля batch, pos)."""
    batches, cur, dur = [], [], estimate_s(TUNING_LINE)
    for e in manifest["entries"]:
        d = estimate_s(e["text"])
        if cur and (len(cur) >= BATCH_MAX or dur + d > BATCH_HARD_S or (len(cur) >= BATCH_MIN and dur + d > BATCH_MAX_S)):
            batches.append(cur)
            cur, dur = [], estimate_s(TUNING_LINE)
        cur.append(e)
        dur += d
    if cur:
        tail = sum(estimate_s(e["text"]) for e in cur)
        prev = sum(estimate_s(e["text"]) for e in batches[-1]) + estimate_s(TUNING_LINE) if batches else None
        if (batches and len(cur) < BATCH_MIN and len(batches[-1]) + len(cur) <= BATCH_MAX
                and prev + tail <= BATCH_HARD_S):
            batches[-1].extend(cur)                  # короткий остаток -- в предыдущую пачку, если влезает
        else:
            batches.append(cur)
    for bi, b in enumerate(batches, 1):
        for pi, e in enumerate(b, 1):
            e["batch"], e["pos"] = bi, pi
    manifest["batches"] = len(batches)
    return batches


def batch_text(batch: list[dict]) -> str:
    """Текст пачки: камертон, затем по фразе в строке через пустую строку (пауза для нарезки)."""
    return "\n\n".join([TUNING_LINE] + [e["text"] for e in batch]) + "\n"


HOWTO = """Голос бота: генерация пачек вручную (Google AI Studio, голос Despina)

1. Новая сессия AI Studio, режим речи, голос Despina. В поле стиля вставить текст файла 00_STYLE_PROMPT.txt.
2. Пачки по порядку: batch_01.txt, batch_02.txt, ... Текст пачки вставить целиком (первая строка «{tune}» --
   камертон, при нарезке отбрасывается). Все пачки -- в одной сессии вкладки.
3. Скачать результат в WAV и сохранить под именем пачки: batch_01.wav, batch_02.wav, ...
   Папка: {incoming}
4. Ничего не обрезать и не обрабатывать: нарезка, выравнивание громкости и проверка комплекта -- офлайн на ПК.
5. Если фраза прочитана не так (ударение, вопросительная интонация) -- пачку сгенерировать заново целиком
   и заменить файл; имя то же.
Всего пачек: {n}. Файлов в комплекте после нарезки: {files}.
"""
