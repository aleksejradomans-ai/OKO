"""oko_shell.py — окно Ока на Windows (Oko Shell, S2, 10.11.2026). ТЗ: claude/TZ_oko_shell_2026-11-10.md.
Макет: Design «Oko Shell — окно Ока» (панель + полоска).

Запуск (сеанс пользователя, без консоли, один экземпляр):
    <python>\\pythonw.exe <repo>\\src\\oko_shell.py
Окно само ничего не решает: каждый текст уходит в канал desktop (src/shell_channel.py, 127.0.0.1) ->
ТЕ ЖЕ обработчики, что у Telegram (маршрутизатор, мозг, судья, D_GUARD). Логика без GUI — src/shell_client.py.
Тахограф — src/hud_state.py (только чтение: хвост лога, Ollama, служба).
Ctrl+Alt+O — показать/спрятать; Esc — свернуть в полоску; трей — стекло, поверх окон, выход.
Голос (S4) и отмена серии «стоп» — кнопки видны, но выключены честно: функция ещё не сделана.
"""
from __future__ import annotations

import ctypes
import sys
import tempfile
import time
from pathlib import Path

from PyQt6.QtCore import (QAbstractNativeEventFilter, QLockFile, QObject, QPoint, QRunnable, QSettings,
                          QThread, QThreadPool, QTimer, Qt, pyqtSignal)
from PyQt6.QtGui import QAction, QColor, QFont, QFontMetrics, QIcon, QPainter, QPixmap
from PyQt6.QtWidgets import (QApplication, QFrame, QHBoxLayout, QLabel, QLineEdit, QMenu, QPushButton,
                             QScrollArea, QSizePolicy, QSystemTrayIcon, QVBoxLayout, QWidget)

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hud_state as hs          # noqa: E402
import shell_client as sc       # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
IS_WIN = sys.platform == "win32"
ACCENT = "#8FD3FF"
TONE = {"ok": ("rgba(124,217,146,0.14)", "#9FE5B1"), "warn": ("rgba(242,193,78,0.16)", "#F5D27A"),
        "alert": ("rgba(255,107,107,0.16)", "#FFB3B3"), "muted": ("rgba(255,255,255,0.06)", "#A3ACB9")}
UI = "Segoe UI Variable Text, Segoe UI"
MONO = "Cascadia Mono, Consolas"
HOTKEY_ID, WM_HOTKEY = 0x4F53, 0x0312
MOD_ALT, MOD_CONTROL, MOD_NOREPEAT = 0x0001, 0x0002, 0x4000
W_PANEL, H_PANEL, H_PILL = 440, 680, 56

QSS = f"""
QWidget#root {{ background: transparent; }}
QLabel {{ color: #E8EBEF; font-family: {UI}; font-size: 10pt; background: transparent; }}
QLabel#title {{ font-size: 11.5pt; font-weight: 600; }}
QLabel#sub, QLabel#meta, QLabel#tacho {{ color: #A3ACB9; font-family: {MONO}; font-size: 8pt; }}
QFrame#tachoBox {{ background: rgba(255,255,255,0.04); border: 1px solid rgba(255,255,255,0.07); border-radius: 14px; }}
QFrame#mine {{ background: #2B3A4A; border-radius: 16px; border-bottom-right-radius: 4px; }}
QFrame#oko {{ background: rgba(255,255,255,0.07); border-radius: 16px; border-bottom-left-radius: 4px; }}
QFrame#err {{ background: rgba(255,107,107,0.10); border: 1px solid rgba(255,107,107,0.30); border-radius: 16px; }}
QFrame#card {{ background: rgba(143,211,255,0.08); border: 1px solid rgba(143,211,255,0.30); border-radius: 16px; }}
QFrame#pill {{ background: transparent; }}
QScrollArea, QScrollArea > QWidget > QWidget {{ background: transparent; border: none; }}
QScrollBar:vertical {{ background: transparent; width: 6px; }}
QScrollBar::handle:vertical {{ background: rgba(255,255,255,0.16); border-radius: 3px; min-height: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
QLineEdit {{ background: rgba(255,255,255,0.06); border: 1px solid rgba(255,255,255,0.12); border-radius: 14px;
            padding: 0 14px; color: #E8EBEF; font-family: {UI}; font-size: 10.5pt; min-height: 42px; }}
QLineEdit:focus {{ border: 1px solid rgba(143,211,255,0.55); }}
QPushButton {{ font-family: {UI}; font-size: 10pt; color: #DDE3EA; background: rgba(255,255,255,0.06);
              border: 1px solid rgba(255,255,255,0.10); border-radius: 14px; min-height: 42px; min-width: 42px; }}
QPushButton:hover {{ background: rgba(255,255,255,0.11); }}
QPushButton:disabled {{ color: #5C6470; background: rgba(255,255,255,0.03); }}
QPushButton#ghost {{ background: transparent; border: none; color: #A3ACB9; font-size: 13pt; }}
QPushButton#ghost:hover {{ background: rgba(255,255,255,0.08); }}
QPushButton#yes {{ background: {ACCENT}; color: #0B1620; border: none; font-weight: 600; }}
QPushButton#stop {{ background: rgba(255,107,107,0.14); border: none; color: #FF9A9A; }}
"""


