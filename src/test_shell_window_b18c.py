"""B18c (18.11, Оператор 15:12): свёрнутая плашка окна -- тумблер режима, 🎙 и строка ТОЛК без разворачивания,
одно состояние на оба вида; кнопки выключены, пока Око отвечает. Живая последовательность через настоящий канал
shell_channel (127.0.0.1, своя петля asyncio) и настоящий sandbox_talk.TalkState; микрофон и модель -- подделки."""
from __future__ import annotations

import threading
from pathlib import Path

import pytest

import sandbox_talk as talk
import shell_client as sc
import shell_voice as sv
from test_shell_window_b18b import _factory, _tone, tmpdir_voice  # noqa: F401 -- фикстура и подделки B18b

SRC = Path(__file__).resolve().parent


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def test_short_reply_for_pill():
    assert sc.short_reply("[Око]: Подсветка: синяя") == "Подсветка: синяя"
    assert sc.short_reply(f"{talk.MARKER}\n\nЭто похоже на команду: свет.\nДальше") == "Это похоже на команду: свет."
    long = "слово " * 40
    r = sc.short_reply(long)
    assert len(r) <= sc.PILL_REPLY_CHARS and r.endswith("…")
    assert sc.short_reply("") == ""


def test_rec_clock_format():
    assert sc.rec_clock(0) == "● 0:00 / 0:30"
    assert sc.rec_clock(7.99) == "● 0:07 / 0:30"
    assert sc.rec_clock(30) == sc.rec_clock(31) == "● 0:30 / 0:30"
    assert sc.rec_clock(75, 90) == "● 1:15 / 1:30"


def test_talk_line_marker_and_timer():
    c = Clock()
    m = sc.ModeView(clock=c)
    assert sc.talk_line(m) == ""
    m.update("/talk", [talk.T_ON])
    assert sc.talk_line(m) == f"{talk.MARKER} · автосброс через 30 мин"     # сразу после /talk -- 30 (ceil)
    c.t += 59.5
    assert m.left_minutes() == 30                                              # осталось 29:00.5
    c.t += 0.5
    assert sc.talk_line(m) == f"{talk.MARKER} · автосброс через 29 мин"     # осталось ровно 29:00
    c.t += 28 * 60 + 59
    assert m.left_minutes() == 1                                               # последняя секунда -- «1 мин», не «0»
    c.t += 1
    assert sc.talk_line(m) == "" and m.left_minutes() == 0


# --- живая последовательность (18.11 15:0x: ТОЛК вживую не включился) --------------------------------
# Окно (настоящее, offscreen) -> настоящий канал shell_channel на 127.0.0.1 (своя петля asyncio в потоке) ->
# настоящий dispatch -> обработчики-двойники бота: /talk /lock и развилка «ТОЛК активен -> песочница, иначе маршрут»
# на настоящем sandbox_talk.TalkState. Журнал канала ([SHELL] kind=...) -- настоящий логгер.
class _Bot:
    def __init__(self):
        import logging
        self.events, self.state = [], talk.TalkState()
        self.gate = threading.Event()
        self.gate.set()

        class H(logging.Handler):
            def emit(h, rec):
                msg = rec.getMessage()
                if "[SHELL] request" in msg:
                    self.events.append("kind=" + msg.rsplit("kind=", 1)[1])
        self.h = H()
        logging.getLogger("shell_channel").addHandler(self.h)
        logging.getLogger("shell_channel").setLevel(logging.INFO)

    async def talk(self, upd, ctx):
        self.state.start(upd.message.from_user.id)
        self.events.append("[TALK] on")
        await upd.message.reply_text(talk.T_ON)

    async def lock(self, upd, ctx):
        self.state.stop(upd.message.from_user.id)
        self.events.append("[TALK] off")
        await upd.message.reply_text(talk.T_OFF)

    async def text(self, upd, ctx):
        import asyncio
        while not self.gate.is_set():
            await asyncio.sleep(0.02)
        if self.state.active(upd.message.from_user.id):
            self.events.append("sandbox")
            await upd.message.reply_text(f"{talk.MARKER}\nспарринг")
        else:
            self.events.append("ROUTE")
            await upd.message.reply_text("[Око]: ответ маршрута")

    async def audio(self, path):
        self.events.append("STT")
        return sv.transcribe_and_delete(path, lambda p: "мысль вслух")


