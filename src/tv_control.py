"""tv_control.py — «руки» Ока на телевизоре Philips (JointSpace API v6).

Транспортный модуль: HTTPS на порт 1926, digest-auth парой, полученной при
пайринге (см. `tv_pair.py`). Разведка устройства и таблицы эндпоинтов — в
документе `tv_philips_api_v6` (RAG, 11 чанков).

РАЗДЕЛЕНИЕ СТАТИКИ И ДИНАМИКИ (решение Оператора 27.10):
- статика (`TV_HOST`, опционально `TV_PORT`) — в `.env`;
- динамика (пара `username`/`password`, выданная телевизором в ответ на
  `/6/pair/grant`) — в `data\\tv_creds.json`, то есть внутри `BASE_DIR`,
  который исключён из git. Секрет не попадает в репозиторий даже при
  ошибке в whitelist публичной выгрузки.

`config.py` НЕ правится: отсюда берётся только `BASE_DIR` (единая точка
правды для data layer), а `TV_HOST`/`TV_PORT` читаются через `os.getenv`
в момент вызова `from_env()`. Причина — `knowledge_indexer.py` и соседи
уже в манифесте целостности; добавление TV-переменных в `config.py`
потребовало бы пересчёта манифеста ради модуля, который пока не вызывается
ни ботом, ни watcher'ом.

ИЗОЛЯЦИЯ ТРАНСПОРТА: ни одна функция этого модуля не создаёт
`requests.Session()` неявно — сессия либо передаётся в конструктор
(тесты передают фейковую), либо создаётся один раз в `from_env()`.
Все сетевые исключения `requests` заворачиваются в `TVTransportError`,
чтобы вызывающий код (bot.py) отличал транспортный сбой от логической
ошибки, как это уже сделано для Ollama в governor.

TLS: телевизор отдаёт self-signed сертификат на 1926, проверка цепочки
невозможна в принципе (CN не совпадает, CA нет). `verify=False` —
осознанное решение для устройства в локальной сети по фиксированному IP,
не недосмотр. Предупреждение urllib3 глушится точечно, по классу, а не
через общий `warnings.filterwarnings("ignore")`.
"""
from __future__ import annotations

import json
import logging
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Mapping

import requests
import urllib3
from requests.auth import HTTPDigestAuth

import config

logger = logging.getLogger("oko.tv")

# Точечное глушение только предупреждения о непроверяемом сертификате.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

DEFAULT_PORT = 1926
API_VERSION = "6"

# Пауза между кнопками в последовательности. Телевизор обрабатывает
# нажатия асинхронно: `POST input/key` возвращает 200 сразу, не дожидаясь
# перерисовки интерфейса. Меню при этом реагирует неравномерно — переход
# между пунктами быстрый, а открытие портала или смена раздела заметно
# медленнее. Без паузы следующая кнопка приходит в ещё не перерисованный
# экран и либо теряется, либо попадает не туда, причём код ответа у такого
# нажатия тот же 200: отличить потерю от успеха по API невозможно.
#
# 0.4 с — компромисс для навигации. Для переходов, меняющих раздел
# целиком, вызывающий код передаёт delay больше (см. send_keys).
DEFAULT_KEY_DELAY_S = 0.4

# Файл с динамически полученной парой. Внутри BASE_DIR (data/), не в src/.
CREDS_PATH: Path = config.BASE_DIR / "tv_creds.json"

# Кнопки `input/key` API v6. Whitelist, а не произвольная строка: телевизор
# на неизвестное имя отвечает 400 без пояснения, и отладка такого вызова по
# логу бота стоит дороже, чем явный ValueError здесь. Состав — из таблицы
# эндпоинтов в документе `tv_philips_api_v6`.
KEYS: frozenset[str] = frozenset({
    "Standby", "Back", "Find", "RedColour", "GreenColour", "YellowColour",
    "BlueColour", "Home", "VolumeUp", "VolumeDown", "Mute", "Options", "Dot",
    "Digit0", "Digit1", "Digit2", "Digit3", "Digit4", "Digit5", "Digit6",
    "Digit7", "Digit8", "Digit9", "Info", "CursorUp", "CursorDown",
    "CursorLeft", "CursorRight", "Confirm", "Next", "Previous", "Adjust",
    "WatchTV", "Viewmode", "Teletext", "Subtitle", "ChannelStepUp",
    "ChannelStepDown", "Source", "AmbilightOnOff", "PlayPause", "Pause",
    "FastForward", "Stop", "Rewind", "Record", "Online",
})

POWERSTATES: frozenset[str] = frozenset({"On", "Standby"})

