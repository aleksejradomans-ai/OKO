"""test_tv_control.py — логика TV-контура без сети.

ENGINE ISOLATION: ни один тест не открывает сокет. `requests.Session`
и `requests.request` в обоих модулях подменены так, что любой реальный
вызов даёт AssertionError — то есть тест, забывший передать фейковую
сессию, падает, а не стучится по сети к боевому телевизору.

Проверяется:
  1. URL: схема, порт, версия API, склейка пути.
  2. digest-auth с парой из `tv_creds.json`.
  3. Whitelist кнопок: неизвестное имя -> ValueError ДО сети.
  4. Классификация сбоев: 401 -> TVAuthError, таймаут -> TVTransportError
     (не наружу сырым requests-исключением).
  5. Креденшы: отсутствие файла -> TVNotPaired; атомарная запись; roundtrip.
  6. Пароль не попадает в repr/str клиента и в лог.
  7. Пайринг: порядок конкатенации в подписи, форма payload, валидация PIN.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging

import pytest
import requests


# --------------------------------------------------------------------------
# Инфраструктура
# --------------------------------------------------------------------------

class FakeResponse:
    def __init__(self, status_code=200, payload=None, text="{}"):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeSession:
    """Записывает вызовы, возвращает заготовленные ответы."""

    def __init__(self, responses=None):
        self.calls: list[dict] = []
        self._responses = list(responses or [])

    def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        if not self._responses:
            return FakeResponse(200, {})
        r = self._responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    @property
    def last(self) -> dict:
        return self.calls[-1]


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Любая попытка настоящего запроса — провал теста, а не запрос к TV."""
    def _boom(*_a, **_k):
        raise AssertionError("реальный сетевой вызов в юнит-тесте TV")

    monkeypatch.setattr(requests, "request", _boom, raising=False)
    monkeypatch.setattr(requests, "post", _boom, raising=False)
    monkeypatch.setattr(requests, "get", _boom, raising=False)
    monkeypatch.setattr(requests, "Session", _boom)