@pytest.fixture
def live_channel():
    import asyncio
    import logging
    import shell_channel as ch
    bot = _Bot()
    loop = asyncio.new_event_loop()
    box = {}

    def run():
        asyncio.set_event_loop(loop)
        c = ch.ShellChannel(None, "k", 7, {"text": bot.text, "talk": bot.talk, "lock": bot.lock, "audio": bot.audio})
        box["port"] = loop.run_until_complete(c.start(0))
        box["ch"] = c
        loop.run_forever()
    th = threading.Thread(target=run, daemon=True)
    th.start()
    import time as _t
    t0 = _t.monotonic()
    while "port" not in box and _t.monotonic() - t0 < 5:
        _t.sleep(0.02)
    yield bot, box["port"]
    asyncio.run_coroutine_threadsafe(box["ch"].stop(), loop).result(5)
    loop.call_soon_threadsafe(loop.stop)
    th.join(5)
    logging.getLogger("shell_channel").removeHandler(bot.h)


def _pump(app, cond, timeout=10.0):
    import time as _t
    t0 = _t.monotonic()
    while not cond() and _t.monotonic() - t0 < timeout:
        app.processEvents()
        _t.sleep(0.02)
    app.processEvents()
    return cond()


def test_live_sequence_window_button_talk_voice_sandbox(monkeypatch, tmpdir_voice, live_channel):
    """старт окна -> кнопка -> /talk kind=cmd -> [TALK] on -> голос -> STT -> песочница -> кнопка -> /lock."""
    pytest.importorskip("PyQt6.QtWidgets")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtCore import QSettings, Qt
    from PyQt6.QtTest import QTest
    from PyQt6.QtWidgets import QApplication
    import oko_shell
    bot, port = live_channel
    app = QApplication.instance() or QApplication([])
    w = oko_shell.Shell(QSettings(str(tmpdir_voice.parent / "s.ini"), QSettings.Format.IniFormat), sc.Conn("k", port))
    w.mode = sc.ModeView(clock=Clock())                 # часы заморожены: строка ТОЛК не зависит от скорости ПК
    w.show()
    assert w.b_mode.text() == talk.BTN_LOCK and w.b_mode.isEnabled()        # состояние: БОЕВОЙ
    QTest.mouseClick(w.b_mode, Qt.MouseButton.LeftButton)
    assert _pump(app, lambda: not w.busy and "[TALK] on" in bot.events)
    assert bot.events == ["kind=cmd", "[TALK] on"]
    assert w.mode.current() == sc.TALK and w.b_mode.text() == talk.BTN_TALK and not w.talk_bar.isHidden()
    assert w.talk_bar.text() == f"{talk.MARKER} · автосброс через 30 мин"   # шапка и плашка -- одно правило
    # голос: 🎙 -> 🎙 -> путь -> канал kind=audio -> STT -> текст -> песочница (не маршрут)
    w.rec = sc.Recorder(stream_factory=_factory([_tone(1.0)]))
    QTest.mouseClick(w.mic, Qt.MouseButton.LeftButton)
    QTest.mouseClick(w.mic, Qt.MouseButton.LeftButton)
    assert _pump(app, lambda: not w.busy and "sandbox" in bot.events)
    assert bot.events[2:] == ["kind=audio", "STT", "sandbox"]
    assert w.mode.current() == sc.TALK
    assert not tmpdir_voice.exists() or not list(tmpdir_voice.iterdir())     # запись не пережила запрос
    QTest.mouseClick(w.b_mode, Qt.MouseButton.LeftButton)
    assert _pump(app, lambda: not w.busy and "[TALK] off" in bot.events)
    assert bot.events[5:] == ["kind=cmd", "[TALK] off"] and w.mode.current() == sc.COMBAT
    w.close()


