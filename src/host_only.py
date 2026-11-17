"""(15.11, этап 1d; решение 14.11 п.4, п.8-12; docs/INVARIANTS.md INV-07): Host-Only -- закрытый режим хозяина.

Хозяин -- технический id отправителя из локального .env, ключ OKO_HOST_USER_ID (значение в отчёты, журналы и обмен
не пишется). Связь с ADMIN_USER_IDS: хозяин обязан быть одним из админов (бот вообще не отвечает не-админам);
id хозяина пуст / не число / не из ADMIN_USER_IDS -> Host-Only закрыт для всех (fail-closed). Текст сообщения роли
не играет: функции этого модуля текст не принимают («я Оператор», «это владелец» Host-Only не открывают).

Что делает Host-Only (03 §3, §4a п.1-2): у хозяина отключается ТОЛЬКО шаблонная заглушка на абстрактные вопросы
(«сознание» -- role_guard.SELF_TEXT, «кто ты» -- router.WHO_TEXT); вопрос идёт в модель. Приводы не трогаются: маршрут
router.HOST -- до правил ТВ, связок и «запомни», связка его не снимает (bind_layer.can_apply: не BRAIN/CLARIFY), команда
в хвосте не исполняется (физическое действие -- только контуром A). Сторожа ответа модели (stamp_cut, fake_api,
fake_done/fake_status, «Принято:» без факта, false_memory) работают как в общем режиме -- Host-Only их не видит.

TEST_HOST_ID -- синтетический хозяин полигона (03 §4a п.3): config.py его НЕ читает, живой бот о нём не знает;
полигон берёт его из своего .env и проверяет test_host_id (не равен id админа/Оператора). Без импорта бота,
железа, сети, баз."""
from __future__ import annotations

import re
from typing import Iterable, Optional

HOST_KEY = "OKO_HOST_USER_ID"       # ключ .env хозяина (имя -- в отчёт; значение -- никуда)
TEST_KEY = "TEST_HOST_ID"           # только .env полигона
_ID_RE = re.compile(r"^[0-9]{1,19}$")
_ID_MAX = 2 ** 63 - 1


def parse_id(raw) -> Optional[int]:
    """Строка из .env -> положительный int или None. bool, отрицательные, 0, «12abc», «1,2», пробел внутри -- None."""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw if 0 < raw <= _ID_MAX else None
    if not isinstance(raw, str):
        return None
    s = raw.strip()
    if not _ID_RE.match(s):
        return None
    v = int(s)
    return v if 0 < v <= _ID_MAX else None


def _admins(cfg) -> frozenset:
    ids = getattr(cfg, "ADMIN_USER_IDS", None) or frozenset()
    try:
        return frozenset(i for i in ids if type(i) is int)
    except TypeError:
        return frozenset()


def host_id(cfg) -> Optional[int]:
    """id хозяина или None (закрыто). Читается на каждый вызов: смена config в процессе (полигон) действует сразу."""
    hid = parse_id(getattr(cfg, HOST_KEY, None))
    if hid is None or hid not in _admins(cfg):
        return None
    return hid


def is_host(user_id, cfg) -> bool:
    """True только для int id отправителя, равного id хозяина. Текст не принимается -- по словам не открывается."""
    if type(user_id) is not int:
        return False
    hid = host_id(cfg)
    return hid is not None and user_id == hid


def state(cfg) -> str:
    """Для отчёта блока, без значения: 'closed:empty' | 'closed:bad' | 'closed:not_admin' | 'open'."""
    raw = getattr(cfg, HOST_KEY, None)
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return "closed:empty"
    hid = parse_id(raw)
    if hid is None:
        return "closed:bad"
    return "open" if hid in _admins(cfg) else "closed:not_admin"


def test_host_id(raw, operator_ids: Iterable[int], other_test_ids: Iterable[int] = ()) -> Optional[int]:
    """TEST_HOST_ID полигона -> int или None. None, если пусто/не число или совпал с id админа/хозяина (Оператора)
    или с другим тестовым id (POLY_TEST_UID): полигон под id Оператора не работает (решение 14.11 п.9)."""
    tid = parse_id(raw)
    if tid is None:
        return None
    if tid in {i for i in operator_ids if type(i) is int} or tid in {i for i in other_test_ids if type(i) is int}:
        return None
    return tid


test_host_id.__test__ = False     # pytest: функция модуля, не тест
