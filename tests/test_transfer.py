"""Exercise transfers against a local PTY shell; never opens physical serial ports."""
import argparse
import contextlib
import errno
import importlib.util
import io
import json
import os
from pathlib import Path
import pty
import select
import subprocess
import sys
import tempfile
import termios
import threading
import time
import unittest
from unittest.mock import patch

MODULE = Path(__file__).resolve().parents[1] / 'serial_transfer.py'
spec = importlib.util.spec_from_file_location('transfer_backend', MODULE)
backend = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backend)
REAL_RUN = subprocess.run


class Shell:
    """Execute protocol command lines locally, with a simulated board prompt."""
    def __init__(self, before_command=None):
        self.master, slave = pty.openpty()
        self.port = os.ttyname(slave)
        self.original = termios.tcgetattr(slave)
        os.close(slave)
        self.done = threading.Event()
        self.before_command = before_command
        self.commands = []
        self.thread = threading.Thread(target=self.serve, daemon=True)

    def __enter__(self):
        def run(args, **kwargs):
            if args[0] == 'fuser':
                return subprocess.CompletedProcess(args, 1, b'', b'')
            return REAL_RUN(args, **kwargs)
        self.mock = patch.object(backend.subprocess, 'run', side_effect=run)
        self.mock.start()
        self.thread.start()
        return self

    def serve(self):
        pending = b''
        while not self.done.is_set():
            if not select.select([self.master], [], [], .02)[0]:
                continue
            try:
                chunk = os.read(self.master, 65536)
            except OSError as error:
                if error.errno == errno.EIO:
                    time.sleep(.01)
                    continue
                raise
            pending += chunk
            while b'\r' in pending:
                command, pending = pending.split(b'\r', 1)
                if command:
                    text = command.decode()
                    self.commands.append(text)
                    if self.before_command:
                        self.before_command(text)
                    result = REAL_RUN(['/bin/sh', '-c', text], capture_output=True)
                    os.write(self.master, result.stdout + result.stderr)
                os.write(self.master, b'\nboard# ')

    def __exit__(self, *args):
        self.done.set()
        self.thread.join(2)
        self.mock.stop()
        os.close(self.master)

    def attributes(self):
        fd = os.open(self.port, os.O_RDWR | os.O_NOCTTY)
        try:
            return termios.tcgetattr(fd)
        finally:
            os.close(fd)


class TransferTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.parser = argparse.ArgumentParser()
        self.log = io.StringIO()
        self.redirect = contextlib.redirect_stdout(self.log)
        self.redirect.__enter__()

    def tearDown(self):
        self.redirect.__exit__(None, None, None)
        self.temp.cleanup()

    def args(self, shell, source, output):
        return argparse.Namespace(port=shell.port, baud=115200,
                                  source=source, output=output)

    def test_receive_binary_and_quoted_path(self):
        source = self.root / "board ' file.bin"
        source.write_bytes(bytes(range(256)) * 80)
        target = self.root / 'local result.bin'
        with Shell() as shell:
            backend.receive_file(self.args(shell, str(source), target), self.parser)
            self.assertEqual(shell.attributes(), shell.original)
        self.assertEqual(source.read_bytes(), target.read_bytes())
        self.assertIn('SHA256:', self.log.getvalue())
        self.assertEqual(list(self.root.glob('.imu-receive-*')), [])

    def test_send_binary_and_permission(self):
        source = self.root / 'local.bin'
        source.write_bytes(os.urandom(2048))
        source.chmod(0o751)
        target = self.root / "board ' output.bin"
        with Shell() as shell:
            backend.send_file(self.args(shell, source, str(target)), self.parser)
            self.assertEqual(shell.attributes(), shell.original)
        self.assertEqual(target.read_bytes(), source.read_bytes())
        self.assertEqual(target.stat().st_mode & 0o777, 0o751)
        self.assertEqual(list(self.root.glob('.serial-transfer.*')), [])

    def test_receive_refuses_existing_or_dangling_symlink(self):
        target = self.root / 'keep.bin'
        target.write_bytes(b'keep')
        for path in [target, self.root / 'dangling']:
            if path != target:
                path.symlink_to(self.root / 'missing')
            with self.assertRaises(SystemExit):
                backend.receive_file(argparse.Namespace(source='/board/file', output=path), self.parser)
        self.assertEqual(target.read_bytes(), b'keep')

    def test_send_refuses_existing(self):
        source, target = self.root / 'source', self.root / 'target'
        source.write_bytes(b'new')
        target.write_bytes(b'keep')
        with Shell() as shell:
            with self.assertRaisesRegex(RuntimeError, 'Output already exists'):
                backend.send_file(self.args(shell, source, str(target)), self.parser)
            self.assertEqual(shell.attributes(), shell.original)
        self.assertEqual(target.read_bytes(), b'keep')

    def test_changed_source_not_published(self):
        source, target = self.root / 'source', self.root / 'target'
        source.write_bytes(b'original')
        def mutate(command):
            if '; gzip -1 -c ' in command:
                source.write_bytes(b'changed!')
        with Shell(mutate) as shell:
            with self.assertRaisesRegex(RuntimeError, 'checksum or size mismatch'):
                backend.receive_file(self.args(shell, str(source), target), self.parser)
            self.assertEqual(shell.attributes(), shell.original)
        self.assertFalse(target.exists())

    def test_send_checksum_failure_cleans_staging(self):
        source, target = self.root / 'source', self.root / 'target'
        source.write_bytes(b'original')
        def corrupt(command):
            if 'sha256sum < ' in command:
                for staged in self.root.glob('.serial-transfer.*/data'):
                    staged.write_bytes(b'corrupt')
        with Shell(corrupt) as shell:
            with self.assertRaisesRegex(RuntimeError, 'checksum or size mismatch'):
                backend.send_file(self.args(shell, source, str(target)), self.parser)
        self.assertFalse(target.exists())
        self.assertEqual(list(self.root.glob('.serial-transfer.*')), [])

    def test_send_destination_created_during_transfer_is_preserved(self):
        source, target = self.root / 'source', self.root / 'target'
        source.write_bytes(b'new')
        def race(command):
            if 'mv -nT' in command:
                target.write_bytes(b'created by someone else')
        with Shell(race) as shell:
            with self.assertRaisesRegex(RuntimeError, 'move was skipped'):
                backend.send_file(self.args(shell, source, str(target)), self.parser)
        self.assertEqual(target.read_bytes(), b'created by someone else')
        self.assertEqual(list(self.root.glob('.serial-transfer.*')), [])

    def test_keyboard_interrupt_restores_console(self):
        with Shell() as shell:
            with self.assertRaises(KeyboardInterrupt):
                with backend.Console(shell.port, 115200):
                    raise KeyboardInterrupt()
            self.assertEqual(shell.attributes(), shell.original)

    def test_events_are_json_lines(self):
        import json
        with patch.object(backend, '_EVENTS', True):
            backend.emit_event('progress', current=42, total=None)
        self.assertEqual(json.loads(self.log.getvalue()),
                         dict(type='progress', current=42, total=None))

    def listing(self, shell, directory):
        self.log.seek(0)
        self.log.truncate()
        backend.list_directory(argparse.Namespace(port=shell.port, baud=115200,
                                                   path=str(directory)), self.parser)
        return json.loads(self.log.getvalue().splitlines()[-1])

    def test_listing_unicode_controls_hidden_and_metadata(self):
        names = ['中文 文件.txt', "quote ' file", 'line\nreturn\rend', '.hidden', '..dot']
        for name in names:
            (self.root / name).write_bytes(b'123')
        (self.root / 'folder').mkdir()
        (self.root / 'folder' / 'not-recursive').touch()
        os.mkfifo(self.root / 'fifo')
        with Shell() as shell:
            listing = self.listing(shell, self.root)
            self.assertEqual(shell.attributes(), shell.original)
        self.assertEqual(listing['path'], str(self.root))
        entries = {entry['name']: entry for entry in listing['entries']}
        self.assertEqual(set(entries), set(names + ['folder', 'fifo']))
        for name in names:
            self.assertEqual(entries[name]['size'], 3)
            self.assertEqual(entries[name]['kind'], 'file')
            self.assertTrue(entries[name]['is_file'])
        self.assertEqual(entries['fifo']['kind'], 'other')
        self.assertFalse(entries['fifo']['is_file'])
        self.assertTrue(entries['folder']['is_dir'])

    def test_listing_path_newline_cr_quote_and_resolved_symlink(self):
        target = self.root / "中文 '\n\r\n"
        target.mkdir()
        (target / 'data').touch()
        link = self.root / 'link'
        link.symlink_to(target)
        with Shell() as shell:
            listing = self.listing(shell, link)
        self.assertEqual(listing['path'], str(target))
        self.assertEqual(listing['entries'][0]['name'], 'data')

    def test_listing_symlinks_and_empty_directory(self):
        folder = self.root / 'dir'
        folder.mkdir()
        (self.root / 'file').write_text('data')
        (self.root / 'dir-link').symlink_to(folder)
        (self.root / 'file-link').symlink_to(self.root / 'file')
        (self.root / 'broken-link').symlink_to(self.root / 'absent')
        with Shell() as shell:
            self.assertEqual(self.listing(shell, folder)['entries'], [])
            entries = {e['name']: e for e in self.listing(shell, self.root)['entries']}
        self.assertEqual(entries['dir-link']['kind'], 'symlink')
        self.assertTrue(entries['dir-link']['is_dir'])
        self.assertTrue(entries['file-link']['is_file'])
        self.assertFalse(entries['broken-link']['is_file'])
        self.assertFalse(entries['broken-link']['is_dir'])

    def test_listing_missing_or_unreadable_path_is_error(self):
        locked = self.root / 'locked'
        locked.mkdir()
        locked.chmod(0o111)
        try:
            paths = [self.root / 'absent']
            if os.getuid() != 0:
                paths.append(locked)
            with Shell() as shell:
                for path in paths:
                    with self.assertRaisesRegex(RuntimeError, 'directory listing'):
                        self.listing(shell, path)
                self.assertEqual(shell.attributes(), shell.original)
        finally:
            locked.chmod(0o700)

    def test_listing_cli_subprocess(self):
        (self.root / '中文 data').write_text('hello')
        with Shell() as shell:
            process = REAL_RUN([sys.executable, str(MODULE), 'list', '--port', shell.port,
                                '--events', '--path', str(self.root)], capture_output=True,
                               text=True, timeout=10)
        self.assertEqual(process.returncode, 0, process.stderr + process.stdout)
        events = [json.loads(line) for line in process.stdout.splitlines()]
        listing = next(event for event in events if event['type'] == 'listing')
        self.assertEqual(listing['entries'][0]['name'], '中文 data')

    def test_receive_rejects_special_file_before_reading(self):
        source = self.root / 'fifo'
        os.mkfifo(source)
        with Shell() as shell:
            with self.assertRaisesRegex(RuntimeError, 'not a regular file'):
                backend.receive_file(self.args(shell, str(source), self.root / 'out'), self.parser)
            self.assertFalse(any('sha256sum' in line for line in shell.commands))

    def test_transfer_rejects_remote_line_controls_before_opening_port(self):
        local = self.root / 'source'
        local.write_bytes(b'data')
        with patch.object(backend, 'Console') as console:
            for control in ('\r', '\n', '\0'):
                with self.assertRaises(SystemExit):
                    backend.receive_file(argparse.Namespace(source='/bad' + control,
                                                            output=self.root / 'out'), self.parser)
                with self.assertRaises(SystemExit):
                    backend.send_file(argparse.Namespace(source=local,
                                                         output='/bad' + control), self.parser)
            console.assert_not_called()


if __name__ == '__main__':
    unittest.main()
