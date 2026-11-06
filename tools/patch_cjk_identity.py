"""CJK_GUARD + IDENTITY_V3 (02.11.2026, решения Оператора A=да, Б=да).

governor.py: CJK_GUARD -- иероглифы в итоговом ответе execute_request -> ОДИН повтор с напоминанием
             «только русский»; снова иероглифы или сбой повтора -> иероглифы вырезаются + пометка
             «⚠️ Модель сбилась на другой язык, ответ очищен.». Повтор мимо судьи (меняется язык, не смысл).
             Лог [CJK_GUARD] cjk=N retry=ok|cjk:M|error. Служебных кодов не добавляет.
identity.md: v3 (1501 симв. вместо 2970; A/B 02.11 21:48: старый -- 5 нарушений из 9 (4 ответа по-английски,
             текст вокруг кода), v3 -- 1 из 9 (CJK, его закрывает CJK_GUARD); pe 1135 -> 552).
             Фразы, от которых зависит код («Не распознал запрос. Сформулируй задачу.» -- P1; «На связи. Что нужно?»),
             сохранены дословно. BOM и переводы строк identity.md -- как у старого файла.

После установки: новая граница OKO_DRIFT_BASELINE_SINCE (метрика S меряет похожесть на identity) -- блок деплоя.
Якоря + хэши «до/после», all-or-nothing, идемпотентно, бэкап *.bak_cjk_identity.
Запуск из C:\\oko: python tools\\patch_cjk_identity.py [--apply]. Коды: 0 OK/ALREADY/DRY-OK, 2 STOP.
"""
import hashlib
import pathlib
import shutil
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent

IDENTITY_V3 = """РОЛЬ: Око (Система 0) — аналитическое расширение Оператора. Ты обрабатываешь, решения принимает Оператор.

ЯЗЫК: Отвечай только на русском языке. Если вопрос, контекст или инструкции на английском или другом языке — переведи ответ на русский. Иероглифы (китайские, японские) в ответе запрещены полностью. Другой язык — только если Оператор прямо об этом попросил.
Пример: «What is TCP?» → «TCP — протокол транспортного уровня с установкой соединения и гарантией доставки.»

ОТВЕТ:
- Обращайся на «ты», отвечай на содержание ввода. Не пересказывай ввод («Пользователь ввёл…», «Запрос представляет собой…») и не начинай ответ с «Твой запрос», «Ваш запрос», «Запрос».
- Отвечай сразу по сути: без вступлений, без «Предполагаю…», без приветствия перед ответом.
- Кратко, технически, списками; без длинных абзацев.
- Просят код — ответ состоит только из блока кода: ни строки текста до и после, если пояснение не просили.
- Дал ответ — остановись: без уточняющих вопросов в конце.
- Без эмпатических клише, извинений, имитации чувств и непрошеных советов.
- Проверяй ввод на логические пробелы; в ответ выводи только найденный пробел. Не хватает данных — назови, чего именно.

КОРОТКИЙ ВВОД (только если во вводе нет вопроса и задачи):
- Приветствие или реплика → «На связи. Что нужно?»
- Один термин → определение одним предложением и 2–4 пункта; не переспрашивай.
- Бессмысленный набор символов → «Не распознал запрос. Сформулируй задачу.» Не цитируй ввод.
- «статус» → «Статус системы: команда /status.»"""

GOV_CONST_OLD = """DETERMINISTIC_OPTIONS = {"temperature": 0, "seed": 0}
"""
GOV_CONST_NEW = """DETERMINISTIC_OPTIONS = {"temperature": 0, "seed": 0}

# CJK_GUARD (02.11): qwen дрейфует в иероглифы посреди русского ответа (A/B identity: «Чтобы列出 PowerShell
# 中的服务…»). Промпт снижает частоту, но не убирает -- проверка в коде. Классы -- как в response_metrics._CJK.
CJK_RE = re.compile(r"[\\u3040-\\u30ff\\u3400-\\u4dbf\\u4e00-\\u9fff\\uf900-\\ufaff\\uac00-\\ud7af]")
_CJK_PUNCT_RE = re.compile(r"[\\u3000-\\u303f\\uff01-\\uff60]")
CJK_RETRY_SUFFIX = "\\n\\nВажно: ответь только на русском языке. Иероглифы и другие языки не используй."
CJK_NOTE = "⚠️ Модель сбилась на другой язык, ответ очищен.\\n"


def cjk_strip(text: str) -> str:
    \"\"\"Вырезает иероглифы и полноширинную пунктуацию, сжимает пробелы; впереди -- пометка.\"\"\"
    cleaned = _CJK_PUNCT_RE.sub("", CJK_RE.sub("", text or ""))
    cleaned = re.sub(r"[ \\t]{2,}", " ", cleaned)
    cleaned = re.sub(r"\\n{3,}", "\\n\\n", cleaned).strip()
    return CJK_NOTE + cleaned if cleaned else CJK_NOTE.strip()
"""

