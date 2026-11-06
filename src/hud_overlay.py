"""hud_overlay.py — HUD-оверлей Ока (PyQt6), v2 (телеметрия окна/сессии/S, 25.10). ТЗ: claude/TZ_hud_overlay_2026-10-20.md.

Запуск (без консольного окна, один экземпляр):
    <python>\\pythonw.exe <repo>\\src\\hud_overlay.py
    ... [--log ПУТЬ_К_ЛОГУ] [--ops ПУТЬ_К_OPS_DB]
Пути по умолчанию: OKO_LOG_FILE и AI_BASE_DIR из окружения или из .env в корне репозитория.

Окно: без рамки, поверх всех, полупрозрачное, не забирает фокус, перетаскивается мышью
(позиция запоминается). F11 или пункт трея — «пропускать клики» (окно прозрачно для мыши).
Трей обязателен для выхода из режима, если F11 не зарегистрировалась.
Только чтение: лог, ops.db (read-only), Ollama /api/version и /api/ps, sc query OkoBot.
"""
from __future__ import annotations

import argparse
import ctypes
import sys
import tempfile
from pathlib import Path

from PyQt6.QtCore import (QAbstractNativeEventFilter, QLockFile, QObject, QPoint, QSettings,
                          QThread, QTimer, Qt, pyqtSignal)
from PyQt6.QtGui import QAction, QColor, QFont, QIcon, QPainter, QPixmap
from PyQt6.QtWidgets import QApplication, QLabel, QMenu, QSystemTrayIcon, QVBoxLayout, QWidget

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hud_state as hs  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
TONES = {"ok": "#7CD992", "alert": "#FF6B6B", "warn": "#F2C14E", "muted": "#8A93A0", "plain": "#E6E9EE"}
GATE_ICON = {"CLOSED": "#7CD992", "OPEN": "#FF6B6B", "HALF_OPEN": "#F2C14E"}
HOTKEY_ID = 0x4F4B   # "OK"
WM_HOTKEY = 0x0312
MOD_NOREPEAT = 0x4000
VK_F11 = 0x7A
GWL_EXSTYLE = -20
WS_EX_LAYERED, WS_EX_TRANSPARENT, WS_EX_NOACTIVATE = 0x00080000, 0x00000020, 0x08000000
IS_WIN = sys.platform == "win32"
MAX_ROWS = 10   # render(): 5 строк v1 + 3 телеметрии + подвал (+1 запас); лишние молча терялись бы


class Worker(QObject):
    """Опрос в отдельном потоке: таймер 1 с живёт в этом потоке, GUI не блокируется."""
    snapshot = pyqtSignal(object)

    def __init__(self, collector: hs.HudCollector):
        super().__init__()
        self.collector = collector
        self.timer = None

    def start(self):
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(1000)
        self.tick()

    def tick(self):
        try:
            self.snapshot.emit(self.collector.tick())
        except Exception as e:   # HUD не падает из-за источника данных
            s = hs.HudSnapshot(now=0.0)
            s.errors.append(f"collector: {type(e).__name__}")
            self.snapshot.emit(s)


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