# Предел громкости, выше которого контур НЕ поднимает звук. Решение
# Оператора 27.10: половина диапазона. Причина не в технике, а в том, что
# автоматика получает право менять громкость сама — ошибка в контуре или
# неверный замер не должны приводить к удару громкостью по человеку.
#
# Доля, а не абсолютное число: диапазон модельный (на «проверочном» 0..60),
# и захардкоженные 30 на другом телевизоре означали бы другую долю.
# Переопределяется `TV_VOLUME_LIMIT_FRACTION` без правки кода.
VOLUME_LIMIT_FRACTION = 0.5
VOLUME_LIMIT_ENV = "TV_VOLUME_LIMIT_FRACTION"


def volume_limit_fraction() -> float:
    """Доля диапазона из окружения. Мусор и значения вне (0; 1] -> дефолт.

    Молча расширять предел из-за опечатки в `.env` нельзя: это ровно тот
    случай, когда тихий откат к «без ограничения» опаснее отказа.
    """
    raw = os.getenv(VOLUME_LIMIT_ENV, "")
    if not raw:
        return VOLUME_LIMIT_FRACTION
    try:
        value = float(raw)
    except ValueError:
        logger.warning("%s=%r не число, предел остаётся %.2f",
                       VOLUME_LIMIT_ENV, raw, VOLUME_LIMIT_FRACTION)
        return VOLUME_LIMIT_FRACTION
    if not 0.0 < value <= 1.0:
        logger.warning("%s=%r вне (0; 1], предел остаётся %.2f",
                       VOLUME_LIMIT_ENV, raw, VOLUME_LIMIT_FRACTION)
        return VOLUME_LIMIT_FRACTION
    return value


def volume_ceiling(lo: int, hi: int, fraction: float | None = None) -> int:
    """Максимум, доступный контуру: `lo + (hi - lo) * доля`, вниз.

    Округление вниз, а не к ближайшему: предел должен быть не выше
    заданной доли, иначе 0.5 от 0..61 дало бы 31 — больше половины.
    """
    f = volume_limit_fraction() if fraction is None else float(fraction)
    return int(lo + (hi - lo) * f)

# Режимы подсветки, которые телевизор РЕАЛЬНО признаёт (проверено
# перебором 27.10): POST отдаёт 200 на любое значение, но закрепляются
# только эти три. `manual` молча подменяется на `lounge`, `follow_video`
# отвечает 503. Поэтому whitelist — не перестраховка, а карта.
AMBILIGHT_MODES: frozenset[str] = frozenset({"internal", "expert", "lounge"})

# Узлы меню настроек Ambilight (data/tv_settings_structure.json, «тв амби» 29.10). Номера — модельные:
# на другом телевизоре проверить по structure. Диапазоны ползунков — из slider_data того же ответа.
SETTING_AMBI_BRIGHTNESS = 710            # 0..10
SETTING_AMBI_SATURATION = 720            # -2..2
SETTING_AMBI_FOLLOW_VIDEO = 320          # 321 ИИ, 322, 323, 324 кино, 325 игра
SETTING_AMBI_FOLLOW_AUDIO = 330          # 331, 332, 333
SLIDERS: dict[int, tuple[int, int]] = {SETTING_AMBI_BRIGHTNESS: (0, 10), SETTING_AMBI_SATURATION: (-2, 2)}

# Стороны подсветки в ответе topology.
SIDES: tuple[str, ...] = ("left", "top", "right", "bottom")


class TVError(Exception):
    """Базовый класс сбоев TV-контура."""


class TVNotPaired(TVError):
    """Нет пары username/password — пайринг не выполнялся."""


class TVAuthError(TVError):
    """401: пара отвергнута (сброс настроек телевизора, нужен пере-пайринг)."""


class TVUnsupported(TVError):
    """404/403: эндпоинт на этой модели отсутствует или закрыт.

    Отдельный класс, а не TVAuthError и не общий TVError: набор путей API v6
    модельный, и это НЕ сбой. Подтверждено пробой на «тв амби» 27.10:
    `powerstate` -> 404, `applications` и `sources/current` -> 403, при том
    что `system`, `audio/volume`, `activities/*`, `ambilight/*` отдают 200
    с той же парой. То есть 403 здесь означает «закрыто на модели», а не
    «пара не принята» — иначе 403 на applications выглядел бы как повод
    к повторному пайрингу, которого модель не требует.
    """


class TVTransportError(TVError):
    """Сеть: таймаут, отказ соединения, телевизор выключен из розетки."""


# --------------------------------------------------------------------------
# Креденшы
# --------------------------------------------------------------------------