def test_live_mode_button_not_lost_while_busy(monkeypatch, tmpdir_voice, live_channel):
    """18.11: нажатие кнопки, пока Око отвечает, молча терялось. Теперь кнопка выключена до ответа, потом работает."""
    pytest.importorskip("PyQt6.QtWidgets")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtCore import QSettings, Qt
    from PyQt6.QtTest import QTest
    from PyQt6.QtWidgets import QApplication
    import oko_shell
    bot, port = live_channel
    app = QApplication.instance() or QApplication([])
    w = oko_shell.Shell(QSettings(str(tmpdir_voice.parent / "s2.ini"), QSettings.Format.IniFormat), sc.Conn("k", port))
    w.mode = sc.ModeView(clock=Clock())                 # часы заморожены: строка ТОЛК не зависит от скорости ПК
    w.show()
    bot.gate.clear()                                     # «мозг думает»
    w.input.setText("долгий вопрос")
    w._send_input()
    assert _pump(app, lambda: "kind=text" in bot.events)
    assert w.busy and not w.b_mode.isEnabled()           # видно, что сейчас не переключить
    QTest.mouseClick(w.b_mode, Qt.MouseButton.LeftButton)   # выключенная кнопка нажатие не принимает
    bot.gate.set()
    assert _pump(app, lambda: not w.busy)
    assert w.b_mode.isEnabled() and bot.events == ["kind=text", "ROUTE"]
    QTest.mouseClick(w.b_mode, Qt.MouseButton.LeftButton)
    assert _pump(app, lambda: not w.busy and "[TALK] on" in bot.events)
    assert bot.events[2:] == ["kind=cmd", "[TALK] on"] and w.mode.current() == sc.TALK
    w.close()


