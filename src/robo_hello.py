"""robo_hello.py — вывести Roborock со станции, поздороваться, вернуть (первый замер движения, 28.10.2026).

Последовательность (только локальный канал, TCP 58867):
  1. предусловия: робот на станции (charging/charging_complete), без ошибок, заряд >= MIN_BATTERY
  2. app_start  -> опрос get_status раз в секунду, журнал КАЖДОГО перехода состояния с отметкой времени
  3. робот считается вышедшим, когда он не на зарядке И в состоянии уборки OUT_HOLD_S секунд подряд
  4. app_pause -> find_me («я здесь») -> пауза -> app_charge
  5. опрос до возвращения на зарядку
Аварийный путь: любой сбой, таймаут или Ctrl+C после app_start -> app_charge (одна попытка).
Если и он не прошёл — робот продолжает уборку, остановить его из приложения Roborock.

Замер: время до выхода (у моющего робота перед выездом может быть мойка тряпок), время возврата,
задержка каждой команды, все промежуточные состояния. Ответ "ok" не считается доказательством —
доказательство в смене состояния и в том, что Оператор видел и слышал.

Коды выхода: 0 успех, 2 транспорт, 4 предусловие, 5 не вышел за LEAVE_TIMEOUT_S, 6 не вернулся за DOCK_TIMEOUT_S,
7 потерян канал во время движения.
Запуск:  python robo_hello.py --host 192.168.50.80
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

from robo_local_status import _default_store, _pick_device, open_local

MIN_BATTERY = 40
LEAVE_TIMEOUT_S = 180       # с запасом на мойку тряпок перед выездом
OUT_HOLD_S = 4              # сколько секунд подряд робот должен быть «в уборке вне станции»
DOCK_TIMEOUT_S = 180
POLL_S = 1.0
MAX_POLL_FAILS = 5

DOCKED_STATES = {8, 100}                                   # charging, charging_complete
CLEANING_STATES = {5, 11, 17, 18} | set(range(6301, 6310))  # уборка в любом виде


def _state_name(code) -> str:
    try:
        from roborock.data.v1.v1_code_mappings import RoborockStateCode
        return RoborockStateCode(code).name
    except Exception:
        return f"code_{code}"


class Run:
    def __init__(self, rpc, exc_type):
        self.rpc, self.E = rpc, exc_type
        self.t0 = time.monotonic()
        self.last = None
        self.fails = 0

    def t(self) -> str:
        return f"{time.monotonic() - self.t0:7.1f}s"

    async def cmd(self, name: str):
        t = time.monotonic()
        res = await self.rpc.send_command(name)
        print(f"{self.t()}  [КОМАНДА] {name} -> {res}  ({(time.monotonic() - t) * 1000:.0f} мс)")
        return res

    async def status(self) -> dict | None:
        try:
            res = await self.rpc.send_command("get_status")
        except self.E as e:
            self.fails += 1
            print(f"{self.t()}  [ТРАНСПОРТ] get_status сбой {self.fails}/{MAX_POLL_FAILS}: {type(e).__name__}: {e}")
            return None
        self.fails = 0
        st = res[0] if isinstance(res, list) and res else res
        key = (st.get("state"), st.get("charge_status"), st.get("error_code"))
        if key != self.last:
            print(f"{self.t()}  [СОСТОЯНИЕ] {_state_name(st.get('state'))} ({st.get('state')})"
                  f"  зарядка={st.get('charge_status')}  батарея={st.get('battery')}"
                  f"  ошибка={st.get('error_code')}  мойка={st.get('wash_phase')}")
            self.last = key
        return st

    def lost(self) -> bool:
        return self.fails >= MAX_POLL_FAILS


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--host", required=True)
    ap.add_argument("--name", default=None)
    ap.add_argument("--store", type=Path, default=_default_store())
    a = ap.parse_args()

    dev = _pick_device(a.store, a.name)
    print(f"[устройство] {dev.get('name')}  {dev.get('model')}  -> {a.host}:58867")
    opened = await open_local(a.host, dev)
    if opened is None:
        return 2
    ch, rpc, E = opened
    r = Run(rpc, E)
    moving = False
    try:
        st = await r.status()
        if st is None:
            return 2
        if st.get("state") not in DOCKED_STATES or st.get("error_code"):
            print(f"СТОП: робот не стоит на зарядке без ошибок (state={st.get('state')}, error={st.get('error_code')})."
                  " Дождитесь charging и повторите.")
            return 4
        if (st.get("battery") or 0) < MIN_BATTERY:
            print(f"СТОП: заряд {st.get('battery')} < {MIN_BATTERY}")
            return 4

        print(f"{r.t()}  --- выезд ---")
        moving = True
        await r.cmd("app_start")

        out_since = None
        deadline = time.monotonic() + LEAVE_TIMEOUT_S
        while True:
            await asyncio.sleep(POLL_S)
            st = await r.status()
            if r.lost():
                return 7
            if st is None:
                continue
            if st.get("error_code"):
                print(f"{r.t()}  [ОШИБКА РОБОТА] error_code={st.get('error_code')}")
                return 5
            out = st.get("charge_status") == 0 and st.get("state") in CLEANING_STATES
            out_since = (out_since or time.monotonic()) if out else None
            if out_since and time.monotonic() - out_since >= OUT_HOLD_S:
                print(f"{r.t()}  [ЗАМЕР] вне станции {OUT_HOLD_S} с подряд")
                break
            if time.monotonic() > deadline:
                print(f"{r.t()}  [ЗАМЕР] за {LEAVE_TIMEOUT_S} с не вышел")
                return 5

        print(f"{r.t()}  --- приветствие ---")
        await r.cmd("app_pause")
        await asyncio.sleep(1.5)
        await r.cmd("find_me")
        await asyncio.sleep(4)

        print(f"{r.t()}  --- возврат ---")
        await r.cmd("app_charge")
        deadline = time.monotonic() + DOCK_TIMEOUT_S
        while True:
            await asyncio.sleep(POLL_S)
            st = await r.status()
            if r.lost():
                return 7
            if st and st.get("charge_status") == 1:
                moving = False
                print(f"{r.t()}  [ЗАМЕР] на станции, зарядка идёт")
                return 0
            if time.monotonic() > deadline:
                print(f"{r.t()}  [ЗАМЕР] за {DOCK_TIMEOUT_S} с не вернулся")
                return 6
    except (E, asyncio.CancelledError, KeyboardInterrupt) as e:
        print(f"{r.t()}  [ПРЕРВАНО] {type(e).__name__}: {e}")
        return 7
    finally:
        if moving:
            try:
                await r.cmd("app_charge")
                print(f"{r.t()}  [АВАРИЯ] отправлен app_charge — проверьте, что робот едет на станцию")
            except Exception as e:
                print(f"{r.t()}  [АВАРИЯ] app_charge НЕ прошёл ({type(e).__name__}: {e})."
                      " Остановите робота в приложении Roborock.")
        ch.close()


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(7)