GOV_CALL_OLD = """        final = await self._enforce_compliance(raw, prompt)
        if not is_service_marker(final):
            self._observe_structure(final, point="B", question=user_input)
"""
GOV_CALL_NEW = """        final = await self._enforce_compliance(raw, prompt)
        final = await self._cjk_guard(final, prompt)          # CJK_GUARD (02.11)
        if not is_service_marker(final):
            self._observe_structure(final, point="B", question=user_input)
"""

GOV_METHOD_OLD = """    def _observe_structure(self, text: str, point: str, question: str = "") -> None:
"""
GOV_METHOD_NEW = """    async def _cjk_guard(self, text: str, prompt: str) -> str:
        \"\"\"CJK_GUARD (02.11, решение Оператора A): иероглифы в ответе -> один повтор с напоминанием;
        снова иероглифы или сбой повтора -> иероглифы вырезаются + пометка. Повтор идёт мимо судьи:
        исходный ответ его уже прошёл, меняется язык, не смысл.\"\"\"
        if not text or is_service_marker(text):
            return text
        n = len(CJK_RE.findall(text))
        if not n:
            return text
        try:
            with _ctx_call("cjk_retry"):
                retry = await self._call_llm_with_timeout(
                    prompt + CJK_RETRY_SUFFIX, system_prompt=self._main_system())
        except Exception as e:
            logger.warning("[CJK_GUARD] cjk=%d retry=error %s -> stripped", n, type(e).__name__)
            return cjk_strip(text)
        retry = (retry or "").strip()
        m = len(CJK_RE.findall(retry))
        if retry and not m:
            logger.info("[CJK_GUARD] cjk=%d retry=ok", n)
            return retry
        logger.warning("[CJK_GUARD] cjk=%d retry=cjk:%d -> stripped", n, m)
        return cjk_strip(retry if retry and m < n else text)

    def _observe_structure(self, text: str, point: str, question: str = "") -> None:
"""


def h16(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()[:16].upper()


def _decode(raw: bytes):
    bom = raw.startswith(b"\xef\xbb\xbf")
    text = (raw[3:] if bom else raw).decode("utf-8")
    return bom, text, ("\r\n" if "\r\n" in text else "\n")


def _encode(text: str, bom: bool) -> bytes:
    out = text.encode("utf-8")
    return b"\xef\xbb\xbf" + out if bom else out


def build_governor(raw: bytes):
    bom, text, nl = _decode(raw)
    for old, new in ((GOV_CONST_OLD, GOV_CONST_NEW), (GOV_CALL_OLD, GOV_CALL_NEW), (GOV_METHOD_OLD, GOV_METHOD_NEW)):
        o, n = old.replace("\n", nl), new.replace("\n", nl)
        c = text.count(o)
        if c != 1:
            return None, f"anchor count={c}: {old.strip().splitlines()[0][:60]}"
        text = text.replace(o, n)
    return _encode(text, bom), None


def build_identity(raw: bytes):
    bom, _old, nl = _decode(raw)
    return _encode(IDENTITY_V3.replace("\n", nl) + nl, bom), None


TARGETS = [
    ("src\\governor.py", "9290EE889AB18FDB", {"4A9B762259AA3A12"}, build_governor),
    ("src\\identity\\identity.md", "AEBF398B93492A9F", {"5B2C16E593CB7B10", "DAFD57C7F732473E", "F92CB4BEE3A72552", "FBEDB9CE18B401CE"}, build_identity),
]


def main() -> int:
    apply = "--apply" in sys.argv
    plan = []
    for rel, before, afters, build in TARGETS:
        p = ROOT.joinpath(*rel.split("\\"))
        if not p.is_file():
            print(f"STOP missing {rel}")
            return 2
        raw = p.read_bytes()
        cur = h16(raw)
        if cur in afters:
            print(f"ALREADY {rel} {cur}")
            continue
        if cur != before:
            print(f"STOP hash {rel} have={cur} want={before}")
            return 2
        new, err = build(raw)
        if err:
            print(f"STOP {rel} {err}")
            return 2
        if h16(new) not in afters:
            print(f"STOP result {rel} got={h16(new)} want one of {sorted(afters)}")
            return 2
        plan.append((p, rel, new))
        print(f"{'APPLY' if apply else 'DRY-OK'} {rel} {before} -> {h16(new)}")
    if not apply or not plan:
        return 0
    for p, rel, new in plan:
        shutil.copy2(p, p.with_name(p.name + ".bak_cjk_identity"))
    for p, rel, new in plan:
        p.write_bytes(new)
        print(f"WROTE {rel} {h16(p.read_bytes())}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
