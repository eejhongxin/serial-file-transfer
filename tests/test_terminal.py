"""Interactive serial terminal checks over a local PTY, without board access."""
import os
import pty
import select
import subprocess
import termios
import time
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtCore import Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import QApplication
import serial_terminal as terminal


class TerminalTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self):
        self.master, self.slave = pty.openpty()
        self.original = termios.tcgetattr(self.slave)
        self.port = os.ttyname(self.slave)
        self.widget = terminal.SerialTerminal()
        self.fuser = patch('serial_transfer.subprocess.run', return_value=
                           subprocess.CompletedProcess(['fuser'], 1, b'', b''))
        self.fuser.start()

    def tearDown(self):
        self.widget.close_port()
        self.widget.deleteLater()
        self.application.processEvents()
        self.fuser.stop()
        os.close(self.master)
        os.close(self.slave)

    def wait_for(self, predicate):
        deadline = time.monotonic() + 3
        while not predicate() and time.monotonic() < deadline:
            self.application.processEvents()
            time.sleep(.005)
        self.assertTrue(predicate())

    def test_read_ansi_split_utf8_and_keyboard_roundtrip(self):
        self.widget.open_port(self.port, 115200)
        self.assertIsNotNone(self.widget.console)
        # Opening/resuming must not send commands or an unsolicited newline.
        self.assertFalse(select.select([self.master], [], [], .01)[0])
        os.write(self.master, b'old\r\x1b[2Kboard# ' + '中文'.encode()[:2])
        self.application.processEvents()
        os.write(self.master, '中文'.encode()[2:])
        self.wait_for(lambda: 'board# 中文' in self.widget.view.toPlainText())
        self.assertNotIn('old', self.widget.view.toPlainText())
        self.assertNotIn('\x1b', self.widget.view.toPlainText())
        view = self.widget.view
        for key, modifiers, text in (
                (Qt.Key.Key_L, Qt.KeyboardModifier.NoModifier, 'l'),
                (Qt.Key.Key_S, Qt.KeyboardModifier.NoModifier, 's'),
                (Qt.Key.Key_Return, Qt.KeyboardModifier.NoModifier, '\r'),
                (Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier, 'c'),
                (Qt.Key.Key_Up, Qt.KeyboardModifier.NoModifier, ''),
                (Qt.Key.Key_Tab, Qt.KeyboardModifier.NoModifier, '\t')):
            view.keyPressEvent(QKeyEvent(QKeyEvent.Type.KeyPress, key, modifiers, text))
        self.assertTrue(select.select([self.master], [], [], 1)[0])
        received = bytearray()
        def complete():
            if select.select([self.master], [], [], 0)[0]:
                received.extend(os.read(self.master, 1024))
            return len(received) >= len(b'ls\r\x03\x1b[A\t')
        self.wait_for(complete)
        self.assertEqual(received, b'ls\r\x03\x1b[A\t')
        self.widget.close_port()
        self.assertEqual(termios.tcgetattr(self.slave), self.original)

    def test_partial_write_queue_and_paste(self):
        self.widget.open_port(self.port)
        real_write = os.write
        def short_write(fd, data):
            return real_write(fd, data[:2])
        with patch.object(terminal.os, 'write', side_effect=short_write):
            self.widget.write(b'abcdef')
            self.assertTrue(self.widget.pending)
            self.wait_for(lambda: not self.widget.pending)
        self.assertEqual(os.read(self.master, 1024), b'abcdef')
        QApplication.clipboard().setText('echo 中文\nls')
        self.widget.view.paste()
        self.assertEqual(os.read(self.master, 1024), 'echo 中文\rls'.encode())

    def test_disconnect_and_reopen(self):
        failures = []
        self.widget.failed.connect(failures.append)
        self.widget.open_port(self.port)
        with patch.object(terminal.os, 'read', side_effect=OSError('unplugged')):
            self.widget.read()
        self.assertIsNone(self.widget.console)
        self.assertEqual(termios.tcgetattr(self.slave), self.original)
        self.assertEqual(failures, ['unplugged'])
        self.widget.open_port(self.port)
        self.assertIsNotNone(self.widget.console)

    def test_occupied_port_is_not_opened(self):
        with patch('serial_transfer.subprocess.run', return_value=
                   subprocess.CompletedProcess(['fuser'], 0, b'1234', b'')):
            self.widget.open_port(self.port)
        self.assertIsNone(self.widget.console)
        self.assertIn('in use', self.widget.state.text())
        self.assertEqual(termios.tcgetattr(self.slave), self.original)

    def test_history_bounded_and_clear(self):
        self.widget.stream.feed(('line\r\n' * 2100).encode())
        self.widget.render()
        self.assertLessEqual(len(self.widget.screen.history.top), 2000)
        self.widget.clear_screen()
        self.assertEqual(self.widget.view.toPlainText().strip(), '')


if __name__ == '__main__':
    unittest.main()
