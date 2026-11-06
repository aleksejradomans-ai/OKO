#!/usr/bin/env python3
"""apply_rag_outage_fix.py -- bot.py: Ollama down must not be a RED event.

Found by the live outage test: with Ollama stopped, retrieve_context() raised
requests.ConnectionError (embeddings call) BEFORE the governor was reached ->
"Unhandled exception in handler" at ERROR level, 1 red line per request,
reply [ERR: INTERNAL]. 6 such lines in 60 s would make process_watchdog kill
the bot. Fix: a transport-level failure in RAG retrieval is a WARNING and the
request continues with an empty context (the governor then answers with the
yellow [ERR: LLM_UNAVAILABLE]).

    python apply_rag_outage_fix.py [--src C:\\oko\\src] [--apply]
Dry-run by default. Backup bot.py.bak_rag_fix before writing. BOM/CRLF kept.
"""
import argparse, os, sys
from pathlib import Path

OLD = '''    if RAG_RETRIEVAL_AVAILABLE:
        return await asyncio.to_thread(_query_rag_context, query)
    return await asyncio.to_thread(_query_knowledge_db, query)
'''
NEW = '''    # RAG_OUTAGE_FIX: embeddings go to Ollama (requests.post). When Ollama is down
    # this is a transport failure (requests exceptions are OSError subclasses):
    # YELLOW (WARNING, not counted by process_watchdog), the request continues
    # with an empty context and the governor reports LLM_UNAVAILABLE itself.
    # Non-I/O errors (bugs) still propagate and stay RED.
    try:
        if RAG_RETRIEVAL_AVAILABLE:
            return await asyncio.to_thread(_query_rag_context, query)
        return await asyncio.to_thread(_query_knowledge_db, query)
    except OSError as e:
        logger.warning(
            "[RAG_UNAVAILABLE] retrieval skipped, transport failure (%s): %s",
            type(e).__name__, str(e)[:200],
        )
        return "Релевантный контекст не найден."
'''
MARK = "RAG_OUTAGE_FIX"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", type=Path, default=Path(__file__).resolve().parent / "src")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    p = a.src / "bot.py"
    raw = p.read_bytes()
    bom = raw.startswith(b"\xef\xbb\xbf")
    text = raw.decode("utf-8-sig")
    crlf = "\r\n" in text
    text = text.replace("\r\n", "\n")
    if MARK in text:
        print("already patched"); return 0
    n = text.count(OLD)
    if n != 1:
        print(f"ABORT: anchor found {n}x (need 1). Nothing written."); return 2
    new = text.replace(OLD, NEW, 1)
    compile(new, "bot.py", "exec")
    print(f"[OK] bot.py: 1 edit  ({'APPLY' if a.apply else 'DRY-RUN'})")
    if not a.apply:
        return 0
    (p.with_name("bot.py.bak_rag_fix")).write_bytes(raw)
    out = new.replace("\n", "\r\n") if crlf else new
    data = out.encode("utf-8")
    if bom:
        data = b"\xef\xbb\xbf" + data
    tmp = p.with_name("bot.py.tmp_patch")
    tmp.write_bytes(data)
    os.replace(tmp, p)
    print("[WRITE] bot.py (backup: bot.py.bak_rag_fix)")
    return 0


if __name__ == "__main__":
    sys.exit(main())