"""Подготовка голоса бота (без сети): v5 фраз, словарь {summary}, каталог {cmd}, manifest.json, пачки для ручной генерации.

  python tools/voice_prep.py --v4 <VOICE_phrases_v4.jsonl> --data <каталог данных> --batches <каталог пачек>
         [--incoming <папка приёма WAV для инструкции>]
Пишет: <data>/voice/VOICE_phrases_v5.jsonl, <data>/voice/despina/manifest.json, <data>/voice/despina/catalogs.json,
<batches>/batch_NN.txt, 00_STYLE_PROMPT.txt, 00_HOWTO.txt. Каждый файл после записи читается обратно и сверяется
(sha256). Вывод -- строки «KEY значение» для отчёта; код 0 -- всё записано и сверено.
"""
import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
import voice_kit as k  # noqa: E402


def out(s: str) -> None:
    sys.stdout.buffer.write((s + "\n").encode("utf-8"))


def write_back(path: Path, text: str) -> str:
    """Запись UTF-8 (без BOM, LF) через временный файл + чтение назад; -> sha256[:16]. Расхождение -> исключение."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = text.encode("utf-8")
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)
    back = path.read_bytes()
    if back != data:
        raise OSError(f"чтение назад не совпало: {path.name}")
    return hashlib.sha256(back).hexdigest()[:16]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="voice_prep")
    ap.add_argument("--v4", required=True)
    ap.add_argument("--data", required=True)
    ap.add_argument("--batches", required=True)
    ap.add_argument("--incoming", default="<data>/voice/despina/_incoming")
    a = ap.parse_args(argv)
    v4 = k.load_jsonl(Path(a.v4))
    c4 = k.counts(v4)
    out("V4 " + " ".join(f"{x}={c4[x]}" for x in c4))
    bad = {x: (c4[x], k.EXPECTED_V4[x]) for x in k.EXPECTED_V4 if c4[x] != k.EXPECTED_V4[x]}
    if bad:
        out(f"STOP v4 counts differ from the approved file: {bad}")
        return 2
    v5, diff = k.make_v5(v4)
    for x in diff:
        out("DIFF " + x)
    summ = k.summary_dict(ROOT)
    for s in summ:
        out(f"SUMMARY {s['file']} «{s['text']}» source={s['source']}")
    cat = k.cmd_catalog(ROOT)
    for c in cat:
        out(f"CMD {c['file']} «{c['text']}» id={c['id']} source={c['source']}")
    man = k.build_manifest(v5, summ, cat)
    batches = k.make_batches(man)
    data, bdir = Path(a.data), Path(a.batches)
    shas = {}
    shas["v5"] = write_back(data / "voice" / "VOICE_phrases_v5.jsonl",
                            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in v5))
    shas["manifest"] = write_back(data / "voice" / "despina" / "manifest.json",
                                  json.dumps(man, ensure_ascii=False, indent=1) + "\n")
    shas["catalogs"] = write_back(data / "voice" / "despina" / "catalogs.json",
                                  json.dumps({"summary": summ, "cmd": cat}, ensure_ascii=False, indent=1) + "\n")
    for i, b in enumerate(batches, 1):
        shas[f"batch_{i:02d}"] = write_back(bdir / f"batch_{i:02d}.txt", k.batch_text(b))
    shas["style"] = write_back(bdir / "00_STYLE_PROMPT.txt", k.speakable(k.STYLE_PROMPT) + "\n")
    shas["howto"] = write_back(bdir / "00_HOWTO.txt", k.HOWTO.format(tune=k.TUNING_LINE, incoming=a.incoming,
                                                                    n=len(batches), files=man["counts"]["total"]))
    est = [sum(k.estimate_s(e["text"]) for e in b) + k.estimate_s(k.TUNING_LINE) for b in batches]
    out("MANIFEST " + " ".join(f"{x}={v}" for x, v in man["counts"].items()))
    out(f"BATCHES n={len(batches)} lines_min={min(len(b) for b in batches)} lines_max={max(len(b) for b in batches)} "
        f"est_s_max={max(est):.1f} est_s_total={sum(est):.0f}")
    out(f"WRITTEN files={len(shas)} read_back=ok v5={shas['v5']} manifest={shas['manifest']} catalogs={shas['catalogs']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
