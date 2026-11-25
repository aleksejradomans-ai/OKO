"""B33 H3: боевой режим Σ без модуля шлюза = стоп (fail-closed), и сторож шаблона «except ImportError -> X = None».

H3.1 / H3.3: OKO_SIGMA_LIVE=1 и модуль sigma_gate подменён на None -> handle / _execute отвечают SIGMA_HALT_TEXT,
_execute_core не вызывается, на ТВ 0 отправок. Теневой режим без модуля -- прежний путь (его закрепляет
test_b08d_P4_without_sigma_module_old_path, здесь не трогается).
H3.2: на старте бота при LIVE=1 и без модуля -- одна строка ERROR со служебным кодом SIGMA_MODULE_MISSING.
H3.4 (QD): в модулях красной зоны (реализации инвариантов с зоной «красная» в docs/INVARIANTS.md, колонка
implementation docs/control_map.csv) обработчик ImportError / ModuleNotFoundError, подставляющий запасное значение
(None / False / заглушку), разрешён только из реестра IMPORT_FALLBACKS: либо fail_closed (в модуле есть явная
ветка `if <имя> is None` с return / raise), либо allowed с причиной. Новое место без записи -- красный.
"""
from __future__ import annotations

import ast
import csv
import logging
import re
from pathlib import Path

import pytest

import governor
import tv_bot_handler as H
from test_b08d import ADMIN, WireTV

SRC = Path(__file__).resolve().parent
ROOT = SRC.parent


@pytest.fixture(autouse=True)
def _iso(monkeypatch, tmp_path):
    monkeypatch.setattr(H, "SHADOW_DEFAULT_PATH", tmp_path / "default" / "sigma_shadow.jsonl")
    monkeypatch.setenv(H.SHADOW_PATH_ENV, str(tmp_path / "j" / "sigma_shadow.jsonl"))
    monkeypatch.setenv(H.SHADOW_FLAG_ENV, "1")
    monkeypatch.delenv(H.SIGMA_LIVE_ENV, raising=False)
    monkeypatch.setenv("TV_READBACK_DELAY_S", "0")
    monkeypatch.setenv("TV_SETTING_SETTLE_S", "0")
    monkeypatch.setattr(H.tv_intent, "understand", lambda *a, **k: None)
    monkeypatch.setattr(H, "VERIFY", {})
    monkeypatch.setattr(H, "PRE", {})
    monkeypatch.setattr(H, "_stop_music", lambda name: "")
    yield


def _play(script, tv):
    store = H.PendingStore()
    return [H.handle(ADMIN, t, lambda: tv, pending=store, admin_ids=[ADMIN], now=float(i)) for i, t in enumerate(script)]


# ------------------------------------------------------------------ H3.1 / H3.3
def test_b33_live_flag_name_is_the_gate_flag():
    import sigma_gate
    assert H.SIGMA_LIVE_ENV == sigma_gate.LIVE_ENV


@pytest.mark.parametrize("shadow", ["1", "0"])
def test_b33_liar_live1_without_sigma_module_halts_and_sends_nothing(monkeypatch, shadow):
    """Тест-лжец (аудит H3): LIVE=1, модуль подменён на None -> _execute_core не вызван, 0 отправок, ответ HALT."""
    monkeypatch.setenv(H.SIGMA_LIVE_ENV, "1")
    monkeypatch.setenv(H.SHADOW_FLAG_ENV, shadow)
    monkeypatch.setattr(H, "sigma_gate", None)
    core = []
    real = H._execute_core
    monkeypatch.setattr(H, "_execute_core", lambda *a, **k: core.append(a) or real(*a, **k))
    tv = WireTV()
    ans = _play(["подсветка ярче", "подсветка яркость 7", "подсветка тусклее"], tv)
    assert core == []
    assert tv.wire == []
    assert all(a == H.SIGMA_HALT_TEXT.format(why=H.SIGMA_MISSING_WHY) for a in ans), ans


@pytest.mark.parametrize("flag", [" 1 ", "1"])
def test_b33_live_flag_same_rule_as_gate(monkeypatch, flag):
    monkeypatch.setenv(H.SIGMA_LIVE_ENV, flag)
    monkeypatch.setattr(H, "sigma_gate", None)
    assert H._live_flag() is True and H.sigma_module_missing_live() is True


