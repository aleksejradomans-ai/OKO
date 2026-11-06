"""Поиск телевизора по MAC при смене сети (Оператор 29.10). Живой случай 11:25–11:44: сеть сменилась,
TV_HOST остался 10.17.115.41, телевизор получил другой адрес. Сеть подменена: ни одного настоящего запроса."""
import json

import pytest
import requests

import tv_control as C
import tv_bot_handler as H

MAC = "D4-0A-DC-4D-F6-2C"
ARP = """
Interface: 192.168.50.20 --- 0xb
  Internet Address      Physical Address      Type
  192.168.50.1          aa-bb-cc-dd-ee-ff     dynamic
  192.168.50.37         d4-0a-dc-4d-f6-2c     dynamic
Интерфейс: 10.17.115.20 --- 0x5
  10.17.115.41          d4-0a-dc-4d-f6-2c     динамический
"""


class Resp:
    status_code = 200

    def json(self):
        return {}


class Session:
    """Старый адрес не принимает соединение, новый — отвечает. Записывает адреса запросов."""

    def __init__(self, dead=("10.17.115.41",), exc=requests.exceptions.ConnectTimeout):
        self.urls, self.dead, self.exc = [], set(dead), exc

    def request(self, method, url, **kw):
        self.urls.append(url)
        if any(f"//{d}:" in url for d in self.dead):
            raise self.exc("нет соединения")
        return Resp()


@pytest.fixture(autouse=True)
def isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(C, "HOST_CACHE_PATH", tmp_path / "tv_host.json")
    monkeypatch.setattr(C, "port_open", lambda h, p, timeout=2.0: True)
    monkeypatch.setattr(C, "poke_subnets", lambda ips=None: (_ for _ in ()).throw(AssertionError("poke")))
    monkeypatch.setattr(C, "arp_table", lambda: (_ for _ in ()).throw(AssertionError("arp")))


def client(session, finder, mac=MAC):
    return C.TVClient("10.17.115.41", "u", "p", session=session, mac=mac, finder=finder)


def test_parse_arp_and_find_skips_old_address():
    rows = C._MAC_RE.findall(ARP)
    ip = C.find_host(MAC, exclude="10.17.115.41", arp=lambda: rows, poke=lambda: None,
                     wait=lambda s: None, local=lambda: ["192.168.50.20"])
    assert ip == "192.168.50.37"


def test_find_pokes_once_when_not_in_arp():
    calls = []
    tables = iter([[], [("192.168.50.37", "d4:0a:dc:4d:f6:2c")]])
    ip = C.find_host(MAC, arp=lambda: next(tables), poke=lambda: calls.append("poke"),
                     wait=lambda s: calls.append(s), local=lambda: ["192.168.50.20"])
    assert ip == "192.168.50.37" and calls == ["poke", 2.0]


def test_find_ignores_foreign_subnet_and_gives_up():
    rows = [("10.17.115.41", MAC)]
    assert C.find_host(MAC, exclude=None, arp=lambda: rows, poke=lambda: None, wait=lambda s: None,
                       local=lambda: ["192.168.50.20"]) is None


def test_connect_timeout_rediscovers_retries_and_caches():
    s = Session()
    tv = client(s, lambda mac, exclude: "192.168.50.37")
    tv.send_key("Home")
    assert [u.split("/")[2] for u in s.urls] == ["10.17.115.41:1926", "192.168.50.37:1926"]
    assert tv.host == "192.168.50.37" and tv.moved_from == "10.17.115.41"
    assert json.loads(C.HOST_CACHE_PATH.read_text())["host"] == "192.168.50.37"


def test_read_timeout_is_not_retried():
    """Соединение было — нажатие могло дойти. Повтор нажал бы кнопку дважды."""
    s = Session(exc=requests.exceptions.ReadTimeout)
    found = []
    tv = client(s, lambda mac, exclude: found.append(1) or "192.168.50.37")
    with pytest.raises(C.TVTransportError):
        tv.send_key("Home")
    assert len(s.urls) == 1 and not found


def test_without_mac_no_search():
    s = Session()
    tv = client(s, lambda mac, exclude: pytest.fail("поиск без MAC"), mac=None)
    with pytest.raises(C.TVTransportError):
        tv.send_key("Home")


def test_not_found_raises_transport_and_searches_once():
    s = Session()
    n = []
    tv = client(s, lambda mac, exclude: n.append(1))
    for _ in range(2):
        with pytest.raises(C.TVTransportError):
            tv.send_key("Home")
    assert len(n) == 1 and tv.host == "10.17.115.41"


def test_found_but_port_closed_is_not_used(monkeypatch):
    monkeypatch.setattr(C, "port_open", lambda h, p, timeout=2.0: False)
    tv = client(Session(), lambda mac, exclude: "192.168.50.37")
    with pytest.raises(C.TVTransportError):
        tv.send_key("Home")
    assert tv.host == "10.17.115.41" and not C.HOST_CACHE_PATH.exists()


def test_from_env_prefers_cache_with_same_mac(monkeypatch, tmp_path):
    creds = tmp_path / "c.json"
    creds.write_text(json.dumps({"username": "u", "password": "p"}), encoding="utf-8")
    monkeypatch.setenv("TV_HOST", "10.17.115.41")
    monkeypatch.setenv("TV_MAC", MAC)
    C.save_host_cache("d4:0a:dc:4d:f6:2c", "192.168.50.37")
    assert C.TVClient.from_env(creds, session=Session()).host == "192.168.50.37"
    C.save_host_cache("00-11-22-33-44-55", "192.168.50.99")        # другой телевизор — не наследуем
    assert C.TVClient.from_env(creds, session=Session()).host == "10.17.115.41"
    monkeypatch.delenv("TV_MAC")
    C.save_host_cache(MAC, "192.168.50.37")
    assert C.TVClient.from_env(creds, session=Session()).host == "10.17.115.41"


def test_bot_says_address_changed():
    tv = client(Session(), lambda mac, exclude: "192.168.50.37")

    class Cmd:
        name, kind, needs_confirm, meta = "home", H.BLIND, False, {"key": "Home"}

        @staticmethod
        def run(t):
            t.send_key("Home")
            return "Отправил Home"
    out = H._execute(Cmd(), lambda: tv)
    assert out.startswith("Отправил Home") and "сменил адрес в сети (10.17.115.41 → 192.168.50.37)" in out
