"""CAM_PROBE (03.11.2026): первый кадр с камеры iCSee X6C-WEQ (прошивка Xiongmai V5.00.R02, 2023-12).

Найдена 13:47: 10.95.175.243 (MAC 6C-22-1A-AD-79-25), открыты 554 (RTSP), 34567 (XM), 80, 8899 (ONVIF).
Данные входа -- только из C:\\oko\\.env (OKO_CAM_HOST / OKO_CAM_USER / OKO_CAM_PASS), в вывод пароль не попадает.
Перебирает пути RTSP Xiongmai (канал 1/2 -- два объектива, поток 0 основной / 1 дополнительный) и ONVIF-путь,
на первом рабочем снимает один кадр в C:\\oko\\data\\cam\\probe_*.jpg. Ничего в камере не меняет.
"""
import os
import sys
import time
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "cam"


def env() -> dict:
    vals = {}
    p = ROOT / ".env"
    for line in p.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            vals[k.strip()] = v.strip().strip('"').strip("'")
    return vals


def urls(host: str, user: str, pw: str):
    for enc in (False, True):
        u = quote(user, safe="") if enc else user
        p = quote(pw, safe="") if enc else pw
        for ch in (1, 2):
            for st in (0, 1):
                yield (f"xm ch{ch} st{st}{' enc' if enc else ''}",
                       f"rtsp://{host}:554/user={u}&password={p}&channel={ch}&stream={st}.sdp?real_stream")
    yield ("std onvif", f"rtsp://{quote(user, safe='')}:{quote(pw, safe='')}@{host}:554/onvif1")


def grab(url: str, dst: Path) -> str:
    import av
    t0 = time.monotonic()
    c = av.open(url, options={"rtsp_transport": "tcp", "stimeout": "6000000"}, timeout=(8, 8))
    try:
        for frame in c.decode(video=0):
            if frame.key_frame or time.monotonic() - t0 > 3:
                frame.to_image().save(dst, quality=85)
                return f"{frame.width}x{frame.height} за {time.monotonic() - t0:.1f} с"
    finally:
        c.close()
    raise RuntimeError("поток без кадров")


def main() -> int:
    e = env()
    host, user, pw = e.get("OKO_CAM_HOST"), e.get("OKO_CAM_USER"), e.get("OKO_CAM_PASS", "")
    if not (host and user):
        print("FATAL: нет OKO_CAM_HOST / OKO_CAM_USER в .env")
        return 2
    OUT.mkdir(parents=True, exist_ok=True)
    ok = 0
    seen_ch = set()
    for name, url in urls(host, user, pw):
        ch = name.split()[1] if name.startswith("xm") else name
        if ch in seen_ch:
            continue
        dst = OUT / f"probe_{name.replace(' ', '_')}.jpg"
        try:
            info = grab(url, dst)
            print(f"OK   {name:16} {info} -> {dst}")
            ok += 1
            seen_ch.add(ch)              # канал заработал -- другие варианты этого канала не нужны
        except Exception as ex:  # noqa: BLE001
            msg = str(ex).replace(pw, "***") if pw else str(ex)
            print(f"FAIL {name:16} {type(ex).__name__}: {msg[:120]}")
    print(f"RESULT: {ok} рабочих потоков")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
