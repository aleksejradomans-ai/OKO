"""B33 v2 C1 (B32r H1 -- основание FAIL; B32d O-M6; B32e E-L2): одна точка записи в ТВ под шлюзом Σ.

Тест-лжец по сценарию B32r repro/r2_cam_calib_bypass.py: OKO_SIGMA_LIVE=1 -> /cam calib auto, мост amb_music (старт
и сам процесс моста), audio_bridge, повтор пробуждения bot._wol_again, CLI tv_control / tools-пробы делают 0 записей
в ТВ и отвечают «боевой Σ: действие мимо шлюза запрещено». LIVE != 1 -> поведение как было (записи идут).
Сторож по AST: клиент ТВ (TVClient.from_env() / TVClient(...)) создаётся только в tv_bot_handler.make_factory (путь
шлюза), в sigma_live.tv_client (точка отказа) и в диагностике чтения tools/oko_selfcheck.py::c_tv (только get-запросы)."""
from __future__ import annotations

import ast
import sys
import types
from pathlib import Path

import pytest

import sigma_live

SRC = Path(__file__).resolve().parent
ROOT = SRC.parent
TEXT = "боевой Σ: действие мимо шлюза запрещено"


class CountTV:
    """Клиент ТВ, который считает ЛЮБОЙ вызов метода (запись или чтение)."""

    def __init__(self, log):
        self.log = log

    def __getattr__(self, name):
        def call(*a, **k):
            self.log.append(name)
            return {}
        return call


@pytest.fixture
def made(monkeypatch):
    """TVClient.from_env подменён: каждый созданный клиент и каждый его вызов -- в список."""
    import tv_control
    log, created = [], []

    def from_env(*a, **k):
        created.append(1)
        return CountTV(log)
    monkeypatch.setattr(tv_control.TVClient, "from_env", staticmethod(from_env))
    return types.SimpleNamespace(log=log, created=created)


def _live(monkeypatch, on: bool):
    if on:
        monkeypatch.setenv(sigma_live.LIVE_ENV, "1")
    else:
        monkeypatch.delenv(sigma_live.LIVE_ENV, raising=False)


def test_b33v2_flag_is_the_gate_flag(monkeypatch):
    import sigma_gate
    import tv_bot_handler as H
    assert sigma_live.LIVE_ENV == sigma_gate.LIVE_ENV == H.SIGMA_LIVE_ENV
    for v, exp in ((None, False), ("0", False), ("true", False), (" 1 ", True), ("1", True)):
        if v is None:
            monkeypatch.delenv(sigma_live.LIVE_ENV, raising=False)
        else:
            monkeypatch.setenv(sigma_live.LIVE_ENV, v)
        assert sigma_live.live() is exp is sigma_gate.live_enabled() is H._live_flag()


# ------------------------------------------------------------------ 1. /cam calib auto
def test_b33v2_liar_calib_auto_live_zero_keys(monkeypatch, made):
    import bot
    import camera_watcher as cw
    _live(monkeypatch, True)
    text, path = bot._calib_auto()
    assert TEXT in text and path is None and made.created == [] and made.log == []
    keys = []
    tv = types.SimpleNamespace(send_key=lambda k: keys.append(k))
    text2, path2 = cw.calibrate_auto(tv)                    # тот же путь, клиент передан снаружи
    assert TEXT in text2 and path2 is None and keys == []


def test_b33v2_calib_auto_without_live_presses_as_before(monkeypatch, tmp_path):
    import camera_watcher as cw
    _live(monkeypatch, False)
    monkeypatch.setenv("OKO_CAM_HOST", "192.0.2.2")
    monkeypatch.setenv("OKO_CAM_USER", "u")
    monkeypatch.setenv("OKO_CAM_DIR", str(tmp_path))
    keys = []
    tv = types.SimpleNamespace(send_key=lambda k: keys.append(k))

    def grab(url):
        raise RuntimeError("no camera")
    text, path = cw.calibrate_auto(tv, grab=grab, sleep=lambda s: None, step_s=0.0)
    assert keys[:len(cw.SCREEN_OFF_KEYS)] == list(cw.SCREEN_OFF_KEYS) and TEXT not in text


