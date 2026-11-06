"""samsung_names_ru.py — русские названия программ стиральной и сушильной Samsung для бота Ока (28.10.2026).

Источник каждого кода — Оператор у дисплея (st_name_walk.py, 28.10 11:37–12:28), не догадка:
облако SmartThings подписей программ не отдаёт (st_labels_probe.py: presentation en/ru — 0 подписей).
Бот ПОКАЗЫВАЕТ только `ru`, коды наружу не выходят. ПОНИМАЕТ — `ru` и все `aliases`
(что Оператор ввёл у дисплея, английская надпись машины, разговорные формы) через layout_fix.phonetic_key.

Сверка с картой опций (st_course_map.json) — признак, что коды и названия не перепутаны:
стиральная 3A «Очистка барабана» — всё зафиксировано, 70°; 27/28 «Полоскание+отжим»/«Слив и отжим» — без температуры;
сушильная 27/25/24 «Сушка по времени»/«Тёплый воздух»/«Холодный воздух» — единственные с выбором времени.
"""
from __future__ import annotations

WASHER: dict[str, dict] = {
    "1C": {"ru": "Эко-стирка 40–60",       "aliases": ["eco stirka 40-60", "эко стирка", "eco 40-60"]},
    "1B": {"ru": "Хлопок",                 "aliases": ["hlopok", "cotton"]},
    "1E": {"ru": "Быстрая 15 мин",         "aliases": ["bistraja 15min", "быстрая", "quick 15"]},
    "96": {"ru": "Микрофибра",             "aliases": ["mikrofibra", "microfibre"]},
    "1F": {"ru": "Холодная стирка",        "aliases": ["holodnaja stirka", "cold wash"]},
    "25": {"ru": "Синтетика",              "aliases": ["sintetika", "synthetics"]},
    "26": {"ru": "Деликатная",             "aliases": ["delikatnaja", "delicates"]},
    "33": {"ru": "Полотенца",              "aliases": ["polotence", "towels"]},
    "24": {"ru": "Постельное бельё",       "aliases": ["posteljnoe", "bedding"]},
    "32": {"ru": "Шторы",                  "aliases": ["worti", "shtori", "curtains"]},
    "20": {"ru": "Гигиеническая",          "aliases": ["gigieniceskaja", "hygiene"]},
    "22": {"ru": "Шерсть",                 "aliases": ["werstj", "wool"]},
    "23": {"ru": "Ветровки",               "aliases": ["vetrovki", "outdoor"]},
    "2F": {"ru": "Спортивная одежда",      "aliases": ["sportivka", "спортивка", "sportswear"]},
    "21": {"ru": "Цветное",                "aliases": ["cvetnie", "цветные", "colours"]},
    "2A": {"ru": "Джинсы",                 "aliases": ["denim", "джинса"]},
    "2E": {"ru": "Детская",                "aliases": ["detskaja", "baby care"]},
    "2D": {"ru": "Тихая стирка",           "aliases": ["silent wash"]},
    "34": {"ru": "Микс",                   "aliases": ["mix", "смешанное"]},
    "30": {"ru": "Пасмурный день",         "aliases": ["cloudy day"]},
    "27": {"ru": "Полоскание + отжим",     "aliases": ["poloskanie+otzim", "полоскание", "rinse spin"]},
    "28": {"ru": "Слив и отжим",           "aliases": ["drain spin", "отжим", "слив"]},
    "3A": {"ru": "Очистка барабана",       "aliases": ["drum clean", "чистка барабана"]},
}

DRYER: dict[str, dict] = {
    "51": {"ru": "Эко-хлопок",             "aliases": ["eco cotton", "эко хлопок"]},
    "23": {"ru": "Быстрая сушка",          "aliases": ["quck dry", "quick dry", "быстрая"]},
    "1F": {"ru": "Смешанное бельё",        "aliases": ["mixed load", "микс"]},
    "18": {"ru": "Синтетика",              "aliases": ["synhetics", "synthetics", "sintetika"]},
    "19": {"ru": "Деликатное",             "aliases": ["delikates", "delicates", "деликатная"]},
    "1D": {"ru": "Полотенца",              "aliases": ["towels", "polotence"]},
    "1B": {"ru": "Постельное бельё",       "aliases": ["bedding", "posteljnoe"]},
    "1C": {"ru": "Рубашки",                "aliases": ["shirts", "rubawki"]},
    "21": {"ru": "Гигиеническая обработка", "aliases": ["higiene care", "hygiene care", "гигиеническая"]},
    "1A": {"ru": "Шерсть",                 "aliases": ["wool", "werstj"]},
    "1E": {"ru": "Верхняя одежда",         "aliases": ["outdoor", "ветровки"]},
    "20": {"ru": "Под глажку",             "aliases": ["iron dry", "глажка"]},
    "27": {"ru": "Сушка по времени",       "aliases": ["time dry", "по времени"]},
    "25": {"ru": "Тёплый воздух",          "aliases": ["warm air"]},
    "24": {"ru": "Холодный воздух",        "aliases": ["cool air"]},
    "26": {"ru": "Освежение воздухом",     "aliases": ["air wash", "освежение"]},
}

BY_KIND = {"washer": WASHER, "dryer": DRYER}


def name_ru(kind: str, cycle: str) -> str:
    """Код или полное значение (`Table_02_Course_1C`) -> русское название. Неизвестный код -> честная пометка,
    а не сам код: Оператору коды не показываются."""
    code = str(cycle or "").rsplit("_", 1)[-1].upper()
    entry = BY_KIND.get(kind, {}).get(code)
    return entry["ru"] if entry else "неизвестная программа (нет в словаре)"


def phrases(kind: str) -> list[tuple[str, str]]:
    """[(фраза, код)] — всё, что бот должен понимать для этой машины. Список, а не словарь:
    словарь молча перезаписал бы одинаковую фразу у двух программ, и тест слияний её бы не увидел
    (так и было в первой версии — контрольная подмешанная фраза прошла незамеченной)."""
    return [(p, code) for code, e in BY_KIND[kind].items() for p in [e["ru"], *e["aliases"]]]
