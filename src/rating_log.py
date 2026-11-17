"""#так / #нетак -- оценка Оператором последнего ответа бота (решение 12.11: порядок «происхождение строк ТВ» -> чистка -> #так/#нетак).

- Перехват ДО модели, истории (memory.db) и памяти связок (g6.db): сообщение-оценка не попадает ни туда, ни туда.
- Метка в начале сообщения: #так / #нетак / #не так, латиницей #tak / #netak / #ne tak, в чужой раскладке
  #nfr / #ytnfr («так»/«нетак», набранные на английской раскладке). «#такси» -- не метка.
- Текст после метки -- комментарий Оператора, сохраняется (до COMMENT_MAX символов).
- Привязка -- к последнему ответу бота: источник (brain / route) и номер события памяти (event_id ответа модели,
  если он записан в историю; у ответов модулей номера нет -- None).
- Порядок записей -- номер n (монотонный счётчик журнала), не время (RULES_ps_blocks п.10).
- Журнал: data/ratings.jsonl (D1: это оценки СИСТЕМЫ, полей об Операторе нет). Выгрузка для внешнего разбора -- только отсюда.
"""
from __future__ import annotations

import datetime as _dt
import json
import re
import threading
from pathlib import Path
from typing import Optional

OK, BAD = "ok", "bad"
COMMENT_MAX = 1000
T_SAVED = "Записано в журнал оценок."
T_NO_ANSWER = "Нет ответа для оценки."

_TAG_RE = re.compile(
    r"^\s*#\s*(?P<tag>не\s*так|нетак|так|ne\s*tak|netak|tak|ytnfr|nfr)(?![\wа-яё])[\s:,.\-—–]*(?P<rest>.*)\Z",
    re.IGNORECASE | re.DOTALL,
)
_BAD_TAGS = {"нетак", "netak", "ytnfr"}


def parse(text: str) -> Optional[tuple[str, str]]:
    """(verdict, comment) или None, если сообщение -- не оценка."""
    m = _TAG_RE.match(text or "")
    if not m:
        return None
    tag = re.sub(r"\s+", "", m.group("tag").lower())
    verdict = BAD if tag in _BAD_TAGS else OK
    comment = " ".join(m.group("rest").split())[:COMMENT_MAX]
    return verdict, comment


class RatingLog:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._n = self._last_n()

    def _last_n(self) -> int:
        if not self.path.exists():
            return 0
        n = 0
        with open(self.path, encoding="utf-8") as f:
            for line in f:
                try:
                    n = max(n, int(json.loads(line).get("n", 0)))
                except (ValueError, TypeError, AttributeError):
                    continue                      # битая строка не ломает счётчик
        return n

    def record(self, verdict: str, comment: str, src: str, event_id: Optional[int],
               ts: Optional[str] = None) -> dict:
        if verdict not in (OK, BAD):
            raise ValueError("verdict must be ok|bad")
        with self._lock:
            self._n += 1
            rec = {"n": self._n,
                   "ts": ts or _dt.datetime.now().astimezone().isoformat(timespec="seconds"),
                   "verdict": verdict, "comment": comment[:COMMENT_MAX],
                   "target": {"src": src, "event_id": event_id}}
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                f.flush()
            return rec

    def all(self) -> list[dict]:
        if not self.path.exists():
            return []
        out = []
        with open(self.path, encoding="utf-8") as f:
            for line in f:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
        return sorted(out, key=lambda r: r.get("n", 0))
