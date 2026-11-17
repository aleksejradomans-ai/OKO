"""Чистка истории модели от ответов «Не выполнялось. Используй /tv …» (решение 2026-11-12 21:47 п.2, «да» Оператора).

Модель копировала эти ответы сама за собой (полигон FUZZ 12.11: 25 из 57 ответов модели -- «используй /tv»).
Удаляются ТОЛЬКО пары из PAIRS (9 пар, одобрены Оператором), и только если каждая пара цела:
  - user -> assistant, тот же subject_id, |ts| <= 1 с; ответ assistant содержит «/tv» (навязывание режима).
Логика проверки, аудита и удаления -- общая с tools/purge_b01_history.py (PURGE16), отличается только список и признак.
По умолчанию -- dry run; --apply -- одной транзакцией, аудит JSONL до удаления.
    python tools\\purge_tvpush_history.py [--db PATH] [--backup PATH] [--audit PATH] [--apply]
"""
from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("purge_b01_history", ROOT / "tools" / "purge_b01_history.py")
base = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(base)

PAIRS = [(479, 480), (487, 488), (489, 490), (491, 492), (493, 494), (495, 496), (497, 498), (499, 500), (501, 502)]
_TV_PUSH = re.compile(r"(?<![\w/])/tv\b")


def tv_push(answer: str):
    m = _TV_PUSH.search(answer or "")
    return m.group(0) if m else None


def main(argv=None) -> int:
    base.PAIRS = PAIRS
    orig = base.check
    base.check = lambda c, pairs=PAIRS, fake=None: orig(c, pairs, fake or tv_push)
    return base.main(argv)


if __name__ == "__main__":
    sys.exit(main())
