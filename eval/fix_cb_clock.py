import ast, sys
from pathlib import Path

def load(p):
    raw = p.read_bytes(); bom = raw.startswith(b"\xef\xbb\xbf")
    s = raw.decode("utf-8-sig"); nl = "\r\n" if "\r\n" in s else "\n"
    return s.replace("\r\n", "\n"), bom, nl

def save(p, s, bom, nl):
    ast.parse(s)
    p.write_bytes((b"\xef\xbb\xbf" if bom else b"") + s.replace("\n", nl).encode("utf-8"))

def after_imports(s, block):
    lines = s.split("\n")
    imps = [n for n in ast.parse(s).body if isinstance(n, (ast.Import, ast.ImportFrom))]
    at = imps[-1].end_lineno
    return "\n".join(lines[:at] + block.split("\n") + lines[at:])

src = Path(r"C:\oko\src\circuit_breaker.py")
s, bom, nl = load(src)
if "def _now()" not in s:
    n = s.count("time.monotonic()")
    s = s.replace("time.monotonic()", "_now()")
    s = after_imports(s, '''

def _now() -> float:
    """Единственная точка чтения времени в модуле. Тесты подменяют её ручными
    часами: на Windows time.monotonic() до Python 3.13 идёт шагом ~15.6 мс,
    и тайминговые тесты с запасом в 10 мс падали примерно в 1 из 6 прогонов."""
    return time.monotonic()
''')
    save(src, s, bom, nl); print(f"circuit_breaker.py: заменено вызовов: {n}")
else:
    print("circuit_breaker.py: уже применено")

tst = Path(r"C:\oko\src\test_circuit_breaker.py")
s, bom, nl = load(tst)
if "_advance(" not in s:
    n = s.count("time.sleep(")
    s = s.replace("time.sleep(", "_advance(")
    s = after_imports(s, '''
import pytest
import src.circuit_breaker as _cb


class _ManualClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


_CLOCK = _ManualClock()


@pytest.fixture(autouse=True)
def _manual_clock(monkeypatch):
    """Детерминированное время: автомат видит только ручные часы."""
    global _CLOCK
    _CLOCK = _ManualClock()
    monkeypatch.setattr(_cb, "_now", _CLOCK)
    yield _CLOCK


def _advance(seconds):
    _CLOCK.t += seconds
''')
    save(tst, s, bom, nl); print(f"test_circuit_breaker.py: заменено sleep: {n}")
else:
    print("test_circuit_breaker.py: уже применено")