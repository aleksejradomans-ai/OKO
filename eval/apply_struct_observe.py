import ast, py_compile, shutil, sys
from pathlib import Path
p = Path(sys.argv[1])
raw = p.read_bytes(); bom = raw.startswith(b"\xef\xbb\xbf")
s = raw.decode("utf-8-sig"); nl = "\r\n" if "\r\n" in s else "\n"; s = s.replace("\r\n", "\n")
if "_observe_structure" in s:
    print("[skip] уже применено"); sys.exit(0)
IMP = "from src.monitor import ComplianceEvent, ComplianceMonitor\n"
RET = "        return await self._enforce_compliance(raw, prompt)\n"
if s.count(IMP) != 1 or s.count(RET) != 1:
    print("[ABORT] якоря не найдены"); sys.exit(1)
s = s.replace(IMP, IMP + "from src.structural_check import check as structural_check\n")
s = s.replace(RET, """        self._observe_structure(raw, point="A")
        final = await self._enforce_compliance(raw, prompt)
        if not final.startswith("[ERR:"):
            self._observe_structure(final, point="B")
        return final

    def _observe_structure(self, text: str, point: str) -> None:
        \"\"\"Структурные проверки v1: ТОЛЬКО наблюдение (лог), поведение не меняется.
        A - исходная генерация до судьи; B - итог после судьи и коррекции
        (дефекты H5 наблюдались именно в выходе коррекции). Сбой проверки
        никогда не ломает ответ.\"\"\"
        try:
            sources = [t for t in (self.identity_text,
                                   self.manifesto.HARD_PROHIBITION.text,
                                   self.manifesto.HALT_CONDITION.text) if t]
            for issue in structural_check(text, sources=sources):
                logger.warning("STRUCT point=%s kind=%s detail=%s", point, issue.kind, issue.detail)
        except Exception as e:
            logger.error("Structural check failed (ignored): %s", e)
""")
ast.parse(s)
shutil.copy2(p, p.with_suffix(".py.prestruct"))
p.write_bytes((b"\xef\xbb\xbf" if bom else b"") + s.replace("\n", nl).encode("utf-8"))
py_compile.compile(str(p), doraise=True)
print("[DONE] наблюдение STRUCT подключено (точки A и B)")