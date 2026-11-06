"""Понятные ответы телевизора (Оператор 29.10: «понятные значения выводов, пользовательское понимание»).
Живой случай 29.10 11:25: ПК и телевизор в разных сетях -> ConnectTimeout -> раньше в чат уходил сырой текст urllib3."""
import pytest

import tv_bot_handler as H
from tv_control import TVAuthError, TVError, TVNotPaired, TVTransportError, TVUnsupported

RAW = "HTTPSConnectionPool(host='10.17.115.41', port=1926): Max retries exceeded"


# Имена совпадают с классами requests/urllib3: классификация идёт по имени в цепочке причин.
class ConnectionError(OSError):            # noqa: A001
    pass


class ConnectTimeout(ConnectionError):
    pass


class ReadTimeout(OSError):
    pass


class SSLError(ConnectionError):
    pass


def transport(cause):
    try:
        try:
            raise cause
        except Exception as c:
            raise TVTransportError("POST input/key: " + RAW) from c
    except TVTransportError as e:
        return e


class TV:
    host = "10.17.115.41"


def cmd(exc=None, result="ok"):
    def run(tv):
        if exc:
            raise exc
        return result
    class C:
        name = "key_home"
        kind = "ack"
        needs_confirm = False
        meta = {"key": "Home"}
    C.run = staticmethod(run)
    return C()


@pytest.mark.parametrize("cause,needle", [
    (ConnectTimeout("timed out"), "не отвечает по адресу 10.17.115.41"),
    (ReadTimeout("read"), "не ответил вовремя"),
    (SSLError("ssl"), "защищённое соединение"),
    (ConnectionError("refused"), "отказал в соединении"),
    (OSError("x"), "недоступен по сети"),
])
def test_transport_reasons_are_human(cause, needle):
    out = H._execute(cmd(transport(cause)), lambda: TV())
    assert needle in out
    assert "HTTPSConnectionPool" not in out and "1926" not in out and "Max retries" not in out


def test_other_network_hint_for_timeout():
    out = H._execute(cmd(transport(ConnectTimeout("t"))), lambda: TV())
    assert "другой сети" in out and "выключен" in out


def test_not_configured_and_not_paired():
    def no_host():
        raise ValueError("TV_HOST не задан в .env")

    def no_pair():
        raise TVNotPaired("нет файла")
    assert "не настроен: не задан его адрес" in H._execute(cmd(), no_host)
    assert "пайринг" in H._execute(cmd(), no_pair)


def test_bad_value_is_not_called_config():
    out = H._execute(cmd(ValueError("Громкость 99 вне диапазона 0..60")), lambda: TV())
    assert out == "📺 Команда не выполнена: Громкость 99 вне диапазона 0..60." and "не задан" not in out


def test_auth_unsupported_generic_unknown(monkeypatch):
    monkeypatch.setattr(H, "describe_command", lambda c: "домой")
    assert "отклонил" in H._execute(cmd(TVAuthError("403")), lambda: TV())
    assert "не умеет (домой)" in H._execute(cmd(TVUnsupported("x")), lambda: TV())
    assert "ответил ошибкой" in H._execute(cmd(TVError("HTTP 500")), lambda: TV())
    out = H._execute(cmd(KeyError("k")), lambda: TV())
    assert "KeyError" in out and "журнале" in out


def test_success_unchanged():
    assert H._execute(cmd(result="Нажато: Домой"), lambda: TV()) == "Нажато: Домой"


def test_real_requests_exceptions():
    """Настоящие классы requests (как в живом случае 29.10 11:25)."""
    import requests.exceptions as R
    tv = TV()
    assert "не отвечает" in H._execute(cmd(transport(R.ConnectTimeout("t"))), lambda: tv)
    assert "не ответил вовремя" in H._execute(cmd(transport(R.ReadTimeout("t"))), lambda: tv)
    assert "отказал в соединении" in H._execute(cmd(transport(R.ConnectionError("refused"))), lambda: tv)
    assert "защищённое соединение" in H._execute(cmd(transport(R.SSLError("x"))), lambda: tv)


def test_unsupported_names_the_command():
    assert H._execute(cmd(TVUnsupported("404")), lambda: TV()) == "📺 Этот телевизор такое не умеет (нажатие кнопки Home)."


def test_factory_errors_never_escape():
    """Старый контракт (test_tv_bot_handler): любой сбой фабрики клиента — текстом, не исключением."""
    for err, needle in ((TVAuthError("401"), "отклонил"), (TVTransportError("таймаут"), "недоступен"),
                        (RuntimeError("x"), "Непредвиденная"), (KeyError("k"), "Непредвиденная")):
        def f(err=err):
            raise err
        assert needle in H._execute(cmd(), f)


# --- короткие ответы кнопок (Оператор 29.10 11:45) ---
def test_key_answer_is_short_and_russian():
    import tv_commands as T

    class FakeTV:
        def send_key(self, key):
            self.key = key
    tv = FakeTV()
    c = T.parse("вправо")
    out = H._execute(c, lambda: tv)
    assert tv.key == "CursorRight"
    assert out == "Отправил: вправо (телевизор не подтверждает нажатия — смотри на экран)"
    assert "200" not in out and "CursorRight" not in out


def test_every_key_word_has_russian_name():
    import tv_commands as T
    assert not sorted(set(T.KEY_WORDS.values()) - set(T.KEY_SAY))