# --- Windows: стекло, скругление, горячая клавиша --------------------------------------
class _ACCENT(ctypes.Structure):
    _fields_ = [("AccentState", ctypes.c_int), ("AccentFlags", ctypes.c_int),
                ("GradientColor", ctypes.c_uint), ("AnimationId", ctypes.c_int)]


class _WCAD(ctypes.Structure):
    _fields_ = [("Attribute", ctypes.c_int), ("Data", ctypes.c_void_p), ("SizeOfData", ctypes.c_size_t)]


def win_glass(hwnd: int, on: bool) -> bool:
    """Acrylic за окном (Win10 1803+/Win11) + скруглённые углы Win11. Любой сбой -> False, окно рисует свой фон."""
    if not IS_WIN:
        return False
    try:
        pref = ctypes.c_int(2)  # DWMWCP_ROUND
        ctypes.windll.dwmapi.DwmSetWindowAttribute(ctypes.c_void_p(hwnd), 33, ctypes.byref(pref), ctypes.sizeof(pref))
    except Exception:
        pass
    try:
        acc = _ACCENT(4 if on else 0, 2, 0x991B1512, 0)   # ACRYLICBLURBEHIND, цвет ABGR
        data = _WCAD(19, ctypes.cast(ctypes.pointer(acc), ctypes.c_void_p), ctypes.sizeof(acc))
        return bool(ctypes.windll.user32.SetWindowCompositionAttribute(ctypes.c_void_p(hwnd), ctypes.byref(data))) and on
    except Exception:
        return False


class HotkeyFilter(QAbstractNativeEventFilter):
    def __init__(self, callback):
        super().__init__()
        self.callback = callback

    def nativeEventFilter(self, event_type, message):
        if IS_WIN and event_type == b"windows_generic_MSG":
            from ctypes import wintypes
            msg = wintypes.MSG.from_address(int(message))
            if msg.message == WM_HOTKEY and msg.wParam == HOTKEY_ID:
                self.callback()
                return True, 0
        return False, 0


# --- фоновые задачи -------------------------------------------------------------------
class _AskSignals(QObject):
    done = pyqtSignal(object, str)


class AskJob(QRunnable):
    def __init__(self, conn, text: str):
        super().__init__()
        self.conn, self.text, self.signals = conn, text, _AskSignals()

    def run(self):
        try:
            ans = sc.ask(self.conn, self.text)
        except Exception as e:   # окно не падает из-за канала
            ans = sc.Answer(False, error=f"Сбой окна: {type(e).__name__}")
        self.signals.done.emit(ans, self.text)


class StatusWorker(QObject):
    snapshot = pyqtSignal(object)

    def __init__(self, collector):
        super().__init__()
        self.collector, self.timer = collector, None

    def start(self):
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(1000)
        self.tick()

    def tick(self):
        try:
            self.snapshot.emit(self.collector.tick())
        except Exception as e:
            s = hs.HudSnapshot(now=time.time())
            s.errors.append(f"collector: {type(e).__name__}")
            self.snapshot.emit(s)


