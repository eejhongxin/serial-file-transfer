#!/usr/bin/env python3
"""Transfer files between this computer and a board through its serial shell."""
import argparse
import array
import base64
import errno
import fcntl
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import select
import shlex
import stat
import subprocess
import sys
import tempfile
import termios
import time
import tty


_EVENTS = False


def emit_event(kind, **fields):
    if _EVENTS:
        print(json.dumps(dict(type=kind, **fields)), flush=True)


class Console:
    def __init__(self, port, baud=None):
        self.port = port
        self.baud = baud
        self.fd = None
        self.old = None
        self.exclusive = False

    def open_ready_port(self):
        for attempt in range(3):
            try:
                check = subprocess.run(['fuser', self.port], capture_output=True)
            except FileNotFoundError as error:
                raise RuntimeError('Missing fuser; install psmisc on this computer') from error
            if check.returncode == 0:
                raise RuntimeError('Serial port is in use; exit tio/other receivers first. PIDs: '
                                   + check.stdout.decode().strip())
            try:
                self.fd = os.open(self.port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
                self.old = termios.tcgetattr(self.fd)
                return
            except (OSError, termios.error) as error:
                if self.fd is not None:
                    os.close(self.fd)
                    self.fd = None
                code = error.args[0] if error.args else None
                if code == errno.EIO:
                    if attempt < 2:
                        print(f'Serial port {self.port} is not ready (EIO); retrying '
                              f'({attempt + 1}/2)...', file=sys.stderr, flush=True)
                        time.sleep(.5)
                        continue
                    raise RuntimeError(
                        f'Cannot initialize {self.port}: device returned Input/output error '
                        'after 3 attempts. No transfer started. Check the board USB connection '
                        f'and try: stty -F {shlex.quote(self.port)} -a') from error
                raise

    def __enter__(self):
        self.open_ready_port()
        try:
            fcntl.ioctl(self.fd, termios.TIOCEXCL)
            self.exclusive = True
            tty.setraw(self.fd, termios.TCSANOW)
            attrs = termios.tcgetattr(self.fd)
            attrs[2] |= termios.CLOCAL | termios.CREAD
            attrs[2] &= ~termios.HUPCL
            if self.baud is not None:
                speed = getattr(termios, 'B' + str(self.baud), None)
                if speed is None:
                    raise ValueError(f'Unsupported baud rate: {self.baud}')
                attrs[4] = attrs[5] = speed
            termios.tcsetattr(self.fd, termios.TCSANOW, attrs)
            try:
                fcntl.ioctl(self.fd, termios.TIOCMBIS,
                            array.array('i', [termios.TIOCM_DTR | termios.TIOCM_RTS]))
            except OSError:
                pass
            self.send('\r')
            response = self.read_until_prompt(8)
            if not re.search(rb'#[ \t]*$', response.rstrip(b'\r\n')):
                raise RuntimeError('No root shell prompt. Return to the board shell in tio, '
                                   'then exit tio and retry.')
            return self
        except BaseException:
            self.__exit__(*sys.exc_info())
            raise

    def __exit__(self, *args):
        if self.fd is not None:
            try:
                if self.old is not None:
                    try:
                        termios.tcsetattr(self.fd, termios.TCSANOW, self.old)
                    except (OSError, termios.error) as error:
                        print(f'Could not restore serial settings for {self.port}: {error}',
                              file=sys.stderr)
            finally:
                try:
                    if self.exclusive:
                        try:
                            fcntl.ioctl(self.fd, termios.TIOCNXCL)
                        except OSError:
                            pass
                finally:
                    os.close(self.fd)
                    self.fd = None
                    self.old = None
                    self.exclusive = False

    def send(self, text):
        data = text.encode()
        while data:
            try:
                size = os.write(self.fd, data)
                data = data[size:]
            except BlockingIOError:
                select.select([], [self.fd], [], 1)

    def read_until_prompt(self, timeout):
        data = bytearray()
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if select.select([self.fd], [], [], .2)[0]:
                chunk = os.read(self.fd, 262144)
                if not chunk:
                    raise RuntimeError('Serial device disconnected')
                data.extend(chunk)
                if re.search(rb'#[ \t]*$', data[-400:].rstrip(b'\r\n')):
                    return bytes(data)
        return bytes(data)

    def command(self, command, timeout=600, progress=False):
        token = 'SERIAL_' + secrets.token_hex(12)
        begin, end = token + '_BEGIN', token + '_END'
        self.send("printf '\\n%s\\n' " + shlex.quote(begin) + '; ' + command
                  + "; printf '\\n%s\\n' " + shlex.quote(end) + '\r')
        data = bytearray()
        deadline = time.monotonic() + timeout
        last_report = time.monotonic()
        marker_crlf = b'\n' + end.encode() + b'\r\n'
        marker_lf = b'\n' + end.encode() + b'\n'
        while time.monotonic() < deadline:
            if select.select([self.fd], [], [], .2)[0]:
                chunk = os.read(self.fd, 262144)
                if not chunk:
                    raise RuntimeError('Serial device disconnected')
                data.extend(chunk)
            if progress and time.monotonic() - last_report > 5:
                emit_event('progress', current=len(data), total=None)
                print(f'Received encoded data: {len(data) / 1048576:.1f} MiB', flush=True)
                last_report = time.monotonic()
            if marker_crlf in data[-4096:] or marker_lf in data[-4096:]:
                if progress:
                    emit_event('progress', current=len(data), total=None)
                normalized = bytes(data).replace(b'\r', b'')
                return normalized.split(b'\n' + begin.encode() + b'\n', 1)[1].split(
                    b'\n' + end.encode() + b'\n', 1)[0]
        raise TimeoutError('Serial command timed out; transfer is incomplete')

    def checked_command(self, command, timeout=60):
        """Run one shell command and check its exit status independently of its output."""
        marker = 'SERIAL_RC_' + secrets.token_hex(12)
        output = self.command(
            '( ' + command + ' ); serial_transfer_rc=$?; '
            + "printf '\\n" + marker + ":%s\\n' \"$serial_transfer_rc\"",
            timeout=timeout)
        match = re.search(rb'(?m)^' + marker.encode() + rb':([0-9]+)$', output)
        if match is None:
            raise RuntimeError('Cannot read board command exit status')
        body = output[:match.start()].rstrip(b'\n')
        if int(match[1]) != 0:
            raise RuntimeError('Board command failed: '
                               + body.decode(errors='replace')[-1000:])
        return body


def file_metadata(output):
    match = re.search(rb'(?m)^([0-9a-fA-F]{64})[ \t]+.+$', output)
    sizes = re.findall(rb'(?m)^[ \t]*([0-9]+)[ \t]*$', output)
    if not match or len(sizes) != 1:
        raise RuntimeError('Cannot read board SHA256/size')
    return match[1].decode().lower(), int(sizes[0])


def directory_command(path):
    """Encode both paths and records so serial CR/LF normalization is harmless."""
    encoded = base64.b64encode(os.fsencode(path)).decode('ascii')
    # A suffix protects trailing newlines from shell command substitution.
    # Successful completion has its own record because POSIX sh lacks pipefail.
    producer = (
        "serial_path=$(printf '%s' " + shlex.quote(encoded)
        + " | base64 -d; printf '.'); serial_path=${serial_path%?}; "
        'cd "$serial_path" || exit 1; '
        '[ -r . ] && [ -x . ] || { echo "Cannot read directory" >&2; exit 1; }; '
        "serial_pwd=$(pwd -P; printf '.'); serial_pwd=${serial_pwd%?}; "
        'serial_pwd=${serial_pwd%?}; '
        "printf 'SERIAL_DIR_V1\\000%s\\000' \"$serial_pwd\"; "
        'for serial_entry in ./* ./.[!.]* ./..?*; do '
        '[ -e "$serial_entry" ] || [ -L "$serial_entry" ] || continue; '
        "serial_meta=$(stat -c '%f %s %Y' -- \"$serial_entry\") || exit 1; "
        'serial_dir=0; serial_file=0; '
        '[ ! -d "$serial_entry" ] || serial_dir=1; '
        '[ ! -f "$serial_entry" ] || serial_file=1; '
        "printf '%s\\000%s\\000%s\\000%s\\000' \"${serial_entry#./}\" "
        '"$serial_meta" "$serial_dir" "$serial_file"; '
        "done; printf 'SERIAL_DIR_END\\000'")
    return '( ' + producer + ' ) | base64'


def parse_directory(payload):
    try:
        raw = base64.b64decode(b''.join(payload.split()), validate=True)
        fields = raw.split(b'\0')
        if (len(fields) < 4 or fields[0] != b'SERIAL_DIR_V1'
                or fields[-2:] != [b'SERIAL_DIR_END', b'']
                or (len(fields) - 4) % 4):
            raise ValueError('incomplete directory records')
        path = os.fsdecode(fields[1])
        entries = []
        for offset in range(2, len(fields) - 2, 4):
            name, metadata, is_dir, is_file = fields[offset:offset + 4]
            if not name or b'/' in name or name in (b'.', b'..'):
                raise ValueError('invalid directory entry')
            mode_hex, size, modified = metadata.split()
            mode = int(mode_hex, 16)
            kind = ('symlink' if stat.S_ISLNK(mode) else
                    'directory' if stat.S_ISDIR(mode) else
                    'file' if stat.S_ISREG(mode) else 'other')
            entries.append(dict(name=os.fsdecode(name), kind=kind, size=int(size),
                                mtime=float(modified), mode=f'{stat.S_IMODE(mode):04o}',
                                is_dir=is_dir == b'1', is_file=is_file == b'1'))
        entries.sort(key=lambda entry: (not entry['is_dir'], entry['name'].casefold()))
        return dict(type='listing', path=path, entries=entries)
    except (ValueError, TypeError) as error:
        raise RuntimeError('Cannot read complete board directory listing; '
                           'check path permissions and stat/base64 support') from error


def list_directory(args, parser):
    if not args.path.startswith('/') or '\0' in args.path:
        parser.error('Directory must be an absolute board path')
    emit_event('status', message='正在读取板端目录…')
    with Console(args.port, getattr(args, 'baud', None)) as console:
        console.checked_command(
            'for serial_tool in stat base64; do '
            'command -v "$serial_tool" >/dev/null || { '
            'printf "Missing board tool: %s\\n" "$serial_tool"; exit 1; }; done')
        listing = parse_directory(console.checked_command(directory_command(args.path), timeout=120))
    # Listing is the command result and is emitted even without --events.
    print(json.dumps(listing), flush=True)


def receive_file(args, parser):
    if any(character in args.source for character in '\r\n\0'):
        parser.error('Transfer paths cannot contain CR, LF or NUL characters')
    if os.path.lexists(args.output):
        parser.error('Output already exists; choose another output name')
    if not args.output.parent.is_dir():
        parser.error('Output directory does not exist')
    source = shlex.quote(args.source)
    started = time.monotonic()
    with Console(args.port, getattr(args, 'baud', None)) as console:
        console.checked_command(
            'test -f ' + source + " || { echo 'Source is not a regular file'; exit 1; }")
        info = console.command('sha256sum ' + source + '; wc -c < ' + source, timeout=60)
        expected_hash, expected_size = file_metadata(info)
        print(f'Board: {expected_size} bytes, SHA256 {expected_hash}', flush=True)
        emit_event('status', message='正在接收压缩数据…')
        payload = console.command('gzip -1 -c ' + source + ' | base64', progress=True)
    emit_event('status', message='正在解压并校验 SHA256…')
    compressed = base64.b64decode(b''.join(payload.split()), validate=True)
    raw = gzip.decompress(compressed)
    actual_hash = hashlib.sha256(raw).hexdigest()
    if len(raw) != expected_size or actual_hash != expected_hash:
        raise RuntimeError('Board/host checksum or size mismatch; no output file saved')
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=args.output.parent, prefix='.imu-receive-',
                                         delete=False) as output:
            temporary = Path(output.name)
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        # Publish atomically without overwriting an existing destination.
        os.link(temporary, args.output)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    print(f'Verified and saved: {args.output}')
    print(f'SHA256: {actual_hash}')
    print(f'Elapsed: {time.monotonic() - started:.1f} seconds')


