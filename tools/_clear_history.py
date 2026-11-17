"""Разовая очистка истории диалога (таблица sessions) одного пользователя.

Запуск (из любой папки):  python tools/_clear_history.py --user-id <Telegram ID>
Без --user-id берётся единственный id из ADMIN_USER_IDS (.env, config.py). Нет id или их несколько --
ошибка запуска, база не открывается (fail-closed). Значение id в коде не хранится.
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]   # корень репозитория (файл лежит в tools/)
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))


def resolve_user_id(argv=None) -> int:
    ap = argparse.ArgumentParser(description="очистка истории sessions одного пользователя")
    ap.add_argument("--user-id", type=int, default=None, help="Telegram ID (иначе -- единственный из ADMIN_USER_IDS)")
    a = ap.parse_args(argv)
    if a.user_id is not None:
        if a.user_id <= 0:
            raise SystemExit("СТОП: --user-id должен быть положительным числом")
        return a.user_id
    import config
    ids = sorted(config.ADMIN_USER_IDS)
    if len(ids) != 1:
        raise SystemExit(f"СТОП: нет --user-id, а в ADMIN_USER_IDS {len(ids)} id (нужен ровно один) -- ничего не удалено")
    return ids[0]


def main(argv=None) -> int:
    uid = resolve_user_id(argv)
    import db
    conn = db.get_connection()
    try:
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (uid,))
        conn.commit()
    finally:
        conn.close()
    print("История очищена")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