def load_creds(path: Path | None = None) -> dict[str, str]:
    """Читает пару из `data\\tv_creds.json`.

    TVNotPaired, а не возврат None — отсутствие пары это не «пусто», а
    конкретное состояние системы с конкретным лечением (запустить пайринг),
    и текст исключения должен об этом говорить прямо.
    """
    p = path or CREDS_PATH
    if not p.exists():
        raise TVNotPaired(
            f"Файл {p} не найден. Выполните пайринг: python src\\tv_pair.py"
        )
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise TVNotPaired(f"Файл {p} нечитаем или не JSON: {e}") from e

    missing = [k for k in ("username", "password") if not data.get(k)]
    if missing:
        raise TVNotPaired(f"В {p} нет полей: {', '.join(missing)}")
    return data


def save_creds(data: Mapping[str, Any], path: Path | None = None) -> Path:
    """Пишет пару в `data\\tv_creds.json` (0600, где ОС это поддерживает).

    Запись атомарная через временный файл в той же директории: прерывание
    процесса посередине не оставит обрезанный JSON, из которого потом
    нельзя восстановить пару — повторный пайринг требует физического
    доступа к пульту, это не бесплатная операция.
    """
    p = path or CREDS_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(dict(data), indent=2, ensure_ascii=False), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        # На Windows chmod почти no-op; защита файла — ACL каталога data/.
        pass
    tmp.replace(p)
    logger.info("[TV] креденшы сохранены: %s", p)
    return p


# --------------------------------------------------------------------------
# Поиск телевизора по MAC (29.10, решение Оператора)
# --------------------------------------------------------------------------
# Живой случай 29.10 11:25–11:44: ПК и телевизор перешли из сети точки доступа в домашнюю, роутер выдал
# телевизору другой адрес, а TV_HOST в .env остался старым — команды уходили в пустоту, адрес правился руками.
# Теперь: если задан TV_MAC и телевизор НЕ ПРИНЯЛ СОЕДИНЕНИЕ (таймаут соединения / отказ), клиент один раз
# ищет его MAC в ARP-таблице ПК (`arp -a`), при необходимости «будит» ARP пустыми UDP-пакетами по своей /24,
# проверяет порт управления и повторяет ЗАПРОС ОДИН РАЗ по новому адресу.
# Не повторяем при таймауте ЧТЕНИЯ: соединение было, нажатие могло дойти — повтор нажал бы кнопку дважды.
# Найденный адрес — динамика: data/tv_host.json (как пара в tv_creds.json), .env не трогается.

HOST_CACHE_PATH: Path = config.BASE_DIR / "tv_host.json"
_MAC_RE = re.compile(r"(\d{1,3}(?:\.\d{1,3}){3})\s+([0-9A-Fa-f]{2}(?:[-:][0-9A-Fa-f]{2}){5})")


def norm_mac(mac: str | None) -> str:
    return re.sub(r"[^0-9a-f]", "", (mac or "").lower())


def arp_table() -> list[tuple[str, str]]:
    """[(ip, mac)] из `arp -a`. Заголовки локализованы — разбираются только строки «IP MAC»."""
    kw: dict[str, Any] = {}
    if sys.platform == "win32":
        kw["creationflags"] = 0x08000000           # CREATE_NO_WINDOW: служба без консоли
    try:
        out = subprocess.run(["arp", "-a"], capture_output=True, timeout=10, **kw).stdout
    except (OSError, subprocess.SubprocessError) as e:
        logger.warning("[TV] arp -a не выполнен: %s", e)
        return []
    text = out.decode("ascii", errors="replace") if isinstance(out, bytes) else str(out)
    return [(ip, mac) for ip, mac in _MAC_RE.findall(text)]


def local_ipv4() -> list[str]:
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
    except OSError:
        infos = []
    ips = {i[4][0] for i in infos}
    try:                                   # основной интерфейс: UDP-«connect» не отправляет пакетов
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))
            ips.add(s.getsockname()[0])
    except OSError:
        pass
    return sorted(ip for ip in ips if not ip.startswith(("127.", "169.254.", "0.")))


def poke_subnets(ips: list[str] | None = None) -> None:
    """Пустой UDP на порт 9 (discard) каждому адресу своей /24: ОС делает ARP-запрос и заполняет таблицу."""
    for ip in (ips if ips is not None else local_ipv4()):
        pre = ip.rsplit(".", 1)[0]
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.setblocking(False)
            for i in range(1, 255):
                try:
                    s.sendto(b"", (f"{pre}.{i}", 9))
                except OSError:
                    pass


