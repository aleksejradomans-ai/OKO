"""voice_dsp / voice_clips / CLI voice_cut, voice_check: громкость по BS.1770 (опорный синус, сверка с pyloudnorm,
если он стоит), истинный пик, нормализация А и Б, нарезка синтетической пачки (камертон, носитель чисел, буферы),
проверка комплекта (нет / лишние / формат / клиппинг). Только синтетические WAV во временной папке."""
import importlib.util
import json
import math
from pathlib import Path

import numpy as np
import pytest
from scipy.io import wavfile

import voice_clips as vc
import voice_dsp as d
import voice_kit as k

ROOT = Path(__file__).resolve().parent.parent
SR = 24000


def test_lufs_reference_sine():
    t = np.arange(SR * 5) / SR
    x = 0.5 * np.sin(2 * np.pi * 997 * t)
    assert abs(d.integrated_lufs(x, SR) - (20 * math.log10(0.5) - 3.01)) < 0.1
    assert d.integrated_lufs(np.zeros(SR), SR) == -math.inf


def test_lufs_matches_pyloudnorm_if_installed():
    pyln = pytest.importorskip("pyloudnorm")
    rng = np.random.default_rng(3)
    for sr in (24000, 48000):
        x = 0.1 * rng.standard_normal(sr * 4)
        assert abs(d.integrated_lufs(x, sr) - pyln.Meter(sr).integrated_loudness(x)) < 0.2


def test_true_peak_and_normalizations():
    t = np.arange(SR * 3) / SR
    x = 0.05 * np.sin(2 * np.pi * 300 * t) * (1 + 0.5 * np.sin(2 * np.pi * 3 * t))
    y, info = d.normalize_batch(x, SR)
    assert abs(d.integrated_lufs(y, SR) - d.BATCH_LUFS) < 0.2 and d.true_peak_db(y, SR) <= d.TP_CEIL_DBTP + 0.05
    s = 0.01 * np.sin(2 * np.pi * 400 * np.arange(int(0.5 * SR)) / SR)
    z, nb = d.normalize_short(s, SR)
    assert abs(d.active_rms_db(z, SR) - d.CLIP_RMS_DBFS) < 0.3 and d.true_peak_db(z, SR) <= d.TP_CEIL_DBTP + 0.05
    loud = 0.9 * np.sign(np.sin(2 * np.pi * 100 * np.arange(int(0.5 * SR)) / SR))
    w, nb2 = d.normalize_short(loud * 0.01, SR)
    assert d.true_peak_db(w, SR) <= d.TP_CEIL_DBTP + 0.05


def test_read_wav_formats_and_resample(tmp_path):
    x = (0.3 * np.sin(2 * np.pi * 440 * np.arange(48000) / 48000)).astype(np.float32)
    wavfile.write(str(tmp_path / "f.wav"), 48000, np.stack([x, x], axis=1))
    y, sr = d.read_wav(tmp_path / "f.wav")
    assert sr == 48000 and y.ndim == 1
    z = d.to_target_sr(y, sr)
    assert abs(z.size - SR) <= 2
    d.write_wav(tmp_path / "o.wav", z)
    inf = d.wav_info(tmp_path / "o.wav")
    assert inf["sr"] == SR and inf["channels"] == 1 and inf["dtype"] == "int16" and inf["clipped"] == 0


def _voice(seconds, rng, words=3):
    """Синтетическая «речь»: words слов-тонов с паузами 60 мс (внутри фразы паузы короткие)."""
    parts = []
    w = max(0.08, seconds / words - 0.06)
    for i in range(words):
        n = int(w * SR)
        f0 = rng.uniform(140, 260)
        t = np.arange(n) / SR
        env = np.sin(np.pi * np.arange(n) / n) ** 0.5
        parts.append(0.2 * env * (np.sin(2 * np.pi * f0 * t) + 0.4 * np.sin(2 * np.pi * 2 * f0 * t)))
        if i < words - 1:
            parts.append(np.zeros(int(0.06 * SR)))
    return np.concatenate(parts)


def _batch_wav(path, entries, rng, gap_s=0.7):
    seq = [np.zeros(int(0.3 * SR)), _voice(1.2, rng)]          # камертон
    for e in entries:
        seq.append(np.zeros(int(gap_s * SR)))
        if e["kind"] == "num":
            seq += [_voice(0.9, rng, 2), np.zeros(int(0.25 * SR)), _voice(0.4, rng, 1)]
        else:
            seq.append(_voice(max(0.3, len(e["text"]) / 13.0), rng, max(1, len(e["text"].split()))))
    seq.append(np.zeros(int(0.4 * SR)))
    x = np.concatenate(seq)
    wavfile.write(str(path), SR, (x * 32767).astype(np.int16))