def send_file(args, parser):
    if not args.source.is_file():
        parser.error('Source must be an existing local file')
    # Absolute board paths make source/destination direction unambiguous.
    if not args.output.startswith('/') or args.output.endswith('/'):
        parser.error('Output must be an absolute board file path, e.g. /userdata/test.sh')
    if any(character in args.output for character in '\r\n\0'):
        parser.error('Transfer paths cannot contain CR, LF or NUL characters')
    raw = args.source.read_bytes()
    expected_hash = hashlib.sha256(raw).hexdigest()
    permissions = stat.S_IMODE(args.source.stat().st_mode) & 0o777
    encoded = base64.b64encode(gzip.compress(raw, compresslevel=1, mtime=0)).decode('ascii')
    destination = shlex.quote(args.output)
    parent = shlex.quote(str(Path(args.output).parent))
    started = time.monotonic()
    print(f'Local: {len(raw)} bytes, SHA256 {expected_hash}', flush=True)
    emit_event('status', message='正在连接板端并检查目标…')
    with Console(args.port, getattr(args, 'baud', None)) as console:
        console.checked_command(
            'for serial_tool in mktemp base64 gzip sha256sum wc chmod mv rm; do '
            'command -v "$serial_tool" >/dev/null || exit 1; done')
        move_help = console.checked_command('mv --help 2>&1')
        if b'-T' not in move_help:
            raise RuntimeError('Board mv must support -T (treat the output as a file)')
        move_options = '-nT --no-copy' if b'--no-copy' in move_help else '-nT'
        console.checked_command(
            f'if [ -e {destination} ] || [ -L {destination} ]; then '
            "echo 'Output already exists; choose another board output name'; exit 1; fi; "
            f'test -d {parent}')
        template = shlex.quote(str(Path(args.output).parent / '.serial-transfer.XXXXXX'))
        temporary = console.checked_command('mktemp -d ' + template).decode().strip()
        if not temporary.startswith(str(Path(args.output).parent).rstrip('/')
                                    + '/.serial-transfer.') or '\n' in temporary:
            raise RuntimeError('Unexpected board temporary directory')
        remote_encoded = shlex.quote(temporary + '/payload.b64')
        remote_compressed = shlex.quote(temporary + '/payload.gz')
        remote_file = shlex.quote(temporary + '/data')
        try:
            last_report = time.monotonic()
            last_event = 0.0
            emit_event('status', message='正在发送压缩数据…')
            emit_event('progress', current=0, total=len(encoded))
            # Keep shell lines short and wait for acknowledgment of each chunk.
            # This avoids overrunning the board's serial input/command buffer.
            for offset in range(0, len(encoded), 256):
                chunk = shlex.quote(encoded[offset:offset + 256])
                console.checked_command(f"printf '%s' {chunk} >> {remote_encoded}")
                sent = min(offset + 256, len(encoded))
                if time.monotonic() - last_event > .2 or sent == len(encoded):
                    emit_event('progress', current=sent, total=len(encoded))
                    last_event = time.monotonic()
                if time.monotonic() - last_report > 5:
                    print(f'Sent encoded data: {sent}/{len(encoded)} bytes '
                          f'({100 * sent / len(encoded):.1f}%)', flush=True)
                    last_report = time.monotonic()
            emit_event('status', message='正在板端解压并校验 SHA256…')
            console.checked_command(
                f'base64 -d {remote_encoded} > {remote_compressed} && '
                f'gzip -dc {remote_compressed} > {remote_file}', timeout=600)
            info = console.checked_command(
                f'sha256sum < {remote_file}; wc -c < {remote_file}')
            actual_hash, actual_size = file_metadata(info)
            if actual_size != len(raw) or actual_hash != expected_hash:
                raise RuntimeError('Board/host checksum or size mismatch; output was not published')
            # Rename within the destination filesystem; FAT/FUSE need not support hard links.
            # -T refuses a directory destination. A skipped mv -n can return success,
            # so also require that the staged file was actually moved away.
            console.checked_command(
                f'chmod {permissions:o} {remote_file} && '
                f'mv {move_options} -- {remote_file} {destination} && '
                f'if [ -e {remote_file} ] || [ -L {remote_file} ]; then '
                "echo 'Output already exists or the move was skipped; output was not replaced'; "
                'exit 1; fi')
        finally:
            try:
                console.checked_command('rm -rf -- ' + shlex.quote(temporary))
            except Exception as error:
                print(f'Could not remove board temporary directory {temporary}: {error}',
                      file=sys.stderr)
    print(f'Verified and saved on board: {args.output}')
    print(f'SHA256: {expected_hash}')
    print(f'Elapsed: {time.monotonic() - started:.1f} seconds')


