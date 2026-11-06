"""robo_probe.py — разведка пылесоса Roborock в локальной сети. ТОЛЬКО ЧТЕНИЕ.

    python src\\robo_probe.py                 # ARP + handshake по широковещанию
    python src\\robo_probe.py --ports         # ещё и проверка портов кандидатов
    python src\\robo_probe.py --host 10.17.115.50

Что делает и чего НЕ делает:
  * читает таблицу ARP (без трафика вообще) и сопоставляет префикс MAC с
    производителем;
  * посылает штатный пакет приветствия miIO на UDP 54321 — тот же, которым
    пользуется официальное приложение и `python-miio`. Это обычное
    обнаружение устройства в своей сети, а не вскрытие;
  * опционально проверяет, открыты ли известные порты (connect + закрытие,
    без данных);
  * НЕ подбирает пароли, НЕ шлёт команд, НЕ меняет состояние устройства.

ТОКЕН НЕ ПЕЧАТАЕТСЯ. Ответ на приветствие содержит 16 байт, где у части
прошивок лежит ключ устройства. Это учётные данные: вывод сообщает только
его СОСТОЯНИЕ (нули / FF / присутствует), но не значение. Так сделано
намеренно: вывод скрипта попадает в журналы, переписку и документы, а
напечатанный однажды секрет считается раскрытым.

Диагностика, тестов нет по той же причине, что у tv_probe.py: модуль
измеряет чужое устройство, и проверять здесь нечего, кроме самого
устройства. Разбор ответа — единственная чистая логика — вынесен в
`parse_hello`, его поведение видно глазами на живом выводе.
"""
from __future__ import annotations

import argparse
import re
import socket
import struct
import subprocess
import sys
from typing import Iterable

MIIO_PORT = 54321
HELLO = bytes.fromhex("21310020ffffffffffffffffffffffffffffffffffffffffffffffffffffffff")
MAGIC = 0x2131

# Префиксы MAC. Список намеренно короткий и неполный: он помогает заметить
# устройство, а не доказать, что его нет. Отсутствие совпадения НЕ означает,
# что пылесоса в сети нет — производители меняют пулы, а часть устройств
# использует случайный MAC.
OUI = {
    "b0:4a:39": "Roborock",
    "1c:c1:16": "Roborock",
    "70:7d:b9": "Roborock",
    "54:ef:44": "Xiaomi/Lumi",
    "78:11:dc": "Xiaomi",
    "64:90:c1": "Xiaomi",
    "04:cf:8c": "Xiaomi",
    "28:6c:07": "Xiaomi",
    "f0:b4:29": "Xiaomi",
    "50:ec:50": "Xiaomi",
    "8c:de:52": "Roborock/Tuya",
}

# Порты, которые имеет смысл проверить у пылесоса. Назначение — подсказка,
# а не утверждение: на конкретной ревизии прошивки состав отличается.
PORTS = {
    22: "SSH (открыт только на кастомной прошивке)",
    80: "HTTP (Valetudo или сервисная страница)",
    443: "HTTPS",
    1883: "MQTT без TLS",
    8883: "MQTT TLS (облачный канал)",
    6665: "Tuya local",
    6668: "Tuya local",
    54321: "miIO (локальное управление)",
    58867: "P2P/видео (встречается на моделях с камерой)",
}


def say(text: str = "") -> None:
    """Печать, не падающая на консоли без UTF-8 (PowerShell 5.1, cp866)."""
    try:
        print(text)
    except UnicodeEncodeError:
        print(text.encode("ascii", "replace").decode("ascii"))


