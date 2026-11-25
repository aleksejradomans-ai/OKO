"""B33 п.6.7: исход КАЖДОЙ попытки пуша (tools/notify.py) -- отдельная строка run_metrics (через src/block_finish.py).

  python tools/push_record.py --db <quarantine_state.db> --task B33 --script selfrun|run|stage|promote|cycle
      --started ISO --notify-rc N --notify-line "<строка NOTIFY ...>"

Строка run_metrics: block = notify_<script>, outcome GREEN (Telegram принял: rc 0 и ровно `NOTIFY sent=yes
message_id=<цифры>`) или RED (не принят; fail_code -- машинный код причины из `sent=no reason=<код>:`, иначе
no_answer), steps 1, net_errors 1 при сетевой причине, остальные счётчики 0. Своя соль на каждую попытку (новая
строка), текст пуша не пишется. Вывод -- строки block_finish (BLOCK_FINISH ..., SIGNAL ...) с префиксом PUSH_METRIC.
Код возврата: 0 -- строка записана и прочитана назад; 1 -- нет (итог блока от этого не меняется)."""
import argparse
import datetime as dt
import hashlib
import re
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import block_finish as bf  # noqa: E402

SCRIPTS = ("selfrun", "run", "stage", "promote", "cycle")
_YES = re.compile(r"^NOTIFY sent=yes message_id=\d{1,20}$")
_NO = re.compile(r"^NOTIFY sent=no reason=([a-z][a-z0-9_]{0,31})(?::|$)")
NET = ("network", "timeout")


def metric_lines(task: str, script: str, started: str, ended: str, rc: int, line: str) -> list:
    ln = (line or "").strip()
    sent = rc == 0 and bool(_YES.match(ln))
    m = _NO.match(ln)
    code = "" if sent else (m.group(1) if m else "no_answer")
    vals = {"task": task, "block": f"notify_{script}", "started_at": started, "ended_at": ended, "steps": 1,
            "rollbacks": 0, "uac": 0, "hitl": 0, "parse_errors": 0, "net_errors": int(code in NET), "flaky": 0,
            "signal_wanted": 1 if sent else 2, "report_sha16": hashlib.sha256(ln.encode("utf-8")).hexdigest()[:16],
            "run_nonce": secrets.token_hex(16), "outcome": "GREEN" if sent else "RED",
            "fail_stage": "" if sent else "push", "fail_code": code, "launch_ts": started,
            "launch_delay_s": "", "uac_wait_s": "0", "hitl_wait_s": "0"}
    return [f"METRIC {k}={v}" for k, v in vals.items()]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="push_record")
    ap.add_argument("--db", required=True)
    ap.add_argument("--task", required=True)
    ap.add_argument("--script", required=True, choices=SCRIPTS)
    ap.add_argument("--started", required=True)
    ap.add_argument("--notify-rc", type=int, required=True)
    ap.add_argument("--notify-line", default="")
    a = ap.parse_args(argv)
    ended = dt.datetime.now().replace(microsecond=0).isoformat()
    started = dt.datetime.fromisoformat(a.started).replace(microsecond=0).isoformat()
    if started > ended:
        started = ended
    r = bf.finish(metric_lines(a.task, a.script, started, ended, a.notify_rc, a.notify_line), Path(a.db))
    out = (f"PUSH_METRIC BLOCK_FINISH recorded={r['recorded']} outcome={r['outcome']} attempt={r['attempt']} "
           f"reason={r['reason'] or '-'}\nPUSH_METRIC {r['line']}\n")
    sys.stdout.buffer.write(out.encode("ascii", "backslashreplace"))  # B33 attempt 4: output ASCII
    return 0 if r["recorded"] else 1


if __name__ == "__main__":
    sys.exit(main())
