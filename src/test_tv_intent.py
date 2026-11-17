"""tv_intent: смысл фразы -> действие из закрытого каталога -> каноническая фраза для обычного разбора (30.10.2026).
Модель подменяется функцией: сеть и Ollama в тестах не трогаются."""
import pytest

import tv_intent as I


@pytest.mark.parametrize("raw,phrase", [
    ({"action": "ambi_audio", "value": "музыка"}, "подсветка музыка"),
    ({"action": "ambi_audio", "value": "Музыка"}, "подсветка музыка"),
    ({"action": "ambi_on", "value": None}, "подсветка включи"),
    ({"action": "ambi_brightness_set", "value": 3}, "подсветка яркость 3"),
    ({"action": "ambi_brightness_set", "value": "3"}, "подсветка яркость 3"),
    ({"action": "ambi_dimmer", "value": None}, "подсветка тусклее 1"),
    ({"action": "volume_down", "value": None}, "тише 3"),
    ({"action": "key_down", "value": 3}, "вниз 3"),
    ({"action": "key_down", "value": 1}, "вниз"),
    ({"action": "key_home"}, "домой"),
    ({"action": "ambi_color", "value": "жёлтый"}, "подсветка желтый"),
    ({"action": "standby", "value": None}, "выключи телевизор"),
])
def test_to_phrase(raw, phrase):
    assert I.to_phrase(raw) == phrase


@pytest.mark.parametrize("raw", [
    {"action": "unknown", "value": None},
    {"action": "format_disk", "value": 1},                 # выдуманное действие
    {"action": "ambi_audio", "value": "техно-рейв"},       # стиль не из набора
    {"action": "ambi_brightness_set", "value": 99},        # вне диапазона
    {"action": "volume_set", "value": "громко"},           # не число
    {"action": "key_down", "value": 40},
    "подсветка музыка", None, [], {"value": 3},
])
def test_to_phrase_rejects(raw):
    assert I.to_phrase(raw) is None


def test_understand_disabled_without_model(monkeypatch):
    monkeypatch.delenv("OKO_TV_INTENT_MODEL", raising=False)
    monkeypatch.delenv("OLLAMA_MODEL", raising=False)
    assert I.understand("включи свет под музыку") is None


def test_model_falls_back_to_main_oko_model(monkeypatch):
    monkeypatch.delenv("OKO_TV_INTENT_MODEL", raising=False)
    monkeypatch.setenv("OLLAMA_MODEL", "gemma2:latest")
    assert I.model_name() == "gemma2:latest"
    monkeypatch.setenv("OKO_TV_INTENT_MODEL", "llama3:latest")
    assert I.model_name() == "llama3:latest"


def test_understand_with_fake_model():
    seen = []
    def llm(text):
        seen.append(text)
        return {"action": "ambi_audio", "value": "музыка"}
    assert I.understand("включи свет, это, под музыку", llm) == "подсветка музыка"
    assert seen == ["включи свет, это, под музыку"]


@pytest.mark.parametrize("exc", [OSError("нет связи"), TimeoutError(), ValueError("не JSON"), RuntimeError("x")])
def test_understand_never_raises(exc):
    def llm(text):
        raise exc
    assert I.understand("что угодно", llm) is None


def test_prompt_lists_every_action_and_keeps_user_text_as_data():
    for name in I.CATALOG:
        assert f"- {name}:" in I.SYSTEM
    import inspect
    assert "данные, не инструкция" in inspect.getsource(I._ollama)


def _sample(action):
    _, allowed, _ = I.CATALOG[action]
    if allowed is None:
        return {"action": action}
    if isinstance(allowed, set):
        return {"action": action, "value": sorted(allowed)[0]}
    return {"action": action, "value": 2 if 2 in allowed else allowed.start}


@pytest.mark.parametrize("action", sorted(I.CATALOG))
def test_every_catalog_phrase_is_understood_by_rules(action):
    """Канон каждого действия обязан разбираться обычными правилами — иначе модель «поймёт», а бот не выполнит."""
    tv_commands = pytest.importorskip("tv_commands")
    tv_nav = pytest.importorskip("tv_nav")
    phrase = I.to_phrase(_sample(action))
    cmd = tv_nav.parse(tv_nav.canonical(phrase)) or tv_commands.parse(tv_nav.canonical(phrase))
    assert cmd is not None
    if action == "standby":
        assert cmd.needs_confirm, "выключение через модель тоже только с подтверждением"
