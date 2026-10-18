"""PUBLISH_PREP: тесты не видят настоящий .env.

Размещение: C:\\oko\\src\\test_config_isolation.py

src/conftest.py задаёт TELEGRAM_TOKEN и ADMIN_USER_IDS до первого импорта
config.py, а load_dotenv() уже заданные переменные не перезаписывает. Поэтому
тесты не получают ни настоящий токен, ни настоящий список админов.

Сравнение вынесено в bool до assert: иначе pytest при падении напечатал бы
настоящее значение (токен, ID) в консоль.
"""
import os

FAKE = "test-token-not-real"


def test_token_is_fake_for_tests():
    import config
    env_is_fake = os.environ.get("TELEGRAM_TOKEN") == FAKE
    config_is_fake = config.TELEGRAM_TOKEN == FAKE
    assert env_is_fake, "TELEGRAM_TOKEN in tests is not the fake value: real .env leaks into tests"
    assert config_is_fake, "config.TELEGRAM_TOKEN is not the fake value"


def test_admin_list_is_empty_for_tests():
    import config
    is_empty = config.ADMIN_USER_IDS == frozenset()
    assert is_empty, "config.ADMIN_USER_IDS is not empty in tests: real .env leaks into tests"


def test_base_dir_is_temporary():
    import config
    is_tmp = "oko_pytest_" in str(config.BASE_DIR)
    assert is_tmp, "config.BASE_DIR is not the temporary pytest dir"
