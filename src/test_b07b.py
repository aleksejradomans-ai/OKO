"""(15.11, фаза 2b Σ): теневой журнал команд ТВ (tv_bot_handler, OKO_SIGMA_SHADOW=1).

(1) с журналом и без -- те же вызовы API, порядок и ответы (заглушка ТВ); (2) схема строки -- только белый список,
без текста ввода и id; (3) сбой записи не роняет команду; (4) ротация; (5) флаг выключен -> файла нет;
(6) режим сбора OKO_COLLECTOR=1 не перенаправляет путь журнала во временную папку (решение Оператора 15.11).
Сеть не затрагивается: tv_intent.understand заглушен, клиент ТВ -- двойник.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import tv_bot_handler as H
import tv_intent
from tv_control import TVTransportError

ADMIN = 4242
MARK = "zqxshadowmark"

# Разные ветки: verified чтение/запись/шаг, слепые кнопки, карта меню, подтверждение (да / отмена), сбой разбора.
SCRIPT = [
    "громкость 20", "громче", "тише 3", "громкость", "статус", "без звука", "со звуком",
    "подсветка выключи", "подсветка включи", "подсветка яркость 5", "подсветка ярче",
    "вниз 3", "ок", "назад", "домой", "настройки звука",
    "выключи телевизор", "да",
    "выключи телевизор", "нет",
    f"свари кофе {MARK}",
]


class FakeTV:
    """Двойник клиента: пишет вызовы, отвечает правдоподобно, сети нет."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple]] = []
        self.host = "192.0.2.10"
        self.moved_from = None

    def _rec(self, name, args, res):
        self.calls.append((name, tuple(args)))
        return res

    def send_key(self, key): return self._rec("send_key", (key,), None)
    def send_keys(self, keys, delay=None): return self._rec("send_keys", (tuple(keys),), None)
    def get_volume(self): return self._rec("get_volume", (), {"current": 12, "max": 60, "muted": False})
    def set_volume(self, level, muted=False): return self._rec("set_volume", (level,), min(int(level), 30))
    def volume_step(self, delta): return self._rec("volume_step", (delta,), 12 + int(delta))
    def set_muted(self, muted): return self._rec("set_muted", (muted,), None)
    def get_ambilight_power(self): return self._rec("get_ambilight_power", (), {"power": "On"})
    def set_ambilight_power(self, on): return self._rec("set_ambilight_power", (on,), None)
    def get_ambilight_config(self): return self._rec("get_ambilight_config", (), {"styleName": "FOLLOW_COLOR",
                                                                                  "menuSetting": "MANUAL_HUE"})
    def set_ambilight_style(self, style, setting=None): return self._rec("set_ambilight_style", (style, setting), None)
    def get_lounge_color(self): return self._rec("get_lounge_color", (), {"color": {"hue": 85, "saturation": 255,
                                                                                    "brightness": 0}})
    def set_lounge_color(self, h, s, b): return self._rec("set_lounge_color", (h, s, b), None)
    def get_setting_value(self, node): return self._rec("get_setting_value", (node,), 4)
    def set_setting_value(self, node, value, lo, hi): return self._rec("set_setting_value", (node, value, lo, hi),
                                                                        max(lo, min(hi, int(value))))
    def set_setting_enum(self, node, enum_id): return self._rec("set_setting_enum", (node, enum_id), enum_id)
    def get_current_activity(self): return self._rec("get_current_activity", (), {"id": 1})
    def set_powerstate(self, state): return self._rec("set_powerstate", (state,), None)
    def get_powerstate(self): return self._rec("get_powerstate", (), {"powerstate": "On"})


class NetDownTV(FakeTV):
    def get_volume(self):
        self.calls.append(("get_volume", ()))
        raise TVTransportError("нет сети")


@pytest.fixture(autouse=True)
def _iso(monkeypatch, tmp_path):
    """Журнал только во временной папке теста; модель смысла не вызывается; пауза чтения 0."""
    monkeypatch.setattr(H, "SHADOW_DEFAULT_PATH", tmp_path / "default" / "sigma_shadow.jsonl")
    monkeypatch.setenv(H.SHADOW_PATH_ENV, str(tmp_path / "j" / "sigma_shadow.jsonl"))
    monkeypatch.delenv(H.SHADOW_MAX_ENV, raising=False)
    monkeypatch.delenv("OKO_COLLECTOR", raising=False)
    monkeypatch.setenv("TV_READBACK_DELAY_S", "0")
    monkeypatch.setattr(tv_intent, "understand", lambda *a, **k: None)
    monkeypatch.setattr(H, "VERIFY", {})
    monkeypatch.setattr(H, "PRE", {})
    H._SHADOW_WARNED.clear()


def _journal() -> Path:
    return Path(os.environ[H.SHADOW_PATH_ENV])


def _rows(p: Path | None = None) -> list[dict]:
    p = p or _journal()
    if not p.exists():
        return []
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]