@pytest.mark.parametrize("flag", [None, "", "0", "true"])
def test_b33_shadow_without_module_is_not_halted(monkeypatch, flag):
    """Без боевого флага отсутствие модуля -- не стоп (прежний путь), проверка старта молчит."""
    if flag is None:
        monkeypatch.delenv(H.SIGMA_LIVE_ENV, raising=False)
    else:
        monkeypatch.setenv(H.SIGMA_LIVE_ENV, flag)
    monkeypatch.setattr(H, "sigma_gate", None)
    assert H.sigma_module_missing_live() is False
    tv = WireTV()
    ans = _play(["подсветка ярче"], tv)
    assert not ans[0].startswith("⏸ Σ") and tv.wire != []


def test_b33_module_present_live_check_is_false(monkeypatch):
    monkeypatch.setenv(H.SIGMA_LIVE_ENV, "1")
    assert H.sigma_gate is not None
    assert H.sigma_module_missing_live() is False


# ------------------------------------------------------------------ H3.2
class _Cap(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append(record)


def test_b33_startup_code_is_red_and_logged_once():
    assert "SIGMA_MODULE_MISSING" in governor.RED_CODES and "SIGMA_MODULE_MISSING" in governor.SERVICE_CODES
    cap = _Cap()
    lg = logging.getLogger("kernel.governor")
    old = lg.level
    lg.addHandler(cap)
    lg.setLevel(logging.DEBUG)
    try:
        m = governor.report_sigma_module_missing()
    finally:
        lg.removeHandler(cap)
        lg.setLevel(old)
    assert m == "[ERR: SIGMA_MODULE_MISSING]"
    hits = [r for r in cap.records if "[ERR: SIGMA_MODULE_MISSING]" in r.getMessage()]
    assert len(hits) == 1 and hits[0].levelno == logging.ERROR


def test_b33_bot_post_init_calls_the_startup_check():
    """post_init: при sigma_module_missing_live() -- report_sigma_module_missing() (порядок строк, по AST)."""
    tree = ast.parse((SRC / "bot.py").read_text(encoding="utf-8-sig"))
    fn = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "post_init")
    ifs = [n for n in ast.walk(fn) if isinstance(n, ast.If) and "sigma_module_missing_live" in ast.unparse(n.test)]
    assert len(ifs) == 1
    assert "report_sigma_module_missing()" in ast.unparse(ifs[0])


# ------------------------------------------------------------------ H3.4 (QD): сторож AST
# (модуль, имя): решение. fail_closed -- в модуле обязана быть явная ветка `if ... <имя> is None ...` с return / raise;
# allowed -- запасное значение допустимо, причина обязательна (без молчаливых исключений).
IMPORT_FALLBACKS = {
    ("src/tv_bot_handler.py", "sigma_gate"): (
        "fail_closed",
        "шлюз Σ (INV-05 / INV-10): без модуля при OKO_SIGMA_LIVE=1 -- HALT в _execute (B33 H3.1), на старте -- "
        "SIGMA_MODULE_MISSING; теневой режим -- прежний путь"),
    ("src/bot.py", "RAG_RETRIEVAL_AVAILABLE"): (
        "allowed",
        "поиск по базе знаний oko_rag (INV-18, рабочая зона) -- не предохранитель: без него ответ идёт без "
        "контекста базы, WARNING в лог; судья, Σ и Host-Only от него не зависят"),
}
_FALLBACK_VALUES = (ast.Constant,)


def red_zone_modules(root: Path = ROOT) -> list:
    """Файлы реализации инвариантов красной зоны: зона из таблицы INVARIANTS.md, файлы -- из control_map.csv."""
    zrx = re.compile(r"^\|\s*(INV-\d{2})\s*\|\s*(красная|рабочая)\s*\|")
    zones = {}
    for ln in (root / "docs" / "INVARIANTS.md").read_text(encoding="utf-8-sig").splitlines():
        m = zrx.match(ln.strip())
        if m:
            zones.setdefault(m.group(1), set()).add(m.group(2))
    mods = set()
    with (root / "docs" / "control_map.csv").open(encoding="utf-8-sig", newline="") as fh:
        for r in csv.DictReader(fh):
            if zones.get(r["id"]) == {"красная"}:
                mods |= {x.split("::", 1)[0].strip() for x in r["implementation"].split(";") if x.strip()}
    return sorted(mods)


