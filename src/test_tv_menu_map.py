"""TV_MENU_MAP (04.11): карта меню ТВ — валидатор, путь по карте, сверка с живым путём."""
from __future__ import annotations

import copy

import pytest

import tv_menu_map as tm


@pytest.fixture
def m():
    return tm.load()


def test_map_is_valid(m):
    assert tm.validate(m) == []


def test_screen_off_route_equals_live_path(m):
    """Путь по карте == путь, проверенный вживую 3 прогонами и /cam calib auto (camera_watcher.SCREEN_OFF_KEYS)."""
    cw = pytest.importorskip("camera_watcher")
    assert tuple(tm.route("settings.general.power:Выкл. экран", m, press=True)) == cw.SCREEN_OFF_KEYS


def test_keys_known_to_tv_control():
    tvc = pytest.importorskip("tv_control")
    assert tm.NETWORK_KEYS <= tvc.KEYS


def test_confirm_only_on_whitelist(m):
    keys = tm.route("settings.general.power:Выкл. экран", m, press=True)
    assert keys.count("Confirm") == 3
    with pytest.raises(tm.MapError, match="запрещён"):
        tm.route("settings.general.power:Таймер сна", m, press=True)
    with pytest.raises(tm.MapError, match="запрещён"):
        tm.route("settings.general:EasyLink", m, press=True)          # тумблер: «ок» переключил бы настройку


def test_route_without_press_has_no_final_confirm(m):
    keys = tm.route("settings.general.power:Выкл. экран", m)
    assert keys[-1] == "CursorDown" and keys.count("Confirm") == 2


def test_route_to_sections_and_items(m):
    base = ["Home", "CursorUp", "CursorUp"] + ["CursorRight"] * 6 + ["Confirm"]
    assert tm.route("settings.picture", m) == base
    assert tm.route("settings.region", m) == base + ["CursorDown"] * 9
    assert tm.route("settings.access:Описание аудио", m) == base + ["CursorDown"] * 7 + ["CursorRight"] + ["CursorDown"] * 2
    assert tm.route("home.topbar:Приложения", m) == ["Home", "CursorUp", "CursorUp"] + ["CursorRight"] * 3


def test_pending_node_refused(m):
    with pytest.raises(tm.MapError, match="не подтверждён"):
        tm.route("settings.update:что-то", m)


def test_unknown_targets(m):
    with pytest.raises(tm.MapError):
        tm.route("settings.nowhere", m)
    with pytest.raises(tm.MapError):
        tm.route("settings.general:Нет такого", m)


@pytest.mark.parametrize("a,b,same", [
    ("Настройки клав...", "Настройки клавиатур...", True),
    ("Питание и энерг...", "Питание и энергопотребление", True),
    ("Apple AirPlay и H...", "Apple AirPlay и HomeKit", True),
    ("Настройки клав...", "Настройки заста...", False),
    ("Выкл. экран", "Экран", False),
    ("Таймер сна", "Таймер автовык...", False),
    ("", "Сеть", False),
    ("EasyLinkX", "EasyLink", False),
    ("Сеть", "Сеть", True),
])
def test_same_name(a, b, same):
    assert tm.same_name(a, b) is same


def test_ambiguous_prefix_is_error(m):
    node = {"items": ["Стиль звука", "Стиль изображ."]}
    with pytest.raises(tm.MapError, match="неоднозначен"):
        tm.index_of(node, "Стиль...")


def test_known_reading_errors_are_absent(m):
    """Ошибки чтения кадров 04.11 (внешняя модель) — сторож: карта их не содержит."""
    n = m["nodes"]
    assert tm.item_name(n["settings.access"]["items"][0]) == "Универсальный доступ"
    assert tm.item_name(n["settings.parental"]["items"][0]) == "Родительский контроль"
    assert len(n["home.topbar"]["items"]) == 7
    assert n["popup.eco"]["focus_on_enter"] == "OK"
    assert len(n["quickmenu.frequent"]["items"]) == 9
    assert len(n["settings.sound"]["items"]) == 10
    assert n["settings.general.power"]["items"][0]["value"] == "Домаш..."


@pytest.mark.parametrize("breaker,fragment", [
    (lambda m: m["nodes"]["settings.screen"].__setitem__("frames", []), "без кадра"),
    (lambda m: m["confirm_whitelist"].append("settings.general:EasyLinkX"), "confirm_whitelist"),
    (lambda m: m["nodes"]["settings.general"]["opens"].__setitem__("Питание и энерг...", "nope"), "узла нет"),
    (lambda m: m["nodes"]["settings.region"]["items"].append("Язык"), "повтор"),
    (lambda m: m["nodes"]["settings.general"].__setitem__("enter", ["Adjust"]), "не из сетевых"),
    (lambda m: m["nodes"]["popup.eco"].__setitem__("focus_on_enter", "Позже"), "focus_on_enter"),
    (lambda m: m["nodes"]["settings.network"].__setitem__("status", "OK"), "статус"),
])
def test_validator_catches(m, breaker, fragment):
    bad = copy.deepcopy(m)
    breaker(bad)
    assert any(fragment in e for e in tm.validate(bad))