def _play(monkeypatch, flag: str | None, tv_cls=FakeTV, script=SCRIPT):
    if flag is None:
        monkeypatch.delenv(H.SHADOW_FLAG_ENV, raising=False)
    else:
        monkeypatch.setenv(H.SHADOW_FLAG_ENV, flag)
    tv = tv_cls()
    store = H.PendingStore()
    answers = [H.handle(ADMIN, t, lambda: tv, pending=store, admin_ids=[ADMIN], now=float(i))
               for i, t in enumerate(script)]
    return tv.calls, answers


# --- (1) одинаковые команды, порядок и исходы ---------------------------------------------------------------------

@pytest.mark.parametrize("tv_cls", [FakeTV, NetDownTV], ids=["ok", "net"])
def test_b07b_same_calls_order_and_answers(monkeypatch, tv_cls):
    calls_off, ans_off = _play(monkeypatch, None, tv_cls)
    assert not _journal().exists()
    calls_on, ans_on = _play(monkeypatch, "1", tv_cls)
    assert calls_on == calls_off and len(calls_off) > 10
    assert ans_on == ans_off
    assert len(_rows()) > 0


def test_b07b_off_path_passes_raw_client(monkeypatch):
    """Флаг выключен -> команде передаётся сам клиент, без обёртки (прежний путь без изменений)."""
    monkeypatch.delenv(H.SHADOW_FLAG_ENV, raising=False)
    seen = []
    cmd = H.Command(kind=H.tv_commands.VERIFIED, name="volume_get", run=lambda tv: seen.append(tv) or "ok")
    tv = FakeTV()
    H._execute(cmd, lambda: tv)
    assert seen == [tv]


# --- (2) схема строки ----------------------------------------------------------------------------------------------

def test_b07b_rows_whitelist_only_no_text_no_id(monkeypatch):
    _play(monkeypatch, "1")
    _play(monkeypatch, "1", NetDownTV)
    raw = _journal().read_text(encoding="utf-8")
    rows = _rows()
    assert rows
    for r in rows:
        assert set(r) <= H.SHADOW_FIELDS, set(r) - H.SHADOW_FIELDS
        assert r["step"] in H.SHADOW_STEPS and isinstance(r["ts"], int)
        assert H.shadow_validate(r) == r
    assert MARK not in raw and "кофе" not in raw and str(ADMIN) not in raw
    assert "192.0.2" not in raw and "нет сети" not in raw          # адрес ТВ и текст исключения -- нет
    steps = {r["step"] for r in rows}
    assert {"send", "ack", "readback", "outcome", "menu_step", "error"} <= steps
    errs = {r.get("err") for r in rows}
    assert {"net", "parse_fail", "cancel"} <= errs
    outs = [r for r in rows if r["step"] == "outcome"]
    assert {r["outcome"] for r in outs} <= H.SHADOW_OUTCOMES and {"PASS", "WARN", "FAIL"} <= {r["outcome"] for r in outs}
    # связка шагов: у каждого outcome есть свой seq, шаги одной команды -- тот же seq
    assert len({r["seq"] for r in outs}) == len(outs)


@pytest.mark.parametrize("bad", [
    {"step": "send", "ts": 1, "text": "громкость 20"},
    {"step": "send", "ts": 1, "user_id": 5},
    {"step": "send", "ts": 1, "action": "громкость 20"},
    {"step": "send", "ts": 1, "s_obs": "12"},
    {"step": "chat", "ts": 1},
    {"step": "outcome", "ts": 1, "outcome": "OK"},
    {"step": "error", "ts": 1, "err": "Traceback"},
    {"step": "menu_step", "ts": 1, "node_to": "a\nb"},
    {"step": "ack", "ts": 1, "raw": {"current": 1}},
], ids=["text", "uid", "act_text", "s_str", "step", "outc", "err", "node_nl", "raw"])
def test_b07b_schema_rejects_bad_row(monkeypatch, bad):
    assert H.shadow_validate(bad) is None
    monkeypatch.setenv(H.SHADOW_FLAG_ENV, "1")
    H.shadow_write(**bad)
    assert not _journal().exists()


# --- (3) сбой записи не роняет команду -----------------------------------------------------------------------------

def test_b07b_write_error_does_not_break_command(monkeypatch):
    calls_off, ans_off = _play(monkeypatch, None)

    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(H, "_shadow_append", boom)
    calls_on, ans_on = _play(monkeypatch, "1")
    assert calls_on == calls_off and ans_on == ans_off


def test_b07b_path_is_directory_does_not_break(monkeypatch, tmp_path):
    d = tmp_path / "isdir"
    d.mkdir()
    monkeypatch.setenv(H.SHADOW_PATH_ENV, str(d))
    calls_off, ans_off = _play(monkeypatch, None)
    calls_on, ans_on = _play(monkeypatch, "1")
    assert calls_on == calls_off and ans_on == ans_off


# --- (4) ротация ---------------------------------------------------------------------------------------------------