def _manifest():
    v5, _ = k.make_v5([
        {"id": "volume_get", "ok": ["Громкость: {n}."], "glue": {"prefix": "Громкость:"}},
        {"id": "amb_off", "ok": ["Подсветка выключена.", "Подсветка телевизора погашена."]},
        {"id": "gen_loop", "say": ["Одна и та же команда {n} раз подряд."]},
        {"id": "gen_series_stopped", "say": ["Серия остановлена на шаге {i} из {m}."]},
    ])
    m = k.build_manifest(v5, [{"text": "включён", "file": "stat_vklyuchen", "source": "x"}], [])
    m["entries"] = [e for e in m["entries"] if e["kind"] != "num" or e["n"] in (0, 7, 25)]
    k.make_batches(m)
    return m


def test_cut_batch_end_to_end_and_check(tmp_path):
    m = _manifest()
    rng = np.random.default_rng(7)
    inc, out = tmp_path / "in", tmp_path / "clips"
    inc.mkdir()
    for b in range(1, m["batches"] + 1):
        _batch_wav(inc / f"batch_{b:02d}.wav", vc.batch_entries(m, b), rng)
        log = vc.cut_batch(inc / f"batch_{b:02d}.wav", m, b, out)
        assert not log["split"]["ambiguous"] and len(log["clips"]) == len(vc.batch_entries(m, b))
        assert abs(log["norm_a"]["lufs_after"] - d.BATCH_LUFS) < 0.3 or log["norm_a"]["peak_limited"]
    r = vc.check_set(m, out)
    assert r["complete"] and r["ok"] == len(m["entries"]) and not r["extra"]
    num = d.wav_info(out / "num_025.wav")["seconds"]
    assert num < 0.4 + 0.04 + 0.12 + 0.05                       # носитель «Текущее значение:» отрезан
    a = [e for e in m["entries"] if e.get("join_tail")][0]
    x, _ = d.read_wav(out / (a["file"] + ".wav"))
    tail = x[-int(0.12 * SR):]
    assert d.frame_db(tail[-int(0.03 * SR):], SR).max() < -45 and d.frame_db(tail, SR).max() > -45   # хвост 40 мс
    # повтор нарезки -- те же имена, без лишних
    vc.cut_batch(inc / "batch_01.wav", m, 1, out)
    assert vc.check_set(m, out)["complete"]


def test_cut_batch_wrong_phrase_count_is_error(tmp_path):
    m = _manifest()
    es = vc.batch_entries(m, 1)
    rng = np.random.default_rng(1)
    _batch_wav(tmp_path / "b.wav", es[:-3], rng)
    assert len(es) > 3
    with pytest.raises(ValueError, match="паузы между фразами короче"):
        vc.cut_batch(tmp_path / "b.wav", m, 1, tmp_path / "o")
    assert not (tmp_path / "o").exists()                       # ничего не записано


def test_check_set_reports_missing_extra_bad(tmp_path):
    m = {"entries": [{"file": "a", "kind": "phrase"}, {"file": "b", "kind": "phrase"}, {"file": "c", "kind": "num"}]}
    t = np.arange(int(0.8 * SR)) / SR
    d.write_wav(tmp_path / "a.wav", 0.3 * np.sin(2 * np.pi * 200 * t))
    wavfile.write(str(tmp_path / "b.wav"), 16000, (0.3 * np.sin(2 * np.pi * 200 * t) * 32767).astype(np.int16))
    wavfile.write(str(tmp_path / "x.wav"), SR, np.zeros(100, dtype=np.int16))
    clip = np.full(int(0.5 * SR), 32767, dtype=np.int16)
    wavfile.write(str(tmp_path / "c.wav"), SR, clip)
    r = vc.check_set(m, tmp_path)
    assert r["missing"] == [] and r["extra"] == ["x.wav"] and not r["complete"]
    bad = dict(r["bad"])
    assert "формат" in bad["b.wav"] and "клиппинг" in bad["c.wav"] and "a.wav" not in bad


def _cli(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_cli_cut_and_check(tmp_path, capsys):
    m = _manifest()
    mp = tmp_path / "manifest.json"
    mp.write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")
    inc, out = tmp_path / "in", tmp_path / "clips"
    inc.mkdir()
    rng = np.random.default_rng(11)
    for b in range(1, m["batches"] + 1):
        _batch_wav(inc / f"batch_{b:02d}.wav", vc.batch_entries(m, b), rng)
    assert _cli("voice_cut").main(["--manifest", str(mp), "--incoming", str(inc), "--out", str(out)]) == 0
    lines = [json.loads(x) for x in capsys.readouterr().out.splitlines()]
    assert len(lines) == m["batches"] and all(x["status"] == "ok" for x in lines)
    assert _cli("voice_check").main(["--manifest", str(mp), "--clips", str(out)]) == 0
    assert "complete=True" in capsys.readouterr().out
    (out / "num_007.wav").unlink()
    assert _cli("voice_check").main(["--manifest", str(mp), "--clips", str(out)]) == 1
    o = capsys.readouterr().out
    assert "MISSING num_007.wav" in o and "complete=False" in o
