import requests
from rag_config import OLLAMA_HOST, EMBED_MODEL, embed_request_body


def get_embedding(text: str) -> list[float]:
    resp = requests.post(
        f"{OLLAMA_HOST}/api/embeddings",
        json=embed_request_body(EMBED_MODEL, text),  # EMBED_CPU (01.11): bge-m3 на CPU
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()["embedding"]