# --- виджеты --------------------------------------------------------------------------
def _label(text="", name=None, wrap=False, sel=False) -> QLabel:
    lb = QLabel(text)
    if name:
        lb.setObjectName(name)
    lb.setWordWrap(wrap)
    lb.setTextFormat(Qt.TextFormat.PlainText)
    if sel:
        lb.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return lb


def _dot(color: str, size: int = 10, glow: bool = False) -> QLabel:
    d = QLabel()
    d.setFixedSize(size, size)
    d.setStyleSheet(f"background: {color}; border-radius: {size // 2}px;")
    return d


def _chip(text: str, tone: str) -> QLabel:
    bg, fg = TONE.get(tone, TONE["muted"])
    c = _label("● " + text)
    c.setStyleSheet(f"background: {bg}; color: {fg}; border-radius: 8px; padding: 3px 8px;"
                    f" font-family: {MONO}; font-size: 8pt;")
    return c


class Bubble(QWidget):
    def __init__(self, text: str, kind: str, meta: str = ""):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(3)
        box = QFrame()
        box.setObjectName({"mine": "mine", "oko": "oko", "err": "err"}[kind])
        bl = QVBoxLayout(box)
        bl.setContentsMargins(13, 9, 13, 9)
        self.text = _label(text, wrap=True, sel=True)
        # ширина пузыря по тексту: перенос в Maximum-раскладке иначе режет строки. Шрифт задан у самой метки,
        # чтобы замер (QFontMetrics) и отрисовка шли одним шрифтом.
        f = QFont("Segoe UI")
        f.setPointSizeF(10.5)
        self.text.setFont(f)
        self.text.setStyleSheet("font-family: 'Segoe UI'; font-size: 10.5pt;")
        fm = QFontMetrics(f)
        longest = max((fm.horizontalAdvance(ln) for ln in text.split("\n")), default=0)
        self.text.setFixedWidth(max(24, min(320, longest + 10)))
        self.text.setMinimumHeight(self.text.heightForWidth(self.text.width()))
        bl.addWidget(self.text)
        box.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred)
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        if kind == "mine":
            row.addStretch(1)
            row.addWidget(box)
        else:
            row.addWidget(box)
            row.addStretch(1)
        lay.addLayout(row)
        if meta:
            m = _label(meta, "meta")
            m.setContentsMargins(6, 0, 6, 0)
            if kind == "mine":
                m.setAlignment(Qt.AlignmentFlag.AlignRight)
            lay.addWidget(m)


