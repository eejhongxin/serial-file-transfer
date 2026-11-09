"""Qt frontend integration checks; never opens a physical serial port."""
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtCore import QProcess, QSettings, Qt
from PySide6.QtWidgets import QApplication

spec = importlib.util.spec_from_file_location('frontend', Path(__file__).resolve().parents[1] / 'app.py')
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)


def child(script):
    return [sys.executable, '-u', '-c', script]


class CommandTest(unittest.TestCase):
    def test_direction_and_quoted_unicode_arguments(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "中文 ' $ file.bin"
            source.write_bytes(b'contents')
            remote = "/userdata/中文 ' $ file.bin"
            command = app.transfer_command('send', '/dev/ttyACM0', '115200', str(source), remote)
            self.assertEqual(command[command.index('--source') + 1], str(source))
            self.assertEqual(command[command.index('--output') + 1], remote)
            self.assertEqual(command[command.index('--baud') + 1], '115200')
            local = str(Path(directory) / 'new.bin')
            command = app.transfer_command('receive', '/dev/ttyACM0', '保持当前', local, remote)
            self.assertEqual(command[command.index('--source') + 1], remote)
            self.assertEqual(command[command.index('--output') + 1], local)
            self.assertNotIn('--baud', command)

    def test_receive_refuses_existing_and_dangling_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'existing'
            target.write_bytes(b'keep')
            dangling = Path(directory) / 'dangling'
            dangling.symlink_to(Path(directory) / 'missing')
            for path in (target, dangling):
                with self.assertRaisesRegex(ValueError, '已存在'):
                    app.transfer_command('receive', '/dev/ttyACM0', '保持当前', str(path), '/userdata/file')
            self.assertEqual(target.read_bytes(), b'keep')


class WindowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])
        app.select_font(cls.application)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        QSettings.setDefaultFormat(QSettings.Format.IniFormat)
        QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, self.temp.name)
        QSettings('SerialTransfer', 'Desktop').clear()
        self.window = app.Application()
        self.window.settings.clear()
        self.window.navigate_local(self.temp.name)
        self.window.port_box.setEditText('/dev/serial-transfer-no-such-device')
        self.application.processEvents()

    def tearDown(self):
        if self.window.process is not None:
            self.window.process.kill()
            self.window.process.waitForFinished(2000)
        self.window.close()
        self.window.deleteLater()
        self.application.processEvents()
        self.temp.cleanup()

    def wait_for(self, predicate, timeout=5):
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            self.application.processEvents()
            time.sleep(.01)
        self.application.processEvents()
        self.assertTrue(predicate(), 'Qt operation did not reach expected state')

    def wait_finished(self):
        self.wait_for(lambda: self.window.process is None and not self.window.jobs)

    @staticmethod
    def listing():
        return dict(type='listing', path='/userdata/中文', entries=[
            dict(name='子目录', kind='directory', size=4096, mtime=0, mode='755'),
            dict(name='测试.bin', kind='file', size=42, mtime=0, mode='644'),
            dict(name='.hidden', kind='file', size=1, mtime=0, mode='600'),
            dict(name='link', kind='symlink', is_dir=True, size=4, mtime=0, mode='777'),
            dict(name='ttyS0', kind='other', size=0, mtime=0, mode='660')])

    def test_listing_connects_and_hidden_filter_preserves_metadata(self):
        payload = json.dumps(self.listing(), ensure_ascii=False)
        with patch.object(app, 'backend_command', return_value=child('print(' + repr(payload) + ')')):
            self.window.connect_board()
        self.wait_finished()
        self.assertTrue(self.window.connected)
        self.assertEqual(self.window.remote_dir, '/userdata/中文')
        self.assertEqual(self.window.remote_tree.topLevelItemCount(), 5)
        self.window.hidden_action.setChecked(False)
        self.window.toggle_hidden()
        self.assertEqual(self.window.remote_tree.topLevelItemCount(), 4)
        self.assertEqual(len(self.window.remote_entries), 5)
        directory = self.window.remote_tree.topLevelItem(0)
        with patch.object(self.window, 'request_listing') as request:
            self.window.remote_double_click(directory, 0)
        request.assert_called_once_with('/userdata/中文/' + directory.text(0))

    def test_download_enqueues_regular_file_and_skips_devices_links_and_directories(self):
        self.window.set_listing(self.listing())
        self.window.connected = True
        for number in range(self.window.remote_tree.topLevelItemCount()):
            self.window.remote_tree.topLevelItem(number).setSelected(True)
        with patch.object(self.window, 'start_next'):
            self.window.download_selected()
        self.assertEqual(len(self.window.jobs), 2)
        regular = next(job for job in self.window.jobs if job['remote'].endswith('测试.bin'))
        self.assertEqual(regular['local'], str(Path(self.temp.name) / '测试.bin'))
        self.assertEqual(regular['remote'], '/userdata/中文/测试.bin')
        self.assertIn('已跳过', self.window.log.toPlainText())
        self.window.jobs.clear()

    def test_zero_exit_without_listing_does_not_connect(self):
        with patch.object(app, 'backend_command', return_value=child('print("no listing")')):
            self.window.connect_board()
        self.wait_finished()
        self.assertFalse(self.window.connected)
        self.assertIn('失败', self.window.status_label.text())

    def test_real_backend_missing_device_failure_is_visible(self):
        self.window.connect_board()
        self.wait_finished()
        self.assertFalse(self.window.connected)
        self.assertIn('失败', self.window.status_label.text())
        self.assertIn('ERROR:', self.window.log.toPlainText())

    def test_progress_100_percent_is_not_success_until_process_exits(self):
        target = str(Path(self.temp.name) / 'new.bin')
        script = ('import time; print(\'{"type":"progress","current":100,"total":100}\',flush=True);'
                  'time.sleep(.3); print("SHA256: simulated-test-only")')
        self.window.enqueue('receive', target, '/userdata/new.bin', 100)
        with patch.object(app, 'transfer_command', return_value=child(script)):
            self.window.start_next()
        self.wait_for(lambda: self.window.progress.maximum() == 1000)
        self.assertNotIn('校验通过', self.window.queue_table.item(0, 4).text())
        self.assertIsNotNone(self.window.process)
        self.wait_finished()
        self.assertIn('校验通过', self.window.queue_table.item(0, 4).text())

    def test_failed_transfer_does_not_report_success(self):
        self.window.enqueue('receive', str(Path(self.temp.name) / 'new.bin'), '/userdata/new.bin', 10)
        with patch.object(app, 'transfer_command', return_value=child('import sys; print("ERROR: mismatch"); sys.exit(1)')):
            self.window.start_next()
        self.wait_finished()
        self.assertIn('失败', self.window.queue_table.item(0, 4).text())
        self.assertNotIn('校验通过', self.window.status_label.text())

    def test_cancel_stops_current_child_and_pending_queue(self):
        for name in ('one.bin', 'two.bin'):
            self.window.enqueue('receive', str(Path(self.temp.name) / name), '/userdata/' + name, 1)
        with patch.object(app, 'transfer_command', return_value=child('import time; time.sleep(30)')):
            self.window.start_next()
        self.wait_for(lambda: self.window.process is not None and self.window.process.processId() > 0)
        self.window.cancel()
        self.wait_finished()
        self.assertEqual(self.window.queue_table.item(0, 4).text(), '已取消')
        self.assertEqual(self.window.queue_table.item(1, 4).text(), '已取消')
        self.assertFalse(self.window.cancel_button.isEnabled())

    def test_cancel_requested_before_child_started_is_delivered(self):
        self.window.enqueue('receive', str(Path(self.temp.name) / 'new.bin'), '/userdata/new.bin', 1)
        with patch.object(app, 'transfer_command', return_value=child('import time; time.sleep(30)')):
            self.window.start_next()
        self.window.cancel()
        self.wait_finished()
        self.assertEqual(self.window.queue_table.item(0, 4).text(), '已取消')

    def test_backend_failed_to_start_releases_busy_state(self):
        self.window.launch(['/serial-transfer-no-such-executable'], {'action': 'list', 'path': '/'})
        self.wait_finished()
        self.assertFalse(self.window.connected)
        self.assertIn('失败', self.window.status_label.text())
        self.assertFalse(self.window.cancel_button.isEnabled())

    def test_unicode_split_between_pipe_chunks_is_not_corrupted(self):
        script = ('import os,time; data="中文路径\\n".encode(); '
                  'os.write(1,data[:1]); time.sleep(.2); os.write(1,data[1:])')
        self.window.launch(child(script), {'action': 'list', 'path': '/'})
        self.wait_finished()
        self.assertIn('中文路径', self.window.log.toPlainText())
        self.assertNotIn('\ufffd', self.window.log.toPlainText())


if __name__ == '__main__':
    unittest.main()
