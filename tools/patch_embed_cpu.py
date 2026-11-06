"""EMBED_CPU (01.11): эмбеддинги bge-m3 на CPU (options.num_gpu=0), чтобы не вытеснять qwen2.5:14b.

Замер 01.11 (RTX 3070 Laptop, 8 ГБ): qwen2.5:14b-instruct-q4_K_M держит ~6.1 ГБ VRAM; эмбеддинг
bge-m3 на GPU выгружал её (/api/ps: модели по очереди), каждый вопрос мозгу платил load ~6.0 с.
С num_gpu=0 обе модели в памяти, повторный вызов qwen 0.5 с, эмбеддинг на CPU 0.24 с (холодный 2.3 с).

Единый источник: config.OKO_EMBED_NUM_GPU (.env OKO_EMBED_NUM_GPU, по умолчанию 0; пусто/auto -> options
не передаётся, прежнее поведение) и config.embed_request_body(model, prompt). Все 4 прямых POST на
/api/embeddings переводятся на него: oko_rag/embeddings.py, src/governor.py (VECTOR_S),
src/knowledge_indexer.py (Approve/OkoWatcher), src/rebuild_index_bge_m3.py. Пропуск любого вернул бы
вытеснение -- поэтому тест-сторож в src/test_embed_cpu.py.

Файлы из security_tools/manifest.sha256 (если есть в нём) получают новый хэш; старый сверяется.
Применяется только к известным версиям (SHA256[:16]). Повторный запуск -- no-op.
Запуск из C:\\oko:  <python службы> tools\\patch_embed_cpu.py
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "security_tools" / "manifest.sha256"
TEST = ROOT / "src" / "test_embed_cpu.py"
MARKER = "EMBED_CPU"

F_EMB = ROOT / "oko_rag" / "embeddings.py"
F_RAGCFG = ROOT / "oko_rag" / "rag_config.py"
F_GOV = ROOT / "src" / "governor.py"
F_KI = ROOT / "src" / "knowledge_indexer.py"
F_REB = ROOT / "src" / "rebuild_index_bge_m3.py"
F_CFG = ROOT / "src" / "config.py"

EXPECTED = {
    F_EMB: "51962F0D24E1F923",
    F_RAGCFG: "75C042CCC21880F2",
    F_GOV: "39F0C98466EF0261",
    F_KI: "7BC4B72171487E31",
    F_REB: "15C185D070AA4E97",
    F_CFG: "27E2997F77100E40",
}

CFG_ANCHOR = ('OKO_RAG_HYGIENE: bool = os.getenv("OKO_RAG_HYGIENE", "1").strip().lower() '
              'not in ("0", "false", "no", "off")\n')
CFG_ADD = r'''
# --- EMBED_CPU (01.11): эмбеддинги (bge-m3) на CPU ---
# Замер 01.11 (RTX 3070 Laptop, 8 ГБ): qwen2.5:14b держит ~6.1 ГБ VRAM, bge-m3 на GPU её вытесняла
# (/api/ps: модели по очереди) -> каждый вопрос мозгу платил ~6 с перезагрузки основной модели.
# num_gpu=0: bge-m3 считается на CPU (~0.24 с на эмбеддинг, холодная загрузка ~2.3 с), обе модели в
# памяти, VRAM под игры не занимается. Ollama перезагружает модель при других options, поэтому ВСЕ
# запросы эмбеддингов идут через embed_request_body (сторож -- test_embed_cpu.py).
# Пусто/"auto" -> options не передаётся (прежнее поведение, откат без правки кода). Битое/<0 -> 0.
def _get_embed_num_gpu() -> "int | None":
    raw = os.getenv("OKO_EMBED_NUM_GPU", "0").strip().lower()
    if raw in ("", "auto"):
        return None
    try:
        value = int(raw)
    except ValueError:
        return 0
    return value if value >= 0 else 0


OKO_EMBED_NUM_GPU: "int | None" = _get_embed_num_gpu()


def embed_request_body(model: str, prompt: str) -> dict:
    """Тело POST /api/embeddings: model, prompt и options.num_gpu (если OKO_EMBED_NUM_GPU задан)."""
    body = {"model": model, "prompt": prompt}
    if OKO_EMBED_NUM_GPU is not None:
        body["options"] = {"num_gpu": OKO_EMBED_NUM_GPU}
    return body
'''

# (файл, старый фрагмент, новый фрагмент)
EDITS = [
    (F_EMB,
     "from rag_config import OLLAMA_HOST, EMBED_MODEL\n",
     "from rag_config import OLLAMA_HOST, EMBED_MODEL, embed_request_body\n"),
    (F_EMB,
     '        json={"model": EMBED_MODEL, "prompt": text},\n',
     "        json=embed_request_body(EMBED_MODEL, text),  # EMBED_CPU (01.11): bge-m3 на CPU\n"),
    (F_RAGCFG,
     'CHAT_MODEL = getattr(_root_config, "OLLAMA_MODEL", "llama3")',
     "# EMBED_CPU (01.11): тело запроса эмбеддинга (options.num_gpu) -- из корневого config.py.\n"
     "embed_request_body = _root_config.embed_request_body\n"
     'CHAT_MODEL = getattr(_root_config, "OLLAMA_MODEL", "llama3")'),
    (F_GOV,
     '            url, json={"model": config.OLLAMA_EMBED_MODEL, "prompt": text}\n',
     "            url, json=config.embed_request_body(config.OLLAMA_EMBED_MODEL, text)  # EMBED_CPU (01.11)\n"),
    (F_KI,
     '    payload = {"model": config.OLLAMA_EMBED_MODEL, "prompt": text}\n',
     "    payload = config.embed_request_body(config.OLLAMA_EMBED_MODEL, text)  # EMBED_CPU (01.11): bge-m3 на CPU\n"),
    (F_REB,
     "def _embed(text: str, model: str):\n    import requests\n",
     "def _embed(text: str, model: str):\n    import requests\n    import config\n"),
    (F_REB,
     '        json={"model": model, "prompt": text},\n',
     "        json=config.embed_request_body(model, text),  # EMBED_CPU (01.11)\n"),
    (F_CFG, CFG_ANCHOR, CFG_ANCHOR + CFG_ADD),
]

TEST_SRC = r'''"""EMBED_CPU (01.11): все запросы эмбеддингов несут options.num_gpu из config.OKO_EMBED_NUM_GPU."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
PRUNE = {".git", "data", "chroma_db", "__pycache__", "venv", ".venv", "node_modules",
         "logs", "backups", "snapshots", "voice_tmp", "documents"}