def import_fallbacks(source: str) -> list:
    """[(lineno, имя)] -- обработчики ImportError / ModuleNotFoundError, присваивающие имени константу или заглушку."""
    out = []
    for n in ast.walk(ast.parse(source)):
        if not isinstance(n, ast.Try):
            continue
        for h in n.handlers:
            ty = ast.unparse(h.type) if h.type is not None else "BaseException"
            if "ImportError" not in ty and "ModuleNotFoundError" not in ty and ty not in ("Exception", "BaseException"):
                continue
            imports = any(isinstance(b, (ast.Import, ast.ImportFrom)) for b in ast.walk(ast.Module(body=n.body, type_ignores=[])))
            if not imports:
                continue
            for b in h.body:
                if isinstance(b, ast.Assign):
                    for tg in b.targets:
                        if isinstance(tg, ast.Name):
                            out.append((b.lineno, tg.id))
    return out


def has_fail_closed_branch(source: str, name: str) -> bool:
    """Явная ветка: `if` с условием `<имя> is None` (в составе условия), в теле которого есть return или raise."""
    for n in ast.walk(ast.parse(source)):
        if isinstance(n, ast.If):
            cond = [c for c in ast.walk(n.test) if isinstance(c, ast.Compare) and isinstance(c.left, ast.Name)
                    and c.left.id == name and any(isinstance(o, ast.Is) for o in c.ops)
                    and any(isinstance(x, ast.Constant) and x.value is None for x in c.comparators)]
            if cond and any(isinstance(x, (ast.Return, ast.Raise)) for x in ast.walk(ast.Module(body=n.body, type_ignores=[]))):
                return True
    return False


def fallback_problems(root: Path = ROOT, registry: dict = IMPORT_FALLBACKS) -> list:
    p = []
    seen = set()
    for m in red_zone_modules(root):
        f = root / m
        if not f.is_file():
            p.append(f"{m}: файла нет")
            continue
        src = f.read_text(encoding="utf-8-sig")
        for line, name in import_fallbacks(src):
            seen.add((m, name))
            rec = registry.get((m, name))
            if rec is None:
                p.append(f"{m}:{line} except ImportError -> {name} = запасное значение без записи в IMPORT_FALLBACKS")
                continue
            kind, why = rec
            if kind not in ("fail_closed", "allowed") or len(why.strip()) < 20:
                p.append(f"{m}:{line} {name}: решение «{kind}» без причины")
            elif kind == "fail_closed" and not has_fail_closed_branch(src, name):
                p.append(f"{m}:{line} {name}: fail_closed, но нет ветки `if {name} is None` с return / raise")
    for k in sorted(set(registry) - seen):
        p.append(f"{k[0]} {k[1]}: запись реестра без места в коде (устарела)")
    return p


def test_b33_red_zone_modules_include_the_sigma_handler_and_bot():
    mods = red_zone_modules()
    assert "src/tv_bot_handler.py" in mods and "src/bot.py" in mods and len(mods) >= 10


def test_b33_import_fallbacks_in_red_zone_are_registered_and_fail_closed():
    assert fallback_problems() == []


def test_b33_tv_bot_handler_sigma_fallback_has_fail_closed_branch():
    src = (SRC / "tv_bot_handler.py").read_text(encoding="utf-8")
    assert ("sigma_gate" in [n for _, n in import_fallbacks(src)]) and has_fail_closed_branch(src, "sigma_gate")


