#!/usr/bin/env bash
set -euo pipefail
python3 - <<'PY'
from pathlib import Path
import shutil

home = Path.home()
for path in (home / '.local/bin/serial-transfer-gui',
             home / '.local/share/applications/serial-transfer-gui.desktop'):
    path.unlink(missing_ok=True)
app = home / '.local/share/serial-transfer-gui'
if app.is_symlink():
    app.unlink()
elif app.is_dir():
    shutil.rmtree(app)
print('已卸载串口文件传输图形界面。原 serial-transfer 命令及传输的文件均保留。')
PY
