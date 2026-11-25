"""sigma_live.py -- B33 v2 C1 (B32r H1; B32d O-M6; B32e E-L2): ОДНА точка записи в ТВ под шлюзом Σ.

Флаг боевого режима -- тот же, что у шлюза (sigma_gate.LIVE_ENV / tv_bot_handler._live_flag): OKO_SIGMA_LIVE=1 в
окружении процесса. В боевом режиме писать в ТВ можно только через tv_bot_handler._execute (вердикт Σ).

Точки записи в ТВ мимо шлюза и их отказ (B33 v2 попытка 2, ревизор G4 -- полный список по коду):
  клиент ТВ ТОЛЬКО через tv_client() (обёртка Guarded):
    /cam calib auto        -- bot._calib_auto, camera_watcher.calibrate_auto (refusal в начале);
    повтор пробуждения     -- bot._wol_again;
    мост подсветки         -- amb_music.start (отказ запуска), amb_music.run (процесс моста);
    мост звука             -- audio_bridge.run_bridge / run_soundcard;
    CLI                    -- tv_control.main, tools/cam_latency.py, tools/amb_off_probe.py;
  сырой HTTP / WoL без клиента -- refusal() первой строкой main(), модули не импортируются никем, кроме тестов:
    src/tv_probe.py (POST input/key, screenstate, ambilight, power, DIAL), src/tv_snapshot.py (POST power,
    currentconfiguration, audio/volume), src/tv_pair.py (POST pair/request, pair/grant), tools/tv_wol_probe.py (UDP WoL).
  Транспорт шлюза (tv_control.TVClient, tv_control.send_wol) -- сам путь шлюза, не точка обхода.
Сторож по AST (src/test_b33v2_sigma_single_point.py): клиент ТВ создаётся только в шлюзе, в tv_client и в
диагностике чтения tools/oko_selfcheck.py::c_tv; модуль с сырой записью в ТВ (вызов post / put / request / sendto /
urlopen при признаке ТВ: порт 1925 / 1926, /6/, input/key, ambilight, screenstate, магический пакет, HTTPDigestAuth)
-- только из списка, с refusal() первой строкой main() и без импортов из рабочего кода.

Ревизор G5: клиент из tv_client() -- обёртка Guarded: флаг проверяется перед КАЖДЫМ вызовом клиента, а не только при
создании. Долгий процесс (мосты) передаёт env_file: тогда флаг -- ИЛИ окружения процесса, ИЛИ строки
OKO_SIGMA_LIVE=1 в этом файле (.env перечитывается по изменению mtime / размера; ошибка чтения -- боевой режим,
fail-closed). Включили боевой Σ в .env во время работы моста -- следующая запись не уходит (BypassRefused).
Без боевого режима -- прежнее поведение. Только стандартная библиотека; ничего не делает при импорте."""
from __future__ import annotations

import logging
import os
from typing import Any, Callable, Optional

LIVE_ENV = "OKO_SIGMA_LIVE"
BYPASS_TEXT = "⏸ боевой Σ: действие мимо шлюза запрещено ({path}). Ничего не отправлено."
logger = logging.getLogger("oko.sigma_live")


class BypassRefused(PermissionError):
    """Боевой режим Σ: запись в ТВ мимо шлюза. text -- готовый ответ человеку."""

    def __init__(self, path: str):
        self.path = path
        self.text = BYPASS_TEXT.format(path=path)
        super().__init__(self.text)


def live() -> bool:
    """OKO_SIGMA_LIVE=1 (то же правило, что sigma_gate.live_enabled; модуль шлюза для чтения флага не нужен)."""
    return os.getenv(LIVE_ENV, "").strip() == "1"


def refusal(path: str) -> Optional[str]:
    """Текст отказа, если боевой режим включён (и строка WARNING в лог: только имя пути); иначе None."""
    if not live():
        return None
    logger.warning("[SIGMA_LIVE] запись в ТВ мимо шлюза отклонена: %s", path)
    try:
        import layer_events
        layer_events.record("L3", "catch", "SIGMA_HALT")
    except Exception:  # noqa: BLE001 - телеметрия не влияет на отказ
        pass
    return BYPASS_TEXT.format(path=path)


_FILE_CACHE: dict = {}


def file_flag(env_file) -> bool:
    """OKO_SIGMA_LIVE=1 в файле .env (последнее присваивание). Нет файла -- False; не читается -- True (fail-closed)."""
    from pathlib import Path
    f = Path(env_file)
    try:
        st = f.stat()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    key = (str(f), st.st_mtime_ns, st.st_size)
    if key in _FILE_CACHE:
        return _FILE_CACHE[key]
    try:
        text = f.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return True
    val = ""
    for ln in text.splitlines():
        s = ln.strip()
        if s.startswith("export "):
            s = s[7:].lstrip()
        if s.startswith(LIVE_ENV) and s[len(LIVE_ENV):].lstrip().startswith("="):
            val = s.split("=", 1)[1].split(" #", 1)[0].strip().strip("'\"").strip()
    _FILE_CACHE.clear()
    _FILE_CACHE[key] = val == "1"
    return _FILE_CACHE[key]


def live_now(env_file=None) -> bool:
    """Флаг для долгого процесса: окружение ИЛИ файл env_file (если задан)."""
    return live() or (env_file is not None and file_flag(env_file))


class Guarded:
    """Клиент ТВ пути мимо шлюза: перед КАЖДЫМ вызовом метода -- проверка боевого режима (ревизор G5)."""

    def __init__(self, client: Any, path: str, env_file=None):
        object.__setattr__(self, "_client", client)
        object.__setattr__(self, "_path", path)
        object.__setattr__(self, "_env_file", env_file)

    def __getattr__(self, name: str):
        attr = getattr(object.__getattribute__(self, "_client"), name)
        if not callable(attr):
            return attr
        path, env_file = object.__getattribute__(self, "_path"), object.__getattribute__(self, "_env_file")

        def call(*a, **k):
            if live_now(env_file):
                if refusal(path) is None:                 # флаг только в файле: та же строка лога
                    logger.warning("[SIGMA_LIVE] запись в ТВ мимо шлюза отклонена (флаг в файле .env): %s", path)
                raise BypassRefused(path)
            return attr(*a, **k)
        return call


def tv_client(path: str, factory: Optional[Callable[[], Any]] = None, env_file=None) -> Any:
    """Клиент ТВ для пути мимо шлюза. Боевой режим -> BypassRefused ДО создания клиента (0 записей); иначе --
    обёртка Guarded (проверка перед каждым вызовом; env_file -- для долгих процессов, см. live_now)."""
    if refusal(path) is not None or (env_file is not None and file_flag(env_file)):
        raise BypassRefused(path)
    if factory is None:
        from tv_control import TVClient
        factory = TVClient.from_env
    return Guarded(factory(), path, env_file)
