"""forget_document.py — полностью убрать документ из Ока: база знаний, поиск, все копии файлов (28.10.2026).

Запрос Оператора: «файл можем полностью удалить, он ненужен». Следы документа в системе:
  SQLite knowledge          — строки с таким именем файла (и все строки с тем же текстом, content_hash);
  ChromaDB (RAG)            — чанки с этими content_hash (иначе поиск продолжит находить удалённый текст);
  файлы в data/             — knowledge/, archived/, archived/rejected/, staging/, backups/*.bak, incoming/,
                              print_queue/ (с картинками страниц), print_probe_in/;
  %LOCALAPPDATA%\\oko         — print_out/, print_probe/ (консольные прогоны печати), и то же у SYSTEM.
Без --apply — только список (ничего не удаляется). С --apply: сначала резервная копия SQLite
(knowledge_base.db.before_forget_<время>, через sqlite backup API — корректно при WAL), потом удаление.
Оригинал в «Загрузках» Оператора не трогается — это его файл, не системный след.

Запуск:  python forget_document.py LATVIJA            # список
         python forget_document.py LATVIJA --apply    # удалить
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from pathlib import Path

import config

SYSTEM_LOCAL = Path(r"C:\Windows\System32\config\systemprofile\AppData\Local\oko")


def file_traces(prefix: str, base: Path, extra_roots: list[Path]) -> list[Path]:
    low = prefix.lower()
    roots = [base / d for d in ("knowledge", "staging", "backups", "incoming", "print_queue", "print_probe_in")]
    roots += extra_roots
    out = []
    for r in roots:
        try:
            if not r.is_dir():
                continue
            for p in r.rglob("*"):
                if p.is_file() and low in p.name.lower():
                    out.append(p)
        except OSError as e:          # профиль SYSTEM из консоли пользователя может быть закрыт
            print(f"   [нет доступа] {r}: {type(e).__name__}")
    return sorted(set(out))


def db_traces(conn: sqlite3.Connection, prefix: str) -> tuple[list[tuple], set[str]]:
    rows = conn.execute("SELECT id, filename, content_hash FROM knowledge WHERE lower(filename) LIKE ?",
                        (f"%{prefix.lower()}%",)).fetchall()
    hashes = {r[2] for r in rows if r[2]}
    if hashes:
        q = ",".join("?" * len(hashes))
        rows = conn.execute(f"SELECT id, filename, content_hash FROM knowledge WHERE lower(filename) LIKE ? "
                            f"OR content_hash IN ({q})", (f"%{prefix.lower()}%", *hashes)).fetchall()
    return rows, hashes


def chroma_collection():
    try:
        import knowledge_indexer as K
    except Exception as e:
        return None, f"knowledge_indexer не импортируется: {type(e).__name__}"
    if not getattr(K, "RAG_SYNC_AVAILABLE", False):
        return None, "oko_rag недоступен"
    try:
        return K.oko_rag_indexer.get_collection(), None
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("prefix", help="часть имени файла, например LATVIJA")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    if len(a.prefix.strip()) < 4:
        print("СТОП: слишком короткая часть имени (меньше 4 символов) — заденет лишнее")
        return 2
    base = Path(config.BASE_DIR)
    local = Path(os.environ.get("LOCALAPPDATA") or str(Path.home())) / "oko"
    extra = [local / "print_out", local / "print_probe", SYSTEM_LOCAL / "print_out", SYSTEM_LOCAL / "print_probe"]

    files = file_traces(a.prefix, base, extra)
    conn = sqlite3.connect(str(config.DB_PATH))
    rows, hashes = db_traces(conn, a.prefix)
    col, col_err = chroma_collection()
    chunk_ids: list[str] = []
    if col is not None:
        for h in hashes:
            try:
                chunk_ids += col.get(where={"content_hash": h}).get("ids", [])
            except Exception as e:
                col_err = f"{type(e).__name__}: {e}"

    print(f"== SQLite knowledge ({config.DB_PATH}): {len(rows)} строк")
    for r in rows:
        print(f"   id={r[0]}  {r[1]}  hash={str(r[2])[:12]}")
    print(f"== ChromaDB: {len(chunk_ids)} чанков" + (f"  [{col_err}]" if col_err else ""))
    print(f"== файлы: {len(files)}")
    for p in files:
        print(f"   {p}")
    if not a.apply:
        print("\n[список] ничего не удалено. Для удаления: --apply")
        return 0

    if rows:
        bak = Path(str(config.DB_PATH) + f".before_forget_{time.strftime('%Y%m%d_%H%M%S')}")
        with sqlite3.connect(str(bak)) as dst:
            conn.backup(dst)
        print(f"[резерв] {bak}")
        conn.execute(f"DELETE FROM knowledge WHERE id IN ({','.join('?' * len(rows))})", [r[0] for r in rows])
        conn.commit()
    conn.close()
    if chunk_ids and col is not None:
        col.delete(ids=chunk_ids)
    failed = []
    for p in files:
        try:
            p.unlink()
        except OSError as e:
            failed.append(f"{p}: {e}")
    print(f"[удалено] строк SQLite {len(rows)}, чанков ChromaDB {len(chunk_ids)}, файлов {len(files) - len(failed)}")
    for f in failed:
        print(f"[НЕ удалён] {f}")
    if col_err and hashes:
        print(f"[ВНИМАНИЕ] ChromaDB не проверена ({col_err}) — чанки могли остаться")
        return 1
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