class _Resp:
    def __init__(self, vec=(0.1, 0.2)):
        self._vec = list(vec)

    def raise_for_status(self):
        pass

    def json(self):
        return {"embedding": self._vec}


@pytest.fixture
def cfg(monkeypatch):
    import config
    monkeypatch.setattr(config, "OKO_EMBED_NUM_GPU", 0)
    return config


def test_body_cpu_default(cfg):
    assert cfg.embed_request_body("bge-m3", "x") == {
        "model": "bge-m3", "prompt": "x", "options": {"num_gpu": 0}}


def test_body_auto_has_no_options(cfg, monkeypatch):
    monkeypatch.setattr(cfg, "OKO_EMBED_NUM_GPU", None)
    assert cfg.embed_request_body("bge-m3", "x") == {"model": "bge-m3", "prompt": "x"}


@pytest.mark.parametrize("raw,want", [
    (None, 0), ("0", 0), ("", None), ("auto", None), (" AUTO ", None),
    ("99", 99), ("-1", 0), ("bad", 0),
])
def test_env_parse(monkeypatch, raw, want):
    import config
    if raw is None:
        monkeypatch.delenv("OKO_EMBED_NUM_GPU", raising=False)
    else:
        monkeypatch.setenv("OKO_EMBED_NUM_GPU", raw)
    assert config._get_embed_num_gpu() == want


def test_knowledge_indexer_sends_num_gpu(cfg, monkeypatch):
    import knowledge_indexer as ki
    seen = {}

    def fake_post(url, json=None, timeout=None):
        seen["url"], seen["json"] = url, json
        return _Resp()

    monkeypatch.setattr(ki.requests, "post", fake_post)
    assert ki._request_embedding_vector("текст") == [0.1, 0.2]
    assert seen["url"].endswith("/api/embeddings")
    assert seen["json"]["options"] == {"num_gpu": 0}
    assert seen["json"]["model"] == cfg.OLLAMA_EMBED_MODEL


def test_oko_rag_embeddings_sends_num_gpu(cfg, monkeypatch):
    import knowledge_indexer  # noqa: F401  (добавляет oko_rag/ в sys.path)
    import embeddings
    seen = {}

    def fake_post(url, json=None, timeout=None):
        seen["json"] = json
        return _Resp()

    monkeypatch.setattr(embeddings.requests, "post", fake_post)
    assert embeddings.get_embedding("q") == [0.1, 0.2]
    assert seen["json"]["options"] == {"num_gpu": 0}


def test_rebuild_embed_sends_num_gpu(cfg, monkeypatch):
    import requests
    import rebuild_index_bge_m3 as reb
    seen = {}

    def fake_post(url, json=None, timeout=None):
        seen["json"] = json
        return _Resp()

    monkeypatch.setattr(requests, "post", fake_post)
    assert reb._embed("q", "bge-m3") == [0.1, 0.2]
    assert seen["json"] == {"model": "bge-m3", "prompt": "q", "options": {"num_gpu": 0}}