def test_live_compact_pill_toggle_voice_reply_without_expanding(monkeypatch, tmpdir_voice, live_channel):
    """Свёрнуто: тумблер -> /talk kind=cmd -> строка ТОЛК с таймером; 🎙 -> kind=audio -> песочница ->
    короткий ответ в плашке, полный во всплывающей подсказке; панель так и не развернулась; /lock -> строка ушла."""
    pytest.importorskip("PyQt6.QtWidgets")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtCore import QSettings, Qt
    from PyQt6.QtTest import QTest
    from PyQt6.QtWidgets import QApplication
    import oko_shell
    bot, port = live_channel
    app = QApplication.instance() or QApplication([])
    w = oko_shell.Shell(QSettings(str(tmpdir_voice.parent / "s3.ini"), QSettings.Format.IniFormat), sc.Conn("k", port))
    w.mode = sc.ModeView(clock=Clock())                 # часы заморожены: строка ТОЛК не зависит от скорости ПК
    w.show()
    w.set_compact(True)
    h0 = w.height()
    assert w.pill_mode.isVisibleTo(w) and w.pill_mic.isVisibleTo(w) and not w.panel.isVisibleTo(w)
    assert w.pill_mode.text() == w.b_mode.text() == talk.BTN_LOCK and w.pill_talk.isHidden()
    QTest.mouseClick(w.pill_mode, Qt.MouseButton.LeftButton)
    assert _pump(app, lambda: not w.busy and "[TALK] on" in bot.events)
    assert bot.events == ["kind=cmd", "[TALK] on"]
    assert w.pill_mode.text() == w.b_mode.text() == talk.BTN_TALK                    # одно состояние на оба вида
    assert not w.pill_talk.isHidden() and w.pill_talk.text().startswith(talk.MARKER) and "автосброс через 30 мин" in w.pill_talk.text()
    assert w.height() > h0 and not w.panel.isVisibleTo(w)
    w.rec = sc.Recorder(stream_factory=_factory([_tone(1.0)]))
    QTest.mouseClick(w.pill_mic, Qt.MouseButton.LeftButton)
    assert w.rec.active and w.pill_mic.text().startswith("■") and w.mic.text() == w.pill_mic.text()
    QTest.mouseClick(w.pill_mic, Qt.MouseButton.LeftButton)
    assert w.busy and not w.pill_mode.isEnabled() and not w.pill_mic.isEnabled()      # пока Око отвечает
    assert _pump(app, lambda: not w.busy and "sandbox" in bot.events)
    assert bot.events[2:] == ["kind=audio", "STT", "sandbox"]
    assert not w.pill_info.isHidden() and "спарринг" in w.pill_info.text() and talk.MARKER not in w.pill_info.text()
    assert "мысль вслух" in w.pill_info.toolTip() and talk.MARKER in w.pill_info.toolTip()   # полный ответ -- подсказкой
    assert not w.panel.isVisibleTo(w) and w.pill_mode.isEnabled() and w.pill_mic.isEnabled()
    assert not tmpdir_voice.exists() or not list(tmpdir_voice.iterdir())
    w._pill_reply_clear()
    assert w.pill_info.isHidden()
    QTest.mouseClick(w.pill_mode, Qt.MouseButton.LeftButton)
    assert _pump(app, lambda: not w.busy and "[TALK] off" in bot.events)
    assert w.pill_mode.text() == talk.BTN_LOCK and w.pill_talk.isHidden()
    w._pill_reply_clear()
    assert w.height() == h0
    w.set_compact(False)                                 # в развёрнутом виде -- полный чат и тот же режим
    assert w.panel.isVisibleTo(w) and w.b_mode.text() == talk.BTN_LOCK and w.pill_talk.isHidden()
    w.close()


def test_rec_stopwatch_header_and_pill_frozen_clock(monkeypatch, tmpdir_voice):
    """Секундомер записи на замороженных часах: «● 0:00» при старте, «● 0:07 / 0:30» через 7,4 с (в шапке и в
    плашке одинаково, на 🎙 -- «■ 22»), автостоп на 30 с, после -- секундомер скрыт, строка связи вернулась."""
    pytest.importorskip("PyQt6.QtWidgets")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtCore import QSettings
    from PyQt6.QtWidgets import QApplication
    import oko_shell
    app = QApplication.instance() or QApplication([])
    w = oko_shell.Shell(QSettings(str(tmpdir_voice.parent / "s4.ini"), QSettings.Format.IniFormat), sc.Conn("k", 1))
    sent = []
    monkeypatch.setattr(w, "_send", lambda t, shown=None, audio="": sent.append((t, shown, audio)))
    c = Clock()
    w.rec = sc.Recorder(stream_factory=_factory([_tone(1.0)]), clock=c)
    w.set_compact(True)
    assert w.rec_clock.isHidden() and w.pill_rec.isHidden() and not w.pill_line.isHidden()
    w._mic_clicked()
    assert w.rec_clock.text() == w.pill_rec.text() == "● 0:00 / 0:30" and w.pill_mic.text() == "■ 30"
    assert not w.rec_clock.isHidden() and not w.pill_rec.isHidden() and w.pill_line.isHidden()
    c.t += 7.4
    w._rec_tick()
    assert w.rec_clock.text() == w.pill_rec.text() == "● 0:07 / 0:30"
    assert w.pill_mic.text() == w.mic.text() == "■ 22"
    c.t += 22.6
    w._rec_tick()                                          # 30 с -> автостоп
    assert not w.rec.active and w.rec_clock.isHidden() and w.pill_rec.isHidden() and not w.pill_line.isHidden()
    assert sent and sent[-1][2] and w.pill_mic.text() == "🎙"
    sc.discard(sent[-1][2])
    w.close()
