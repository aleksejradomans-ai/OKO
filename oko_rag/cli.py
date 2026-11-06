import sys
import requests

from rag_config import OLLAMA_HOST, CHAT_MODEL, TOP_K
from retriever import build_context
from prompts import SYSTEM_INSTRUCTION


def ask(query: str, top_k: int = TOP_K, stream: bool = False) -> str:
    context, hits = build_context(query, top_k)

    print("=== Контекст, собранный для промпта ===")
    print(context)
    print("=== Конец контекста ===\n")

    prompt = (
        f"Контекст:\n{context}\n\n"
        f"Вопрос: {query}\n\n"
        f"Ответь на русском языке, опираясь только на контекст выше:"
    )

    resp = requests.post(
        f"{OLLAMA_HOST}/api/chat",
        json={
            "model": CHAT_MODEL,
            "messages": [
                {"role": "system", "content": SYSTEM_INSTRUCTION},
                {"role": "user", "content": prompt},
            ],
            "stream": False,
        },
        timeout=120,
    )
    resp.raise_for_status()
    answer = resp.json()["message"]["content"]

    print(answer)
    print("\n--- Источники ---")
    for h in hits:
        print(f"- {h['source']} (чанк {h['chunk']}, dist={h['distance']:.4f})")

    return answer


if __name__ == "__main__":
    query = " ".join(sys.argv[1:])
    if not query:
        print("Использование: python cli.py <вопрос>")
        sys.exit(1)
    ask(query)
