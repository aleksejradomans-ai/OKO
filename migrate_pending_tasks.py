"""
Разовый скрипт миграции pending_tasks: registry_state.json -> SQLite
(таблица pending_tasks через StagingRepository).

ВАЖНО: запускать ОДИН РАЗ, ДО первого старта обновлённого bot.py/
run_system.py, и ТОЛЬКО когда оба процесса остановлены — иначе
run_system.py может дописать pending_tasks в JSON уже после того, как
этот скрипт его прочитал, и такая задача не попадёт в SQL (см. историю
решений — это ограничение уже проговаривалось раньше, не новое).

Использует РЕАЛЬНУЮ схему StagingRepository (таблица pending_tasks,
семь типизированных колонок), не предположение о структуре — импортирует
add_task() напрямую, чтобы миграция и рантайм-код гарантированно писали
в одном формате (нет риска, что схема мигрирующего скрипта разойдётся
со схемой репозитория, если та изменится в будущем).
"""
import json
import sys

import config
from staging_repository import StagingRepository


def migrate_pending_tasks_to_sqlite() -> None:
    old_state_path = config.BASE_DIR / "registry_state.json"
    if not old_state_path.exists():
        print(f"Старый файл состояния не найден по пути {old_state_path} — миграция не требуется.")
        return

    with open(old_state_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    pending_tasks = data.get("pending_tasks", {})
    if not pending_tasks:
        print("pending_tasks пуст в registry_state.json — нечего мигрировать.")
        return

    repo = StagingRepository()
    repo.setup_database()

    # Проверка ДО записи: каждая задача обязана содержать все поля,
    # которые ожидает add_task() / реальная схема таблицы. Если формат
    # старой записи не совпадает (например, ручное вмешательство в JSON,
    # или задача создана более старой версией кода с другим набором
    # полей) — явно падаем на этой записи с понятной ошибкой, не
    # записываем частично валидную/битую строку в SQL молча.
    required_keys = {"type", "original_path", "staging_path", "hash", "timestamp"}
    migrated, skipped = 0, 0

    for task_uuid, task_data in pending_tasks.items():
        missing = required_keys - task_data.keys()
        if missing:
            print(
                f"ПРОПУЩЕНО {task_uuid}: отсутствуют обязательные поля {sorted(missing)} "
                f"— требуется ручной разбор, запись НЕ перенесена."
            )
            skipped += 1
            continue

        repo.add_task(task_uuid, task_data)
        migrated += 1
        print(f"Мигрирована задача {task_uuid} ({task_data.get('original_path', '?')})")

    print()
    print(f"Итог: мигрировано {migrated}, пропущено (требует ручного разбора) {skipped}, "
          f"всего в исходном файле {len(pending_tasks)}.")

    if skipped > 0:
        print(
            "\nВНИМАНИЕ: есть пропущенные записи — разберитесь с ними вручную "
            "ДО запуска нового bot.py, иначе они будут молча потеряны "
            "(не появятся ни в JSON после отказа от Registry, ни в SQL)."
        )
        sys.exit(1)


if __name__ == "__main__":
    migrate_pending_tasks_to_sqlite()
