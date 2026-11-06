import pytest
from src.data_hygiene import DataHygieneFilter, build_notice

f = DataHygieneFilter()

@pytest.mark.parametrize("text,category", [
    ("ключ sk-ant-api03-AbCdEfGhIjKlMnOpQrStUv123", "API_KEY"),
    ("AKIAABCDEFGHIJKLMNOP", "API_KEY"),
    ("password=Hunter2xyz", "CREDENTIAL"),
    ("пароль: qwerty123", "CREDENTIAL"),
    ("a.b@mail.lv", "EMAIL"),
    ("4111 1111 1111 1111", "CARD"),
    ("LV80BANK0000435195001", "IBAN"),
    ("123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsawq", "BOT_TOKEN"),
    ("Zx9Qw2Lp8Rt5Yv3Nb7Mk1Hj6Gf4Ds0Aq", "HIGH_ENTROPY"),
])
def test_redacts(text, category):
    r = f.dispatch(text)
    assert r.is_allowed
    assert f"[{category}_REDACTED]" in r.sanitized_text
    assert all(i.level == "WARNING" for i in r.incidents)

@pytest.mark.parametrize("text", [
    "1234 5678 9012 3456",                                  # не проходит Luhn
    "commit 3f786850e387550fdab836ed7e6dc881de23001b",      # hex-хэш
    "123e4567-e89b-12d3-a456-426614174000",                 # UUID
    "Сценарий: убийца прячет орудие в лесу",                # тема, не артефакт
    "мессианство, грандиозность, хаос гипотез",
])
def test_passes_untouched(text):
    r = f.dispatch(text)
    assert r.sanitized_text == text and not r.incidents
    assert build_notice(r.incidents) is None

def test_raw_secret_never_in_incident():
    secret = "sk-ant-api03-AbCdEfGhIjKlMnOpQrStUv123"
    r = f.dispatch(f"x {secret} y")
    assert all(secret not in repr(i) for i in r.incidents)

def test_credential_keeps_key_name():
    assert f.dispatch("token=abcd1234").sanitized_text == "token=[CREDENTIAL_REDACTED]"
