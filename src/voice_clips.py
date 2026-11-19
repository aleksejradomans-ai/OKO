"""voice_clips.py -- нарезка пачек голоса по manifest и проверка комплекта (офлайн, numpy + scipy).

cut_batch: WAV пачки -> моно 24 кГц -> нормализация А (-18 LUFS, пик -1,5 dBTP) -> участки речи выше -50 dBFS ->
фразы по самым длинным паузам (ожидается камертон + строки пачки) -> камертон отбрасывается -> у носителя чисел
(«Текущее значение: N.») отрезается всё до самой длинной внутренней паузы -> буферы 40 мс / 120 мс (у частей __a --
40 мс в конце) -> клипы < 1 с: нормализация Б (-20 dBFS RMS активного окна, пик -1,5 dBTP) -> WAV PCM 16 бит.
Повтор безопасен: клипы пачки перезаписываются (атомарно), журнал пачки -- _cut_batch_NN.json.
check_set: по manifest -- нет / лишние / формат / длительность / клиппинг / пик.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

import voice_dsp as d

MIN_S = {"phrase": 0.35, "part": 0.15, "stat": 0.25, "cmd": 0.25, "num": 0.15}
MAX_S = 9.0
TP_FAIL_DBTP = -1.0


def load_manifest(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def batch_entries(manifest: dict, batch: int) -> list[dict]:
    es = sorted((e for e in manifest["entries"] if e.get("batch") == batch), key=lambda e: e["pos"])
    if not es:
        raise ValueError(f"в manifest нет пачки {batch}")
    if [e["pos"] for e in es] != list(range(1, len(es) + 1)):
        raise ValueError(f"пачка {batch}: позиции не подряд")
    return es


def cut_batch(wav: Path, manifest: dict, batch: int, out_dir: Path) -> dict:
    es = batch_entries(manifest, batch)
    x, sr = d.read_wav(wav)
    x = d.to_target_sr(x, sr)
    sr = d.TARGET_SR
    x, norm = d.normalize_batch(x, sr)
    runs = d.speech_runs(x, sr)
    segs, split = d.split_by_largest_gaps(runs, len(es) + 1, sr)
    if split["too_short_gap"]:
        raise ValueError(f"пачка {batch}: паузы между фразами короче 0,25 с ({split['min_split_gap_s']} с) -- в записи "
                         f"меньше фраз, чем строк пачки, или фразы слиты; клипы не записаны")
    clips = []
    for e, seg in zip(es, segs[1:]):                 # segs[0] -- камертон
        if e["kind"] == "num":
            seg = d.after_first_long_gap(x, sr, seg)
        tail = d.TAIL_JOIN_S if e.get("join_tail") else d.TAIL_S
        y = d.take_clip(x, sr, seg, d.HEAD_S, tail)
        info = {"file": e["file"] + ".wav", "seconds": round(y.size / sr, 3)}
        if y.size / sr < d.SHORT_CLIP_S:
            y, nb = d.normalize_short(y, sr)
            info["norm_b"] = nb
        else:
            tp = d.true_peak_db(y, sr)
            if tp > d.TP_CEIL_DBTP:                  # после А пик пачки уже под потолком; страховка на округление
                y = y * 10 ** ((d.TP_CEIL_DBTP - tp) / 20.0)
        d.write_wav(Path(out_dir) / info["file"], y, sr)
        clips.append(info)
    log = {"batch": batch, "source": Path(wav).name, "expected": len(es) + 1, "found_runs": len(runs),
           "norm_a": norm, "split": split, "clips": clips}
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    (Path(out_dir) / f"_cut_batch_{batch:02d}.json").write_text(json.dumps(log, ensure_ascii=False, indent=1),
                                                                 encoding="utf-8")
    return log


def check_set(manifest: dict, clip_dir: Path) -> dict:
    """-> {missing, extra, bad: [(файл, причина)], ok, total}. Файлы вне manifest с префиксом «_» не считаются."""
    want = {e["file"] + ".wav": e for e in manifest["entries"]}
    have = {p.name: p for p in Path(clip_dir).glob("*.wav")} if Path(clip_dir).is_dir() else {}
    missing = sorted(set(want) - set(have))
    extra = sorted(n for n in set(have) - set(want) if not n.startswith("_"))
    bad = []
    for name in sorted(set(want) & set(have)):
        e, p = want[name], have[name]
        try:
            inf = d.wav_info(p)
        except (ValueError, OSError) as ex:
            bad.append((name, f"не читается: {type(ex).__name__}"))
            continue
        why = []
        if inf["sr"] != d.TARGET_SR or inf["channels"] != 1 or inf["dtype"] != "int16":
            why.append(f"формат {inf['sr']} Гц / {inf['channels']} кан / {inf['dtype']}")
        lo = MIN_S.get(e["kind"], 0.15)
        if not lo <= inf["seconds"] <= MAX_S:
            why.append(f"длительность {inf['seconds']:.2f} с вне {lo}..{MAX_S}")
        if inf["clipped"]:
            why.append(f"клиппинг: {inf['clipped']} отсчётов на краю шкалы")
        if not why:
            x, sr = d.read_wav(p)
            tp = d.true_peak_db(x, sr)
            if tp > TP_FAIL_DBTP:
                why.append(f"истинный пик {tp:.1f} dBTP > {TP_FAIL_DBTP}")
        if why:
            bad.append((name, "; ".join(why)))
    ok = len(want) - len(missing) - len(bad)
    return {"total": len(want), "present": len(set(want) & set(have)), "missing": missing, "extra": extra,
            "bad": bad, "ok": ok, "complete": not missing and not bad}
