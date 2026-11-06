"""CAPABILITIES (этапы A+B, 30.10–01.11.2026): каталог функций Ока.

Проблема (глобальный тест 30.10 14:29): на «какие функции доступны» модель
выдумывала «IP, открытые порты, команды через API» -- скрипты прикручены к боту,
мозг о них не знал.

Два потребителя:
- build_catalog() -> system prompt основного контура governor (set_capabilities:
  только _process и _self_correct; судья и рефлексия холодные);
- human_text() -> детерминированный ответ на «что умеешь» (router, этап B):
  без модели, по образцу /memory. Ответ модели о себе неполон (30.10 16:13: 5 из 8
  пунктов), а ложь, попавшая в историю, воспроизводится (30.10 16:10).

Два слоя:
- объявление (DECLARED) -- что есть в коде; drift-тесты: slash-команды обязаны быть
  зарегистрированы в bot.py, фразы ТВ разбираться tv_commands.parse(), фразы
  маршрутизатора (route_examples) -- уходить в объявленный модуль;
- события (этап C) -- не здесь: event_log.facts_block() идёт в контекст запроса, не в system prompt;
- подтверждение -- последняя запись proof_registry по ключу функции; пометка
  добавляется ТОЛЬКО если запись есть (нет записи -- нет утверждения).

Пометка -- с ДАТОЙ, без времени: каталог стоит в system prompt, и его изменение
сбрасывает кэш промпта Ollama. С датой каталог меняется не чаще раза в сутки на
функцию (или при смене статуса), а не после каждой команды.

build_catalog/human_text -- чистые функции: без диска, без сети, без config.
Не строится из tv_commands.help_text(): тот противоречит сам себе (громкость
описана и как работающая, и как недоступная «по оптике»), см. HANDOFF 30.10.
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from typing import Optional

HEADER = "[ФУНКЦИИ ОКА — полный фактический список; других нет]"

# D (минимум): модель в чате не исполняет действий с устройствами. Команды, которые узнал
# маршрутизатор, исполняются ДО модели -- до неё доходит только то, что он не узнал.
RULE = (
    "[ПРАВИЛО] Команды устройствам выполняет бот до тебя; если просьба дошла до тебя — она НЕ "
    "выполнена: не пиши «сделал/включил», назови фразу из списка. Состояния устройств ты не видишь. "
    "Не придумывай функций, IP, портов, API. [PASS/WARN/FAIL дата] — последняя проверка."
)


@dataclass(frozen=True)
class Capability:
    key: str                                  # ключ записи в proof_registry
    text: str                                 # строка каталога
    commands: tuple[str, ...] = ()            # slash-команды; сверяются с bot.py
    tv_examples: tuple[str, ...] = ()         # фразы режима /tv; сверяются с tv_commands.parse
    route_examples: tuple[str, ...] = ()      # фразы без /tv; сверяются с router.route -> key
    checkable: bool = False                   # пометка проверки имеет смысл (устройство/внешний сервис)
    brief: str = ""                           # CAPS_BRIEF (02.11): строка для system prompt; пусто -> text


DECLARED: tuple[Capability, ...] = (
    Capability(
        key="tv",
        brief='ТВ Philips: «подсветка музыка», цвет, яркость, громкость, «включи телевизор», «выключи телевизор»; кнопки — /tv. Приложения нельзя.',
        checkable=True,
        text=("Телевизор Philips, текстом или голосом: «подсветка музыка», «подсветка красная», "
              "«яркость минус 3», «громкость плюс 5», «включи телевизор», «выключи телевизор» (с подтверждением). "
              "Включение и выключение проверяю по кадру камеры. Кнопки и «статус» — в режиме /tv. "
              "Открыть приложения нельзя."),
        commands=("tv",),
        tv_examples=("назад", "громкость плюс 5", "подсветка музыка",
                     "подсветка красная", "яркость минус 3", "статус", "включи телевизор", "выключи телевизор"),
        route_examples=("подсветка музыка", "подсветка красная", "яркость минус 3",
                        "громкость плюс 5", "включи телевизор", "выключи телевизор"),
    ),
    Capability(
        key="garmin",
        brief='Garmin: сон, шаги, /garmin; сводки 08:00 и 21:00.',
        checkable=True,
        text=("Часы Garmin: «как я спал», «сколько шагов», /garmin — сводка сейчас; "
              "сами приходят в 08:00 и 21:00."),
        commands=("garmin",),
        route_examples=("как я спал", "сколько шагов"),
    ),
    Capability(
        key="devices",
        brief='Стиральная и сушильная Samsung: уведомления и состояние; управлять нельзя.',
        checkable=True,
        text=("Стиральная и сушильная Samsung: уведомления приходят сами; «когда закончит сушилка», "
              "«что со стиральной» — состояние сейчас. Управлять техникой не могу."),
        route_examples=("когда закончит сушилка", "что со стиральной"),
    ),
    Capability(
        key="net",
        brief='Сеть: сводка проверки при смене сети ПК.',
        checkable=True,
        text="Сеть: при смене сети ПК — сводка проверки ТВ, SmartThings, Garmin, принтера.",
    ),
    Capability(
        key="docs",
        brief='Документы PDF/DOCX/XLSX: печать, в базу знаний, удалить.',
        checkable=True,
        text=("Документы PDF/DOCX/XLSX: присланный файл — кнопки печать / в базу знаний / "
              "удалить; подпись «печать» — печать сразу. .doc/.xls не читаю."),
    ),
    Capability(
        key="discuss",
        brief='/discuss, /enddiscuss — обсуждение документа.',
        text="/discuss <файл> — обсуждение документа из базы знаний, /enddiscuss — выход.",
        commands=("discuss", "enddiscuss"),
    ),
    Capability(
        key="memory",
        brief='/memory, /forget, /newepoch, /status.',
        text="/memory — что я храню, /forget — удалить память, /newepoch — история заново, /status — состояние.",
        commands=("memory", "forget", "newepoch", "status"),
    ),
    Capability(
        key="self",
        brief='«что умеешь» — полный список без участия модели.',
        text="«что умеешь» — этот список без участия модели. Голос распознаётся локально.",
        route_examples=("что умеешь", "что ты умеешь", "какие функции тебе доступны"),
    ),
)

MAX_AGE_S = 7 * 24 * 3600


def _parse_ts(ts: str) -> Optional[_dt.datetime]:
    try:
        t = _dt.datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None
    return t if t.tzinfo else t.astimezone()


def status_suffix(
    rec: Optional[dict],
    *,
    net: Optional[str] = None,
    now: Optional[_dt.datetime] = None,
    max_age_s: float = MAX_AGE_S,
) -> str:
    """Короткая пометка проверки: [PASS 30.10] / [давно: PASS 20.10] / [в этой сети не проверено].
    Пусто, если записи нет."""
    if not rec:
        return ""
    t = _parse_ts(rec.get("ts", ""))
    if t is None:
        return ""
    now = now or _dt.datetime.now().astimezone()
    when = t.strftime("%d.%m")
    if net and rec.get("net") and rec["net"] != net:
        return " [в этой сети не проверено]"
    if (now - t).total_seconds() > max_age_s:
        return f" [давно: {rec.get('status')} {when}]"
    return f" [{rec.get('status')} {when}]"


def _lines(index: Optional[dict], net, now, max_age_s, brief: bool = False) -> list[str]:
    index = index or {}
    out = []
    for cap in DECLARED:
        rec = index.get(cap.key) if cap.checkable else None
        body = (cap.brief or cap.text) if brief else cap.text
        out.append(body + status_suffix(rec, net=net, now=now, max_age_s=max_age_s))
    return out


def build_catalog(
    index: Optional[dict] = None,
    *,
    net: Optional[str] = None,
    now: Optional[_dt.datetime] = None,
    max_age_s: float = MAX_AGE_S,
) -> str:
    """Каталог для governor.set_capabilities(). index -- ProofRegistry.latest()."""
    # RULE сразу после заголовка: при обрезке по CAPABILITIES_MAX_CHARS теряется
    # хвост списка, а не запрет ложного «сделал».
    # CAPS_BRIEF (02.11): в system prompt -- короткие строки; полный текст -- в human_text («что умеешь»).
    return "\n".join([HEADER, RULE] + ["- " + l for l in _lines(index, net, now, max_age_s, brief=True)])


def human_text(
    index: Optional[dict] = None,
    *,
    net: Optional[str] = None,
    now: Optional[_dt.datetime] = None,
    max_age_s: float = MAX_AGE_S,
) -> str:
    """Ответ человеку на «что умеешь» -- тот же список, без служебного правила."""
    body = "\n".join("• " + l for l in _lines(index, net, now, max_age_s))
    return ("Что я умею (полный список):\n" + body +
            "\nПометка [PASS/WARN/FAIL дата] — последняя проверка; без пометки — пока не проверялось.")
