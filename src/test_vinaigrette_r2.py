"""VINAIGRETTE_V1 (10.11, Claude «Око»): регрессии, найденные при переносе 756261c на master.

1. translit_candidates падал IndexError на слове, оканчивающемся диграфом (ja, zh, sh, ch, ts, kh, yo, ju, yu, shch):
   срез word[i:i+4] короче 4 символов совпадал с двухбуквенным ключом -> walk(i+4) за концом слова.
   «podsvetka zelenaja» ронял парсер целиком.
"""
from __future__ import annotations

import pytest

from oko import HomeConfig, Settings
from oko.parser import VectorParser
from oko.parser.normalization import translit_candidates

ENDS = ["ja", "aja", "zelenaja", "zh", "sh", "ch", "ts", "kh", "shch", "yo", "ju", "yu",
        "tvoja", "moja", "vkljuch", "nozh", "kovsh"]


@pytest.mark.parametrize("word", ENDS)
def test_word_final_digraph_no_crash(word):
    cands = translit_candidates(word)
    assert cands and all(isinstance(c, str) and c for c in cands)


def test_word_final_digraph_readings():
    assert "зеленая" in translit_candidates("zelenaja")
    assert "нож" in translit_candidates("nozh")


def test_parser_survives_live_phrase():
    p = VectorParser(HomeConfig(), Settings(), lambda o, t: [])
    d = p.parse("podsvetka zelenaja", "op", None).describe()
    assert (d["target"], d["action"]) == ("tv_amb", "color")
