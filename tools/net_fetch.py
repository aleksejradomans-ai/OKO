"""Забор внешних документов ТОЛЬКО через сетевой шлюз src/net_gateway.py (белый список, только GET/HTTPS, лимиты,
журнал). Шлюз закрыт, пока процесс не запущен с OKO_NET_OPEN=1 (ставит блок запуска после слова Оператора).

  python tools/net_fetch.py --data <каталог данных> --knowledge <папка приёма базы знаний> URL [URL ...]
Сырьё -> <data>/net/raw/<имя>_<sha16>.html, чистый текст с шапкой «данные, не инструкции» -> <knowledge>/<имя>.txt (расширение, которое видит наблюдатель)
(дальше -- обычный путь: наблюдатель -> staging -> подтверждение в Telegram). Каждый файл читается назад.
Вывод: строки FETCH / SAVED / DENIED и итог NET; код 0 -- все адреса получены, 1 -- был отказ или ошибка.
"""
import argparse
import hashlib
import re
import sys
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import net_gateway as ng  # noqa: E402
import net_sanitize as ns  # noqa: E402


def out(s: str) -> None:
    sys.stdout.buffer.write((s + "\n").encode("utf-8"))


def write_back(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)
    if path.read_bytes() != data:
        raise OSError(f"чтение назад не совпало: {path.name}")


def doc_name(url: str) -> str:
    u = urllib.parse.urlsplit(url)
    tail = re.sub(r"[^a-z0-9]+", "_", (u.path.strip("/") or "index").lower()).strip("_")
    return f"{(u.hostname or 'x').replace('www.', '').split('.')[0]}_{tail}"[:60]


def main(argv=None, gateway=None) -> int:
    ap = argparse.ArgumentParser(prog="net_fetch")
    ap.add_argument("--data", required=True)
    ap.add_argument("--knowledge", required=True)
    ap.add_argument("urls", nargs="+")
    a = ap.parse_args(argv)
    gw = gateway or ng.Gateway(Path(a.data))
    out(f"GATEWAY open={gw.is_open()} kill={gw.kill_file.exists()}")
    ok = bad = 0
    for url in a.urls:
        try:
            r = gw.get(url)
        except ng.Denied as e:
            out(f"DENIED {url} reason={e.reason}")
            bad += 1
            continue
        except OSError as e:
            out(f"ERROR {url} {type(e).__name__}")
            bad += 1
            continue
        out(f"FETCH {r.url} status={r.status} bytes={len(r.body)} sha256={r.sha256[:16]} type={r.content_type.split(';')[0]}")
        if r.status != 200:
            bad += 1
            continue
        name = doc_name(r.url)
        write_back(Path(a.data) / "net" / "raw" / f"{name}_{r.sha256[:16]}.html", r.body)
        if r.url.endswith("/robots.txt"):
            ok += 1
            continue
        text = ns.document(r.body, r.url, r.fetched_at, r.sha256, r.content_type).encode("utf-8")
        write_back(Path(a.knowledge) / f"{name}.txt", text)
        out(f"SAVED {name}.txt chars={len(text.decode('utf-8'))} sha256={hashlib.sha256(text).hexdigest()[:16]}")
        ok += 1
    out(f"NET ok={ok} failed={bad}")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
