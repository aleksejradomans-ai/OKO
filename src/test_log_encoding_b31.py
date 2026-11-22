"""B31 P5: лог бота в UTF-8 явно. Под NSSM stderr -- файл, Python на Windows кодировал его в cp1251.
Кириллическая фраза проходит путь записи (StreamHandler на stderr после force_utf8) и читается как UTF-8 байт-в-байт;
строка-граница [LOG_ENCODING] -- ASCII, с ts переключения."""
from __future__ import annotations

import io
import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest

import logger as oko_logger

PHRASE = "[VOICE] понял: «яркость плюс два» символов=21 ёЁ"
ROOT = Path(__file__).resolve().parent


@pytest.mark.parametrize("nl", ["\n", "\r\n"], ids=["lf", "crlf_windows"])
def test_force_utf8_reencodes_cp1251_stream_byte_for_byte(nl):
    """поток как у NSSM на Windows (cp1251, перевод строк потока nl) -> после force_utf8: те же переводы строк, UTF-8."""
    raw = io.BytesIO()
    st = io.TextIOWrapper(raw, encoding="cp1251", errors="strict", newline=nl, write_through=True)
    st.write("до переключения\n")                     # старые строки не переписываются
    old = raw.getvalue()
    assert old == ("до переключения" + nl).encode("cp1251")
    sys_stderr = sys.stderr
    try:
        sys.stderr = st
        assert oko_logger.force_utf8(("stderr",)) == "cp1251"
        h = logging.StreamHandler(sys.stderr)
        h.setFormatter(logging.Formatter("%(message)s"))
        lg = logging.getLogger("b31.enc")
        lg.addHandler(h)
        lg.propagate = False
        lg.warning(PHRASE)
        lg.removeHandler(h)
        st.flush()
    finally:
        sys.stderr = sys_stderr
    data = raw.getvalue()
    assert data.startswith(old)
    assert data[len(old):] == (PHRASE + nl).encode("utf-8")              # байт-в-байт: UTF-8 и перевод строк потока
    assert data[len(old):].decode("utf-8") == PHRASE + nl


def test_force_utf8_leaves_utf8_and_foreign_streams():
    class NoReconf:
        encoding = "cp1251"
    s = sys.stderr
    try:
        sys.stderr = NoReconf()
        assert oko_logger.force_utf8(("stderr",)) == ""
        sys.stderr = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
        assert oko_logger.force_utf8(("stderr",)) == ""
    finally:
        sys.stderr = s


def test_setup_logging_in_cp1251_process_writes_utf8_and_ascii_marker():
    code = ("import sys, logging; sys.path.insert(0, %r); import logger as L; L.setup_logging(); "
            "logging.getLogger('oko.stt').warning(%r)" % (str(ROOT), PHRASE))
    env = {"PYTHONIOENCODING": "cp1251", "PYTHONUTF8": "0", "PATH": "", "SYSTEMROOT": os.environ.get("SYSTEMROOT", "")}
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, timeout=60, env=env)
    assert r.returncode == 0, r.stderr[-300:]
    sep = os.linesep.encode("ascii")                  # перевод строк stderr платформы (Windows: CRLF) сохраняется
    assert r.stderr.endswith(sep) and (sep == b"\r\n" or b"\r" not in r.stderr)
    lines = r.stderr.split(sep)
    mark = [ln for ln in lines if oko_logger.ENCODING_MARK.encode("ascii") in ln]
    assert len(mark) == 1 and mark[0].decode("ascii") and b"(stderr was cp1251)" in mark[0]
    body = [ln for ln in lines if "oko.stt".encode() in ln]
    assert len(body) == 1 and body[0].endswith(PHRASE.encode("utf-8"))   # байт-в-байт UTF-8
