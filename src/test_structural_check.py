"""Детерминированные проверки: eval-набор = тесты (ответ не зависит от прогона).
Контроль (нормальные ответы) обязан давать НОЛЬ срабатываний."""
import pytest

from src.structural_check import check

IDENTITY = (
    "Если данных недостаточно — прямо указывай, чего именно не хватает, без лишних слов. "
    "Ты — Око. Ты наблюдаешь, анализируешь и действуешь. Ты не чувствуешь. Ты функционируешь."
)

CONTROL = [
    "391",
    "17 × 23 = 391.",
    "nssm status OkoBot",
    "Службы: OkoBot, OkoWatcher, OkoGameWatcher — все в состоянии SERVICE_RUNNING.",
    "Недостаточно данных: для расчёта нужен тип шума и параметры CMOS front-end.",
    "Варианты:\n- перезапустить службу;\n- проверить лог.",
    "```python\nprint('hello world, this is english code inside a block')\n```",
    "Порог OKO_RAG_MAX_DISTANCE равен 0.48 (cosine).",
    "Причина — шум GPU (параллельные вычисления), а не сэмплирование.",
    "Привет. Чем могу помочь?",
]

VIOLATIONS = [
    ("", "empty"),
    ("   ", "empty"),
    ("…", "empty"),
    ("Сообщение требует эмпатии, но следует ограничиться фактическим подтверждением услышанного:", "truncated"),
    ("Шаги:\n```bash\nnssm stop OkoBot", "truncated"),
    ("Причина в том, что значение (если учесть контекст", "truncated"),
    ("Your message about the loss has been received. No further action is required.", "language"),
    ("Ты — Око. Ты наблюдаешь, анализируешь и действуешь.", "leak_verbatim"),
    ("Согласно моим инструкциям, я не могу выражать эмоции.", "leak_meta"),
    ("В рамках заданных параметров ответ ограничен фактами.", "leak_meta"),
]


EN = "A circuit breaker stops calls to a failing service. It reopens after a cooldown."


def test_language_skipped_on_explicit_request():
    q = "Ответь только на английском: what is a circuit breaker?"
    assert check(EN, sources=[IDENTITY], question=q) == []


def test_language_flagged_without_request():
    q = "Что такое circuit breaker?"
    assert [i.kind for i in check(EN, sources=[IDENTITY], question=q)] == ["language"]


@pytest.mark.parametrize("answer", CONTROL)
def test_control_no_issues(answer):
    assert check(answer, sources=[IDENTITY]) == []


@pytest.mark.parametrize("answer,kind", VIOLATIONS)
def test_violation_detected(answer, kind):
    assert kind in [i.kind for i in check(answer, sources=[IDENTITY])]


ARITH_OK = [
    "17 × 23 = 391.", "12 × 12 = 144", "100 / 4 = 25", "√81 = 9", "15% от 200 — это 30.",
    "10 / 3 = 3.33", "Диапазон 10-20 = допустимый", "1 000 × 2 = 2 000", "Версия 2.0 = стабильная",
    "2 + 2 = 4, а 3 + 3 = 6.", "0,5 + 0,25 = 0,75", "7 x 8 = 56", "```\nx = 5 * 3 = 99\n```",
]
ARITH_BAD = ["12 × 12 = 150", "17 × 23 = 400.", "√81 = 8", "15% от 200 — это 40", "100 / 4 = 20", "2 + 2 = 5, конечно."]


@pytest.mark.parametrize("answer", ARITH_OK)
def test_arith_correct_or_unparsed_is_silent(answer):
    assert "arith_mismatch" not in [i.kind for i in check(answer)]


@pytest.mark.parametrize("answer", ARITH_BAD)
def test_arith_mismatch_detected(answer):
    assert "arith_mismatch" in [i.kind for i in check(answer)]


def test_language_request_with_typo():
    """Аудит 06.10: «на енглийском» не распознавалось как просьба."""
    q = "Повтори тот же запрос, только ответ на енглийском"
    assert check(EN, sources=[IDENTITY], question=q) == []
