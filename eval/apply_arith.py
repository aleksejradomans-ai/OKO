import ast, py_compile, sys
from pathlib import Path

ARITH = r'''

# --- Арифметика (детерминированно, вместо неявного фактчекинга судьи) ---
_NUM = r"-?\d+(?:[.,]\d+)?"
_LB = r"(?<![\d.,])(?<!\d )"          # не хвост числа «1 000» и не «2.0.1»
_LA = r"(?![\d.,]?\d)(?! \d)"         # не начало длинного числа
_BIN = re.compile(_LB + rf"({_NUM})(?:\s*([×*·/÷+−])\s*|\s+([x-])\s+)({_NUM})\s*=\s*({_NUM})" + _LA)
_SQRT = re.compile(rf"√\s*({_NUM})\s*=\s*({_NUM})" + _LA)
_PCT = re.compile(_LB + rf"({_NUM})\s*%\s*от\s*({_NUM})\s*(?:—|–|-|=|это|равно|составляет)\s*(?:это\s*)?({_NUM})" + _LA, re.I)


def _f(x: str) -> Fraction:
    return Fraction(x.replace(",", "."))


def _decimals(x: str) -> int:
    x = x.replace(",", ".")
    return len(x.split(".")[1]) if "." in x else 0


def _matches(actual: Fraction, stated: str) -> bool:
    """Сравнение с точностью, которую заявил сам ответ: «10 / 3 = 3.33» верно."""
    d = _decimals(stated)
    return round(actual, d) == round(_f(stated), d)


def check_arithmetic(answer: str) -> Optional[Issue]:
    prose = _strip_code(answer or "")
    for m in _BIN.finditer(prose):
        a, op, b, res = _f(m.group(1)), m.group(2) or m.group(3), _f(m.group(4)), m.group(5)
        if op in "/÷" and b == 0:
            continue
        actual = {"×": a * b, "*": a * b, "·": a * b, "x": a * b,
                  "/": a / b if b else None, "÷": a / b if b else None,
                  "+": a + b, "−": a - b, "-": a - b}[op]
        if not _matches(actual, res):
            return Issue("arith_mismatch", f"{m.group(0).strip()} (верно: {float(actual):g})")
    for m in _SQRT.finditer(prose):
        n, res = m.group(1), m.group(2)
        root = Fraction(math.sqrt(float(_f(n)))).limit_denominator(10**6)
        if _f(n) >= 0 and not _matches(root, res):
            return Issue("arith_mismatch", f"{m.group(0).strip()} (верно: {float(root):g})")
    for m in _PCT.finditer(prose):
        p, base, res = _f(m.group(1)), _f(m.group(2)), m.group(3)
        actual = p * base / 100
        if not _matches(actual, res):
            return Issue("arith_mismatch", f"{m.group(0).strip()} (верно: {float(actual):g})")
    return None
'''

TESTS = '''

ARITH_OK = [
    "17 × 23 = 391.", "12 × 12 = 144", "100 / 4 = 25", "√81 = 9", "15% от 200 — это 30.",
    "10 / 3 = 3.33", "Диапазон 10-20 = допустимый", "1 000 × 2 = 2 000", "Версия 2.0 = стабильная",
    "2 + 2 = 4, а 3 + 3 = 6.", "0,5 + 0,25 = 0,75", "7 x 8 = 56", "```\\nx = 5 * 3 = 99\\n```",
]
ARITH_BAD = ["12 × 12 = 150", "17 × 23 = 400.", "√81 = 8", "15% от 200 — это 40", "100 / 4 = 20", "2 + 2 = 5, конечно."]


@pytest.mark.parametrize("answer", ARITH_OK)
def test_arith_correct_or_unparsed_is_silent(answer):
    assert "arith_mismatch" not in [i.kind for i in check(answer)]


@pytest.mark.parametrize("answer", ARITH_BAD)
def test_arith_mismatch_detected(answer):
    assert "arith_mismatch" in [i.kind for i in check(answer)]
'''


def rw(path, fn):
    p = Path(path)
    raw = p.read_bytes(); bom = raw.startswith(b"\xef\xbb\xbf")
    s = raw.decode("utf-8-sig"); nl = "\r\n" if "\r\n" in s else "\n"; s = s.replace("\r\n", "\n")
    s = fn(s)
    if s is None:
        return
    ast.parse(s)
    p.write_bytes((b"\xef\xbb\xbf" if bom else b"") + s.replace("\n", nl).encode("utf-8"))
    py_compile.compile(str(p), doraise=True)
    print(f"{p.name}: ok")


def mod(s):
    if "def check_arithmetic" in s:
        print("structural_check.py: уже применено"); return None
    FOUND = "    found = [check_truncated(answer), lang, check_leak(answer, list(sources))]"
    for anchor in ("\nimport re\n", "\n\ndef _words(text: str)", FOUND):
        if s.count(anchor) != 1:
            print(f"[ABORT] якорь: {anchor!r}"); sys.exit(1)
    s = s.replace("\nimport re\n", "\nimport math\nimport re\nfrom fractions import Fraction\n")
    s = s.replace("\n\ndef _words(text: str)", ARITH + "\n\ndef _words(text: str)")
    s = s.replace(FOUND, FOUND[:-1] + ",\n             check_arithmetic(answer)]")
    return s


def tst(s):
    if "ARITH_OK" in s:
        print("test_structural_check.py: уже применено"); return None
    return s.rstrip("\n") + "\n" + TESTS


rw(r"C:\oko\src\structural_check.py", mod)
rw(r"C:\oko\src\test_structural_check.py", tst)