def port_open(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def find_host(mac: str, exclude: str | None = None, arp=arp_table, poke=poke_subnets,
              wait=time.sleep, local=local_ipv4) -> str | None:
    """Адрес устройства с этим MAC в сети ПК. `exclude` — старый адрес: ARP может помнить его из прежней сети."""
    want = norm_mac(mac)
    if len(want) != 12:
        return None
    nets = {ip.rsplit(".", 1)[0] for ip in local()}
    for attempt in (0, 1):
        hits = [ip for ip, m in arp() if norm_mac(m) == want and ip != exclude]
        own = [ip for ip in hits if ip.rsplit(".", 1)[0] in nets]
        if own or (hits and not nets):
            return (own or hits)[0]
        if attempt == 0:
            poke()
            wait(2.0)
    return None


def load_host_cache(path: Path | None = None) -> dict:
    try:
        data = json.loads((path or HOST_CACHE_PATH).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_host_cache(mac: str, host: str, path: Path | None = None) -> None:
    p = path or HOST_CACHE_PATH
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps({"mac": mac, "host": host, "found_at": time.time()}), encoding="utf-8")
        tmp.replace(p)
    except OSError as e:
        logger.warning("[TV] новый адрес не сохранён: %s", e)


def _connect_failure(e: BaseException) -> bool:
    """Соединение НЕ установлено: только тогда повтор безопасен (нажатие точно не дошло)."""
    ex = requests.exceptions
    if isinstance(e, (ex.ReadTimeout, ex.SSLError)):
        return False
    return isinstance(e, (ex.ConnectTimeout, ex.ConnectionError))


# --------------------------------------------------------------------------
# Клиент
# --------------------------------------------------------------------------

def wol_packet(mac: str) -> bytes:
    hexs = re.sub(r"[^0-9a-fA-F]", "", mac or "")
    if len(hexs) != 12:
        raise ValueError(f"MAC должен быть из 12 hex-цифр: {mac!r}")
    return b"\xff" * 6 + bytes.fromhex(hexs) * 16


def send_wol(mac: str, host: str | None, repeats: int = 5, gap_s: float = 1.0) -> list[str]:
    """Магический пакет на общий широковещательный адрес, на широковещательный адрес /24 ТВ и на сам ТВ, порты 9 и 7.
    Несколько повторов: Wi-Fi ТВ в режиме ожидания может пропустить одиночный пакет. 03.11 17:24: 3 серии за 1 с
    не разбудили ТВ (проба tv_wol_probe -- серии через 1 с -- будила): 5 серий через gap_s, всего ~4 с."""
    pkt = wol_packet(mac)
    targets = ["255.255.255.255"]
    if host and re.fullmatch(r"\d+\.\d+\.\d+\.\d+", host):
        targets += [host.rsplit(".", 1)[0] + ".255", host]
    sent = []
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        for i in range(repeats):
            for t in targets:
                for port in (9, 7):
                    try:
                        s.sendto(pkt, (t, port))
                        sent.append(f"{t}:{port}")
                    except OSError as e:
                        logger.warning("[TV] WoL %s:%s: %s", t, port, e)
            if i + 1 < repeats:
                time.sleep(gap_s)
    finally:
        s.close()
    if not sent:
        raise OSError("пакет пробуждения не отправлен ни на один адрес")
    logger.info("[TV] WoL sent=%d", len(sent))
    return sent


class TVClient:
    """Тонкая обёртка над API v6. Без состояния, кроме сессии и пары."""

    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        port: int = DEFAULT_PORT,
        timeout: float = 5.0,
        session: Any | None = None,
        mac: str | None = None,
        finder: Any | None = None,
    ) -> None:
        if not host:
            raise ValueError("host пуст: задайте TV_HOST в .env")
        self.host = host
        self.mac = mac or None             # без MAC поиск выключен (так и в старых тестах)
        self._finder = finder or find_host
        self._searched = False
        self.moved_from: str | None = None  # был другой адрес — сообщить человеку
        self.port = int(port)
        self.timeout = float(timeout)
        self._auth = HTTPDigestAuth(username, password)
        self._session = session if session is not None else requests.Session()

    # --- служебное ---

    def __repr__(self) -> str:
        # Пароль не попадает ни в repr, ни в traceback через repr аргументов.
        return f"<TVClient {self.host}:{self.port}>"

    __str__ = __repr__

    def url(self, path: str) -> str:
        return f"https://{self.host}:{self.port}/{API_VERSION}/{path.lstrip('/')}"

    def _rediscover(self) -> bool:
        """Один поиск на клиента: найден другой адрес с открытым портом — переключаемся и запоминаем."""
        if not self.mac or self._searched:
            return False
        self._searched = True
        ip = self._finder(self.mac, exclude=self.host)
        if not ip or not port_open(ip, self.port):
            logger.warning("[TV] поиск по MAC: телевизор не найден в сети ПК (был %s)", self.host)
            return False
        logger.warning("[TV] телевизор сменил адрес: %s -> %s", self.host, ip)
        self.moved_from, self.host = self.host, ip
        save_host_cache(self.mac, ip)
        return True

    def _request(self, method: str, path: str, payload: Any | None):
        url = self.url(path)
        logger.debug("[TV] %s %s", method, url)
        return self._session.request(method, url, json=payload, auth=self._auth,
                                     timeout=self.timeout, verify=False)

    def _call(self, method: str, path: str, payload: Any | None = None, want_json: bool | None = None) -> Any:
        try:
            try:
                resp = self._request(method, path, payload)
            except requests.exceptions.RequestException as e:
                if not (_connect_failure(e) and self._rediscover()):
                    raise
                resp = self._request(method, path, payload)       # один повтор по новому адресу
        except requests.exceptions.RequestException as e:
            # Текст исключения requests может содержать URL, но не пару:
            # digest не кладёт секрет в URL. Пароль здесь не утекает.
            raise TVTransportError(f"{method} {path}: {e}") from e

        status = getattr(resp, "status_code", None)
        if status == 401:
            raise TVAuthError(
                f"{method} {path}: 401 — пара отвергнута, нужен повторный пайринг"
            )
        if status in (403, 404):
            raise TVUnsupported(
                f"{method} {path}: {status} — эндпоинт недоступен на этой модели"
            )
        if status is not None and status >= 400:
            raise TVError(f"{method} {path}: HTTP {status}")

        if want_json if want_json is not None else method == "GET":
            try:
                return resp.json()
            except ValueError as e:
                raise TVError(f"{method} {path}: ответ не JSON") from e
        return None

    # --- кнопки ---

    def wake(self) -> list[str]:
        """POWER_ON (03.11): пакет пробуждения Wake-on-LAN на MAC ТВ. Работает, если в ТВ включено «Включение по
        сигналу Wi-Fi» -- проверено 03.11 16:53 и 17:14 (после 15 мин сна) через раздачу планшета, по кадру камеры.
        Ответа у пакета нет -- результат проверяет вызывающий (кадр камеры), а не этот метод."""
        if not self.mac:
            raise ValueError("не задан TV_MAC в .env -- без MAC разбудить ТВ по сети нельзя")
        return send_wol(self.mac, self.host)

    def send_key(self, key: str) -> None:
        """POST input/key. Неизвестное имя — ValueError до обращения к сети."""
        if key not in KEYS:
            raise ValueError(f"Неизвестная кнопка: {key!r}")
        self._call("POST", "input/key", {"key": key})
        logger.info("[TV] key=%s", key)

    def send_keys(self, keys: list[str], delay: float | None = None) -> None:
        """Последовательность кнопок с паузой между ними.

        Валидирует ВСЮ последовательность до первого вызова: иначе
        «Home, CursorDown, Опечатка» нажала бы первые две и упала
        посередине, оставив телевизор в промежуточном состоянии.

        `delay` — пауза ПОСЛЕ каждого нажатия, кроме последнего. После
        последнего не ждём: вызывающий код сам решает, нужна ли ему
        задержка перед чтением состояния, и платить за неё в каждом
        вызове незачем. По умолчанию берётся `TV_KEY_DELAY_S` из
        окружения, иначе DEFAULT_KEY_DELAY_S — чтобы настроить скорость
        под конкретный телевизор можно было без правки кода.
        """
        unknown = [k for k in keys if k not in KEYS]
        if unknown:
            raise ValueError(f"Неизвестные кнопки: {', '.join(map(repr, unknown))}")
        if delay is None:
            delay = float(os.getenv("TV_KEY_DELAY_S", str(DEFAULT_KEY_DELAY_S)))
        last = len(keys) - 1
        for i, k in enumerate(keys):
            self.send_key(k)
            if delay > 0 and i != last:
                time.sleep(delay)

    # --- состояние ---

    def get_powerstate(self) -> Any:
        """На «тв амби» -> 404 (TVUnsupported), проверено 27.10. Метод
        оставлен: путь есть в API v6 и работает на других моделях.
        Выключение на этой модели делается кнопкой `Standby`."""
        return self._call("GET", "powerstate")

    def set_powerstate(self, state: str) -> None:
        if state not in POWERSTATES:
            raise ValueError(f"powerstate должен быть одним из {sorted(POWERSTATES)}, получено {state!r}")
        self._call("POST", "powerstate", {"powerstate": state})

    def get_volume(self) -> Any:
        return self._call("GET", "audio/volume")

    def set_volume(self, level: int, muted: bool = False) -> int:
        """Ставит громкость, не выше предела контура. Возвращает ФАКТ.

        Границы берутся из ответа телевизора (`min`/`max`), а не из
        константы: диапазон зависит от модели, захардкоженные 0..60 на
        другом телевизоре молча обрезали бы громкость.

        Запрос выше предела не отвергается, а обрезается, и возвращается
        фактически выставленное значение — вызывающий код сравнивает его с
        запрошенным и сообщает человеку об обрезке. Исключение здесь
        означало бы, что «сделай громче» у предела просто не работает без
        объяснения.
        """
        current = self.get_volume() or {}
        lo = int(current.get("min", 0))
        hi = int(current.get("max", 60))
        if not lo <= int(level) <= hi:
            raise ValueError(f"Громкость {level} вне диапазона {lo}..{hi}")
        target = min(int(level), volume_ceiling(lo, hi))
        self._call("POST", "audio/volume", {"muted": bool(muted), "current": target})
        return target

    def get_current_activity(self) -> Any:
        return self._call("GET", "activities/current")

    def get_applications(self) -> Any:
        """На «тв амби» -> 403 (TVUnsupported), проверено 27.10."""
        return self._call("GET", "applications")

    def get_activities_tv(self) -> Any:
        return self._call("GET", "activities/tv")

    def get_ambilight_power(self) -> Any:
        return self._call("GET", "ambilight/power")

    def set_ambilight_power(self, on: bool) -> None:
        self._call("POST", "ambilight/power", {"power": "On" if on else "Off"})

    def get_ambilight_config(self) -> Any:
        return self._call("GET", "ambilight/currentconfiguration")

    def get_supported_styles(self) -> Any:
        return self._call("GET", "ambilight/supportedstyles")

    def set_ambilight_style(self, style: str, setting: str | None = None) -> None:
        """POST ambilight/currentconfiguration.

        Форма payload подтверждена пробой 27.10: `FOLLOW_AUDIO:VU_METER` и
        `FOLLOW_VIDEO:STANDARD` записались и прочитались обратно.

        `OFF` через этот путь НЕ работает: POST отдаёт 200, а GET остаётся
        на прежнем стиле (проверено). Поэтому выключение идёт через
        `ambilight/power` — для этого есть `set_ambilight_power(False)`,
        и `OFF` здесь отклоняется явно, чтобы вызывающий код не считал
        задачу выполненной по коду 200.
        """
        st = style.strip().upper()
        if st == "OFF":
            raise ValueError(
                "OFF не записывается через currentconfiguration (проверено): "
                "используйте set_ambilight_power(False)"
            )
        payload: dict[str, Any] = {"styleName": st, "isExpert": False}
        if setting:
            payload["menuSetting"] = setting.strip().upper()
        self._call("POST", "ambilight/currentconfiguration", payload)

    def get_ambilight_mode(self) -> Any:
        return self._call("GET", "ambilight/mode")

    def set_ambilight_mode(self, mode: str) -> None:
        """POST ambilight/mode.

        Поштучная запись пикселей работает ТОЛЬКО в `expert` — проверено:
        в `lounge` запись отдаёт 200, а чтение возвращает цвета, которые
        телевизор посчитал сам. Поэтому режим — обязательный шаг перед
        `set_pixels`, а не настройка «на всякий случай».
        """
        m = mode.strip().lower()
        if m not in AMBILIGHT_MODES:
            raise ValueError(
                f"режим должен быть одним из {sorted(AMBILIGHT_MODES)}, "
                f"получено {mode!r} (manual подменяется телевизором на lounge, "
                f"follow_video даёт 503)"
            )
        self._call("POST", "ambilight/mode", {"current": m})

    def get_topology(self) -> Any:
        """Сколько светодиодов на каждой стороне. На «тв амби»:
        left 3, top 7, right 3, bottom 0."""
        return self._call("GET", "ambilight/topology")

    def get_pixels(self) -> Any:
        return self._call("GET", "ambilight/cached")

    def set_pixels(self, layer: Mapping[str, Mapping[Any, Mapping[str, int]]]) -> None:
        """POST ambilight/cached: цвета отдельных светодиодов.

        Формат: `{"left": {0: {"r":..,"g":..,"b":..}}, ...}` — сторона,
        номер, цвет. Номера приводятся к строкам, потому что телевизор
        отдаёт и принимает их как ключи JSON.

        Проверяется ВСЁ до отправки: имена сторон и диапазон номеров по
        topology, значения цвета. Телевизор на мусор в теле отвечает 200,
        так что ошибка в индексе иначе проявилась бы просто погасшим
        светодиодом без всякой диагностики.
        """
        if not layer:
            raise ValueError("пустой набор пикселей")
        top = self.get_topology() or {}
        payload: dict[str, dict[str, dict[str, int]]] = {}
        for side, pixels in layer.items():
            if side not in SIDES:
                raise ValueError(f"неизвестная сторона {side!r}, допустимо {SIDES}")
            count = int(top.get(side, 0) or 0)
            if count == 0:
                raise ValueError(f"на стороне {side} нет светодиодов (topology)")
            out: dict[str, dict[str, int]] = {}
            for index, colour in pixels.items():
                i = int(index)
                if not 0 <= i < count:
                    raise ValueError(
                        f"{side}:{i} вне диапазона 0..{count - 1}")
                rgb = {}
                for ch in ("r", "g", "b"):
                    v = int(colour[ch])
                    if not 0 <= v <= 255:
                        raise ValueError(f"{side}:{i} {ch}={v} вне 0..255")
                    rgb[ch] = v
                out[str(i)] = rgb
            payload[side] = out
        self._call("POST", "ambilight/cached", {"layer1": payload})

    def get_lounge_color(self) -> Any:
        return self._call("GET", "ambilight/lounge")

    def set_lounge_color(self, hue: int, saturation: int, brightness: int) -> None:
        """POST ambilight/lounge. Подтверждено пробой: записали 0,100,255 —
        прочитали ровно это. Диапазоны берутся как в ответе устройства
        (0..255), сужать их на основании одного замера нет оснований."""
        for name, v in (("hue", hue), ("saturation", saturation), ("brightness", brightness)):
            if not 0 <= int(v) <= 255:
                raise ValueError(f"{name}={v} вне диапазона 0..255")
        self._call("POST", "ambilight/lounge", {
            "color": {
                "hue": int(hue),
                "saturation": int(saturation),
                "brightness": int(brightness),
            }
        })

    def volume_step(self, delta: int) -> int:
        """Изменяет громкость на `delta`, обрезая по диапазону устройства.

        Через GET+POST, а не кнопками VolumeUp/VolumeDown: у кнопок нет
        обратной связи (200 приходит на любое имя), а здесь можно вернуть
        фактическое новое значение и показать его человеку.
        """
        cur = self.get_volume() or {}
        lo = int(cur.get("min", 0))
        hi = int(cur.get("max", 60))
        now = int(cur.get("current", 0))
        # Потолок контура, не потолок устройства. Понижать можно всегда;
        # повышать — не выше предела. Если громкость УЖЕ выше предела
        # (выставлена пультом), запрос «громче» не трогает её, а не
        # обрушивает до предела: насильно убавлять звук человеку,
        # попросившему громче, — худший из возможных откликов.
        ceiling = volume_ceiling(lo, hi)
        target = now + int(delta)
        if int(delta) >= 0:
            target = min(target, max(ceiling, now))
        target = max(lo, min(hi, target))
        self._call("POST", "audio/volume", {
            "muted": bool(cur.get("muted", False)),
            "current": target,
        })
        return target

    def set_muted(self, muted: bool) -> None:
        cur = self.get_volume() or {}
        self._call("POST", "audio/volume", {
            "muted": bool(muted),
            "current": int(cur.get("current", 0)),
        })

    def get_system(self) -> Any:
        """`/6/system` на 1926. Не путать с `/1/system` на 1925 — тот
        доступен без пайринга и используется для пассивной проверки
        присутствия (см. документ разведки)."""
        return self._call("GET", "system")

    # --- настройки меню (menuitems/settings), 30.10 ---
    # Проверено на «тв амби» (50PUS9010, Titan OS) 29–30.10: чтение `POST menuitems/settings/current`
    # {"nodes":[{"nodeid":N}]} -> {"values":[{"value":{Nodeid, Controllable, Available, data}}]};
    # запись `POST menuitems/settings/update` {"values":[{"value": <прочитанный объект с новым data>}]}
    # — яркость 710: 4->3->4, подтверждено чтением. Узлы без значения отвечают {"value": {}}.
    # Запись всегда проверяется чтением: 200 у JointSpace не доказывает, что значение принято
    # (так было с ambilight/currentconfiguration OFF).

    def read_settings(self, nodes: list[int]) -> dict[int, dict]:
        """{nodeid: value}. Пустые ответы ({"value": {}}) пропускаются."""
        body = {"nodes": [{"nodeid": int(n)} for n in nodes]}
        r = self._call("POST", "menuitems/settings/current", body, want_json=True) or {}
        out: dict[int, dict] = {}
        for e in r.get("values") or []:
            v = e.get("value") if isinstance(e, dict) else None
            if not isinstance(v, dict):
                continue
            n = next((v[k] for k in v if k.lower() == "nodeid"), None)
            if n is not None:
                out[int(n)] = v
        return out

    def _setting(self, node: int) -> dict:
        v = self.read_settings([node]).get(int(node))
        if not v:
            raise TVUnsupported(f"настройка {node}: телевизор не отдаёт значение")
        if v.get("Controllable") is False or v.get("Available") is False:
            raise TVUnsupported(f"настройка {node}: сейчас недоступна для изменения")
        return v

    def _write_setting(self, item: dict, data: dict) -> dict:
        new = dict(item)
        new["data"] = data
        self._call("POST", "menuitems/settings/update", {"values": [{"value": new}]})
        time.sleep(float(os.getenv("TV_SETTING_SETTLE_S", "1.0")))
        node = next(item[k] for k in item if k.lower() == "nodeid")
        return (self.read_settings([node]).get(int(node)) or {}).get("data") or {}

    def get_setting_value(self, node: int) -> int:
        d = self._setting(node).get("data") or {}
        if "value" in d:
            return int(d["value"])
        if "selected_item" in d:
            return int(d["selected_item"])
        raise TVError(f"настройка {node}: в ответе нет значения")

    def set_setting_value(self, node: int, value: int, lo: int, hi: int) -> int:
        """Ползунок: значение обрезается до [lo; hi]. Возвращает ФАКТ после записи (чтением)."""
        item = self._setting(node)
        d = dict(item.get("data") or {})
        if "value" not in d:
            raise TVError(f"настройка {node} — не ползунок")
        target = max(int(lo), min(int(hi), int(value)))
        got = self._write_setting(item, {**d, "value": target}).get("value")
        if got is None or int(got) != target:
            raise TVError(f"настройка {node}: телевизор не принял {target} (прочитано {got})")
        logger.info("[TV] setting %s=%s", node, target)
        return target

    def set_setting_enum(self, node: int, enum_id: int) -> int:
        """Список: только вариант, который телевизор объявил доступным. Возвращает ФАКТ."""
        item = self._setting(node)
        d = dict(item.get("data") or {})
        allowed = {int(e["enum_id"]) for e in d.get("enum_values") or []
                   if e.get("available", True) and e.get("controllable", True)}
        if allowed and int(enum_id) not in allowed:
            raise ValueError(f"настройка {node}: вариант {enum_id} недоступен, есть {sorted(allowed)}")
        got = self._write_setting(item, {**d, "selected_item": int(enum_id)}).get("selected_item")
        if got is None or int(got) != int(enum_id):
            raise TVError(f"настройка {node}: телевизор не принял вариант {enum_id} (прочитано {got})")
        logger.info("[TV] setting %s=enum %s", node, enum_id)
        return int(enum_id)

    # --- фабрика ---

    @classmethod
    def from_env(
        cls,
        creds_path: Path | None = None,
        session: Any | None = None,
    ) -> "TVClient":
        """Статика из `.env`, динамика из `data\\tv_creds.json` и `data\\tv_host.json`.

        29.10: при заданном TV_MAC адрес, найденный поиском по MAC, важнее TV_HOST — но только если
        в файле тот же MAC (замена телевизора не должна унаследовать чужой адрес)."""
        host = os.getenv("TV_HOST", "")
        mac = os.getenv("TV_MAC", "").strip() or None
        if mac:
            cached = load_host_cache()
            if norm_mac(cached.get("mac")) == norm_mac(mac) and cached.get("host"):
                host = str(cached["host"])
        if not host:
            raise ValueError("TV_HOST не задан в .env")
        port = int(os.getenv("TV_PORT", str(DEFAULT_PORT)))
        timeout = float(os.getenv("TV_TIMEOUT_S", "5.0"))
        creds = load_creds(creds_path)
        return cls(
            host=host,
            username=creds["username"],
            password=creds["password"],
            port=port,
            timeout=timeout,
            session=session,
            mac=mac,
        )