class Shell(QWidget):
    def __init__(self, settings: QSettings, conn):
        super().__init__()
        self.settings, self.conn = settings, conn
        self.busy = False
        self.glass = settings.value("glass", "1") == "1"
        self.glass_ok = False
        self.compact = False
        self.last_snap = None
        self.confirm_left = 0
        self.hotkey_ok = False
        self.pool = QThreadPool.globalInstance()
        self.setObjectName("root")
        self.setWindowTitle("Око")
        flags = Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool
        if settings.value("on_top", "1") == "1":
            flags |= Qt.WindowType.WindowStaysOnTopHint
        self.setWindowFlags(flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setStyleSheet(QSS)
        self._build()
        pos = settings.value("pos")
        self.move(pos if isinstance(pos, QPoint) else QPoint(80, 80))
        self.resize(W_PANEL, H_PANEL)
        self.confirm_timer = QTimer(self)
        self.confirm_timer.timeout.connect(self._confirm_tick)

    # ---------- сборка ----------
    def _build(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        # панель
        self.panel = QWidget()
        pl = QVBoxLayout(self.panel)
        pl.setContentsMargins(14, 12, 14, 14)
        pl.setSpacing(10)
        head = QHBoxLayout()
        head.setSpacing(10)
        head.addWidget(_dot(ACCENT, 12))
        tbox = QVBoxLayout()
        tbox.setSpacing(0)
        tbox.addWidget(_label("Око", "title"))
        self.sub = _label("канал 127.0.0.1", "sub")
        tbox.addWidget(self.sub)
        head.addLayout(tbox, 1)
        b_min = QPushButton("–")
        b_min.setObjectName("ghost")
        b_min.setToolTip("Свернуть в полоску (Esc)")
        b_min.clicked.connect(lambda: self.set_compact(True))
        b_hide = QPushButton("×")
        b_hide.setObjectName("ghost")
        b_hide.setToolTip("Спрятать в трей (Ctrl+Alt+O — показать)")
        b_hide.clicked.connect(self.hide)
        head.addWidget(b_min)
        head.addWidget(b_hide)
        pl.addLayout(head)

        tacho = QFrame()
        tacho.setObjectName("tachoBox")
        tl = QVBoxLayout(tacho)
        tl.setContentsMargins(12, 9, 12, 9)
        tl.setSpacing(7)
        self.chips_row = QHBoxLayout()
        self.chips_row.setSpacing(6)
        tl.addLayout(self.chips_row)
        self.tacho = _label("выполняется: — · последнее сообщение: —", "tacho", wrap=True)
        tl.addWidget(self.tacho)
        pl.addWidget(tacho)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        feed = QWidget()
        self.feed = QVBoxLayout(feed)
        self.feed.setContentsMargins(2, 4, 6, 4)
        self.feed.setSpacing(10)
        self.feed.addStretch(1)
        self.scroll.setWidget(feed)
        self.scroll.verticalScrollBar().rangeChanged.connect(
            lambda _a, b: self.scroll.verticalScrollBar().setValue(b))
        pl.addWidget(self.scroll, 1)

        self.card = QFrame()
        self.card.setObjectName("card")
        cl = QVBoxLayout(self.card)
        cl.setContentsMargins(12, 11, 12, 11)
        cl.setSpacing(9)
        self.card_text = _label("", wrap=True)
        cl.addWidget(self.card_text)
        crow = QHBoxLayout()
        crow.setSpacing(8)
        self.b_yes = QPushButton("Да")
        self.b_yes.setObjectName("yes")
        self.b_yes.clicked.connect(lambda: self._answer_confirm(True))
        b_no = QPushButton("Нет")
        b_no.clicked.connect(lambda: self._answer_confirm(False))
        crow.addWidget(self.b_yes, 1)
        crow.addWidget(b_no, 1)
        cl.addLayout(crow)
        self.card_meta = _label("", "meta")
        cl.addWidget(self.card_meta)
        self.card.hide()
        pl.addWidget(self.card)

        foot = QHBoxLayout()
        foot.setSpacing(8)
        self.input = QLineEdit()
        self.input.setPlaceholderText("Напиши Оку…")
        self.input.setMaxLength(4000)
        self.input.returnPressed.connect(self._send_input)
        foot.addWidget(self.input, 1)
        mic = QPushButton("🎙")
        mic.setEnabled(False)
        mic.setToolTip("Голос — этап S4, пока выключен")
        stop = QPushButton("■")
        stop.setObjectName("stop")
        stop.setEnabled(False)
        stop.setToolTip("Отмена серии нажатий — ещё не сделана в боте (открытая задача)")
        foot.addWidget(mic)
        foot.addWidget(stop)
        pl.addLayout(foot)
        outer.addWidget(self.panel)

        # полоска
        self.pill = QFrame()
        self.pill.setObjectName("pill")
        pr = QHBoxLayout(self.pill)
        pr.setContentsMargins(16, 6, 8, 6)
        pr.setSpacing(12)
        self.pill_dot = _dot(ACCENT, 10)
        pr.addWidget(self.pill_dot)
        pr.addWidget(_label("Око", "title"))
        self.pill_line = _label("", "tacho")
        pr.addWidget(self.pill_line, 1)
        b_up = QPushButton("▴")
        b_up.setObjectName("ghost")
        b_up.setToolTip("Развернуть")
        b_up.clicked.connect(lambda: self.set_compact(False))
        pr.addWidget(b_up)
        self.pill.hide()
        outer.addWidget(self.pill)
        self._add("Око на связи через этот ПК. Команды и вопросы — как в Telegram.", "oko",
                  "канал desktop · 127.0.0.1")

    # ---------- лента ----------
    def _add(self, text: str, kind: str, meta: str = "") -> Bubble:
        b = Bubble(text, kind, meta)
        self.feed.addWidget(b)
        return b

    def _send(self, text: str, shown: str = None):
        if self.busy or not text.strip():
            return
        self.busy = True
        self.input.setEnabled(False)
        self._add(shown if shown is not None else text, "mine", time.strftime("%H:%M:%S"))
        self.thinking = self._add("…", "oko", "Око обрабатывает")
        job = AskJob(self.conn, text)
        job.signals.done.connect(self._on_answer)
        self.pool.start(job)
        self._render_tacho()

    def _send_input(self):
        t = self.input.text().strip()
        if t:
            self.input.clear()
            self._send(t)

    def _on_answer(self, ans, sent_text):
        self.busy = False
        self.input.setEnabled(True)
        self.input.setFocus()
        if getattr(self, "thinking", None) is not None:
            self.thinking.setParent(None)
            self.thinking.deleteLater()
            self.thinking = None
        stamp = f"{time.strftime('%H:%M:%S')} · {ans.elapsed_s:.1f} с"
        if not ans.ok:
            self._add(ans.error, "err", stamp)
        elif not ans.replies:
            self._add("Ответа нет (обработчик ничего не отправил).", "oko", stamp)
        for r in ans.replies:
            self._add(sc.clean(r), "oko", stamp)
            if sc.needs_confirm(r):
                self._show_confirm(sc.confirm_text(r))
        self._render_tacho()

    # ---------- подтверждение (роль P4) ----------
    def _show_confirm(self, question: str):
        self.card_text.setText(question)
        self.confirm_left = sc.CONFIRM_TTL_S
        self.card_meta.setText(f"ждёт {self.confirm_left} с")
        self.card.show()
        self.confirm_timer.start(1000)

    def _confirm_tick(self):
        self.confirm_left -= 1
        if self.confirm_left <= 0:
            self.confirm_timer.stop()
            self.card.hide()
            self._add("Время на подтверждение вышло, команда не выполнялась.", "oko", time.strftime("%H:%M:%S"))
        else:
            self.card_meta.setText(f"ждёт {self.confirm_left} с")

    def _answer_confirm(self, yes: bool):
        self.confirm_timer.stop()
        self.card.hide()
        self._send("/tv да" if yes else "/tv нет", "да" if yes else "нет")

    # ---------- состояние ----------
    def show_snapshot(self, snap):
        self.last_snap = snap
        while self.chips_row.count():
            it = self.chips_row.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        cs = sc.chips(snap)
        for text, tone in cs:
            self.chips_row.addWidget(_chip(text, tone))
        self.chips_row.addStretch(1)
        self.sub.setText(sc.model_line(snap) + " · 127.0.0.1")
        worst = "alert" if any(t == "alert" for _, t in cs) else ("muted" if cs and cs[0][1] == "muted" else "ok")
        self.pill_dot.setStyleSheet(f"background: {ACCENT if worst == 'ok' else TONE[worst][1]}; border-radius: 5px;")
        self.pill_line.setText(" · ".join(t for t, _ in cs))
        self._render_tacho()

    def _render_tacho(self):
        if self.last_snap is not None:
            self.tacho.setText(sc.tacho_line(self.last_snap, "запрос к Оку" if self.busy else ""))

    # ---------- вид ----------
    def set_compact(self, on: bool):
        self.compact = on
        self.panel.setVisible(not on)
        self.pill.setVisible(on)
        if on:
            self.setFixedSize(W_PANEL + 80, H_PILL)
        else:
            self.setMinimumSize(0, 0)
            self.setMaximumSize(16777215, 16777215)
            self.resize(W_PANEL, H_PANEL)
            self.input.setFocus()
        self.update()

    def apply_glass(self):
        self.glass_ok = win_glass(int(self.winId()), self.glass)
        self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        alpha = 150 if self.glass_ok else 238
        p.setBrush(QColor(18, 21, 27, alpha))
        p.setPen(QColor(255, 255, 255, 26))
        r = H_PILL // 2 if self.compact else 22
        p.drawRoundedRect(self.rect().adjusted(0, 0, -1, -1), r, r)

    def keyPressEvent(self, e):
        if e.key() == Qt.Key.Key_Escape:
            self.set_compact(not self.compact)
        else:
            super().keyPressEvent(e)

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton and self.windowHandle() is not None:
            self.windowHandle().startSystemMove()

    def moveEvent(self, e):
        self.settings.setValue("pos", self.pos())
        super().moveEvent(e)

    def toggle_visible(self):
        if self.isVisible():
            self.hide()
        else:
            self.show()
            self.raise_()
            self.activateWindow()
            if not self.compact:
                self.input.setFocus()

    def register_hotkey(self) -> bool:
        if not IS_WIN:
            return False
        from ctypes import wintypes
        rk = ctypes.windll.user32.RegisterHotKey
        rk.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
        rk.restype = wintypes.BOOL
        self.hotkey_ok = bool(rk(int(self.winId()), HOTKEY_ID, MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, ord("O")))
        return self.hotkey_ok

    def unregister_hotkey(self):
        if IS_WIN and self.hotkey_ok:
            ctypes.windll.user32.UnregisterHotKey(int(self.winId()), HOTKEY_ID)


def tray_icon(color: str) -> QIcon:
    pm = QPixmap(16, 16)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QColor(color))
    p.setPen(QColor(20, 20, 20))
    p.drawEllipse(2, 2, 12, 12)
    p.end()
    return QIcon(pm)


def main() -> int:
    app = QApplication(sys.argv[:1])
    app.setQuitOnLastWindowClosed(False)
    app.setFont(QFont("Segoe UI", 10))
    lock = QLockFile(str(Path(tempfile.gettempdir()) / "oko_shell.lock"))
    if not lock.tryLock(100):
        return 0   # уже запущено
    settings = QSettings("Oko", "Shell")
    shell = Shell(settings, sc.load_conn(REPO_ROOT))
    shell.show()
    shell.apply_glass()
    hk = shell.register_hotkey()
    filt = HotkeyFilter(shell.toggle_visible)
    app.installNativeEventFilter(filt)

    tray = None
    if QSystemTrayIcon.isSystemTrayAvailable():
        menu = QMenu()
        a_show = QAction("Показать / спрятать" + (" (Ctrl+Alt+O)" if hk else ""))
        a_show.triggered.connect(shell.toggle_visible)
        a_glass = QAction("Стекло (размытие)")
        a_glass.setCheckable(True)
        a_glass.setChecked(shell.glass)

        def on_glass(on):
            shell.glass = bool(on)
            settings.setValue("glass", "1" if on else "0")
            shell.apply_glass()
        a_glass.toggled.connect(on_glass)
        a_top = QAction("Поверх окон (после перезапуска)")
        a_top.setCheckable(True)
        a_top.setChecked(settings.value("on_top", "1") == "1")
        a_top.toggled.connect(lambda on: settings.setValue("on_top", "1" if on else "0"))
        a_quit = QAction("Выход")
        a_quit.triggered.connect(app.quit)
        for a in (a_show, a_glass, a_top):
            menu.addAction(a)
        menu.addSeparator()
        menu.addAction(a_quit)
        tray = QSystemTrayIcon(tray_icon(ACCENT))
        tray.setContextMenu(menu)
        tray.setToolTip("Око")
        tray.activated.connect(lambda reason: reason == QSystemTrayIcon.ActivationReason.Trigger and shell.toggle_visible())
        tray.show()

    log_path, ops_path = hs.resolve_paths(REPO_ROOT, None, None)
    collector = hs.HudCollector(log_path, ops_path)
    thread = QThread()
    worker = StatusWorker(collector)
    worker.moveToThread(thread)
    thread.started.connect(worker.start)
    worker.snapshot.connect(shell.show_snapshot)
    thread.start()

    def shutdown():
        shell.unregister_hotkey()
        thread.quit()
        thread.wait(3000)
        lock.unlock()

    app.aboutToQuit.connect(shutdown)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