def test_governor_vector_s_sends_num_gpu(cfg):
    from governor import AgentGovernor
    gov = AgentGovernor.__new__(AgentGovernor)
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value={"embedding": [0.3]})
    gov._http = MagicMock()
    gov._http.post = AsyncMock(return_value=resp)
    assert asyncio.run(gov._embed_for_marker("ответ")) == [0.3]
    body = gov._http.post.await_args.kwargs["json"]
    assert body["options"] == {"num_gpu": 0}


def test_guard_every_embeddings_post_uses_body():
    """Сторож: любой нетестовый .py с '/api/embeddings' обязан строить тело через embed_request_body.
    Пропущенный вызов вернёт bge-m3 на GPU и вытеснение qwen -- функционально это незаметно."""
    offenders = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in PRUNE]
        for name in filenames:
            if not name.endswith(".py") or name.startswith("test_"):
                continue
            p = Path(dirpath) / name
            if p.parent.name == "tools" and name.startswith("patch_"):
                continue
            try:
                text = p.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            if "/api/embeddings" in text and "embed_request_body" not in text:
                offenders.append(str(p.relative_to(ROOT)))
    assert not offenders, f"POST /api/embeddings без embed_request_body: {offenders}"
'''


def _sha_full(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest().upper()


def _decode(raw: bytes) -> tuple[str, str, bool]:
    bom = raw.startswith(b"\xef\xbb\xbf")
    text = raw.decode("utf-8-sig")
    eol = "\r\n" if "\r\n" in text else "\n"
    return text.replace("\r\n", "\n"), eol, bom


def _encode(text: str, eol: str, bom: bool) -> bytes:
    return (b"\xef\xbb\xbf" if bom else b"") + text.replace("\n", eol).encode("utf-8")


def _norm(path_text: str) -> str:
    s = path_text.strip().replace("/", "\\")
    while "\\\\" in s:
        s = s.replace("\\\\", "\\")
    return s.lower()


def main() -> int:
    files = list(EXPECTED)
    for p in files:
        if not p.exists():
            print(f"FAIL missing {p}")
            return 2
    texts = {p: _decode(p.read_bytes()) for p in files}

    if all(MARKER in texts[p][0] for p in files) and TEST.exists():
        print("SKIP already applied")
        return 0

    old_full = {}
    for p, want in EXPECTED.items():
        got = _sha_full(p)
        if got[:16] != want:
            print(f"FAIL hash {p.relative_to(ROOT)}: {got[:16]} != {want} (не та версия, ничего не изменено)")
            return 3
        old_full[p] = got

    new = {p: texts[p][0] for p in files}
    for p, old, rep in EDITS:
        n = new[p].count(old)
        if n != 1:
            print(f"FAIL anchor {p.relative_to(ROOT)}: найдено {n}, нужно 1: {old[:60]!r}")
            return 4
        new[p] = new[p].replace(old, rep, 1)

    for p in files:
        try:
            compile(new[p], str(p), "exec")
        except SyntaxError as e:
            print(f"FAIL syntax {p.relative_to(ROOT)}: {e}")
            return 5
    compile(TEST_SRC, "test_embed_cpu.py", "exec")

    out_bytes = {p: _encode(new[p], texts[p][1], texts[p][2]) for p in files}
    new_full = {p: hashlib.sha256(b).hexdigest().upper() for p, b in out_bytes.items()}

    # manifest.sha256: файлы из него получают новый хэш; старый обязан совпасть с текущим файлом.
    man_out = None
    man_changed = []
    if MANIFEST.exists():
        m_text, m_eol, m_bom = _decode(MANIFEST.read_bytes())
        lines = m_text.split("\n")
        by_norm = {_norm(str(p.relative_to(ROOT))): p for p in files}
        for i, line in enumerate(lines):
            parts = line.strip().split(None, 1)
            if len(parts) != 2 or len(parts[0]) != 64:
                continue
            p = by_norm.get(_norm(parts[1]))
            if p is None:
                continue
            if parts[0].upper() != old_full[p]:
                print(f"FAIL manifest {parts[1]}: {parts[0][:16]} != файл {old_full[p][:16]} "
                      f"(манифест уже расходится с файлом, ничего не изменено)")
                return 6
            lines[i] = line.replace(parts[0], new_full[p], 1)
            man_changed.append(parts[1])
        if man_changed:
            man_out = _encode("\n".join(lines), m_eol, m_bom)
    else:
        print(f"WARN manifest не найден: {MANIFEST} (файлы не сверены с манифестом)")

    for p, b in out_bytes.items():
        p.write_bytes(b)
    if man_out is not None:
        MANIFEST.write_bytes(man_out)
    TEST.write_bytes(TEST_SRC.encode("utf-8"))

    for p in files:
        print(f"OK {str(p.relative_to(ROOT)):<30} {new_full[p][:16]}")
    print(f"OK {'src/test_embed_cpu.py':<30} {_sha_full(TEST)[:16]}")
    print(f"manifest: {', '.join(man_changed) if man_changed else 'строк для изменённых файлов нет'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