def main() -> int:
    """CLI для ручной проверки: `python src\\tv_control.py Home`."""
    import sys

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = sys.argv[1:]
    try:
        tv = TVClient.from_env()
    except (TVError, ValueError) as e:
        print(f"FAIL: {e}")
        return 2

    if args:
        try:
            tv.send_keys(args)
        except (TVError, ValueError) as e:
            print(f"FAIL: {e}")
            return 1
        print(f"OK: {len(args)} кнопк(и) отправлено")
        return 0

    # Чтение состояния: каждый пункт отдельно. TVUnsupported по одному пути
    # не должен обрывать весь отчёт — набор эндпоинтов модельный, и отчёт
    # «что вообще доступно» полезнее, чем падение на первом 404.
    probes = (
        ("name      ", lambda: (tv.get_system() or {}).get("name")),
        ("volume    ", tv.get_volume),
        ("activity  ", tv.get_current_activity),
        ("ambilight ", tv.get_ambilight_power),
        ("powerstate", tv.get_powerstate),
    )
    failed = 0
    for label, fn in probes:
        try:
            print(f"{label} :", fn())
        except TVUnsupported as e:
            print(f"{label} : n/a ({e})")
        except (TVError, ValueError) as e:
            print(f"{label} : FAIL {e}")
            failed += 1
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
