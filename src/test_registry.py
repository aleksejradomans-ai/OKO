"""Тесты для registry.py.

Дополняет TestRegistrySingleton в test_governor.py (там покрыты update_state
happy/invalid-key/invalid-value и pending_tasks-схема) — здесь фокус на
том, что НЕ было протестировано вообще: increment_state (атомарный
инкремент под блокировкой), персистентность чекпоинта между отдельными
экземплярами Registry (тот же класс риска, что уже был найден и исправлен
в SnapshotManager — in-memory состояние, не переживающее "новый процесс"),
и восстановление после повреждённого файла чекпоинта.

ВАЖНО про импорт: этот файл импортирует `from registry import Registry`
(БЕЗ префикса src.), точно как это делают conftest.py и test_governor.py.
Registry — синглтон, его идентичность живёт в атрибуте класса _instance.
Если этот файл импортирует `from src.registry import Registry`, а
conftest.py — `from registry import Registry`, при текущей настройке
sys.path (conftest.py добавляет src/ саму в путь) это МОГУТ оказаться два
разных объекта модуля в sys.modules и, соответственно, два разных класса
с независимыми _instance — сброс синглтона в фикстуре `registry` перестал
бы реально влиять на класс, который видит этот тестовый файл. Стиль
импорта здесь — не вкусовщина, а требование корректности синглтон-теста.
"""
import threading

import pytest

from registry import Registry


def test_increment_state_happy_path(registry):
    assert registry.get_state("total_requests") == 0

    result = registry.increment_state("total_requests")
    assert result == 1
    assert registry.get_state("total_requests") == 1

    result = registry.increment_state("total_requests", amount=5)
    assert result == 6
    assert registry.get_state("total_requests") == 6


def test_increment_state_undeclared_key_rejected(registry):
    result = registry.increment_state("totally_made_up_key")
    assert result is False


def test_increment_state_non_int_value_rejected(registry):
    """current_task хранит строку — increment_state на нечисловом
    значении обязан отклонить операцию, не бросить исключение и не
    молча превратить строку в мусор."""
    registry.update_state("current_task", "INDEXING")
    result = registry.increment_state("current_task")
    assert result is False
    # Значение не должно быть тронуто отклонённой операцией.
    assert registry.get_state("current_task") == "INDEXING"


def test_increment_state_thread_safe_under_concurrency(registry):
    """Регрессионный тест именно на риск, который описан в собственном
    docstring increment_state ('тот паттерн допускает lost update между
    конкурентными запросами') — 50 потоков одновременно инкрементируют
    один счётчик; без _data_lock финальное значение почти наверняка
    было бы меньше 50."""
    registry.update_state("total_requests", 0)

    threads = [
        threading.Thread(target=lambda: registry.increment_state("total_requests"))
        for _ in range(50)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert registry.get_state("total_requests") == 50


def test_checkpoint_persists_across_separate_registry_instances(registry, tmp_base_dir):
    """Тот же класс регрессии, что test_metadata_persists_across_separate_manager_instances
    в SnapshotManager: состояние обязано переживать 'новый процесс'
    (эмулируется пересозданием синглтона на тот же filepath), не жить
    только in-memory."""
    registry.update_state("current_task", "PERSIST_TEST")
    registry.increment_state("total_requests")
    filepath = registry.filepath

    # Эмулируем новый процесс — сброс синглтона + новый экземпляр на тот
    # же файл. Тест-фикстура `registry` тоже сбросит _instance после
    # теста, повторный сброс здесь безопасен и не конфликтует с ней.
    Registry._instance = None
    second = Registry(filepath=filepath)

    assert second.get_state("current_task") == "PERSIST_TEST"
    assert second.get_state("total_requests") == 1


def test_load_checkpoint_corrupted_json_starts_clean(tmp_base_dir):
    """Битый JSON в файле чекпоинта — не должен ронять инициализацию
    Registry, должен просто стартовать с дефолтным состоянием (см.
    _load_checkpoint: except (JSONDecodeError, OSError) -> лог и продолжение,
    не re-raise)."""
    filepath = tmp_base_dir / "corrupted_registry_state.json"
    filepath.write_text("{not valid json", encoding="utf-8")

    Registry._instance = None
    try:
        reg = Registry(filepath=str(filepath))
        assert reg.get_state("status") == "READY"
        assert reg.get_state("pending_tasks") == {}
    finally:
        Registry._instance = None
