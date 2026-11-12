"""Interactive serial console with bounded ANSI screen/history and Qt fd polling."""
import os
import termios

import pyte
from PySide6.QtCore import QSocketNotifier, Qt, Signal
from PySide6.QtGui import QFontDatabase, QTextCursor
from PySide6.QtWidgets import QApplication, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton, QVBoxLayout, QWidget

from serial_transfer import Console


class TerminalView(QPlainTextEdit):
    send_bytes = Signal(bytes)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.setStyleSheet('QPlainTextEdit { background: #182231; color: #e2e8f0; padding: 8px; }')

    def keyPressEvent(self, event):
        key, modifiers = event.key(), event.modifiers()
        if modifiers & Qt.KeyboardModifier.ControlModifier:
            if key == Qt.Key.Key_C and (modifiers & Qt.KeyboardModifier.ShiftModifier
                                         or self.textCursor().hasSelection()):
                self.copy()
                return
            if key == Qt.Key.Key_V:
                self.paste()
                return
            if Qt.Key.Key_A <= key <= Qt.Key.Key_Z:
                self.send_bytes.emit(bytes([key - Qt.Key.Key_A + 1]))
                return
        if modifiers & Qt.KeyboardModifier.ShiftModifier and key == Qt.Key.Key_Insert:
            self.paste()
            return
        sequences = {
            Qt.Key.Key_Return: b'\r', Qt.Key.Key_Enter: b'\r', Qt.Key.Key_Backspace: b'\x7f',
            Qt.Key.Key_Tab: b'\t', Qt.Key.Key_Escape: b'\x1b',
            Qt.Key.Key_Up: b'\x1b[A', Qt.Key.Key_Down: b'\x1b[B',
            Qt.Key.Key_Right: b'\x1b[C', Qt.Key.Key_Left: b'\x1b[D',
            Qt.Key.Key_Home: b'\x1b[H', Qt.Key.Key_End: b'\x1b[F',
            Qt.Key.Key_Delete: b'\x1b[3~', Qt.Key.Key_PageUp: b'\x1b[5~', Qt.Key.Key_PageDown: b'\x1b[6~',
        }
        data = sequences.get(key)
        if data is None and event.text():
            data = event.text().encode('utf-8')
            if modifiers & Qt.KeyboardModifier.AltModifier:
                data = b'\x1b' + data
        if data:
            self.send_bytes.emit(data)
        else:
            super().keyPressEvent(event)

    def paste(self):
        self.insertFromMimeData(QApplication.clipboard().mimeData())

    def insertFromMimeData(self, source):
        if source.hasText():
            self.send_bytes.emit(source.text().replace('\r\n', '\n').replace('\n', '\r').encode('utf-8'))

    def focusNextPrevChild(self, next):
        # Tab belongs to the board shell's completion, not Qt focus navigation.
        return False

    def contextMenuEvent(self, event):
        from PySide6.QtWidgets import QMenu
        menu = QMenu(self)
        copy = menu.addAction('复制', self.copy)
        copy.setEnabled(self.textCursor().hasSelection())
        menu.addAction('粘贴到串口', self.paste)
        menu.exec(event.globalPos())


class SerialTerminal(QWidget):
    failed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.console = None
        self.reader = self.writer = None
        self.pending = bytearray()
        self.screen = pyte.HistoryScreen(120, 24, history=2000)
        self.stream = pyte.ByteStream(self.screen)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 0)
        row = QHBoxLayout()
        self.state = QLabel('未连接')
        row.addWidget(self.state, 1)
        interrupt = QPushButton('Ctrl+C')
        interrupt.clicked.connect(lambda: self.write(b'\x03'))
        row.addWidget(interrupt)
        clear = QPushButton('清屏')
        clear.clicked.connect(self.clear_screen)
        row.addWidget(clear)
        layout.addLayout(row)
        self.view = TerminalView()
        self.view.send_bytes.connect(self.write)
        layout.addWidget(self.view)
        hint = QLabel('直接输入命令 · Enter 执行 · Tab 补全 · ↑↓ 历史 · Ctrl+Shift+C 复制 / Ctrl+V 粘贴；文件操作前请先回到 root shell。')
        hint.setWordWrap(True)
        layout.addWidget(hint)

    def open_port(self, port, baud=None):
        if self.console is not None:
            return
        console = Console(port, baud, require_prompt=False)
        try:
            console.__enter__()
            self.console = console
            self.reader = QSocketNotifier(console.fd, QSocketNotifier.Type.Read, self)
            self.reader.activated.connect(self.read)
            self.writer = QSocketNotifier(console.fd, QSocketNotifier.Type.Write, self)
            self.writer.setEnabled(False)
            self.writer.activated.connect(self.flush)
            self.state.setText('终端已连接 · 按 Enter 显示提示符')
        except (OSError, termios.error, RuntimeError, ValueError) as error:
            self.close_port()
            self.state.setText('终端连接失败：' + str(error))
            self.failed.emit(str(error))

    def close_port(self):
        for notifier in (self.reader, self.writer):
            if notifier is not None:
                notifier.setEnabled(False)
                notifier.deleteLater()
        self.reader = self.writer = None
        self.pending.clear()
        if self.console is not None:
            self.console.__exit__(None, None, None)
            self.console = None
        self.state.setText('终端已暂停 / 串口已释放')

    def fail(self, error):
        self.close_port()
        self.state.setText('串口已断开：' + str(error))
        self.failed.emit(str(error))

    def read(self, *args):
        if self.console is None:
            return
        try:
            # Bound each callback so sustained board output cannot freeze Qt.
            for _ in range(16):
                try:
                    data = os.read(self.console.fd, 4096)
                except BlockingIOError:
                    break
                if not data:
                    raise OSError('Serial device disconnected')
                self.stream.feed(data)
            self.render()
        except OSError as error:
            self.fail(error)

    def write(self, data):
        if self.console is None:
            return
        if len(self.pending) + len(data) > 1024 * 1024:
            self.state.setText('发送缓冲区已满，请稍后再试')
            return
        self.pending.extend(data)
        self.flush()

    def flush(self, *args):
        if self.console is None:
            return
        try:
            if self.pending:
                try:
                    written = os.write(self.console.fd, self.pending)
                    del self.pending[:written]
                except BlockingIOError:
                    pass
            self.writer.setEnabled(bool(self.pending))
        except OSError as error:
            self.fail(error)

    def clear_screen(self):
        self.screen.reset()
        self.render()

    def render(self):
        scrollbar = self.view.verticalScrollBar()
        following = scrollbar.value() >= scrollbar.maximum()
        old_scroll = scrollbar.value()
        lines = [''.join(line[x].data for x in range(self.screen.columns)).rstrip()
                 for line in self.screen.history.top]
        lines.extend(self.screen.display)
        minimum = len(self.screen.history.top) + self.screen.cursor.y + 1
        while len(lines) > minimum and not lines[-1].strip():
            lines.pop()
        self.view.setPlainText('\n'.join(lines))
        cursor = self.view.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.Start)
        cursor.movePosition(QTextCursor.MoveOperation.Down,
                            n=len(self.screen.history.top) + self.screen.cursor.y)
        cursor.movePosition(QTextCursor.MoveOperation.Right, n=self.screen.cursor.x)
        self.view.setTextCursor(cursor)
        scrollbar.setValue(scrollbar.maximum() if following else old_scroll)
