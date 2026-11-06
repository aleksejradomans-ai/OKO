"""Строит ПАРАЛЛЕЛЬНУЮ коллекцию ChromaDB с эмбеддингами bge-m3.

Берёт чанки (тексты, id и метаданные) из текущей коллекции и заново
считает для них векторы новой моделью. Чанкинг не повторяется, поэтому
индекс остаётся тем же по составу; старая коллекция НЕ изменяется.
Новая коллекция создаётся в косинусном пространстве (distance = 1 - cos),
шкала 0..2 вместо сотен у L2 — по ней потом можно осмысленно ставить порог.

Режимы:
    python rebuild_index_bge_m3.py             # проверка плана, без эмбеддингов
    python rebuild_index_bge_m3.py --build     # построить новую коллекцию
    python rebuild_index_bge_m3.py --build --rebuild   # пересоздать НОВУЮ
    python rebuild_index_bge_m3.py --probe "запрос 1" "запрос 2"   # сравнить выдачу

Требования: модель скачана (ollama pull bge-m3), бот можно не останавливать
(скрипт только читает старую коллекцию), но Ollama должна работать.
Запуск из C:\\oko\\src с PYTHONPATH="C:\\oko;C:\\oko\\src".
"""
from __future__ import annotations

import argparse
import pathlib
import sys

NEW_COLLECTION = "oko_knowledge_bge_m3"
MODEL = "bge-m3"
BATCH = 16
DEFAULT_PROBES = [
    "привет",
    "minimum size of neural dust node at SNR of 3",
    "Какой минимальный размер neural dust узла при SNR=3?",
    "статус интеграции RAG контура",
]


def _oko_rag():
    sys.path.append(str(pathlib.Path(__file__).resolve().parent.parent / "oko_rag"))
    import rag_config  # noqa: E402
    import indexer  # noqa: E402
    return rag_config, indexer


def _ollama_base() -> str:
    import config
    return f"http://{config.OLLAMA_HOST}:{config.OLLAMA_PORT}"


def _model_available(model: str) -> bool:
    import requests
    resp = requests.get(f"{_ollama_base()}/api/tags", timeout=15)
    resp.raise_for_status()
    names = [m.get("name", "") for m in resp.json().get("models", [])]
    return any(n == model or n.startswith(model + ":") for n in names)


def _embed(text: str, model: str):
    import requests
    import config
    resp = requests.post(
        f"{_ollama_base()}/api/embeddings",
        json=config.embed_request_body(model, text),  # EMBED_CPU (01.11)
        timeout=120,
    )
    resp.raise_for_status()
    return resp.json()["embedding"]


def _try_get(client, name):
    try:
        return client.get_collection(name=name)
    except Exception:
        return None


def _probe(new_col, queries, model):
    for q in queries:
        print(f"\n== {q}")
        vec = _embed(q, model)
        res = new_col.query(query_embeddings=[vec], n_results=5, include=["metadatas", "distances"])
        for meta, dist in zip(res["metadatas"][0], res["distances"][0]):
            print(f"  {dist:6.3f}  {str(meta.get('source'))[:48]}  чанк {meta.get('chunk')}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", action="store_true", help="реально построить коллекцию")
    ap.add_argument("--rebuild", action="store_true", help="пересоздать НОВУЮ коллекцию, если она уже есть")
    ap.add_argument("--model", default=MODEL)
    ap.add_argument("--new", default=NEW_COLLECTION, help="имя новой коллекции")
    ap.add_argument("--probe", nargs="*", default=None, help="запросы для сравнения выдачи")
    args = ap.parse_args(argv)

    rag_config, indexer = _oko_rag()
    client = indexer.get_client()
    old_name = rag_config.COLLECTION_NAME
    if args.new == old_name:
        print("ОТКАЗ: имя новой коллекции совпадает со старой.")
        return 2

    old = client.get_collection(name=old_name)
    new = _try_get(client, args.new)

    # --- только сравнение выдачи ---
    if args.probe is not None:
        if new is None:
            print(f"Новой коллекции {args.new} ещё нет — сначала --build.")
            return 2
        _probe(new, args.probe or DEFAULT_PROBES, args.model)
        return 0

    data = old.get(include=["documents", "metadatas"])
    ids, docs, metas = data["ids"], data["documents"], data["metadatas"]
    total_chars = sum(len(d) for d in docs)
    print(f"Старая коллекция : {old_name}  чанков: {len(ids)}  символов: {total_chars}")
    print(f"Новая коллекция  : {args.new}  ({'уже существует, чанков: ' + str(new.count()) if new else 'ещё нет'})")
    print(f"Модель эмбеддингов: {args.model}")

    try:
        available = _model_available(args.model)
    except Exception as e:
        print(f"ОТКАЗ: Ollama недоступна ({e}).")
        return 2
    print(f"Модель в Ollama   : {'найдена' if available else 'НЕ найдена -> ollama pull ' + args.model}")
    if not available:
        return 2
    if not ids:
        print("В старой коллекции нет чанков — строить нечего.")
        return 0

    if not args.build:
        print("\nПЛАН (ничего не создано). Для построения добавьте --build.")
        return 0

    if new is not None:
        if not args.rebuild:
            print(f"ОТКАЗ: {args.new} уже существует. Добавьте --rebuild, чтобы пересоздать её "
                  f"(старая коллекция {old_name} затронута не будет).")
            return 2
        client.delete_collection(name=args.new)

    new = client.get_or_create_collection(name=args.new, metadata={"hnsw:space": "cosine"})

    failed = []
    for start in range(0, len(ids), BATCH):
        b_ids, b_emb, b_meta, b_docs = [], [], [], []
        for i in range(start, min(start + BATCH, len(ids))):
            try:
                vec = _embed(docs[i], args.model)
            except Exception as e:
                failed.append((ids[i], str(e)[:80]))
                continue
            b_ids.append(ids[i]); b_emb.append(vec); b_meta.append(metas[i]); b_docs.append(docs[i])
        if b_ids:
            new.add(ids=b_ids, embeddings=b_emb, metadatas=b_meta, documents=b_docs)
        print(f"  {min(start + BATCH, len(ids))}/{len(ids)}")

    print(f"\nПостроено: {new.count()} из {len(ids)} чанков, ошибок эмбеддинга: {len(failed)}")
    for cid, err in failed[:10]:
        print(f"  не удалось: {cid}  ({err})")
    if failed:
        print("Коллекция неполная — не переключайте на неё бота. Повторите с --build --rebuild.")
        return 3

    print(f"\nОК. Старая коллекция {old_name} не изменена. Сравните выдачу:")
    print("    python rebuild_index_bge_m3.py --probe")
    _probe(new, DEFAULT_PROBES, args.model)
    return 0


if __name__ == "__main__":
    sys.exit(main())
