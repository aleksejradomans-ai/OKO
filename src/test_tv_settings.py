"""tv_control: чтение/запись настроек меню (menuitems/settings), формат проверен на «тв амби» 29–30.10."""
import copy
import json

import pytest

import tv_control as t


class Resp:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = body

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


class FakeTV:
    """Минимальный «телевизор»: хранит значения узлов, отвечает в формате, снятом с живого ТВ."""

    def __init__(self, accept=True):
        self.accept = accept
        self.items = {
            710: {"Nodeid": 710, "Controllable": True, "Available": True, "string_id": "B", "data": {"value": 4}},
            720: {"Nodeid": 720, "Controllable": True, "Available": True, "string_id": "S", "data": {"value": -2}},
            320: {"Nodeid": 320, "Controllable": True, "Available": True, "string_id": "FV",
                  "data": {"selected_item": 321, "enum_values": [
                      {"enum_id": 321, "controllable": True, "available": True},
                      {"enum_id": 324, "controllable": True, "available": True},
                      {"enum_id": 325, "controllable": True, "available": False}]}},
        }
        self.updates = []

    def request(self, method, url, json=None, auth=None, timeout=None, verify=None):
        path = url.split("/6/", 1)[1]
        if path == "menuitems/settings/current":
            vals = [{"value": copy.deepcopy(self.items[n["nodeid"]])} if n["nodeid"] in self.items else {"value": {}}
                    for n in json["nodes"]]
            return Resp(200, {"version": 23, "values": vals})
        if path == "menuitems/settings/update":
            v = json["values"][0]["value"]
            self.updates.append(copy.deepcopy(v))
            if self.accept:
                self.items[v["Nodeid"]] = copy.deepcopy(v)
            return Resp(200, None)
        return Resp(404, None)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(t.time, "sleep", lambda s: None)


def client(fake):
    return t.TVClient("1.2.3.4", "u", "p", session=fake)


def test_read_settings_skips_empty_values():
    tv = client(FakeTV())
    got = tv.read_settings([710, 730])
    assert list(got) == [710] and got[710]["data"]["value"] == 4


def test_slider_write_returns_fact_and_clamps():
    fake = FakeTV()
    tv = client(fake)
    assert tv.set_setting_value(710, 15, 0, 10) == 10
    assert fake.items[710]["data"]["value"] == 10
    assert fake.updates[-1]["Nodeid"] == 710 and fake.updates[-1]["Controllable"] is True   # прочитанный объект целиком
    assert tv.get_setting_value(710) == 10


def test_slider_write_not_accepted_raises():
    tv = client(FakeTV(accept=False))
    with pytest.raises(t.TVError, match="не принял"):
        tv.set_setting_value(710, 3, 0, 10)


def test_enum_write_and_unavailable_variant():
    fake = FakeTV()
    tv = client(fake)
    assert tv.set_setting_enum(320, 324) == 324
    assert fake.items[320]["data"]["selected_item"] == 324
    assert fake.items[320]["data"]["enum_values"]                     # список вариантов не потерян
    with pytest.raises(ValueError, match="недоступен"):
        tv.set_setting_enum(320, 325)
    assert len(fake.updates) == 1                                      # недоступный вариант не отправлялся


def test_missing_or_locked_node_unsupported():
    fake = FakeTV()
    tv = client(fake)
    with pytest.raises(t.TVUnsupported):
        tv.set_setting_value(730, 1, 0, 10)
    fake.items[710]["Controllable"] = False
    with pytest.raises(t.TVUnsupported, match="недоступна"):
        tv.set_setting_value(710, 5, 0, 10)
    assert fake.updates == []


def test_post_without_want_json_still_returns_none():
    tv = client(FakeTV())
    assert tv._call("POST", "menuitems/settings/update",
                    {"values": [{"value": {"Nodeid": 710, "data": {"value": 4}}}]}) is None
