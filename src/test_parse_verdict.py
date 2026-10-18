"""Red-Team Сценарий 5: судья выдаёт 'FAIL :POL_001' (пробел перед
двоеточием) — проверяем, что парсер уходит в ERROR (fail-safe), а не
молча пропускает как PASS и не падает с исключением.

Запуск: pytest test_parse_verdict.py -v
Положить рядом с governor.py, либо в стандартный tests/ проекта — путь
не принципиален, важен только импорт `from src.governor import ...`,
соответствующий структуре пакета C:\\oko\\src.
"""
from src.governor import AgentGovernor, ComplianceVerdict
from src.manifesto import Manifesto


def test_verdict_pass():
    result = AgentGovernor._parse_verdict("PASS", Manifesto())
    assert result.verdict == ComplianceVerdict.PASS
    assert result.violation_id is None


def test_verdict_fail_with_id():
    result = AgentGovernor._parse_verdict("FAIL:POL_001", Manifesto())
    assert result.verdict == ComplianceVerdict.FAIL
    assert result.violation_id == "POL_001"


def test_verdict_halt_with_id():
    result = AgentGovernor._parse_verdict("HALT:POL_002", Manifesto())
    assert result.verdict == ComplianceVerdict.HALT
    assert result.violation_id == "POL_002"


def test_verdict_with_space_before_colon_fails_safe():
    """Сценарий 5. Пробел перед двоеточием не матчится regex'ом —
    fail-safe в ERROR, не fail-open в PASS."""
    result = AgentGovernor._parse_verdict("FAIL :POL_001", Manifesto())
    assert result.verdict == ComplianceVerdict.ERROR


def test_verdict_unknown_rule_id_fails_safe():
    result = AgentGovernor._parse_verdict("FAIL:POL_999", Manifesto())
    assert result.verdict == ComplianceVerdict.ERROR


def test_verdict_garbage_fails_safe():
    result = AgentGovernor._parse_verdict("MAYBE", Manifesto())
    assert result.verdict == ComplianceVerdict.ERROR


def test_verdict_empty_fails_safe():
    result = AgentGovernor._parse_verdict("", Manifesto())
    assert result.verdict == ComplianceVerdict.ERROR
