"""CAPS_PROBE (30.10.2026): видит ли модель каталог функций -- в обход бота.

Разделяет две гипотезы:
  H1 -- каталог не доходит до модели в боте (не установлен / не применён / нет рестарта);
  H3 -- каталог доходит, но модель его игнорирует (system prompt слабее контекста).
Шлёт в Ollama тот же /api/generate, что governor._route_to_llm (prompt через
AgentGovernor._build_user_prompt, пустая история, поле "system"), в двух вариантах:
  A -- system = identity.md (как было до этапа A);
  B -- system = identity.md + capabilities.build_catalog() (как должно быть сейчас).
Ничего не пишет: ни в журнал бота, ни в память, ни в реестр. Текст ответов -- в консоль.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for p in (ROOT / "src", ROOT):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import httpx  # noqa: E402

import config  # noqa: E402
import capabilities  # noqa: E402
from governor import AgentGovernor, compose_system_prompt  # noqa: E402

QUESTIONS = ("какие функции тебе доступны", "включи подсветку под музыку")
CONTEXT = "История диалога:\n(начало диалога)\n\nДополнительные справочные данные:\n"


def main() -> int:
    ident_path = ROOT / "src" / "identity" / "identity.md"
    identity = ident_path.read_text(encoding="utf-8").strip() if ident_path.is_file() else None
    catalog = capabilities.build_catalog()
    variants = {"A identity": identity, "B identity+catalog": compose_system_prompt(identity, catalog)}
    url = f"http://{config.OLLAMA_HOST}:{config.OLLAMA_PORT}/api/generate"
    print(f"model={config.OLLAMA_MODEL} identity_chars={len(identity or '')} catalog_chars={len(catalog)}")
    with httpx.Client(timeout=300) as http:
        for q in QUESTIONS:
            prompt = AgentGovernor._build_user_prompt(q, CONTEXT)
            for name, system in variants.items():
                payload = {"model": config.OLLAMA_MODEL, "prompt": prompt, "stream": False}
                if system:
                    payload["system"] = system
                t0 = time.monotonic()
                try:
                    r = http.post(url, json=payload)
                    r.raise_for_status()
                    data = r.json()
                except Exception as e:
                    print(f"\n=== Q: {q} | {name} | FAIL {type(e).__name__}: {e}")
                    return 2
                ans = (data.get("response") or "").strip().replace("\n", " | ")
                print(f"\n=== Q: {q} | {name} | pe={data.get('prompt_eval_count')} "
                      f"{time.monotonic() - t0:.1f}s\n{ans[:500]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
