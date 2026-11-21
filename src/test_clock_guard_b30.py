"""Сторож живых часов в тестах (src/clock_guard.py): синтетические исходники + проверка всего дерева проекта
по списку исключений .github/live_clock_allow.txt (вызов живых часов без заморозки и без строки в списке -- красный)."""
from pathlib import Path

import pytest

import clock_guard as cg

ROOT = Path(__file__).resolve().parent.parent
ALLOW = ROOT / ".github" / "live_clock_allow.txt"


@pytest.mark.parametrize("src,key", [
    ("import time\n\ndef test_a():\n    t = time.time()\n", "test_a"),
    ("import time as t\n\ndef test_a():\n    x = t.monotonic()\n", "test_a"),
    ("import datetime as dt\n\ndef test_a():\n    x = dt.datetime.now()\n", "test_a"),
    ("from datetime import datetime\n\ndef test_a():\n    x = datetime.now()\n", "test_a"),
    ("from datetime import date\n\ndef test_a():\n    x = date.today()\n", "test_a"),
    ("from time import time as now\n\ndef test_a():\n    x = now()\n", "test_a"),
    ("from time import perf_counter\n\ndef test_a():\n    x = perf_counter()\n", "test_a"),
    ("import time\n\nT = time.time()\n", "<module>"),
    ("import time\n\nclass TestK:\n    def test_a(self):\n        time.time()\n", "TestK.test_a"),
    ("def helper():\n    import time\n    return time.time()\n", "helper"),
])
def test_live_clock_calls_found(src, key):
    assert cg.scan_source(src) == {key: 1}


@pytest.mark.parametrize("src", [
    "import time\n\ndef test_a(monkeypatch):\n    monkeypatch.setattr(time, 'time', lambda: 5.0)\n    assert time.time() == 5.0\n",
    "import time\n\ndef test_a(monkeypatch):\n    monkeypatch.setattr('time.time', lambda: 5.0)\n    time.time()\n",
    "import datetime as dt\nfrom unittest import mock\n\ndef test_a():\n    with mock.patch('mod.dt.datetime') as m:\n        dt.datetime.now()\n",
    "import time\n\ndef test_a(frozen_clock):\n    time.time()\n",
    "import datetime as dt\n\ndef test_a():\n    x = dt.datetime(2026, 11, 21, 12, 0)\n    assert x.year == 2026\n",
    "import time\n\ndef test_a():\n    time.sleep(0)\n    x = 'time.time()'\n",
])
def test_frozen_or_not_a_clock_read(src):
    assert cg.scan_source(src) == {}


def test_allow_list_counts_and_stale(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "test_x.py").write_text("import time\n\ndef test_a():\n    time.time()\n    time.time()\n",
                                                encoding="utf-8")
    found = cg.scan_tree(tmp_path)
    assert found == {"src/test_x.py::test_a": 2}
    allow, errs = cg.parse_allow(["src/test_x.py::test_a | 2 | relative offset | 2026-11-21"])
    assert errs == [] and cg.judge(found, allow, tmp_path)["ok"] is True
    allow, _ = cg.parse_allow(["src/test_x.py::test_a | 1 | relative offset | 2026-11-21"])
    j = cg.judge(found, allow, tmp_path)
    assert j["ok"] is False and j["red"] == [("src/test_x.py::test_a", 2, 1)]       # вызовов стало больше
    allow, _ = cg.parse_allow(["src/test_x.py::test_a | 2 | r | 2026-11-21", "src/test_x.py::test_b | 1 | r | 2026-11-21"])
    j = cg.judge(found, allow, tmp_path)
    assert j["ok"] is False and j["stale"] == ["src/test_x.py::test_b"]               # строка без нарушения
    allow, _ = cg.parse_allow(["src/test_x.py::test_a | 2 | r | 2026-11-21", "src/test_private.py::test_c | 1 | r | 2026-11-21"])
    assert cg.judge(found, allow, tmp_path)["ok"] is True                               # файла нет в дереве -- не учитывается
    assert cg.judge(found, {}, tmp_path)["red"] == [("src/test_x.py::test_a", 2, 0)]   # без строки -- красный


@pytest.mark.parametrize("bad", ["src/test_x.py::test_a | 0 | r | 2026-11-21", "src/test_x.py::test_a | 1 | | 2026-11-21",
                                 "src/test_x.py::test_a | 1 | r", "src/test_x.py::test_a | 1 | r | 21.11.2026",
                                 "no key | 1 | r | 2026-11-21"])
def test_allow_list_bad_lines(bad):
    allow, errs = cg.parse_allow([bad])
    assert allow == {} and len(errs) == 1


def test_skipped_dirs_and_names():
    assert cg.is_test_path("src/test_a.py") and cg.is_test_path("src/a_test.py") and cg.is_test_path("test_root.py")
    assert not cg.is_test_path("tools/archive/test_old.py") and not cg.is_test_path("src/clock_guard.py")


def test_project_tree_has_no_unlisted_live_clock_calls():
    """CI-сторож: каждый вызов живых часов в тестах проекта -- заморожен или в списке исключений с причиной."""
    allow, errs = cg.parse_allow(ALLOW.read_text(encoding="utf-8").splitlines())
    assert errs == []
    j = cg.judge(cg.scan_tree(ROOT), allow, ROOT)
    assert j["red"] == [], f"live clock in tests without freezing / exception: {j['red']}"
    assert j["stale"] == [], f"stale exceptions (remove the line): {j['stale']}"