@pytest.mark.parametrize("body,expect", [
    ("try:\n    import zz\nexcept ImportError:\n    zz = None\n", ["new"]),
    ("try:\n    import zz\nexcept (ImportError, OSError):\n    zz = None\n", ["new"]),
    ("try:\n    from a import zz\nexcept ModuleNotFoundError:\n    zz = False\n", ["new"]),
    ("try:\n    import zz\nexcept ImportError:\n    raise\n", []),
    ("x = 1\n", []),
])
def test_b33_liar_new_import_fallback_in_red_zone_is_caught(tmp_path, body, expect):
    """Тест-лжец: новое место `except ImportError: X = None` в модуле красной зоны без записи -> находка."""
    (tmp_path / "docs").mkdir()
    (tmp_path / "src").mkdir()
    (tmp_path / "docs" / "INVARIANTS.md").write_text("| INV-01 | красная | x |\n| INV-02 | рабочая | y |\n", encoding="utf-8")
    (tmp_path / "docs" / "control_map.csv").write_text(
        "id,implementation,tests,evidence,scope,known_gap\nINV-01,src/m.py::f,,manual,s,g\nINV-02,src/w.py::f,,manual,s,g\n",
        encoding="utf-8")
    (tmp_path / "src" / "m.py").write_text(body, encoding="utf-8")
    (tmp_path / "src" / "w.py").write_text("try:\n    import q\nexcept ImportError:\n    q = None\n", encoding="utf-8")
    p = fallback_problems(tmp_path, {})
    assert (len(p) == 1 and "src/m.py" in p[0]) if expect else p == []


def test_b33_liar_fail_closed_record_without_branch_is_caught(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "src").mkdir()
    (tmp_path / "docs" / "INVARIANTS.md").write_text("| INV-01 | красная | x |\n", encoding="utf-8")
    (tmp_path / "docs" / "control_map.csv").write_text(
        "id,implementation,tests,evidence,scope,known_gap\nINV-01,src/m.py::f,,manual,s,g\n", encoding="utf-8")
    base = "try:\n    import zz\nexcept ImportError:\n    zz = None\n"
    (tmp_path / "src" / "m.py").write_text(base + "def f():\n    return zz.go() if zz is not None else 0\n", encoding="utf-8")
    reg = {("src/m.py", "zz"): ("fail_closed", "предохранитель: без модуля -- стоп, проверено тестом")}
    assert fallback_problems(tmp_path, reg) != []                      # нет ветки if zz is None: return / raise
    (tmp_path / "src" / "m.py").write_text(base + "def f():\n    if zz is None:\n        return 'HALT'\n    return zz.go()\n",
                                           encoding="utf-8")
    assert fallback_problems(tmp_path, reg) == []
    assert fallback_problems(tmp_path, {**reg, ("src/m.py", "gone"): ("allowed", "устаревшая запись реестра без места")}) != []


# ------------------------------------------------------------------ B33 v2 C7 (claim_check п.5): видно в /status
def _status_report(monkeypatch):
    import asyncio
    import bot
    from test_bot import _make_status_context, _make_status_governor, _make_status_update
    monkeypatch.setattr(bot.config, "ADMIN_USER_IDS", frozenset({111}), raising=False)
    u = _make_status_update(user_id=111)
    asyncio.run(bot.handle_status(u, _make_status_context(_make_status_governor())))
    return u.message.reply_text.await_args.args[0]


def test_b33v2_liar_status_shows_sigma_missing_live(monkeypatch):
    """LIVE=1 и модуль None -> первая строка /status «Σ: модуль не загружен, боевой режим = HALT»."""
    monkeypatch.setenv(H.SIGMA_LIVE_ENV, "1")
    monkeypatch.setattr(H, "sigma_gate", None)
    rep = _status_report(monkeypatch)
    assert rep.startswith("Σ: модуль не загружен, боевой режим = HALT\n") and "KERNEL STATUS" in rep


@pytest.mark.parametrize("live,missing", [("1", False), ("0", True), (None, True)])
def test_b33v2_status_no_sigma_line_otherwise(monkeypatch, live, missing):
    if live is None:
        monkeypatch.delenv(H.SIGMA_LIVE_ENV, raising=False)
    else:
        monkeypatch.setenv(H.SIGMA_LIVE_ENV, live)
    if missing:
        monkeypatch.setattr(H, "sigma_gate", None)
    rep = _status_report(monkeypatch)
    assert "Σ: модуль не загружен" not in rep and rep.startswith("KERNEL STATUS")
