"""Нарезка пачек голоса по manifest (офлайн): WAV пачки -> клипы WAV PCM 16 бит 24 кГц моно.

  python tools/voice_cut.py --manifest <manifest.json> --incoming <папка batch_NN.wav> --out <папка клипов>
         [--batch N ...]
Без --batch -- все пачки, для которых есть batch_NN.wav. Вывод -- строка JSON на пачку; код 0 -- все пачки нарезаны
без неоднозначных пауз, 1 -- есть пачки с флагом (ambiguous / too_short_gap) или ошибкой.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import voice_clips as vc  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="voice_cut")
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--incoming", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--batch", type=int, nargs="*")
    a = ap.parse_args(argv)
    man = vc.load_manifest(Path(a.manifest))
    nums = a.batch or sorted(int(p.stem.split("_")[1]) for p in Path(a.incoming).glob("batch_*.wav")
                             if p.stem.split("_")[1].isdigit())
    rc = 0
    for n in nums:
        wav = Path(a.incoming) / f"batch_{n:02d}.wav"
        try:
            log = vc.cut_batch(wav, man, n, Path(a.out))
            flag = log["split"]["ambiguous"] or log["split"]["too_short_gap"]
            r = {"batch": n, "clips": len(log["clips"]), "lufs_after": log["norm_a"]["lufs_after"],
                 "peak_limited": log["norm_a"]["peak_limited"], **log["split"], "status": "check" if flag else "ok"}
            rc = rc or (1 if flag else 0)
        except (ValueError, OSError) as e:
            r = {"batch": n, "status": "error", "error": f"{type(e).__name__}: {e}"}
            rc = 1
        sys.stdout.buffer.write((json.dumps(r, ensure_ascii=False) + "\n").encode("utf-8"))
    return rc


if __name__ == "__main__":
    sys.exit(main())
