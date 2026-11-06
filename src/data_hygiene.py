"""Слой B: гигиена данных. Детерминированно, без LLM, без оценки тем.

Контракт:
- Срабатывает только на технические артефакты (ключи, токены, PII-реквизиты).
- Никогда не блокирует сообщение: маскирует и сообщает, что замаскировано.
- Сырой секрет не покидает функцию: в Incident только категория и отпечаток.
- Совместим по форме с RealityPrivacyDispatcher.dispatch() -> ScanResult.
"""
from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Tuple


@dataclass(frozen=True)
class Incident:
    category: str
    level: str          # всегда "WARNING": слой B не блокирует
    fingerprint: str    # sha256[:8] совпадения — для дедупликации, не для восстановления


@dataclass
class ScanResult:
    is_allowed: bool
    sanitized_text: str
    incidents: List[Incident] = field(default_factory=list)


def _luhn_ok(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if alt:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
        alt = not alt
    return total % 10 == 0


def _iban_ok(raw: str) -> bool:
    s = raw.replace(" ", "").upper()
    if not 15 <= len(s) <= 34:
        return False
    moved = s[4:] + s[:4]
    num = "".join(str(int(c, 36)) for c in moved)
    return int(num) % 97 == 1


def _entropy(s: str) -> float:
    n = len(s)
    return -sum(c / n * math.log2(c / n) for c in Counter(s).values())


def _high_entropy_ok(tok: str) -> bool:
    # Порог 4.2 бит/символ отсекает hex-хэши и UUID (максимум 4.0) — коммиты и
    # контрольные суммы не маскируются. Требуем смешение классов символов.
    classes = sum(bool(re.search(p, tok)) for p in (r"[a-z]", r"[A-Z]", r"\d"))
    return classes >= 3 and _entropy(tok) > 4.2


# (категория, regex, номер группы для маскирования, валидатор или None)
Rule = Tuple[str, re.Pattern, int, Optional[Callable[[str], bool]]]

RULES: List[Rule] = [
    ("PRIVATE_KEY", re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S), 0, None),
    ("API_KEY", re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9_\-]{20,}"), 0, None),
    ("API_KEY", re.compile(r"\bAKIA[0-9A-Z]{16}\b"), 0, None),
    ("API_KEY", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"), 0, None),
    ("API_KEY", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"), 0, None),
    ("BOT_TOKEN", re.compile(r"\b\d{8,10}:[A-Za-z0-9_\-]{35}\b"), 0, None),
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_\-]+\.eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+"), 0, None),
    # Маскируется только значение, ключ ("password=") остаётся — смысл фразы сохраняется.
    ("CREDENTIAL", re.compile(
        r"(?i)\b(?:password|passwd|pwd|secret|token|api[_-]?key|пароль)\s*[:=]\s*[\"']?([^\s\"']{4,})"),
     1, None),
    ("EMAIL", re.compile(r"\b[\w.+\-]+@[\w\-]+\.[\w.\-]+\b"), 0, None),
    ("CARD", re.compile(r"\b(?:\d[ \-]?){13,19}\b"), 0,
     lambda m: _luhn_ok(re.sub(r"\D", "", m))),
    ("IBAN", re.compile(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]){11,30}\b"), 0, _iban_ok),
    ("HIGH_ENTROPY", re.compile(r"[A-Za-z0-9+/_\-]{32,}={0,2}"), 0, _high_entropy_ok),
]


def _fp(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:8]


class DataHygieneFilter:
    """Порядок RULES = приоритет: специфичные форматы раньше HIGH_ENTROPY,
    перекрывающиеся совпадения маскируются один раз."""

    def __init__(self, rules: List[Rule] = RULES) -> None:
        self.rules = rules

    def dispatch(self, payload: str) -> ScanResult:
        spans: List[Tuple[int, int, str]] = []
        incidents: List[Incident] = []
        for category, rx, grp, validator in self.rules:
            for m in rx.finditer(payload):
                start, end = m.span(grp)
                value = m.group(grp)
                if validator and not validator(value):
                    continue
                if any(s < end and start < e for s, e, _ in spans):
                    continue
                spans.append((start, end, category))
                incidents.append(Incident(category, "WARNING", _fp(value)))

        out = payload
        for start, end, category in sorted(spans, reverse=True):
            out = out[:start] + f"[{category}_REDACTED]" + out[end:]
        return ScanResult(is_allowed=True, sanitized_text=out, incidents=incidents)


def build_notice(incidents: List[Incident]) -> Optional[str]:
    """Нейтральное уведомление: что замаскировано. Без оценок и предупреждений о рисках."""
    if not incidents:
        return None
    counts = Counter(i.category for i in incidents)
    parts = ", ".join(f"{c}×{n}" if n > 1 else c for c, n in counts.items())
    return f"[Замаскировано до сохранения: {parts}]"
