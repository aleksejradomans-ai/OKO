"""doc_intake.py — входящие документы из Telegram: сначала выбор, потом действие (28.10.2026).

Решение Оператора 15:42: документ — либо знание (хранится), либо рабочий процесс (напечатать и удалить).
Поэтому файл из Telegram сначала ложится в data/incoming (НЕ наблюдаемая папка: watcher его не трогает),
бот показывает кнопки, и только выбор решает судьбу файла:
  🖨 Печать и удалить   — печать, затем файл и картинки страниц удаляются;
  🖨 Печать + в базу    — печать, затем файл идёт в knowledge/ (обычный путь: категория -> Approve);
  📚 В базу             — сразу в knowledge/;
  🗑 Удалить            — удалить без печати.
Дубликат (такой же ТЕКСТ уже есть в базе знаний) определяется ДО выбора: кнопки «в базу» не показываются.
Хэш текста считается так же, как в knowledge_indexer.process_single (sha256 извлечённого текста),
тест сверяет это на живом process_single — расхождение правил дало бы ложные «не дубликат».

Файл хранится как incoming/<id>__<имя>; id — 8 hex, его несут кнопки (callback_data ≤ 64 байт).
Состояние — только файлы на диске: после перезапуска бота кнопки продолжают работать.
"""
from __future__ import annotations

import hashlib
import secrets
import shutil
import time
from pathlib import Path
from typing import Callable

import config

INCOMING_DIR = Path(config.BASE_DIR) / "incoming"
KNOWLEDGE_DIR = Path(config.BASE_DIR) / "knowledge"
MAX_AGE_S = 7 * 24 * 3600          # невыбранный файл живёт неделю, потом убирается
ACTIONS = {"print": "🖨 Печать и удалить", "printkeep": "🖨 Печать + в базу", "keep": "📚 В базу",
           "drop": "🗑 Удалить"}
_SEP = "__"


def new_path(filename: str, incoming: Path | None = None) -> tuple[str, Path]:
    d = incoming or INCOMING_DIR
    d.mkdir(parents=True, exist_ok=True)
    doc_id = secrets.token_hex(4)
    return doc_id, d / f"{doc_id}{_SEP}{Path(filename).name}"


def find(doc_id: str, incoming: Path | None = None) -> Path | None:
    if not doc_id or not all(c in "0123456789abcdef" for c in doc_id):
        return None
    hits = sorted((incoming or INCOMING_DIR).glob(f"{doc_id}{_SEP}*"))
    return hits[0] if hits else None


def display_name(path: Path) -> str:
    return path.name.split(_SEP, 1)[-1]


def pages_dir(path: Path) -> Path:
    return path.parent / "pages" / path.name.split(_SEP, 1)[0]


def read_text_like_indexer(path: Path) -> str:
    """Тот же текст, что knowledge_indexer.process_single пишет в базу (для сверки дубликатов)."""
    ext = path.suffix.lower()
    if ext in (".docx", ".xlsx"):
        import office_text
        return office_text.extract(path)
    if ext == ".pdf":
        import knowledge_indexer
        return knowledge_indexer.load_document(str(path))
    return path.read_text(encoding="utf-8")


def known_in_knowledge(path: Path, connect: Callable | None = None) -> bool | None:
    """True — такой текст уже в базе; False — нет; None — не удалось проверить (решит человек)."""
    try:
        content = read_text_like_indexer(path)
    except Exception:
        return None
    if not content.strip():
        return None
    h = hashlib.sha256(content.encode("utf-8")).hexdigest()
    if connect is None:
        import db
        connect = db.get_connection
    try:
        conn = connect()
        try:
            return conn.execute("SELECT 1 FROM knowledge WHERE content_hash = ? LIMIT 1", (h,)).fetchone() is not None
        finally:
            conn.close()
    except Exception:
        return None


def to_knowledge(path: Path, knowledge: Path | None = None) -> Path:
    """Копия в knowledge/ (copy, не rename: watcher ловит создание файла, переименование он не видит),
    затем удаление из incoming. Имя занято — добавляется « (2)», « (3)»…"""
    kd = knowledge or KNOWLEDGE_DIR
    kd.mkdir(parents=True, exist_ok=True)
    name = Path(display_name(path))
    dest = kd / name.name
    n = 2
    while dest.exists():
        dest = kd / f"{name.stem} ({n}){name.suffix}"
        n += 1
    shutil.copy2(path, dest)
    remove(path)
    return dest


def remove(path: Path) -> None:
    """Удалить файл из incoming и картинки его страниц."""
    path.unlink(missing_ok=True)
    pd = pages_dir(path)
    if pd.is_dir():
        shutil.rmtree(pd, ignore_errors=True)


def purge_old(incoming: Path | None = None, now: float | None = None) -> int:
    """Файлы, по которым так и не выбрали действие, старше MAX_AGE_S — удаляются. -> сколько."""
    d = incoming or INCOMING_DIR
    if not d.is_dir():
        return 0
    now = time.time() if now is None else now
    n = 0
    for p in d.glob(f"*{_SEP}*"):
        if p.is_file() and now - p.stat().st_mtime > MAX_AGE_S:
            remove(p)
            n += 1
    return n
