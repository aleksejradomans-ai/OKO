"""response_metrics.py -- RESPONSE_METRICS (M6, 19.10): пассивные метрики ответа.

ТОЛЬКО наблюдение: governor пишет одну INFO-строку "[METRICS] ..." на ответ.
Ни одно решение (судья, CircuitBreaker, SYSTEM_DEGRADED, алерты) от этих
чисел не зависит, пока они не откалиброваны. Текст ответа в лог не попадает.

Чистая функция: без I/O, без LLM, без состояния. Лексика RU+EN по словарям
ниже -- грубая эвристика, значения сравнимы только между собой во времени.

Метрики (0..1, два знака):
  ce  cognitive_empathy     -- доля предложений, обращённых к позиции
                               пользователя (вы/ваш/ты/you/your ...).
  ad  affective_decoupling  -- 1 - 10 * (эмоциональные слова / все слова),
                               не ниже 0; 1.00 = нейтральный текст (POL_001).
  rl  resistance_level      -- (отказы + 0.5 * оговорки) / предложения, не выше 1.
  intent                    -- преобладающий тип предложений:
                               refuse > ask > reassure > instruct > inform
                               (порядок -- приоритет при равенстве), доля.
  cyr/lat/cjk               -- доли кириллицы / латиницы / CJK среди букв
                               (наблюдение дрейфа языка, 14.10).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

_I = re.IGNORECASE

_WORD = re.compile(r"\w+", re.UNICODE)
_SENT_SPLIT = re.compile(r"(?<=[.!?…])\s+")

_USER_REF = re.compile(
    r"\b(вы|вас|вам|вами|ваш\w*|ты|тебя|тебе|тобой|твой|твоя|твоё|твое|твои\w*|твоего|твоей|"
    r"you|your|yours|yourself)\b", _I)

_EMOTION = re.compile(
    r"\b(рад|рада|рады|радост\w*|сожале\w*|сочувств\w*|пережива\w*|беспоко\w*|волну\w*|"
    r"грус\w*|печал\w*|счастлив\w*|обидн\w*|жаль|люблю|восхищ\w*|приятно|огорч\w*|"
    r"чувства|чувствую|чувствуете|"
    r"sorry|glad|happy|sad|feel|feels|feeling|feelings|love|excited|unfortunately|worried|"
    r"afraid|delighted|pleased)\b", _I)

_REFUSE = re.compile(
    r"\b(не могу|не буду|не стану|невозможно|нельзя|отказыва\w*|не в состоянии|"
    r"не имею (?:возможности|права|доступа)|cannot|can't|can not|won't|unable|"
    r"not able|refuse|not allowed)\b", _I)

_HEDGE = re.compile(r"\b(однако|тем не менее|впрочем|however|nevertheless)\b", _I)

_REASSURE = re.compile(
    r"\b(не волнуйтесь|не волнуйся|не переживайте|не переживай|не беспокойтесь|"
    r"не беспокойся|всё будет хорошо|все будет хорошо|всё в порядке|все в порядке|"
    r"don't worry|do not worry|it's okay|it will be fine)\b", _I)

_LIST_ITEM = re.compile(r"^\s*(\d+[.)]|[-*•])\s+")
_IMPERATIVE = re.compile(
    r"^\s*(выполни\w*|запусти\w*|открой\w*|проверь\w*|установи\w*|скопируй\w*|добавь\w*|"
    r"удали\w*|нажми\w*|используй\w*|создай\w*|замени\w*|перезапусти\w*|положи\w*|"
    r"run|open|check|install|copy|add|remove|delete|click|use|create|replace|restart|put)\b", _I)

_CYR = re.compile(r"[Ѐ-ӿ]")
_LAT = re.compile(r"[A-Za-z]")
_CJK = re.compile(r"[぀-ヿ㐀-䶿一-鿿豈-﫿가-힯]")

INTENT_PRIORITY = ("refuse", "ask", "reassure", "instruct", "inform")


@dataclass(frozen=True)
class ResponseMetrics:
    cognitive_empathy: float
    affective_decoupling: float
    resistance_level: float
    intent: str
    intent_share: float
    cyr: float
    lat: float
    cjk: float
    length: int

    def to_log(self) -> str:
        return (
            f"ce={self.cognitive_empathy:.2f} ad={self.affective_decoupling:.2f} "
            f"rl={self.resistance_level:.2f} intent={self.intent}({self.intent_share:.2f}) "
            f"cyr={self.cyr:.2f} lat={self.lat:.2f} cjk={self.cjk:.2f} len={self.length}"
        )


def _sentences(text: str) -> list[tuple[str, bool]]:
    """(предложение, первое_в_элементе_списка). Маркер списка ("1.", "-")
    снимается ДО деления строки на предложения: иначе "1." отделялся бы
    точкой в самостоятельное "предложение"."""
    out = []
    for line in text.splitlines():
        m = _LIST_ITEM.match(line)
        body = line[m.end():] if m else line
        parts = [p.strip() for p in _SENT_SPLIT.split(body)]
        parts = [p for p in parts if _WORD.search(p)]
        out.extend((p, bool(m) and i == 0) for i, p in enumerate(parts))
    return out


def _sentence_intent(s: str, list_item: bool) -> str:
    if _REFUSE.search(s):
        return "refuse"
    if s.rstrip().endswith("?"):
        return "ask"
    if _REASSURE.search(s):
        return "reassure"
    if list_item or _IMPERATIVE.match(s):
        return "instruct"
    return "inform"


def _r(x: float) -> float:
    return round(min(1.0, max(0.0, x)), 2)


def measure(text: str) -> ResponseMetrics:
    text = text or ""
    sents = _sentences(text)
    words = _WORD.findall(text)
    n_s, n_w = len(sents), len(words)

    if n_s == 0:
        return ResponseMetrics(0.0, 1.0, 0.0, "none", 0.0, 0.0, 0.0, 0.0, len(text))

    ce = sum(1 for s, _ in sents if _USER_REF.search(s)) / n_s
    emo = len(_EMOTION.findall(text))
    ad = 1.0 - 10.0 * emo / max(n_w, 1)
    rl = (len(_REFUSE.findall(text)) + 0.5 * len(_HEDGE.findall(text))) / n_s

    counts = {k: 0 for k in INTENT_PRIORITY}
    for s, li in sents:
        counts[_sentence_intent(s, li)] += 1
    intent = max(INTENT_PRIORITY, key=lambda k: (counts[k], -INTENT_PRIORITY.index(k)))

    cyr, lat, cjk = (len(rx.findall(text)) for rx in (_CYR, _LAT, _CJK))
    letters = cyr + lat + cjk
    share = (lambda n: n / letters) if letters else (lambda n: 0.0)

    return ResponseMetrics(
        cognitive_empathy=_r(ce),
        affective_decoupling=_r(ad),
        resistance_level=_r(rl),
        intent=intent,
        intent_share=_r(counts[intent] / n_s),
        cyr=_r(share(cyr)), lat=_r(share(lat)), cjk=_r(share(cjk)),
        length=len(text),
    )