def test_b07b_rotation_by_size(monkeypatch):
    monkeypatch.setenv(H.SHADOW_MAX_ENV, "600")
    for _ in range(3):
        _play(monkeypatch, "1")
    p = _journal()
    old = p.with_name(p.name + ".1")
    assert p.exists() and old.exists()
    assert p.stat().st_size <= 600 + 400 and old.stat().st_size <= 600 + 400
    assert all(set(r) <= H.SHADOW_FIELDS for r in _rows(old))


def test_b07b_bad_max_env_uses_default(monkeypatch):
    for v in ("abc", "0", "-5"):
        monkeypatch.setenv(H.SHADOW_MAX_ENV, v)
        assert H._shadow_max() == H.SHADOW_MAX_BYTES


# --- (5) флаг выключен -> файл не создаётся --------------------------------------------------------------------------

@pytest.mark.parametrize("flag", [None, "0", "true"], ids=["unset", "0", "true"])
def test_b07b_flag_off_no_file(monkeypatch, flag):
    _play(monkeypatch, flag, NetDownTV if flag == "0" else FakeTV)
    assert not _journal().exists() and not _journal().parent.exists()
    assert not H.SHADOW_DEFAULT_PATH.exists()


# --- (6) режим сбора не перенаправляет путь журнала ----------------------------------------------------------------

def test_b07b_collector_tmp_path_rejected(monkeypatch, tmp_path):
    bad = tmp_path / "oko_collector_abc123" / "sigma_shadow.jsonl"
    monkeypatch.setenv(H.SHADOW_PATH_ENV, str(bad))
    assert H.shadow_path() == H.SHADOW_DEFAULT_PATH
    _play(monkeypatch, "1", script=["громкость"])
    assert not bad.exists() and H.SHADOW_DEFAULT_PATH.exists()


def test_b07b_collector_dir_from_config_rejected(monkeypatch, tmp_path):
    import config
    cdir = tmp_path / "anyname"
    cdir.mkdir()
    monkeypatch.setattr(config, "COLLECTOR_DIR", cdir, raising=False)
    monkeypatch.setenv(H.SHADOW_PATH_ENV, str(cdir / "sub" / "sigma_shadow.jsonl"))
    assert H.shadow_path() == H.SHADOW_DEFAULT_PATH
    monkeypatch.setenv(H.SHADOW_PATH_ENV, str(tmp_path / "keep" / "s.jsonl"))
    assert H.shadow_path() == tmp_path / "keep" / "s.jsonl"


def test_b07b_collector_process_keeps_journal(tmp_path):
    """Живой режим сбора в отдельном процессе: config уводит память и events.jsonl в TEMP, журнал Σ -- нет;
    путь в COLLECTOR_DIR отвергается; после выхода папка сбора удалена, журнал на месте."""
    base = tmp_path / "data"
    (base / "memory").mkdir(parents=True)
    keep = tmp_path / "keep" / "sigma_shadow.jsonl"
    env = {k: v for k, v in os.environ.items()
           if k not in ("OKO_COLLECTOR", "OKO_EVENT_LOG", H.SHADOW_PATH_ENV, H.SHADOW_FLAG_ENV)}
    env.update(AI_BASE_DIR=str(base), OKO_EVENT_LOG=str(base / "events.jsonl"), PYTHONIOENCODING="utf-8",
               OKO_COLLECTOR="1", OKO_SIGMA_SHADOW="1", TV_READBACK_DELAY_S="0",
               OKO_SIGMA_SHADOW_PATH="")   # пусто = путь по умолчанию; load_dotenv заданное не перезаписывает
    code = (
        "import json, os, config, tv_bot_handler as H\n"
        "cd = config.COLLECTOR_DIR\n"
        "d0 = str(H.shadow_path())\n"
        "os.environ[H.SHADOW_PATH_ENV] = str(cd / 'sigma_shadow.jsonl')\n"
        "d1 = str(H.shadow_path())\n"
        f"os.environ[H.SHADOW_PATH_ENV] = {str(keep)!r}\n"
        "d2 = str(H.shadow_path())\n"
        "H.shadow_write(step='outcome', outcome='PASS', seq=1)\n"
        "print(json.dumps({'col': config.COLLECTOR, 'cd': str(cd), 'ev': os.environ.get('OKO_EVENT_LOG'),"
        " 'd0': d0, 'd1': d1, 'd2': d2, 'default': str(H.SHADOW_DEFAULT_PATH)}))\n"
    )
    r = subprocess.run([sys.executable, "-c", code], cwd=Path(H.__file__).resolve().parent, env=env,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr[-800:]
    o = json.loads(r.stdout.strip().splitlines()[-1])
    assert o["col"] is True and o["ev"].startswith(o["cd"])          # режим сбора действительно включён
    assert o["d0"] == o["default"] and not o["d0"].startswith(o["cd"])
    assert o["d1"] == o["default"]                                    # путь в COLLECTOR_DIR отвергнут
    assert o["d2"] == str(keep)
    assert not Path(o["cd"]).exists()                                 # папка сбора удалена при выходе
    assert keep.exists() and _rows(keep)[0]["outcome"] == "PASS"      # журнал пережил выход процесса
