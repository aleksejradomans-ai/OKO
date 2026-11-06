"""TV_MENU_MAP (04.11.2026): карта меню ТВ (Philips 50PUS9010 / Titan OS) и путь к пункту по карте.

Карта — src/tv_menu_map.json: узлы, пункты в порядке экрана, кадр-доказательство у каждого узла (архив кадров 04.11).
Здесь — только чистые функции, без сети и без ТВ:
  load()            — карта из файла;
  validate(m)       — список нарушений (пусто = карта годна): узел без кадра, «ок» вне белого списка, ссылки в никуда;
  route(target, m)  — кнопки от любого экрана до пункта: «settings.general.power:Выкл. экран».
                      Confirm — только на узлах из confirm_whitelist; на последнем пункте — только если press=True
                      И пункт в белом списке. Любой другой «ок» может изменить настройку ТВ — карта его не выдаёт.
  same_name(a, b)   — имя с экрана против имени карты по началу строки: в списке имя обрезано («Настройки клав...»),
                      в превью длиннее («Настройки клавиатур...»), OCR даст третий вариант.
Путь «Выкл. экран» по карте обязан совпасть с проверенным вживую camera_watcher.SCREEN_OFF_KEYS (тест) —
это и есть сверка карты с живым прогоном.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Optional

MAP_PATH = Path(__file__).resolve().parent / "tv_menu_map.json"
NETWORK_KEYS = frozenset({"Home", "Back", "CursorUp", "CursorDown", "CursorLeft", "CursorRight", "Confirm"})
_STEP = {"vertical": ("CursorDown", "CursorUp"), "horizontal": ("CursorRight", "CursorLeft")}


class MapError(ValueError):
    """Цель не найдена в карте или путь к ней не подтверждён кадрами."""


def load(path: Optional[Path] = None) -> dict:
    return json.loads((path or MAP_PATH).read_text(encoding="utf-8"))


def item_name(item: Any) -> str:
    return item["name"] if isinstance(item, dict) else str(item)


def _core(s: str) -> str:
    s = (s or "").strip().rstrip(".…").rstrip()
    return " ".join(s.lower().replace("ё", "е").split())


def _cut(s: str) -> bool:
    s = (s or "").strip()
    return s.endswith("...") or s.endswith("…")


def same_name(a: str, b: str) -> bool:
    """Одно имя? Целиком — равны; по началу строки — только если короткая форма обрезана («...» на экране):
    «Настройки клав...» = «Настройки клавиатур...», но «EasyLinkX» != «EasyLink»."""
    x, y = _core(a), _core(b)
    if not x or not y:
        return False
    if x == y:
        return True
    if len(x) < len(y):
        return _cut(a) and y.startswith(x)
    return _cut(b) and x.startswith(y)


def index_of(node: dict, name: str) -> int:
    hits = [i for i, it in enumerate(node.get("items") or [])
            if same_name(item_name(it), name) or (isinstance(it, dict) and same_name(it.get("name_preview", ""), name))]
    if len(hits) != 1:
        raise MapError(f"пункт «{name}»: {'нет в узле' if not hits else 'неоднозначен'}")
    return hits[0]


def _confirmed(m: dict, node_id: str) -> dict:
    node = (m.get("nodes") or {}).get(node_id)
    if node is None:
        raise MapError(f"узла {node_id} нет в карте")
    if not str(node.get("status", "")).startswith("CONFIRMED"):
        raise MapError(f"узел {node_id} не подтверждён кадрами ({node.get('status')})")
    return node


def _move(node: dict, frm: str, to: str) -> list[str]:
    fwd, back = _STEP[node.get("axis", "vertical")]
    d = index_of(node, to) - index_of(node, frm)
    return [fwd] * d if d >= 0 else [back] * (-d)


def _allowed(m: dict, node_id: str, name: str) -> bool:
    for w in m.get("confirm_whitelist") or []:
        n, _, it = w.partition(":")
        if n == node_id and same_name(it, name):
            return True
    return False


def route(target: str, m: Optional[dict] = None, press: bool = False) -> list[str]:
    """Кнопки до цели. target: «settings.<раздел>», «settings.<раздел>:<пункт>», «settings.general.power:<пункт>»,
    «home.topbar:<значок>». Начало — с Home (любой экран). press=True — ещё и «ок» на цели (только белый список)."""
    m = m or load()
    node_id, _, item = target.partition(":")
    keys: list[str] = []
    top = _confirmed(m, "home.topbar")
    keys += list(top["enter"])
    if node_id == "home.topbar":
        keys += _move(top, top["focus_on_enter"], item)
        return keys + _press(m, node_id, item, press)
    keys += _move(top, top["focus_on_enter"], "Настройки")
    if not _allowed(m, "home.topbar", "Настройки"):
        raise MapError("«ок» на Настройках не в белом списке")
    keys.append("Confirm")
    root = _confirmed(m, "settings.root")
    sections = root.get("sections") or {}
    chain = node_id.split(".")
    if chain[0] != "settings" or len(chain) < 2:
        raise MapError(f"цель {target}: поддерживаются home.topbar и settings.*")
    section_id = ".".join(chain[:2])
    sec_name = next((s for s, nid in sections.items() if nid == section_id), None)
    if sec_name is None:
        raise MapError(f"раздела {section_id} нет в settings.root")
    keys += _move(root, root["focus_on_enter"], sec_name)
    if node_id == section_id and not item:
        return keys
    sec = _confirmed(m, section_id)
    keys += list(sec.get("enter") or ["CursorRight"])
    first = item_name(sec["items"][0])
    if node_id == section_id:
        return keys + _move(sec, first, item) + _press(m, node_id, item, press)
    # подменю раздела (пока только Питание в Общих)
    opener = next((name for name, nid in (sec.get("opens") or {}).items() if nid == node_id), None)
    if opener is None:
        raise MapError(f"{node_id}: в {section_id} нет пункта, открывающего это подменю")
    sub = _confirmed(m, node_id)
    keys += _move(sec, first, opener) + list(sub.get("enter") or ["CursorRight"])
    if sub.get("popup_first"):
        pop = _confirmed(m, sub["popup_first"])
        acc = pop.get("focus_on_enter")
        if not _allowed(m, sub["popup_first"], acc):
            raise MapError(f"окно {sub['popup_first']}: «{acc}» не в белом списке")
        keys += list(pop.get("accept") or ["Confirm"])
    if item:
        keys += _move(sub, sub["focus_on_enter"], item) + _press(m, node_id, item, press)
    return keys


def _press(m: dict, node_id: str, item: str, press: bool) -> list[str]:
    if not press:
        return []
    if not _allowed(m, node_id, item):
        raise MapError(f"«ок» на {node_id}:{item} запрещён: нет в confirm_whitelist (может изменить настройку ТВ)")
    return ["Confirm"]


def validate(m: dict, keys: Iterable[str] = NETWORK_KEYS) -> list[str]:
    """Нарушения карты. Пустой список — карта годна."""
    allowed = set(keys)
    errs: list[str] = []
    nodes = m.get("nodes") or {}
    for nid, node in nodes.items():
        status = str(node.get("status", ""))
        if not (status.startswith("CONFIRMED") or status.startswith("PENDING")):
            errs.append(f"{nid}: статус «{status}» — нужен CONFIRMED или PENDING")
        if status.startswith("CONFIRMED") and not node.get("frames"):
            errs.append(f"{nid}: CONFIRMED без кадра")
        if status.startswith("CONFIRMED") and not node.get("items"):
            errs.append(f"{nid}: CONFIRMED без пунктов")
        names = [_core(item_name(it)) for it in node.get("items") or []]
        if len(names) != len(set(names)):
            errs.append(f"{nid}: повтор имени в списке")
        for k in list(node.get("enter") or []) + list(node.get("accept") or []):
            if k not in allowed:
                errs.append(f"{nid}: кнопка {k} не из сетевых")
        if node.get("axis", "vertical") not in _STEP:
            errs.append(f"{nid}: axis «{node.get('axis')}»")
        for fld in ("focus_on_enter",):
            if node.get(fld) and node.get("items"):
                try:
                    index_of(node, node[fld])
                except MapError as e:
                    errs.append(f"{nid}: {fld}: {e}")
        for name, target in (node.get("opens") or {}).items():
            if target not in nodes:
                errs.append(f"{nid}: opens → {target} — узла нет")
            try:
                index_of(node, name)
            except MapError as e:
                errs.append(f"{nid}: opens: {e}")
        if node.get("popup_first") and node["popup_first"] not in nodes:
            errs.append(f"{nid}: popup_first → {node['popup_first']} — узла нет")
    for sec, target in ((nodes.get("settings.root") or {}).get("sections") or {}).items():
        if target not in nodes:
            errs.append(f"settings.root: раздел {sec} → {target} — узла нет")
    for w in m.get("confirm_whitelist") or []:
        nid, _, it = w.partition(":")
        if nid not in nodes:
            errs.append(f"confirm_whitelist {w}: узла нет")
            continue
        try:
            index_of(nodes[nid], it)
        except MapError as e:
            errs.append(f"confirm_whitelist {w}: {e}")
    return errs