def main():
    global _EVENTS
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='direction', required=True)
    for name, description, action in (
            ('receive', 'Download a board file to this computer', receive_file),
            ('send', 'Upload a local file to the board', send_file)):
        command = commands.add_parser(name, help=description, description=description)
        command.add_argument('--port', default='/dev/ttyACM0', help='Local serial device')
        command.add_argument('--baud', type=int, default=None,
                             help='Baud rate; omit to preserve current port speed')
        command.add_argument('--events', action='store_true',
                             help='Emit JSON status/progress lines alongside text logs')
        command.add_argument('--source', required=True, type=Path if name == 'send' else str,
                             help='Local file' if name == 'send' else 'Board file')
        command.add_argument('--output', required=True, type=str if name == 'send' else Path,
                             help='Board file (absolute path)' if name == 'send' else 'Local file')
        command.set_defaults(action=action, command_parser=command)
    listing = commands.add_parser('list', help='List one board directory without recursion')
    listing.add_argument('--port', default='/dev/ttyACM0', help='Local serial device')
    listing.add_argument('--baud', type=int, default=None,
                         help='Baud rate; omit to preserve current port speed')
    listing.add_argument('--events', action='store_true')
    listing.add_argument('--path', required=True, help='Absolute board directory')
    listing.set_defaults(action=list_directory, command_parser=listing)
    arguments = sys.argv[1:]
    if Path(sys.argv[0]).name == 'serial-receive':
        arguments = ['receive', *arguments]
    args = parser.parse_args(arguments)
    if args.baud is not None and not hasattr(termios, 'B' + str(args.baud)):
        args.command_parser.error('Unsupported baud rate')
    _EVENTS = args.events
    emit_event('status', message='正在准备传输…')
    args.action(args, args.command_parser)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('ERROR: Transfer interrupted', file=sys.stderr)
        sys.exit(130)
    except (OSError, termios.error, RuntimeError, ValueError) as error:
        print(f'ERROR: {error}', file=sys.stderr)
        sys.exit(1)
