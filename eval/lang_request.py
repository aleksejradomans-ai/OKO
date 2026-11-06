import ast, py_compile, sys
from pathlib import Path

def edit(path, pairs, marker):
    p = Path(path)
    raw = p.read_bytes(); bom = raw.startswith(b"\xef\xbb\xbf")
    s = raw.decode("utf-8-sig"); nl = "\r\n" if "\r\n" in s else "\n"; s = s.replace("\r\n", "\n")
    if marker in s:
        print(f"{p.name}: уже применено"); return
    for old, new in pairs:
        if s.count(old) != 1:
            print(f"[ABORT] {p.name}: якорь не найден: {old[:50]!r}"); sys.exit(1)
        s = s.replace(old, new)
    ast.parse(s)
    p.write_bytes((b"\xef\xbb\xbf" if bom else b"") + s.replace("\n", nl).encode("utf-8"))
    py_compile.compile(str(p), doraise=True)
    print(f"{p.name}: ok")

edit(r"C:\oko\src\structural_check.py", [
    ('_TRUNC_END = (',
     '# Явная просьба Оператора ответить на другом языке: русский - умолчание,\n'
     '# а не запрет (identity.md: Оператор принимает решения).\n'
     '_LANG_REQUEST = re.compile(\n'
     '    r"на англ\\w*|по-англ\\w*|на (\\w+) язык\\w*|по-(немец|франц|испан|латыш)\\w*|"\n'
     '    r"in english|answer in \\w+|reply in \\w+",\n'
     '    re.I,\n'
     ')\n'
     '_TRUNC_END = ('),
    ('def check(answer: str, sources: Iterable[str] = ()) -> List[Issue]:',
     'def check(answer: str, sources: Iterable[str] = (), question: str = "") -> List[Issue]:'),
    ('    found = [check_truncated(answer), check_language(answer), check_leak(answer, list(sources))]',
     '    lang = None if _LANG_REQUEST.search(question or "") else check_language(answer)\n'
     '    found = [check_truncated(answer), lang, check_leak(answer, list(sources))]'),
], "_LANG_REQUEST")

edit(r"C:\oko\src\test_structural_check.py", [
    ('@pytest.mark.parametrize("answer", CONTROL)',
     'EN = "A circuit breaker stops calls to a failing service. It reopens after a cooldown."\n\n\n'
     'def test_language_skipped_on_explicit_request():\n'
     '    q = "Ответь только на английском: what is a circuit breaker?"\n'
     '    assert check(EN, sources=[IDENTITY], question=q) == []\n\n\n'
     'def test_language_flagged_without_request():\n'
     '    q = "Что такое circuit breaker?"\n'
     '    assert [i.kind for i in check(EN, sources=[IDENTITY], question=q)] == ["language"]\n\n\n'
     '@pytest.mark.parametrize("answer", CONTROL)'),
], "test_language_skipped_on_explicit_request")

edit(r"C:\oko\src\governor.py", [
    ('        self._observe_structure(raw, point="A")',
     '        self._observe_structure(raw, point="A", question=user_input)'),
    ('            self._observe_structure(final, point="B")',
     '            self._observe_structure(final, point="B", question=user_input)'),
    ('    def _observe_structure(self, text: str, point: str) -> None:',
     '    def _observe_structure(self, text: str, point: str, question: str = "") -> None:'),
    ('            for issue in structural_check(text, sources=sources):',
     '            for issue in structural_check(text, sources=sources, question=question):'),
], "question=user_input")