def local_ipv4() -> list[str]:
    """Адреса этой машины. UDP-сокет без отправки — маршрут, не трафик."""
    out: list[str] = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 53))
        out.append(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in out and not ip.startswith("127."):
                out.append(ip)
    except OSError:
        pass
    return out


def arp_table() -> list[tuple[str, str]]:
    """(ip, mac) из системной таблицы ARP. Своего трафика не создаёт."""
    try:
        raw = subprocess.run(["arp", "-a"], capture_output=True, text=True,
                             timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    rows: list[tuple[str, str]] = []
    pattern = re.compile(r"(\d+\.\d+\.\d+\.\d+)\s+([0-9a-fA-F]{2}([-:])[0-9a-fA-F]{2}"
                         r"(\3[0-9a-fA-F]{2}){4})")
    for line in raw.splitlines():
        m = pattern.search(line)
        if m:
            rows.append((m.group(1), m.group(2).lower().replace("-", ":")))
    return rows


def vendor(mac: str) -> str | None:
    return OUI.get(mac[:8])


def parse_hello(data: bytes) -> dict | None:
    """Разбор ответа miIO. Возвращает описание БЕЗ значения токена.

    Формат заголовка (32 байта): магия 0x2131, длина, 4 нулевых байта,
    идентификатор устройства, отметка времени, 16 байт ключа.
    """
    if len(data) < 32:
        return None
    magic, length = struct.unpack(">HH", data[:4])
    if magic != MAGIC:
        return None
    device_id, stamp = struct.unpack(">II", data[8:16])
    token = data[16:32]
    if token == b"\x00" * 16:
        state = "нули (прошивка токен не отдаёт)"
    elif token == b"\xff" * 16:
        state = "FF (не привязан или токен скрыт)"
    else:
        state = "ПРИСУТСТВУЕТ (значение не печатается)"
    return {
        "device_id": device_id,
        "device_id_hex": f"{device_id:08x}",
        "stamp": stamp,
        "token_state": state,
        "length": length,
    }


def hello(targets: Iterable[str], timeout: float = 2.0) -> list[tuple[str, dict]]:
    """Приветствие miIO по списку адресов. Ответы собираются одним сокетом."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.settimeout(timeout)
    sent = 0
    for ip in targets:
        try:
            sock.sendto(HELLO, (ip, MIIO_PORT))
            sent += 1
        except OSError as exc:
            say(f"  не отправлено на {ip}: {exc}")
    say(f"  отправлено приветствий: {sent}")
    found: dict[str, dict] = {}
    while True:
        try:
            data, addr = sock.recvfrom(1024)
        except socket.timeout:
            break
        except OSError:
            break
        info = parse_hello(data)
        if info and addr[0] not in found:
            found[addr[0]] = info
    sock.close()
    return sorted(found.items())


def port_open(host: str, port: int, timeout: float = 0.6) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        return s.connect_ex((host, port)) == 0
    except OSError:
        return False
    finally:
        s.close()


def broadcast_addresses() -> list[str]:
    """255.255.255.255 плюс широковещательный адрес каждой своей сети /24.

    Общий широковещательный адрес на Windows часто не уходит дальше первого
    интерфейса, поэтому адрес подсети добавляется отдельно.
    """
    out = ["255.255.255.255"]
    for ip in local_ipv4():
        parts = ip.split(".")
        if len(parts) == 4:
            candidate = ".".join(parts[:3] + ["255"])
            if candidate not in out:
                out.append(candidate)
    return out


def subnet_hosts(ip: str) -> list[str]:
    """Все адреса /24 вокруг заданного, кроме самого адреса и .0/.255."""
    parts = ip.split(".")
    if len(parts) != 4:
        return []
    base = ".".join(parts[:3])
    mine = parts[3]
    return [f"{base}.{n}" for n in range(1, 255) if str(n) != mine]


def tcp_alive(hosts: Iterable[str], port: int = MIIO_PORT,
              timeout: float = 0.3) -> list[str]:
    """Кто отвечает на TCP по указанному порту. Соединение сразу закрывается."""
    out = []
    for host in hosts:
        if port_open(host, port, timeout=timeout):
            out.append(host)
    return out


def subnet_note(mine: list[str]) -> list[str]:
    """Замечание о достижимости адреса из `.env` из текущей подсети.

    ЗАЧЕМ ЗДЕСЬ. 28.10 машина оказалась в домашней сети 192.168.50.0/24,
    тогда как телевизор прописан в `.env` адресом из сети телефона
    10.17.115.0/24. Устройство достижимо только из той сети, к которой
    подключено, и молчание телевизора в такой ситуации выглядит как
    поломка контура, хотя это смена сети. Поэтому проверка явная.
    """
    import os
    lines = []
    host = os.getenv("TV_HOST", "")
    if not host:
        return lines
    prefixes = {".".join(ip.split(".")[:3]) for ip in mine}
    tv_prefix = ".".join(host.split(".")[:3])
    if tv_prefix in prefixes:
        lines.append(f"  TV_HOST {host} — в этой же подсети")
    else:
        lines.append(f"  TV_HOST {host} — ДРУГАЯ подсеть ({tv_prefix}.0/24)")
        lines.append("  Телевизор из этой сети недостижим: это не сбой контура,")
        lines.append("  а разные сети. Либо подключить его сюда, либо вернуть")
        lines.append("  машину в ту сеть, либо держать два адреса и выбирать.")
    return lines


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Разведка Roborock в локальной сети (чтение)")
    ap.add_argument("--host", action="append", default=[],
                    help="проверить конкретный адрес (можно повторять)")
    ap.add_argument("--ports", action="store_true", help="проверить известные порты")
    ap.add_argument("--sweep", action="store_true",
                    help="послать приветствие каждому адресу своей /24 "
                         "(обходит блокировку широковещания точкой доступа)")
    ap.add_argument("--timeout", type=float, default=2.5, help="ожидание ответов, с")
    args = ap.parse_args(argv)

    say("=== ЭТА МАШИНА ===")
    for ip in local_ipv4():
        say(f"  {ip}")

    say("")
    say("=== ТАБЛИЦА ARP (без трафика) ===")
    rows = arp_table()
    if not rows:
        say("  пусто или arp недоступен")
    candidates: list[str] = list(args.host)
    # Печатаются ВСЕ записи, а не только совпавшие по производителю.
    # Первая версия показывала лишь совпадения — и на живом прогоне 28.10
    # скрыла все девять найденных устройств, потому что ни один префикс MAC
    # не попал в список. Список префиксов неполон по условию, поэтому
    # прятать несовпавшие строки означает прятать именно те данные, по
    # которым человек и опознаёт своё устройство.
    for ip, mac in rows:
        name = vendor(mac)
        if name:
            say(f"  {ip:<16} {mac}   <-- {name}")
            if ip not in candidates:
                candidates.append(ip)
        else:
            say(f"  {ip:<16} {mac}")
    say(f"  записей всего: {len(rows)}, совпало по производителю: "
        f"{sum(1 for _ip, m in rows if vendor(m))}")
    say("  (несовпадение НЕ доказывает отсутствие: пулы MAC меняются,")
    say("   часть устройств использует случайный MAC)")

    mine = local_ipv4()
    note = subnet_note(mine)
    if note:
        say("")
        say("=== ДОСТУПНОСТЬ ПО ПОДСЕТЯМ ===")
        for line in note:
            say(line)

    say("")
    say("=== ПРИВЕТСТВИЕ miIO (UDP 54321) ===")
    targets = list(dict.fromkeys(candidates + broadcast_addresses()))
    if args.sweep:
        for ip in mine:
            targets.extend(subnet_hosts(ip))
        targets = list(dict.fromkeys(targets))
        say(f"  обход подсети включён: адресов {len(targets)}")
    answers = hello(targets, timeout=args.timeout)
    if not answers:
        say("  ответов нет")
        say("  Это НЕ значит, что пылесоса в сети нет. Возможные причины:")
        say("   - он спит на базе и не слушает сеть;")
        say("   - он в другой подсети или в гостевой сети Wi-Fi;")
        say("   - прошивка отвечает только на свой облачный канал;")
        say("   - широковещание блокируется точкой доступа (изоляция клиентов).")
    for ip, info in answers:
        say(f"  {ip}")
        say(f"     идентификатор: {info['device_id']} (hex {info['device_id_hex']})")
        say(f"     отметка времени: {info['stamp']}")
        say(f"     токен: {info['token_state']}")
        if ip not in candidates:
            candidates.append(ip)

    if args.ports:
        say("")
        say("=== ПОРТЫ КАНДИДАТОВ ===")
        if not candidates:
            say("  кандидатов нет")
        for ip in candidates:
            say(f"  {ip}")
            for port, purpose in sorted(PORTS.items()):
                if port_open(ip, port):
                    say(f"     {port:<6} открыт   {purpose}")

    if args.sweep:
        say("")
        say("=== КТО ОТВЕЧАЕТ НА TCP 54321 ===")
        say("  (присутствие устройства miIO, даже если UDP блокируется)")
        alive = tcp_alive([ip for ip, _m in rows] +
                          [h for m in mine for h in subnet_hosts(m)])
        if not alive:
            say("  никто")
        for ip in alive:
            say(f"  {ip}")
            if ip not in candidates:
                candidates.append(ip)

    say("")
    say("=== ИТОГ ===")
    say(f"  кандидатов: {len(candidates)}")
    for ip in candidates:
        say(f"   {ip}")
    if not candidates:
        say("  Ничего не найдено. Следующий шаг — не код, а проверка факта:")
        say("  посмотреть список клиентов на роутере и убедиться, что пылесос")
        say("  вообще в этой сети, а не в гостевой и не только в облаке.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