# ------------------------------------------------------------------ 2. повтор пробуждения
def test_b33v2_liar_wol_again_live_no_wake(monkeypatch, made):
    import bot
    _live(monkeypatch, True)
    bot._wol_again()
    assert made.created == [] and made.log == []
    _live(monkeypatch, False)
    bot._wol_again()
    assert made.created == [1] and made.log == ["wake"]


# ------------------------------------------------------------------ 3. мост подсветки amb_music
def test_b33v2_liar_amb_start_live_no_bridge(monkeypatch, tmp_path):
    import amb_music as M
    spawned = []
    c, s = tmp_path / "c.json", tmp_path / "s.json"
    _live(monkeypatch, True)
    out = M.start({}, control=c, status=s, spawn=lambda: spawned.append(1), fallback=lambda: spawned.append(2),
                  sleep=lambda _: None)
    assert TEXT in out and spawned == [] and not c.exists()
    _live(monkeypatch, False)
    out = M.start({}, control=c, status=s, spawn=lambda: spawned.append(1), timeout=0.01, sleep=lambda _: None)
    assert spawned == [1] and TEXT not in out


def test_b33v2_liar_amb_bridge_process_live_no_client(monkeypatch, made, tmp_path):
    """Процесс моста (amb_music.py run): флаг читается и там -- клиента ТВ нет, статус с причиной, код 3."""
    import amb_music as M
    monkeypatch.setattr(M, "_load_env", lambda: None)
    monkeypatch.setattr(M, "ENV_FILE", tmp_path / "no.env")
    monkeypatch.setattr(M, "_paths", lambda: (tmp_path / "c.json", tmp_path / "s.json"))
    _live(monkeypatch, True)
    assert M.run() == 3
    assert made.created == [] and made.log == []
    assert TEXT in M.read_json(tmp_path / "s.json").get("reason", "")


# ------------------------------------------------------------------ 4. audio_bridge
@pytest.mark.parametrize("fn", ["run_soundcard", "run_bridge"])
def test_b33v2_liar_audio_bridge_live_no_writes(monkeypatch, made, fn):
    import audio_bridge as ab
    monkeypatch.setitem(sys.modules, "soundcard", types.ModuleType("soundcard"))
    monkeypatch.setitem(sys.modules, "sounddevice", types.ModuleType("sounddevice"))
    _live(monkeypatch, True)
    f = getattr(ab, fn)
    rc = f(None, 0.1, 0, False)
    assert rc != 0 and made.created == [] and made.log == []


# ------------------------------------------------------------------ 5. CLI tv_control и пробы tools/
def test_b33v2_liar_tv_control_cli_live_no_keys(monkeypatch, made, capsys):
    import tv_control
    _live(monkeypatch, True)
    monkeypatch.setattr(sys, "argv", ["tv_control.py", "Home"])
    assert tv_control.main() == 3 and made.created == [] and TEXT in capsys.readouterr().out


def test_b33v2_tv_client_refuses_before_factory(monkeypatch):
    calls = []
    _live(monkeypatch, True)
    with pytest.raises(sigma_live.BypassRefused) as e:
        sigma_live.tv_client("x", lambda: calls.append(1))
    assert calls == [] and TEXT in e.value.text
    _live(monkeypatch, False)
    g = sigma_live.tv_client("x", lambda: "client")
    assert isinstance(g, sigma_live.Guarded) and object.__getattribute__(g, "_client") == "client"


# ------------------------------------------------------------------ сторож одной точки (AST)
ALLOWED = {("src/tv_bot_handler.py", "factory"): "путь шлюза Σ (make_factory -> _execute)",
           ("src/sigma_live.py", "tv_client"): "точка отказа мимо шлюза",
           ("tools/oko_selfcheck.py", "c_tv"): "диагностика: только чтение (context, settings/current)"}


def client_sites(root: Path = ROOT) -> list:
    """[(файл, функция, строка)] вызовов TVClient.from_env(...) / TVClient(...) вне тестов и вне tv_control.TVClient."""
    out = []
    files = [p for d in ("src", "tools") for p in sorted((root / d).rglob("*.py"))
             if not p.name.startswith("test_") and "archive" not in p.parts]
    for f in files:
        rel = f.relative_to(root).as_posix()
        tree = ast.parse(f.read_text(encoding="utf-8-sig"))
        stack = []

        def visit(n):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                stack.append(n.name)
                for c in ast.iter_child_nodes(n):
                    visit(c)
                stack.pop()
                return
            if isinstance(n, ast.Call):
                fn = n.func
                s = ast.unparse(fn)
                if s.endswith("TVClient.from_env") or s in ("TVClient", "tv_control.TVClient", "t.TVClient"):
                    out.append((rel, stack[-1] if stack else "<module>", n.lineno, ".".join(stack)))
            for c in ast.iter_child_nodes(n):
                visit(c)
        visit(tree)
    return [x for x in out if not (x[0] == "src/tv_control.py" and x[3].startswith("TVClient"))]


