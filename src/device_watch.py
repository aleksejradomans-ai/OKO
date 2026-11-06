"""device_watch.py — наблюдение за домашней техникой и события для Telegram (каркас, 28.10.2026).

Схема: адаптер (опрос) -> снимок состояния -> детектор ПЕРЕХОДОВ -> текст события по-русски.
Модуль не знает о Telegram: `poll_once()` возвращает список текстов, отправляет их bot.py
(`device_watch_job` -> `_broadcast_to_admins`), как это уже сделано для алертов деградации (pull-модель).

Сейчас подключены стиральная и сушильная Samsung (облако SmartThings, только ЧТЕНИЕ).
Что измерено 28.10 и заложено в логику:
- `machineState` stop|run|pause — основной признак цикла; значения jobState по фазам НЕ измерены,
  поэтому «закончила» объявляется только после ДВУХ опросов подряд в stop после run/pause
  (защита от возможного кратковременного stop между фазами — не доказано, что его нет);
- облако отвечает 429 при частом опросе — цикл пропускается молча, без события;
- токен PAT живёт 24 ч — 401 даёт ОДНО сообщение, повторы подавляются до восстановления;
- первый опрос после запуска бота — только снимок, без событий (перезапуск не должен слать «закончила»).
Коды программ наружу не выходят: названия — из samsung_names_ru.

29.10: токен — сначала OAuth (smartthings_oauth: обновляется сам раз в сутки, смена refresh-токена сохраняется),
при его отсутствии или поломке — PAT из OKO_SMARTTHINGS_TOKEN. Смена токена НЕ сбрасывает детектор переходов.
Включается OKO_SMARTTHINGS_TOKEN (в .env службы) или файлом data/.smartthings_tokens.json. Интервал — OKO_DEVICE_POLL_S (по умолчанию 60 с,
не меньше 30: лимит облака).
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable

import samsung_names_ru

logger = logging.getLogger("oko.devices")

API = "https://api.smartthings.com/v1"
HTTP_TIMEOUT_S = 15
POLL_MIN_S = 30
STOP_CONFIRM_POLLS = 2          # «закончила» — только после стольких опросов подряд в stop
TRANSPORT_ALERT_AFTER = 10      # столько неудачных опросов подряд (~10 мин при 60 с) -> одно сообщение
ABORT_MARGIN_S = 180            # stop раньше плана больше чем на столько -> «остановлена до окончания»
KINDS = {"oic.d.washer": "washer", "oic.d.dryer": "dryer"}
TITLE = {"washer": "🧺 Стиральная", "dryer": "♨️ Сушильная"}


def poll_interval_s() -> float:
    try:
        v = float(os.getenv("OKO_DEVICE_POLL_S", "60"))
    except ValueError:
        v = 60.0
    return max(float(POLL_MIN_S), v)


def _oauth():
    try:
        import smartthings_oauth
        return smartthings_oauth
    except Exception:            # модуля нет — работаем как раньше, на PAT
        return None


def enabled() -> bool:
    if os.getenv("OKO_SMARTTHINGS_TOKEN", "").strip():
        return True
    o = _oauth()
    return bool(o and o.token_path().is_file())


_OAUTH_BROKEN = False


def current_token() -> tuple[str, list[str]]:
    """(токен, сообщения). OAuth первым; PAT — запасной. О поломке/восстановлении OAuth — по одному сообщению."""
    global _OAUTH_BROKEN
    pat = os.getenv("OKO_SMARTTHINGS_TOKEN", "").strip()
    o = _oauth()
    if o is None:
        return pat, []
    try:
        tok = o.get_token()
    except o.OAuthError as e:
        logger.warning("[DEVICES] OAuth: %s (нужен вход: %s)", e, e.auth)
        if e.auth:
            if _OAUTH_BROKEN:
                return pat, []
            _OAUTH_BROKEN = True
            return pat, ["⚠️ Техника: вход SmartThings (OAuth) больше не действует — нужен повторный вход "
                         "(smartthings_oauth.py exchange). "
                         + ("Пока работаю на токене из .env." if pat else "Уведомления о стирке и сушке остановлены.")]
        data = o.load() or {}           # сеть: прежний access-токен живёт ещё до часа (обновление — заранее)
        alive = float(data.get("expires_at") or 0) > time.time()
        return (data.get("access_token") if alive else None) or pat, []
    if tok is None:
        return pat, []
    notes = []
    if _OAUTH_BROKEN:
        _OAUTH_BROKEN = False
        notes.append("✅ Техника: вход SmartThings (OAuth) восстановлен.")
    return tok, notes


class AuthFailed(Exception):
    """401/403 — токен неверен или истёк."""


class RateLimited(Exception):
    """429 — облако просит подождать."""


class Transport(Exception):
    """Сеть/облако недоступны."""


def _http_get(path: str, token: str) -> dict:
    req = urllib.request.Request(API + path, headers={"Authorization": f"Bearer {token}",
                                                      "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as r:
            return json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise AuthFailed(f"HTTP {e.code}") from e
        if e.code == 429:
            raise RateLimited("HTTP 429") from e
        raise Transport(f"HTTP {e.code}") from e
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        raise Transport(f"{type(e).__name__}: {e}") from e


def _val(main: dict, cap: str, attr: str):
    return ((main.get(cap) or {}).get(attr) or {}).get("value")


@dataclass
class Snapshot:
    kind: str
    label: str
    machine: str | None
    cycle: str | None
    completion: str | None


def fetch_snapshots(token: str, get: Callable[[str, str], dict] = _http_get) -> list[Snapshot]:
    """Один проход по стиральной и сушильной: 1 + N запросов."""
    out = []
    for d in get("/devices", token).get("items") or []:
        kind = KINDS.get((d.get("ocf") or {}).get("ocfDeviceType"))
        if not kind:
            continue
        main = ((get(f"/devices/{d['deviceId']}/status", token).get("components") or {}).get("main") or {})
        out.append(Snapshot(
            kind=kind,
            label=d.get("label") or kind,
            machine=_val(main, f"{kind}OperatingState", "machineState"),
            cycle=_val(main, f"samsungce.{kind}Cycle", f"{kind}Cycle"),
            completion=_val(main, f"{kind}OperatingState", "completionTime"),
        ))
    return out


def _local_hhmm(iso: str | None) -> str | None:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone().strftime("%H:%M")
    except ValueError:
        return None


def _parse_utc(iso: str | None) -> datetime | None:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None


@dataclass
class Watcher:
    """Детектор переходов. Без сети: снимки подаются снаружи (так он и тестируется).

    Живой замер 28.10 12:51–12:57 («Полоскание + отжим», остановлена кнопкой) показал два дефекта первой версии:
    1. после выключения машина сбрасывает выбранную программу на «Эко-стирку 40–60», и событие брало
       название из ТЕКУЩЕГО снимка — пришло «закончила «Эко-стирка 40–60»». Теперь название и плановое время
       запоминаются, пока программа идёт (run/pause), и в событии об окончании используются они;
    2. прерванная программа объявлялась как «закончила». Теперь `stop` раньше планового окончания минус
       ABORT_MARGIN_S объявляется «остановлена до окончания». Плановое время — completionTime облака,
       последнее увиденное в run/pause.
    """
    last: dict[str, str | None] = field(default_factory=dict)       # kind -> подтверждённое состояние
    stop_seen: dict[str, int] = field(default_factory=dict)         # kind -> опросов подряд в stop
    active_cycle: dict[str, str | None] = field(default_factory=dict)
    planned_end: dict[str, str | None] = field(default_factory=dict)
    now: Callable[[], datetime] = field(default=lambda: datetime.now(timezone.utc))
    started: bool = False

    def _finish_text(self, kind: str, title: str) -> str:
        cyc = self.active_cycle.get(kind)
        name = samsung_names_ru.name_ru(kind, cyc) if cyc else "программа не известна"
        end = _parse_utc(self.planned_end.get(kind))
        if end is not None and self.now() < end - timedelta(seconds=ABORT_MARGIN_S):
            return f"{title}: остановлена до окончания «{name}» (по плану до {_local_hhmm(self.planned_end[kind])})."
        return f"{title}: закончила «{name}»."

    def feed(self, snaps: list[Snapshot]) -> list[str]:
        events: list[str] = []
        for s in snaps:
            prev = self.last.get(s.kind)
            name = samsung_names_ru.name_ru(s.kind, s.cycle) if s.cycle else "программа не известна"
            title = TITLE.get(s.kind, s.label)
            if not self.started:
                self.last[s.kind] = s.machine
                if s.machine in ("run", "pause"):            # бот перезапущен посреди программы
                    self.active_cycle[s.kind], self.planned_end[s.kind] = s.cycle, s.completion
                continue
            if s.machine == "stop":
                if prev in ("run", "pause"):
                    n = self.stop_seen.get(s.kind, 0) + 1
                    self.stop_seen[s.kind] = n
                    if n >= STOP_CONFIRM_POLLS:
                        events.append(self._finish_text(s.kind, title))
                        self.last[s.kind] = "stop"
                        self.stop_seen[s.kind] = 0
                        self.active_cycle.pop(s.kind, None)
                        self.planned_end.pop(s.kind, None)
                    continue                       # пока не подтверждено — состояние не меняем
                self.last[s.kind] = "stop"
                continue
            self.stop_seen[s.kind] = 0
            if s.machine in ("run", "pause"):
                if s.cycle:
                    self.active_cycle[s.kind] = s.cycle
                if s.completion:
                    self.planned_end[s.kind] = s.completion
            if s.machine == "run" and prev in (None, "stop"):
                when = _local_hhmm(s.completion)
                events.append(f"{title}: запущена «{name}»" + (f", закончит около {when}." if when else "."))
            elif s.machine == "pause" and prev == "run":
                events.append(f"{title}: пауза («{name}»).")
            elif s.machine == "run" and prev == "pause":
                events.append(f"{title}: продолжает «{name}».")
            if s.machine in ("run", "pause"):
                self.last[s.kind] = s.machine
        if not self.started:
            # 28.10: успешные опросы молчат, и по журналу нельзя было отличить «опрос идёт» от «задача не запущена».
            # Базовый снимок пишется один раз — это доказательство, что задача и облако работают.
            logger.info("[DEVICES] базовый снимок: %s",
                        ", ".join(f"{s.kind}={s.machine}" for s in snaps) or "машин в аккаунте нет")
        self.started = True
        return events


@dataclass
class Monitor:
    """Опрос + детектор + честные сообщения о сбоях канала (по одному, без повторов)."""
    token: str
    get: Callable[[str, str], dict] = _http_get
    watcher: Watcher = field(default_factory=Watcher)
    auth_broken: bool = False
    transport_fails: int = 0
    transport_alerted: bool = False

    def poll_once(self) -> list[str]:
        try:
            snaps = fetch_snapshots(self.token, self.get)
        except RateLimited:
            logger.info("[DEVICES] SmartThings 429 — цикл пропущен")
            return []
        except AuthFailed as e:
            logger.warning("[DEVICES] SmartThings auth: %s", e)
            if self.auth_broken:
                return []
            self.auth_broken = True
            return ["⚠️ Техника: облако SmartThings не принимает токен (истёк или отозван). "
                    "Уведомления о стирке и сушке остановлены до замены OKO_SMARTTHINGS_TOKEN."]
        except Transport as e:
            self.transport_fails += 1
            logger.warning("[DEVICES] SmartThings transport fail=%d: %s", self.transport_fails, e)
            if self.transport_fails >= TRANSPORT_ALERT_AFTER and not self.transport_alerted:
                self.transport_alerted = True
                return ["⚠️ Техника: облако SmartThings недоступно уже "
                        f"{self.transport_fails} опросов подряд. Уведомления могут запаздывать."]
            return []
        events: list[str] = []
        if self.auth_broken or self.transport_alerted:
            events.append("✅ Техника: связь с облаком SmartThings восстановлена.")
        self.auth_broken = False
        self.transport_fails = 0
        self.transport_alerted = False
        return events + self.watcher.feed(snaps)


_MONITOR: Monitor | None = None


def poll_once() -> list[str]:
    """Точка входа для bot.py (вызывается в отдельном потоке: здесь блокирующий HTTP)."""
    global _MONITOR
    token, notes = current_token()
    if not token:
        return notes
    if _MONITOR is None:
        _MONITOR = Monitor(token=token)
    _MONITOR.token = token          # 29.10: смена токена (OAuth раз в сутки) не сбрасывает детектор переходов
    return notes + _MONITOR.poll_once()


STATE_LABEL = {"washer": "Стиральная", "dryer": "Сушильная"}


def state_text(monitor: "Monitor | None" = None) -> str:
    """EVENTS (01.11): текущее состояние техники из памяти детектора, без сети. Пусто -- опросов ещё не было.
    Неподтверждённый stop (STOP_CONFIRM_POLLS) ещё считается run: отставание не больше одного опроса."""
    m = monitor if monitor is not None else _MONITOR
    if m is None or not m.watcher.started:
        return ""
    w = m.watcher
    try:
        last = dict(w.last)
        cycles = dict(w.active_cycle)
        ends = dict(w.planned_end)
    except RuntimeError:                 # словарь меняется в потоке опроса -- в следующий раз
        return ""
    parts = []
    for kind in sorted(last):
        title = STATE_LABEL.get(kind, kind)
        st = last.get(kind)
        if st in ("run", "pause"):
            cyc = cycles.get(kind)
            name = samsung_names_ru.name_ru(kind, cyc) if cyc else "программа не известна"
            end = _local_hhmm(ends.get(kind))
            parts.append(f"{title}: {'идёт' if st == 'run' else 'на паузе'} «{name}»"
                         + (f", по плану до {end}" if end else "") + ".")
        elif st == "stop":
            parts.append(f"{title}: не работает.")
        else:
            parts.append(f"{title}: состояние неизвестно.")
    if not parts:
        parts.append("машин в аккаунте нет.")
    if m.auth_broken or m.transport_fails:
        parts.append("(связь с облаком SmartThings сейчас нарушена — данные могут быть устаревшими)")
    return " ".join(parts)
