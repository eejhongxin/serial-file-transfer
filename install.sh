#!/usr/bin/env bash
set -euo pipefail
source_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
python_bin="${PYTHON_BIN:-python3}"
if ! "$python_bin" -c 'import sys; assert (3, 10) <= sys.version_info < (3, 14)' 2>/dev/null; then
    echo '需要 Python 3.10–3.13。可使用 PYTHON_BIN=/path/to/python3 bash install.sh 指定解释器。' >&2
    exit 1
fi
app_dir="$HOME/.local/share/serial-transfer-gui"
mkdir -p -- "$app_dir"
if [ ! -x "$app_dir/.venv/bin/python" ]; then
    "$python_bin" -m venv "$app_dir/.venv" || {
        echo '无法创建虚拟环境。Ubuntu/Debian 请安装对应版本的 python3-venv。' >&2
        exit 1
    }
fi
if [ -d "$source_dir/wheelhouse" ]; then
    "$app_dir/.venv/bin/python" -m pip install --no-index --find-links "$source_dir/wheelhouse" -r "$source_dir/requirements.txt"
else
    "$app_dir/.venv/bin/python" -m pip install -r "$source_dir/requirements.txt"
fi
"$python_bin" - "$source_dir" <<'PY'
from pathlib import Path
import shlex
import shutil
import sys
source = Path(sys.argv[1])
home = Path.home()
app = home / '.local/share/serial-transfer-gui'
bin_dir = home / '.local/bin'
desktop_dir = home / '.local/share/applications'
files = ('app.py', 'serial_transfer.py', 'README.md', 'requirements.txt', 'uninstall.sh', 'icon.svg')
if any(c in str(home) for c in '\n\r'):
    raise SystemExit('安装路径不能含换行符')
for name in files:
    if not (source / name).is_file():
        raise SystemExit(f'安装包缺少文件: {name}')
for directory in (app, bin_dir, desktop_dir):
    directory.mkdir(parents=True, exist_ok=True)
for name in files:
    if (source / name).resolve() != (app / name).resolve():
        shutil.copy2(source / name, app / name)
if (source / 'compat').is_dir() and (source / 'compat').resolve() != (app / 'compat').resolve():
    shutil.copytree(source / 'compat', app / 'compat', dirs_exist_ok=True)
launcher = bin_dir / 'serial-transfer-gui'
launcher.write_text('#!/bin/sh\nexec ' + shlex.quote(str(app / '.venv/bin/python')) + ' '
                    + shlex.quote(str(app / 'app.py')) + ' "$@"\n')
launcher.chmod(0o755)
python_hex = str(app / '.venv/bin/python').encode().hex()
app_hex = str(app / 'app.py').encode().hex()
entry_code = ("import os; p=bytes.fromhex('" + python_hex + "').decode(); "
              "os.execv(p,[p,bytes.fromhex('" + app_hex + "').decode()])")
(desktop_dir / 'serial-transfer-gui.desktop').write_text(
    '[Desktop Entry]\nType=Application\nName=串口文件传输\n'
    'Name[en]=Serial Transfer\nComment=串口双栏文件管理与 SHA256 校验传输\n'
    f'Exec=python3 -c "{entry_code}"\n'
    f'Icon={str(app / "icon.svg").replace(chr(92), chr(92) * 2)}\n'
    'Terminal=false\nCategories=Utility;FileTransfer;\nStartupNotify=true\n')
print(f'已安装: {app}')
print(f'启动命令: {shlex.quote(str(launcher))}')
print('也可以在应用菜单搜索“串口文件传输”。')
PY