def test_b33v2_one_write_point_tv_clients_only_where_allowed():
    bad = [x for x in client_sites() if (x[0], x[1]) not in ALLOWED]
    assert bad == [], f"клиент ТВ создаётся мимо шлюза и мимо sigma_live.tv_client: {bad}"


def test_b33v2_liar_new_client_site_is_caught(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "tools").mkdir()
    (tmp_path / "src" / "new_mod.py").write_text("from tv_control import TVClient\n\ndef go():\n    TVClient.from_env().send_key('Home')\n",
                                                 encoding="utf-8")
    assert [(x[0], x[1]) for x in client_sites(tmp_path)] == [("src/new_mod.py", "go")]



# ================================================================== B33 v2 попытка 2
# ------------------------------------------------------------------ ревизор G5: флаг перед КАЖДОЙ записью
class _Inner:
    def __init__(self):
        self.calls = []

    def set_lounge_color(self, *a):
        self.calls.append("set_lounge_color")

    host = "192.0.2.41"


def test_b33v2_g5_liar_guarded_flag_on_during_work_next_write_refused(monkeypatch):
    _live(monkeypatch, False)
    inner = _Inner()
    tv = sigma_live.tv_client("amb_bridge", lambda: inner)
    tv.set_lounge_color(1, 2, 3)
    assert inner.calls == ["set_lounge_color"] and tv.host == "192.0.2.41"
    _live(monkeypatch, True)                                 # боевой Σ включили во время работы
    with pytest.raises(sigma_live.BypassRefused):
        tv.set_lounge_color(1, 2, 3)
    assert inner.calls == ["set_lounge_color"]               # следующая запись не ушла


def test_b33v2_g5_liar_env_file_flag_on_during_work(monkeypatch, tmp_path):
    """Флаг включили в .env (окружение процесса моста не меняется) -- следующая запись не уходит."""
    _live(monkeypatch, False)
    env = tmp_path / ".env"
    env.write_text("TV_HOST=192.0.2.41\nOKO_SIGMA_LIVE=0\n", encoding="utf-8")
    inner = _Inner()
    tv = sigma_live.tv_client("audio_bridge", lambda: inner, env_file=env)
    tv.set_lounge_color(1, 2, 3)
    env.write_text("TV_HOST=192.0.2.41\nOKO_SIGMA_LIVE = \"1\"  # боевой\n", encoding="utf-8")
    with pytest.raises(sigma_live.BypassRefused):
        tv.set_lounge_color(1, 2, 3)
    assert inner.calls == ["set_lounge_color"]
    with pytest.raises(sigma_live.BypassRefused):            # уже включён в файле -- клиент не создаётся
        sigma_live.tv_client("audio_bridge", lambda: inner, env_file=env)


@pytest.mark.parametrize("text, want", [("", False), ("OKO_SIGMA_LIVE=1\n", True), ("export OKO_SIGMA_LIVE=1\n", True),
                                        ("# OKO_SIGMA_LIVE=1\n", False), ("OKO_SIGMA_LIVE=1\nOKO_SIGMA_LIVE=0\n", False),
                                        ("OKO_SIGMA_LIVE_X=1\n", False), ("OKO_SIGMA_LIVE='1'\n", True)])
def test_b33v2_g5_file_flag_parse(tmp_path, text, want):
    f = tmp_path / ".env"
    f.write_text(text, encoding="utf-8")
    assert sigma_live.file_flag(f) is want
    assert sigma_live.file_flag(tmp_path / "absent.env") is False