@pytest.fixture
def tvmod(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_TOKEN", "fake_token_for_tests")
    monkeypatch.setenv("AI_BASE_DIR", str(tmp_path))
    import tv_control as mod
    monkeypatch.setattr(mod, "CREDS_PATH", tmp_path / "tv_creds.json")
    return mod


@pytest.fixture
def pairmod(tvmod, monkeypatch):
    import tv_pair as mod
    return mod


@pytest.fixture
def client(tvmod):
    s = FakeSession()
    c = tvmod.TVClient("10.0.0.5", "oko_user", "s3cret", session=s)
    return c, s


# --------------------------------------------------------------------------
# 1. URL и auth
# --------------------------------------------------------------------------

def test_url_scheme_port_version(client):
    c, _ = client
    assert c.url("input/key") == "https://10.0.0.5:1926/6/input/key"
    assert c.url("/audio/volume") == "https://10.0.0.5:1926/6/audio/volume"


def test_custom_port(tvmod):
    c = tvmod.TVClient("h", "u", "p", port=1925, session=FakeSession())
    assert c.url("system") == "https://h:1925/6/system"


def test_empty_host_rejected(tvmod):
    with pytest.raises(ValueError):
        tvmod.TVClient("", "u", "p", session=FakeSession())


def test_digest_auth_and_verify_off(client):
    c, s = client
    c.send_key("Home")
    auth = s.last["auth"]
    assert isinstance(auth, requests.auth.HTTPDigestAuth)
    assert (auth.username, auth.password) == ("oko_user", "s3cret")
    assert s.last["verify"] is False
    assert s.last["json"] == {"key": "Home"}
    assert s.last["method"] == "POST"


# --------------------------------------------------------------------------
# 2. Кнопки
# --------------------------------------------------------------------------

@pytest.mark.parametrize("key", ["Home", "Standby", "CursorUp", "VolumeDown", "Digit7"])
def test_known_keys_sent(client, key):
    c, s = client
    c.send_key(key)
    assert s.last["json"] == {"key": key}


@pytest.mark.parametrize("key", ["home", "HOME", "Power", "", "input/key", "Digit10"])
def test_unknown_key_rejected_without_network(client, key):
    c, s = client
    with pytest.raises(ValueError):
        c.send_key(key)
    assert s.calls == []


def test_send_keys_validates_whole_sequence_first(client):
    """«Home, CursorDown, опечатка» не должно нажать первые две."""
    c, s = client
    with pytest.raises(ValueError):
        c.send_keys(["Home", "CursorDown", "Nope"])
    assert s.calls == []


def test_send_keys_order_preserved(client):
    c, s = client
    c.send_keys(["Home", "CursorDown", "Confirm"])
    assert [x["json"]["key"] for x in s.calls] == ["Home", "CursorDown", "Confirm"]


# --------------------------------------------------------------------------
# 3. Классификация сбоев
# --------------------------------------------------------------------------

def test_401_is_auth_error(tvmod):
    s = FakeSession([FakeResponse(401)])
    c = tvmod.TVClient("h", "u", "p", session=s)
    with pytest.raises(tvmod.TVAuthError):
        c.send_key("Home")


@pytest.mark.parametrize("status", [403, 404])
def test_403_404_are_unsupported_not_auth(tvmod, status):
    """Проверено на живом телевизоре 27.10: с ОДНОЙ И ТОЙ ЖЕ парой
    `system`/`audio/volume` дают 200, а `powerstate` 404 и `applications`
    403. Значит 403/404 — свойство модели, а не отвергнутая пара; если бы
    они попадали в TVAuthError, бот предлагал бы повторный пайринг на
    штатном поведении устройства."""
    s = FakeSession([FakeResponse(status)])
    c = tvmod.TVClient("h", "u", "p", session=s)
    with pytest.raises(tvmod.TVUnsupported) as ei:
        c.send_key("Home")
    assert not isinstance(ei.value, tvmod.TVAuthError)


@pytest.mark.parametrize("status", [400, 500, 502])
def test_other_http_is_plain_tv_error(tvmod, status):
    s = FakeSession([FakeResponse(status)])
    c = tvmod.TVClient("h", "u", "p", session=s)
    with pytest.raises(tvmod.TVError) as ei:
        c.send_key("Home")
    assert not isinstance(ei.value, (tvmod.TVAuthError, tvmod.TVUnsupported))


def test_unsupported_is_tv_error_subclass(tvmod):
    """Вызывающий код в bot.py ловит TVError — TVUnsupported не должен
    пролетать мимо этого перехвата."""
    assert issubclass(tvmod.TVUnsupported, tvmod.TVError)
    assert not issubclass(tvmod.TVUnsupported, tvmod.TVAuthError)


@pytest.mark.parametrize("exc", [
    requests.exceptions.ConnectTimeout("t"),
    requests.exceptions.ConnectionError("refused"),
    requests.exceptions.ReadTimeout("slow"),
    requests.exceptions.SSLError("tls"),
])
def test_network_errors_wrapped(tvmod, exc):
    """Наружу не должно вылетать сырое requests-исключение: вызывающий код
    в bot.py ловит TVError, а не requests.*"""
    s = FakeSession([exc])
    c = tvmod.TVClient("h", "u", "p", session=s)
    with pytest.raises(tvmod.TVTransportError):
        c.send_key("Home")


def test_get_non_json_is_tv_error(tvmod):
    s = FakeSession([FakeResponse(200, payload=None)])
    c = tvmod.TVClient("h", "u", "p", session=s)
    with pytest.raises(tvmod.TVError):
        c.get_powerstate()


# --------------------------------------------------------------------------
# 4. Состояние
# --------------------------------------------------------------------------

def test_powerstate_whitelist(client):
    c, s = client
    c.set_powerstate("Standby")
    assert s.last["json"] == {"powerstate": "Standby"}
    with pytest.raises(ValueError):
        c.set_powerstate("off")


def test_volume_range_from_device_not_hardcoded(tvmod, monkeypatch):
    """Диапазон берётся из ответа телевизора, а не из константы.

    Переписан 27.10 вместе с появлением предела контура: раньше тест
    проверял, что на модели с max=100 проходит 80. Теперь 80 обрезается
    до половины диапазона, поэтому проверяется то же свойство иначе — что
    ПРЕДЕЛ считается от модельного максимума (50 при max=100, не 30).
    Если диапазон захардкодить как 0..60, здесь выйдет 30, и тест упадёт.
    """
    monkeypatch.delenv(tvmod.VOLUME_LIMIT_ENV, raising=False)
    s = FakeSession([FakeResponse(200, {"min": 0, "max": 100, "current": 10}),
                     FakeResponse(200, {})])
    c = tvmod.TVClient("h", "u", "p", session=s)
    assert c.set_volume(80) == 50
    assert s.last["json"] == {"muted": False, "current": 50}


def test_volume_out_of_device_range_rejected(tvmod):
    s = FakeSession([FakeResponse(200, {"min": 0, "max": 60, "current": 10})])
    c = tvmod.TVClient("h", "u", "p", session=s)
    with pytest.raises(ValueError):
        c.set_volume(80)
    assert len(s.calls) == 1  # только GET, POST не ушёл


# --------------------------------------------------------------------------
# 5. Креденшы
# --------------------------------------------------------------------------

def test_load_creds_missing_file_is_not_paired(tvmod, tmp_path):
    with pytest.raises(tvmod.TVNotPaired):
        tvmod.load_creds(tmp_path / "nope.json")


def test_load_creds_broken_json_is_not_paired(tvmod, tmp_path):
    p = tmp_path / "broken.json"
    p.write_text("{not json", encoding="utf-8")
    with pytest.raises(tvmod.TVNotPaired):
        tvmod.load_creds(p)


@pytest.mark.parametrize("data", [
    {"username": "u"},
    {"password": "p"},
    {"username": "", "password": "p"},
    {},
])
def test_load_creds_incomplete_is_not_paired(tvmod, tmp_path, data):
    p = tmp_path / "part.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(tvmod.TVNotPaired):
        tvmod.load_creds(p)


def test_save_then_load_roundtrip(tvmod, tmp_path):
    p = tmp_path / "sub" / "tv_creds.json"
    tvmod.save_creds({"username": "dev123", "password": "key456", "host": "h"}, p)
    got = tvmod.load_creds(p)
    assert (got["username"], got["password"]) == ("dev123", "key456")
    assert not list(p.parent.glob("*.tmp"))  # временный файл убран


def test_creds_default_path_is_under_base_dir_not_src(tvmod):
    """Файл обязан лежать в data/ (BASE_DIR), иначе попадёт в git.

    Проверяется исходное выражение модуля (`config.BASE_DIR / "tv_creds.json"`),
    а не подменённое фикстурой значение: именно оно решает, окажется ли
    секрет внутри репозитория.
    """
    import ast
    import pathlib

    import config

    src = pathlib.Path(tvmod.__file__).read_text(encoding="utf-8")
    assigns = [
        n for n in ast.walk(ast.parse(src))
        if isinstance(n, ast.AnnAssign)
        and getattr(n.target, "id", None) == "CREDS_PATH"
    ]
    assert len(assigns) == 1, "CREDS_PATH должен задаваться ровно один раз"
    expr = ast.unparse(assigns[0].value)
    assert expr == "config.BASE_DIR / 'tv_creds.json'", expr

    default = config.BASE_DIR / "tv_creds.json"
    assert default.parent == config.BASE_DIR
    assert default.parent != pathlib.Path(tvmod.__file__).resolve().parent


def test_from_env_requires_host(tvmod, monkeypatch):
    monkeypatch.delenv("TV_HOST", raising=False)
    with pytest.raises(ValueError):
        tvmod.TVClient.from_env(session=FakeSession())


def test_from_env_uses_file_pair_and_env_host(tvmod, tmp_path, monkeypatch):
    p = tmp_path / "tv_creds.json"
    tvmod.save_creds({"username": "dev", "password": "pw"}, p)
    monkeypatch.setenv("TV_HOST", "10.0.0.5")
    monkeypatch.setenv("TV_PORT", "1926")
    s = FakeSession()
    c = tvmod.TVClient.from_env(creds_path=p, session=s)
    c.send_key("Home")
    assert s.last["url"].startswith("https://10.0.0.5:1926/6/")
    assert s.last["auth"].username == "dev"


def test_from_env_without_pair_is_not_paired(tvmod, tmp_path, monkeypatch):
    monkeypatch.setenv("TV_HOST", "10.0.0.5")
    with pytest.raises(tvmod.TVNotPaired):
        tvmod.TVClient.from_env(creds_path=tmp_path / "absent.json", session=FakeSession())


# --------------------------------------------------------------------------
# 6. Пароль не утекает
# --------------------------------------------------------------------------

def test_password_not_in_repr(client):
    c, _ = client
    assert "s3cret" not in repr(c)
    assert "s3cret" not in str(c)
    assert "10.0.0.5" in repr(c)


def test_password_not_in_logs(tvmod, caplog):
    s = FakeSession()
    c = tvmod.TVClient("10.0.0.5", "oko_user", "s3cret", session=s)
    with caplog.at_level(logging.DEBUG, logger="oko.tv"):
        c.send_key("Home")
    assert caplog.records
    assert "s3cret" not in caplog.text


def test_password_not_in_transport_error_text(tvmod):
    s = FakeSession([requests.exceptions.ConnectionError("refused")])
    c = tvmod.TVClient("10.0.0.5", "oko_user", "s3cret", session=s)
    with pytest.raises(tvmod.TVTransportError) as ei:
        c.send_key("Home")
    assert "s3cret" not in str(ei.value)


# --------------------------------------------------------------------------
# 7. Пайринг
# --------------------------------------------------------------------------

def test_signature_concat_order(pairmod):
    """timestamp + pin, не наоборот: телевизор на обратный порядок отвечает
    тем же кодом, что на неверный PIN — по ответу не отладить."""
    key = base64.b64decode(pairmod.SECRET_B64)
    expected = base64.b64encode(
        hmac.new(key, b"998877661234", hashlib.sha1).digest()
    ).decode()
    assert pairmod.sign(99887766, "1234") == expected
    assert pairmod.sign(99887766, "1234") != pairmod.sign("1234", "99887766")


def test_device_id_shape(pairmod):
    allowed = set("abcdefghijklmnopqrstuvwxyz0123456789")
    ids = {pairmod.new_device_id() for _ in range(50)}
    assert len(ids) == 50, "device_id должен быть случайным"
    assert all(len(i) == 16 and set(i) <= allowed for i in ids)


def test_request_pair_payload_and_url(pairmod):
    s = FakeSession([FakeResponse(200, {"auth_key": "k", "timestamp": 1, "timeout": 60})])
    out = pairmod.request_pair("10.0.0.5", "dev0123456789ab", session=s)
    assert out["auth_key"] == "k"
    assert s.last["url"] == "https://10.0.0.5:1926/6/pair/request"
    assert s.last["json"]["device"]["id"] == "dev0123456789ab"
    assert set(s.last["json"]["scope"]) == {"read", "write", "control"}
    assert s.last["verify"] is False
    assert "auth" not in s.last  # request идёт БЕЗ авторизации


def test_request_pair_missing_fields_is_tv_error(pairmod):
    s = FakeSession([FakeResponse(200, {"timestamp": 1})])
    with pytest.raises(pairmod.TVError):
        pairmod.request_pair("h", "dev", session=s)


def test_request_pair_network_wrapped(pairmod):
    s = FakeSession([requests.exceptions.ConnectionError("x")])
    with pytest.raises(pairmod.TVTransportError):
        pairmod.request_pair("h", "dev", session=s)


def test_grant_payload_shape_and_digest(pairmod):
    s = FakeSession([FakeResponse(200, {})])
    pairmod.grant_pair("h", "dev", "authkey", 99887766, "1234", session=s)
    body = s.last["json"]
    assert body["auth"]["auth_AppId"] == "1"
    assert body["auth"]["pin"] == "1234"
    assert body["auth"]["auth_timestamp"] == 99887766
    assert body["auth"]["auth_signature"] == pairmod.sign(99887766, "1234")
    assert body["device"]["id"] == "dev"
    # digest на grant: username=device_id, password=auth_key
    assert (s.last["auth"].username, s.last["auth"].password) == ("dev", "authkey")
    assert s.last["url"].endswith("/6/pair/grant")


def test_grant_bad_pin_is_tv_error(pairmod):
    s = FakeSession([FakeResponse(401, {})])
    with pytest.raises(pairmod.TVError):
        pairmod.grant_pair("h", "dev", "k", 1, "0000", session=s)


@pytest.mark.parametrize("pin", ["12", "12345", "abcd", "", "12 34"])
def test_pair_rejects_malformed_pin_before_grant(pairmod, tmp_path, pin):
    s = FakeSession([FakeResponse(200, {"auth_key": "k", "timestamp": 1, "timeout": 60})])
    with pytest.raises(ValueError):
        pairmod.pair("h", creds_path=tmp_path / "c.json", session=s, pin_reader=lambda: pin)
    assert len(s.calls) == 1  # grant не отправлен
    assert not (tmp_path / "c.json").exists()


def test_pair_happy_path_saves_pair(pairmod, tvmod, tmp_path):
    s = FakeSession([
        FakeResponse(200, {"auth_key": "KEY", "timestamp": 99887766, "timeout": 60}),
        FakeResponse(200, {}),
    ])
    p = pairmod.pair("10.0.0.5", creds_path=tmp_path / "c.json",
                     session=s, pin_reader=lambda: "4321")
    creds = tvmod.load_creds(p)
    assert creds["password"] == "KEY"
    assert creds["username"] == s.calls[0]["json"]["device"]["id"]
    assert creds["host"] == "10.0.0.5"
    assert "paired_at" in creds
    # тот же device_id на обоих шагах, иначе телевизор отвергнет пару
    assert s.calls[0]["json"]["device"]["id"] == s.calls[1]["json"]["device"]["id"]


def test_pair_grant_failure_does_not_write_creds(pairmod, tmp_path):
    s = FakeSession([
        FakeResponse(200, {"auth_key": "KEY", "timestamp": 1, "timeout": 60}),
        FakeResponse(401, {}),
    ])
    with pytest.raises(pairmod.TVError):
        pairmod.pair("h", creds_path=tmp_path / "c.json", session=s, pin_reader=lambda: "1111")
    assert not (tmp_path / "c.json").exists()


# --------------------------------------------------------------------------
# 8. Ambilight и реальная карта эндпоинтов (проба 27.10)
# --------------------------------------------------------------------------

def test_ambilight_power_get(tvmod):
    s = FakeSession([FakeResponse(200, {"power": "On"})])
    c = tvmod.TVClient("h", "u", "p", session=s)
    assert c.get_ambilight_power() == {"power": "On"}
    assert s.last["url"].endswith("/6/ambilight/power")
    assert s.last["method"] == "GET"


@pytest.mark.parametrize(("on", "expected"), [(True, "On"), (False, "Off")])
def test_ambilight_power_set(tvmod, on, expected):
    s = FakeSession([FakeResponse(200, {})])
    c = tvmod.TVClient("h", "u", "p", session=s)
    c.set_ambilight_power(on)
    assert s.last["json"] == {"power": expected}
    assert s.last["method"] == "POST"


def test_ambilight_config_and_activities_tv(tvmod):
    s = FakeSession([
        FakeResponse(200, {"styleName": "FOLLOW_VIDEO"}),
        FakeResponse(200, {"channel": {}}),
    ])
    c = tvmod.TVClient("h", "u", "p", session=s)
    assert c.get_ambilight_config()["styleName"] == "FOLLOW_VIDEO"
    assert c.get_activities_tv() == {"channel": {}}
    assert [x["url"].rsplit("/6/", 1)[1] for x in s.calls] == [
        "ambilight/currentconfiguration", "activities/tv",
    ]


def test_volume_max_60_from_live_device(tvmod):
    """Живой ответ 27.10: min=0, max=60, current=0. 61 должно отклоняться."""
    s = FakeSession([FakeResponse(200, {"muted": False, "current": 0, "min": 0, "max": 60})])
    c = tvmod.TVClient("h", "u", "p", session=s)
    with pytest.raises(ValueError):
        c.set_volume(61)
    assert len(s.calls) == 1


def test_ambilight_style_off_is_rejected(tvmod):
    """OFF через currentconfiguration молча не применяется (живая проба
    27.10: POST 200, GET остался FOLLOW_VIDEO). Метод обязан отклонять
    его ДО сети, иначе вызывающий код считает задачу выполненной."""
    s = FakeSession()
    c = tvmod.TVClient("h", "u", "p", session=s)
    for spec in ("OFF", "off", " Off "):
        with pytest.raises(ValueError):
            c.set_ambilight_style(spec)
    assert s.calls == []


def test_ambilight_style_payload_matches_live_form(tvmod):
    """Форма подтверждена пробой: styleName + isExpert + menuSetting."""
    s = FakeSession([FakeResponse(200, {})])
    c = tvmod.TVClient("h", "u", "p", session=s)
    c.set_ambilight_style("follow_audio", "vu_meter")
    assert s.last["json"] == {
        "styleName": "FOLLOW_AUDIO", "isExpert": False, "menuSetting": "VU_METER",
    }
    assert s.last["url"].endswith("/6/ambilight/currentconfiguration")


def test_lounge_color_roundtrip_and_range(tvmod):
    s = FakeSession([FakeResponse(200, {})])
    c = tvmod.TVClient("h", "u", "p", session=s)
    c.set_lounge_color(0, 100, 255)
    assert s.last["json"] == {"color": {"hue": 0, "saturation": 100, "brightness": 255}}
    for bad in ((256, 0, 0), (0, -1, 0), (0, 0, 300)):
        with pytest.raises(ValueError):
            c.set_lounge_color(*bad)


def test_volume_step_clamps_to_device_range(tvmod, monkeypatch):
    """Обрезка по устройству проверяется ВНИЗ: вверх теперь ограничивает
    предел контура, а не максимум телевизора (см. блок про предел ниже).
    """
    s = FakeSession([
        FakeResponse(200, {"muted": False, "current": 3, "min": 0, "max": 60}),
        FakeResponse(200, {}),
    ])
    c = tvmod.TVClient("h", "u", "p", session=s)
    assert c.volume_step(-10) == 0
    assert s.last["json"] == {"muted": False, "current": 0}


def test_volume_step_keeps_mute_flag(tvmod):
    """Шаг громкости не должен снимать mute как побочный эффект."""
    s = FakeSession([
        FakeResponse(200, {"muted": True, "current": 10, "min": 0, "max": 60}),
        FakeResponse(200, {}),
    ])
    c = tvmod.TVClient("h", "u", "p", session=s)
    c.volume_step(-3)
    assert s.last["json"] == {"muted": True, "current": 7}


def test_set_muted_preserves_level(tvmod):
    s = FakeSession([
        FakeResponse(200, {"muted": False, "current": 23, "min": 0, "max": 60}),
        FakeResponse(200, {}),
    ])
    c = tvmod.TVClient("h", "u", "p", session=s)
    c.set_muted(True)
    assert s.last["json"] == {"muted": True, "current": 23}


# --------------------------------------------------------------------------
# 9. Паузы между кнопками (асинхронная отрисовка интерфейса)
# --------------------------------------------------------------------------

def test_send_keys_waits_between_but_not_after_last(tvmod, monkeypatch):
    """Телевизор отвечает 200 до перерисовки экрана, поэтому без паузы
    следующее нажатие уходит в ещё не обновлённый интерфейс и теряется —
    и отличить потерю от успеха по коду ответа нельзя.

    После ПОСЛЕДНЕЙ кнопки пауза не нужна: решение ждать принимает
    вызывающий код, а не каждый вызов по умолчанию.
    """
    slept: list[float] = []
    monkeypatch.setattr(tvmod.time, "sleep", lambda s: slept.append(s))
    s = FakeSession()
    c = tvmod.TVClient("h", "u", "p", session=s)
    c.send_keys(["Home", "CursorDown", "Confirm"], delay=0.5)
    assert slept == [0.5, 0.5]
    assert [x["json"]["key"] for x in s.calls] == ["Home", "CursorDown", "Confirm"]


def test_single_key_in_sequence_has_no_sleep(tvmod, monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr(tvmod.time, "sleep", lambda s: slept.append(s))
    c = tvmod.TVClient("h", "u", "p", session=FakeSession())
    c.send_keys(["Info"], delay=0.5)
    assert slept == []


def test_key_delay_from_env(tvmod, monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr(tvmod.time, "sleep", lambda s: slept.append(s))
    monkeypatch.setenv("TV_KEY_DELAY_S", "1.25")
    c = tvmod.TVClient("h", "u", "p", session=FakeSession())
    c.send_keys(["Home", "Back"])
    assert slept == [1.25]


def test_key_delay_default_is_nonzero(tvmod, monkeypatch):
    """Дефолт обязан быть больше нуля: нулевая пауза — это как раз тот
    случай, когда последовательность отправляется быстрее, чем телевизор
    перерисовывает меню."""
    monkeypatch.delenv("TV_KEY_DELAY_S", raising=False)
    assert tvmod.DEFAULT_KEY_DELAY_S > 0
    slept: list[float] = []
    monkeypatch.setattr(tvmod.time, "sleep", lambda s: slept.append(s))
    c = tvmod.TVClient("h", "u", "p", session=FakeSession())
    c.send_keys(["Home", "Back"])
    assert slept == [tvmod.DEFAULT_KEY_DELAY_S]


def test_zero_delay_explicitly_allowed(tvmod, monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr(tvmod.time, "sleep", lambda s: slept.append(s))
    c = tvmod.TVClient("h", "u", "p", session=FakeSession())
    c.send_keys(["Home", "Back"], delay=0)
    assert slept == []


def test_bad_key_in_sequence_sleeps_nothing(tvmod, monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr(tvmod.time, "sleep", lambda s: slept.append(s))
    s = FakeSession()
    c = tvmod.TVClient("h", "u", "p", session=s)
    with pytest.raises(ValueError):
        c.send_keys(["Home", "Nope"], delay=0.5)
    assert s.calls == [] and slept == []


# --------------------------------------------------------------------------
# 10. Режимы подсветки и поштучная запись (expert)
# --------------------------------------------------------------------------

TOPOLOGY_LIVE = {"layers": 1, "left": 3, "top": 7, "right": 3, "bottom": 0}


@pytest.mark.parametrize("mode", ["internal", "expert", "lounge", "EXPERT", " expert "])
def test_ambilight_mode_accepts_confirmed_values(tvmod, mode):
    s = FakeSession([FakeResponse(200, {})])
    c = tvmod.TVClient("h", "u", "p", session=s)
    c.set_ambilight_mode(mode)
    assert s.last["json"] == {"current": mode.strip().lower()}
    assert s.last["url"].endswith("/6/ambilight/mode")


@pytest.mark.parametrize("mode", ["manual", "follow_video", "lounge_light", ""])
def test_ambilight_mode_rejects_unconfirmed(tvmod, mode):
    """Перебор на живом телевизоре 27.10: `manual` подменяется на `lounge`,
    `follow_video` даёт 503. Пропускать их — значит молча оказываться не в
    том режиме, в котором поштучная запись не работает."""
    s = FakeSession()
    c = tvmod.TVClient("h", "u", "p", session=s)
    with pytest.raises(ValueError):
        c.set_ambilight_mode(mode)
    assert s.calls == []


def test_set_pixels_payload_shape(tvmod):
    s = FakeSession([FakeResponse(200, TOPOLOGY_LIVE), FakeResponse(200, {})])
    c = tvmod.TVClient("h", "u", "p", session=s)
    c.set_pixels({"top": {3: {"r": 0, "g": 0, "b": 255}}})
    assert s.last["url"].endswith("/6/ambilight/cached")
    assert s.last["json"] == {"layer1": {"top": {"3": {"r": 0, "g": 0, "b": 255}}}}


def test_set_pixels_index_keys_become_strings(tvmod):
    s = FakeSession([FakeResponse(200, TOPOLOGY_LIVE), FakeResponse(200, {})])
    c = tvmod.TVClient("h", "u", "p", session=s)
    c.set_pixels({"left": {0: {"r": 1, "g": 2, "b": 3},
                           "1": {"r": 4, "g": 5, "b": 6}}})
    assert set(s.last["json"]["layer1"]["left"]) == {"0", "1"}


@pytest.mark.parametrize("index", [3, 7, -1, 99])
def test_set_pixels_index_out_of_topology(tvmod, index):
    """Телевизор на мусор в теле отвечает 200, и ошибка в индексе иначе
    проявилась бы просто погасшим светодиодом без диагностики."""
    s = FakeSession([FakeResponse(200, TOPOLOGY_LIVE)])
    c = tvmod.TVClient("h", "u", "p", session=s)
    with pytest.raises(ValueError):
        c.set_pixels({"left": {index: {"r": 0, "g": 0, "b": 0}}})
    assert len(s.calls) == 1  # только GET topology, POST не ушёл


def test_set_pixels_side_without_leds(tvmod):
    s = FakeSession([FakeResponse(200, TOPOLOGY_LIVE)])
    c = tvmod.TVClient("h", "u", "p", session=s)
    with pytest.raises(ValueError):
        c.set_pixels({"bottom": {0: {"r": 0, "g": 0, "b": 0}}})


def test_set_pixels_unknown_side(tvmod):
    s = FakeSession([FakeResponse(200, TOPOLOGY_LIVE)])
    c = tvmod.TVClient("h", "u", "p", session=s)
    with pytest.raises(ValueError):
        c.set_pixels({"middle": {0: {"r": 0, "g": 0, "b": 0}}})


@pytest.mark.parametrize("colour", [
    {"r": 256, "g": 0, "b": 0}, {"r": -1, "g": 0, "b": 0},
    {"r": 0, "g": 300, "b": 0},
])
def test_set_pixels_colour_range(tvmod, colour):
    s = FakeSession([FakeResponse(200, TOPOLOGY_LIVE)])
    c = tvmod.TVClient("h", "u", "p", session=s)
    with pytest.raises(ValueError):
        c.set_pixels({"top": {0: colour}})


def test_set_pixels_empty_rejected(tvmod):
    s = FakeSession()
    c = tvmod.TVClient("h", "u", "p", session=s)
    with pytest.raises(ValueError):
        c.set_pixels({})
    assert s.calls == []


def test_topology_and_get_pixels_paths(tvmod):
    s = FakeSession([FakeResponse(200, TOPOLOGY_LIVE), FakeResponse(200, {"layer1": {}})])
    c = tvmod.TVClient("h", "u", "p", session=s)
    assert c.get_topology()["top"] == 7
    assert c.get_pixels() == {"layer1": {}}
    assert [x["url"].rsplit("/6/", 1)[1] for x in s.calls] == [
        "ambilight/topology", "ambilight/cached"]


# --------------------------------------------------------------------------
# Предел громкости контура (решение Оператора 27.10: половина диапазона)
# --------------------------------------------------------------------------
# Громкость включена после живого замера, и вместе с ней контур получил
# право менять звук сам. Предел — не удобство, а защита: ошибка в автомате
# или неверный замер не должны бить громкостью по человеку.


@pytest.mark.parametrize(("lo", "hi", "expect"), [
    (0, 60, 30), (0, 61, 30), (0, 1, 0), (0, 0, 0), (10, 60, 35), (0, 100, 50),
    # Эти два случая различают округление вниз и к ближайшему: 0.5 от 0..3
    # даёт 1.5, от 0..63 — 31.5. Без них мутация `int` -> `round` проходит
    # незамеченной, потому что на 0..61 оба способа дают 30.
    (0, 3, 1), (0, 63, 31),
])
def test_volume_ceiling_rounds_down(tvmod, lo, hi, expect):
    """Округление ВНИЗ: 0.5 от 0..61 даёт 30, не 31.

    К ближайшему округлять нельзя — предел тогда оказывается выше
    заданной доли, а смысл предела в том, что он не переступается.
    """
    assert tvmod.volume_ceiling(lo, hi, 0.5) == expect


def test_volume_limit_fraction_default(tvmod, monkeypatch):
    monkeypatch.delenv(tvmod.VOLUME_LIMIT_ENV, raising=False)
    assert tvmod.volume_limit_fraction() == 0.5


@pytest.mark.parametrize("raw", ["мусор", "0", "-0.3", "1.5", "", "0,5"])
def test_volume_limit_bad_value_keeps_default(tvmod, monkeypatch, raw):
    """Опечатка в `.env` НЕ расширяет предел.

    Тихий откат к «без ограничения» из-за `0,5` вместо `0.5` — именно тот
    случай, когда молчаливое смягчение защиты опаснее отказа.
    """
    monkeypatch.setenv(tvmod.VOLUME_LIMIT_ENV, raw)
    assert tvmod.volume_limit_fraction() == 0.5


def test_volume_limit_env_applies(tvmod, monkeypatch):
    monkeypatch.setenv(tvmod.VOLUME_LIMIT_ENV, "0.25")
    assert tvmod.volume_ceiling(0, 60) == 15


def test_set_volume_clamps_to_ceiling_and_returns_fact(tvmod, monkeypatch):
    monkeypatch.delenv(tvmod.VOLUME_LIMIT_ENV, raising=False)
    s = FakeSession([
        FakeResponse(200, {"current": 10, "min": 0, "max": 60, "muted": False}),
        FakeResponse(200, {}),
    ])
    c = tvmod.TVClient("10.0.0.5", "u", "p", session=s)
    applied = c.set_volume(55)
    assert applied == 30
    assert s.last["json"] == {"muted": False, "current": 30}


def test_set_volume_below_ceiling_untouched(tvmod, monkeypatch):
    monkeypatch.delenv(tvmod.VOLUME_LIMIT_ENV, raising=False)
    s = FakeSession([
        FakeResponse(200, {"current": 10, "min": 0, "max": 60, "muted": False}),
        FakeResponse(200, {}),
    ])
    c = tvmod.TVClient("10.0.0.5", "u", "p", session=s)
    assert c.set_volume(22) == 22


def test_volume_step_does_not_exceed_ceiling(tvmod, monkeypatch):
    monkeypatch.delenv(tvmod.VOLUME_LIMIT_ENV, raising=False)
    s = FakeSession([
        FakeResponse(200, {"current": 28, "min": 0, "max": 60, "muted": False}),
        FakeResponse(200, {}),
    ])
    c = tvmod.TVClient("10.0.0.5", "u", "p", session=s)
    assert c.volume_step(10) == 30
    assert s.last["json"]["current"] == 30


def test_volume_step_up_above_ceiling_keeps_level(tvmod, monkeypatch):
    """Громкость выше предела (выставлена пультом) не обрушивается."""
    monkeypatch.delenv(tvmod.VOLUME_LIMIT_ENV, raising=False)
    s = FakeSession([
        FakeResponse(200, {"current": 50, "min": 0, "max": 60, "muted": False}),
        FakeResponse(200, {}),
    ])
    c = tvmod.TVClient("10.0.0.5", "u", "p", session=s)
    assert c.volume_step(5) == 50


def test_volume_step_down_works_above_ceiling(tvmod, monkeypatch):
    monkeypatch.delenv(tvmod.VOLUME_LIMIT_ENV, raising=False)
    s = FakeSession([
        FakeResponse(200, {"current": 50, "min": 0, "max": 60, "muted": False}),
        FakeResponse(200, {}),
    ])
    c = tvmod.TVClient("10.0.0.5", "u", "p", session=s)
    assert c.volume_step(-8) == 42


def test_volume_step_preserves_mute(tvmod, monkeypatch):
    monkeypatch.delenv(tvmod.VOLUME_LIMIT_ENV, raising=False)
    s = FakeSession([
        FakeResponse(200, {"current": 10, "min": 0, "max": 60, "muted": True}),
        FakeResponse(200, {}),
    ])
    c = tvmod.TVClient("10.0.0.5", "u", "p", session=s)
    c.volume_step(3)
    assert s.last["json"]["muted"] is True
