"""TV_WOL_PROBE (03.11.2026): включается ли ТВ по сети пакетом Wake-on-LAN -- проверка по кадру камеры.

Факт 27.10: «включи телевизор» -- честный отказ: powerstate нет, screenstate игнорируется, порт 1926 в режиме
ожидания закрыт. Факт 03.11 15:40 (экран «Сеть» на ТВ): есть пункт «Включение по сигналу Wi-Fi» (WoWLAN).
Если его включить, ТВ может просыпаться от «магического пакета» на свой MAC. Работает ли это через раздачу
планшета -- неизвестно (HYPOTHESIS); проверяем: пакет -> пауза -> порт 1926 и кадр камеры.

Ничего в ТВ не меняет: только UDP-пакеты пробуждения (порты 9 и 7) на широковещательный адрес сети и на адрес ТВ.
Запуск: python tools\\tv_wol_probe.py [MAC] -- по умолчанию MAC ТВ из .env TV_MAC или аргумента.
"""
import socket
import sys
import time
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parent.parent), str(Path(__file__).resolve().parent.parent / "src")]
import config  # noqa: E402,F401  -- .env
import camera_watcher as C  # noqa: E402


def magic(mac: str) -> bytes:
    hexs = mac.replace(":", "").replace("-", "").strip()
    if len(hexs) != 12:
        raise ValueError(f"MAC должен быть из 12 hex-цифр: {mac}")
    return b"\xff" * 6 + bytes.fromhex(hexs) * 16


def send(mac: str, targets) -> list[str]:
    pkt, sent = magic(mac), []
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    for host in targets:
        for port in (9, 7):
            try:
                s.sendto(pkt, (host, port))
                sent.append(f"{host}:{port}")
            except OSError as e:
                sent.append(f"{host}:{port} ERR {e}")
    s.close()
    return sent


def local_base() -> str:
    """Подсеть, в которой ноутбук СЕЙЧАС (TV_HOST в .env может быть из прошлой сети: 03.11 -- 10.133.133.41,
    а ТВ живёт в 10.95.175.x; клиент ТВ находит его по MAC, а этой пробе нужен текущий широковещательный адрес)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))                # пакет не уходит: только выбор интерфейса по маршруту
        return s.getsockname()[0].rsplit(".", 1)[0]
    finally:
        s.close()


def port_open(host: str, port: int = 1926, timeout: float = 1.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    mac = args[0] if args else C._env("TV_MAC")
    base = local_base()
    host = args[1] if len(args) > 1 else f"{base}.{C._env('TV_HOST').rsplit('.', 1)[-1] or '41'}"
    if not mac:
        print("FATAL: нужен MAC (аргумент или TV_MAC в .env)")
        return 2
    print(f"ТВ {host}, MAC {mac}; порт 1926 сейчас: {'ОТКРЫТ' if port_open(host) else 'закрыт'}")
    shot = C.snapshot()
    print(f"кадр до: {shot.screen} (яркость {shot.luma:.0f}) -> {shot.frame_path}")
    if shot.screen == "on" and "--force" not in sys.argv:
        print("STOP: по кадру экран светится. Если ТВ на самом деле выключен -- пришли этот кадр (признаки врут), "
              "или запусти с --force")
        return 1
    for rnd in range(3):                          # 3 серии: Wi-Fi в режиме ожидания может спать между пакетами
        print(f"серия {rnd + 1}: " + ", ".join(send(mac, ["255.255.255.255", f"{base}.255", host])))
        time.sleep(1.0)
    for t in (5, 10, 20):
        time.sleep(t - (0 if t == 5 else (5 if t == 10 else 10)))
        p = port_open(host)
        sh = C.snapshot()
        print(f"через {t} с: порт 1926 {'ОТКРЫТ' if p else 'закрыт'}, кадр: {sh.screen} (яркость {sh.luma:.0f})")
        if sh.screen == "on":
            print("RESULT: ТВ ПРОСНУЛСЯ от Wake-on-LAN (по кадру камеры)")
            return 0
    print("RESULT: не проснулся -- WoWLAN выключен в ТВ, или раздача планшета не пропускает пакет, или Wi-Fi ТВ спит")
    return 1


if __name__ == "__main__":
    sys.exit(main())