def test_b33v2_g5_liar_amb_bridge_stops_when_env_flag_turns_on(monkeypatch, tmp_path):
    """Мост работает (LIVE выкл.), на 3-й записи кадра в .env включают боевой Σ: 4-й записи нет, мост встаёт с
    причиной «боевой Σ», возврат прежнего стиля в ТВ тоже не пишется."""
    import test_amb_music as TA
    _live(monkeypatch, False)
    env = tmp_path / "bridge.env"

    class TV(TA._BridgeTV):
        def __init__(self):
            super().__init__()
            self.after = []

        def _call(self, method, path, body):
            if env.exists():
                self.after.append(path)
            self.posts += 1
            if self.posts == 3:
                env.write_text("OKO_SIGMA_LIVE=1\n", encoding="utf-8")

        def set_ambilight_style(self, style, setting=None):
            if env.exists():
                self.after.append("style")
            super().set_ambilight_style(style, setting)

    tv = TV()
    _, st, _ = TA._run_bridge(monkeypatch, tmp_path, sid=0, tv=tv, env_file=env, self_stop=True,
                              until=lambda st, pk, tv: st.get("running") is False)
    assert tv.posts == 3 and tv.after == []
    assert TEXT in st.get("reason", "") and st.get("running") is False


def test_b33v2_g5_liar_audio_bridge_sender_stops_on_flag(monkeypatch, tmp_path):
    import threading
    import audio_bridge as ab
    _live(monkeypatch, False)
    env = tmp_path / ".env"
    inner = _Inner()
    orig = inner.set_lounge_color

    def set_lounge_color(*a):
        orig(*a)
        if len(inner.calls) == 2:
            env.write_text("OKO_SIGMA_LIVE=1\n", encoding="utf-8")
    inner.set_lounge_color = set_lounge_color
    tv = sigma_live.tv_client("audio_bridge", lambda: inner, env_file=env)
    sent, latest, lock, stop = {"n": 0, "fail": 0}, {"level": 0.0}, threading.Lock(), threading.Event()
    monkeypatch.setattr(ab, "MIN_INTERVAL_S", 0.0)
    th = threading.Thread(target=ab._make_sender(tv, 0, sent, latest, lock, stop), daemon=True)
    th.start()
    for _ in range(200):                                     # потолок ~10 с без чтения часов (сторож живых часов)
        if stop.is_set():
            break
        with lock:
            latest["level"] = 0.9 if latest["level"] < 0.5 else 0.1
        stop.wait(0.05)
    stop.set()
    th.join(2)
    assert len(inner.calls) == 2 and sent.get("refused") == 1 and sent["n"] == 2


# ------------------------------------------------------------------ ревизор G4: сырые HTTP / WoL к ТВ
RAW_MARKS = ("1926", "1925", "/6/", "input/key", "ambilight", "screenstate", "/powerstate", "magic")
RAW_CALLS = {"post", "put", "patch", "delete", "request", "sendto", "urlopen"}
RAW_ALLOWED = {"src/tv_control.py": "транспорт шлюза (TVClient, send_wol)",
               "src/tv_bot_handler.py": "шлюз Σ (store.put -- не ТВ)",
               "src/tv_probe.py": "CLI-проба", "src/tv_snapshot.py": "CLI-снимок", "src/tv_pair.py": "CLI-пайринг",
               "tools/tv_wol_probe.py": "CLI-проба WoL"}
# UDP без признака пробуждения (SNMP принтера, разведка пылесоса) -- не ТВ и не находка
RAW_CLI = ("src/tv_probe.py", "src/tv_snapshot.py", "src/tv_pair.py", "tools/tv_wol_probe.py")