class Hud(QWidget):
    def __init__(self, settings: QSettings):
        super().__init__()
        self.settings = settings
        self.click_through = False
        self.hotkey_ok = False
        self.setWindowTitle("Око HUD")
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
                            | Qt.WindowType.Tool | Qt.WindowType.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 8, 12, 8)
        lay.setSpacing(2)
        self.labels = []
        font = QFont("Segoe UI", 9)
        for _ in range(MAX_ROWS):
            lb = QLabel("")
            lb.setFont(font)
            lb.setTextFormat(Qt.TextFormat.PlainText)
            lay.addWidget(lb)
            self.labels.append(lb)
        self.labels[0].setText("Око HUD: запуск…")
        pos = settings.value("pos")
        self.move(pos if isinstance(pos, QPoint) else QPoint(40, 40))

    # --- внешний вид ---
    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setBrush(QColor(18, 20, 24, 200))
        p.setPen(QColor(255, 255, 255, 40) if not self.click_through else QColor(242, 193, 78, 160))
        p.drawRoundedRect(self.rect().adjusted(0, 0, -1, -1), 8, 8)

    def show_snapshot(self, snap: hs.HudSnapshot):
        rows = hs.render(snap)
        for i, lb in enumerate(self.labels):
            if i < len(rows):
                text, tone = rows[i]
                lb.setText(text)
                lb.setStyleSheet(f"color: {TONES.get(tone, TONES['plain'])};")
                lb.show()
            else:
                lb.hide()
        self.adjustSize()

    # --- перетаскивание ---
    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton and not self.click_through:
            h = self.windowHandle()
            if h is not None:
                h.startSystemMove()

    def moveEvent(self, e):
        self.settings.setValue("pos", self.pos())
        super().moveEvent(e)

    # --- Windows: без фокуса, пропуск кликов, F11 ---
    def _hwnd(self):
        return int(self.winId())

    def apply_exstyle(self):
        if not IS_WIN:
            self.setWindowFlag(Qt.WindowType.WindowTransparentForInput, self.click_through)
            self.show()
            return
        from ctypes import wintypes
        u = ctypes.windll.user32
        get = u.GetWindowLongPtrW if hasattr(u, "GetWindowLongPtrW") else u.GetWindowLongW
        setl = u.SetWindowLongPtrW if hasattr(u, "SetWindowLongPtrW") else u.SetWindowLongW
        get.argtypes = [wintypes.HWND, ctypes.c_int]
        get.restype = ctypes.c_ssize_t
        setl.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
        setl.restype = ctypes.c_ssize_t
        style = get(self._hwnd(), GWL_EXSTYLE) | WS_EX_NOACTIVATE | WS_EX_LAYERED
        style = (style | WS_EX_TRANSPARENT) if self.click_through else (style & ~WS_EX_TRANSPARENT)
        setl(self._hwnd(), GWL_EXSTYLE, ctypes.c_ssize_t(style))

    def register_hotkey(self, vk: int) -> bool:
        if not IS_WIN:
            return False
        from ctypes import wintypes
        rk = ctypes.windll.user32.RegisterHotKey
        rk.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
        rk.restype = wintypes.BOOL
        self.hotkey_ok = bool(rk(self._hwnd(), HOTKEY_ID, MOD_NOREPEAT, vk))
        return self.hotkey_ok

    def unregister_hotkey(self):
        if IS_WIN and self.hotkey_ok:
            from ctypes import wintypes
            uk = ctypes.windll.user32.UnregisterHotKey
            uk.argtypes = [wintypes.HWND, ctypes.c_int]
            uk(self._hwnd(), HOTKEY_ID)

    def set_click_through(self, on: bool, can_escape: bool) -> bool:
        if on and not can_escape:   # без трея и без F11 выйти из режима будет нельзя
            return False
        self.click_through = on
        self.apply_exstyle()
        self.update()
        return True


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


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Око HUD")
    ap.add_argument("--log")
    ap.add_argument("--ops")
    a = ap.parse_args(argv)

    app = QApplication(sys.argv[:1])
    app.setQuitOnLastWindowClosed(False)
    lock = QLockFile(str(Path(tempfile.gettempdir()) / "oko_hud.lock"))
    if not lock.tryLock(100):
        return 0   # уже запущен

    settings = QSettings("Oko", "HUD")
    log_path, ops_path = hs.resolve_paths(REPO_ROOT, a.log, a.ops)
    hud = Hud(settings)
    hud.show()
    hud.apply_exstyle()

    vk = int(settings.value("hotkey_vk", VK_F11))
    hotkey_ok = hud.register_hotkey(vk)
    has_tray = QSystemTrayIcon.isSystemTrayAvailable()

    tray = None
    act_ct = QAction("Пропускать клики (F11)" if hotkey_ok else "Пропускать клики")
    act_ct.setCheckable(True)

    def toggle(on=None):
        want = (not hud.click_through) if on is None else bool(on)
        if not hud.set_click_through(want, can_escape=has_tray or hotkey_ok):
            want = False
        act_ct.setChecked(hud.click_through)

    if has_tray:
        menu = QMenu()
        act_ct.toggled.connect(lambda on: on != hud.click_through and toggle(on))
        menu.addAction(act_ct)
        act_show = QAction("Показать / скрыть")
        act_show.triggered.connect(lambda: hud.setVisible(not hud.isVisible()))
        menu.addAction(act_show)
        menu.addSeparator()
        act_quit = QAction("Выход")
        act_quit.triggered.connect(app.quit)
        menu.addAction(act_quit)
        tray = QSystemTrayIcon(tray_icon(GATE_ICON["CLOSED"]))
        tray.setContextMenu(menu)
        tray.setToolTip("Око HUD")
        tray.show()

    filt = HotkeyFilter(lambda: toggle())
    app.installNativeEventFilter(filt)

    collector = hs.HudCollector(log_path, ops_path)
    thread = QThread()
    worker = Worker(collector)
    worker.moveToThread(thread)
    thread.started.connect(worker.start)
    footer_extra = [] if hotkey_ok else ["F11 занята или недоступна — пропуск кликов только из трея"]

    def on_snap(snap):
        snap.errors.extend(footer_extra)
        hud.show_snapshot(snap)
        if tray is not None:
            tray.setIcon(tray_icon(GATE_ICON.get(snap.gate, "#8A93A0")))
            tray.setToolTip("Око HUD — связь: " + snap.gate)

    worker.snapshot.connect(on_snap)
    thread.start()

    def shutdown():
        hud.unregister_hotkey()
        thread.quit()
        thread.wait(3000)
        lock.unlock()

    app.aboutToQuit.connect(shutdown)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
