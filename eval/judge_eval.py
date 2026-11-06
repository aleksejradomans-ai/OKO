"""Прогон судьи (_verify_compliance) на фиксированном eval-наборе против живой Ollama.

Запуск (изолированно от боевого реестра):
    $env:AI_BASE_DIR = "C:\\oko\\data_test"; $env:PYTHONIOENCODING = "utf-8"
    & $py C:\\oko\\eval\\judge_eval.py
Результат: таблица, матрица ошибок, точность; JSON прогона в data_test/eval_runs/
с отпечатком промпта судьи — чтобы сравнивать версии промпта между собой.
"""
import asyncio
import hashlib
import inspect
import json
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

import config  # noqa: E402
from src.governor import AgentGovernor  # noqa: E402

LABELS = ["PASS", "FAIL:POL_001", "HALT:POL_002"]


def label(result) -> str:
    v = result.verdict.value
    return f"{v}:{result.violation_id}" if result.violation_id and v != "PASS" else v


def judge_fingerprint(gov: AgentGovernor) -> str:
    src = inspect.getsource(type(gov)._verify_compliance)
    rules = gov.manifesto.HARD_PROHIBITION.text + gov.manifesto.HALT_CONDITION.text
    return hashlib.sha256((src + rules).encode("utf-8")).hexdigest()[:12]


async def main() -> int:
    cases = json.loads((Path(__file__).parent / "judge_cases.json").read_text(encoding="utf-8"))["cases"]
    gov = AgentGovernor()
    await gov.start()
    rows = []
    try:
        import os
        repeat = int(os.getenv("EVAL_REPEAT", "3"))
        for c in cases:
            got = [label(await gov._verify_compliance(c["text"])) for _ in range(repeat)]
            top = Counter(got).most_common(1)[0][0]
            rows.append({**c, "got": top, "runs": got, "stable": len(set(got)) == 1})
    finally:
        await gov.shutdown()

    scored = [r for r in rows if r.get("scored", True) and r["expected"] not in ("?", "-")]
    print(f"\n{'id':<13} {'expected':<14} {'got':<14} ok")
    for r in rows:
        if r in scored:
            mark = "✔" if r["got"] == r["expected"] else "✘"
        else:
            mark = "?" if r["expected"] == "?" else "·"
        flag = "" if r.get("stable", True) else f"  ~ {r['runs']}"
        print(f"{r['id']:<13} {r['expected']:<14} {r['got']:<14} {mark}{flag}")

    cm = Counter((r["expected"], r["got"]) for r in scored)
    got_labels = LABELS + sorted({r["got"] for r in scored} - set(LABELS))
    print("\nМатрица (строки = ожидалось, столбцы = получено):")
    print(" " * 14 + "".join(f"{g:<14}" for g in got_labels))
    for e in LABELS:
        print(f"{e:<14}" + "".join(f"{cm.get((e, g), 0):<14}" for g in got_labels))

    ok = sum(r["got"] == r["expected"] for r in scored)
    controls_broken = [r["id"] for r in scored if r["class"] == "control" and r["got"] != "PASS"]
    fp = judge_fingerprint(gov)
    print(f"\nТочность: {ok}/{len(scored)}   ложные срабатывания на контроле: {controls_broken or 'нет'}")
    unstable = [r["id"] for r in rows if not r.get("stable", True)]
    print(f"Нестабильные кейсы: {unstable or 'нет'}")
    print(f"Отпечаток судьи: {fp}")

    out = Path(config.BASE_DIR) / "eval_runs"
    out.mkdir(parents=True, exist_ok=True)
    f = out / f"judge_{time.strftime('%Y%m%d_%H%M%S')}_{fp}.json"
    f.write_text(json.dumps({"fingerprint": fp, "accuracy": [ok, len(scored)],
                             "controls_broken": controls_broken, "rows": rows},
                            ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Сохранено: {f}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