def raw_writers(root: Path = ROOT) -> dict:
    """{файл: [вызовы]} модулей с вызовом post/put/request/sendto/urlopen при признаке ТВ в том же модуле."""
    out = {}
    for d in ("src", "tools"):
        for f in sorted((root / d).rglob("*.py")):
            if f.name.startswith("test_") or "archive" in f.parts:
                continue
            try:
                tree = ast.parse(f.read_text(encoding="utf-8-sig"))
            except SyntaxError:
                continue
            strs = [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)]
            names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | \
                    {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
            calls = sorted({ast.unparse(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)
                            and isinstance(n.func, ast.Attribute) and n.func.attr in RAW_CALLS})
            ids = names | {n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
            wol = any("wol" in i.lower() for i in ids) or any(                # b"\xff" * 6 -- заголовок пакета WoL
                isinstance(n, ast.BinOp) and isinstance(n.op, ast.Mult) and ast.unparse(n).replace(" ", "") == "b'\\xff'*6"
                for n in ast.walk(tree))
            udp = "SOCK_DGRAM" in names and any(c.endswith(".sendto") for c in calls) and wol   # пакет пробуждения
            tv = any(m in s for s in strs for m in RAW_MARKS) or "HTTPDigestAuth" in names or udp
            if tv and calls:
                out[f.relative_to(root).as_posix()] = calls
    return out


def test_b33v2_g4_raw_tv_writers_only_known_modules():
    bad = sorted(set(raw_writers()) - set(RAW_ALLOWED))
    assert bad == [], f"сырая запись в ТВ мимо шлюза и мимо sigma_live: {bad}"
    assert set(RAW_CLI) <= set(raw_writers())


def test_b33v2_g4_liar_new_raw_writer_is_caught(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "x_probe.py").write_text(
        "import requests\n\ndef go(h):\n    requests.post(f'https://{h}:1926/6/input/key', json={'key': 'Home'})\n",
        encoding="utf-8")
    (tmp_path / "tools" / "y_wol.py").write_text(
        "import socket\n\ndef send_wol(p):\n    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)\n    s.sendto(p, ('255.255.255.255', 9))\n",
        encoding="utf-8")
    assert sorted(raw_writers(tmp_path)) == ["tools/x_probe.py", "tools/y_wol.py"]


@pytest.mark.parametrize("rel", RAW_CLI)
def test_b33v2_g4_raw_cli_refuses_first_and_is_not_imported(rel):
    tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
    main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main")
    first = [ast.unparse(s) for s in main.body[:3]]
    assert first[0] == "import sigma_live" and first[1].startswith("no = sigma_live.refusal(") and \
        first[2].startswith("if no is not None:"), first
    mod = Path(rel).stem
    users = []
    for d in ("src", "tools"):
        for f in sorted((ROOT / d).rglob("*.py")):
            if f.name.startswith("test_") or f.as_posix().endswith(rel):
                continue
            tx = f.read_text(encoding="utf-8-sig")
            for n in ast.walk(ast.parse(tx)):
                if isinstance(n, ast.Import) and any(a.name == mod for a in n.names) or \
                        isinstance(n, ast.ImportFrom) and n.module == mod:
                    users.append(f.name)
    assert users == [], f"{rel} импортируется рабочим кодом: {users}"


class _NoNet:
    def __init__(self):
        self.n = 0

    def __call__(self, *a, **k):
        self.n += 1
        raise AssertionError("сеть в боевом Σ")


@pytest.mark.parametrize("mod, argv", [("tv_probe", ["tv_probe.py", "--key", "Home"]),
                                        ("tv_snapshot", ["tv_snapshot.py", "--volume-swing"]),
                                        ("tv_pair", ["tv_pair.py", "192.0.2.41"])])
def test_b33v2_g4_liar_raw_cli_live_no_network(monkeypatch, capsys, mod, argv):
    import importlib
    import requests
    import socket
    net = _NoNet()
    monkeypatch.setattr(requests.Session, "request", net)
    monkeypatch.setattr(socket, "socket", net)
    m = importlib.import_module(mod)
    _live(monkeypatch, True)
    monkeypatch.setattr(sys, "argv", argv)
    assert m.main() == 3 and net.n == 0 and TEXT in capsys.readouterr().out


FAKE_MAC = ":".join(["02"] + ["00"] * 5)                     # не настоящий MAC (собран, чтобы скан среза не путал)


def test_b33v2_g4_liar_wol_probe_live_no_packet(monkeypatch, capsys):
    import importlib.util
    import socket
    net = _NoNet()
    spec = importlib.util.spec_from_file_location("tv_wol_probe_b33", ROOT / "tools" / "tv_wol_probe.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    monkeypatch.setattr(socket, "socket", net)
    _live(monkeypatch, True)
    monkeypatch.setattr(sys, "argv", ["tv_wol_probe.py", FAKE_MAC, "192.0.2.41"])
    assert m.main() == 3 and net.n == 0 and TEXT in capsys.readouterr().out
    with pytest.raises(sigma_live.BypassRefused):
        m.send(FAKE_MAC, ["192.0.2.255"])
    assert net.n == 0
