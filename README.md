# Serial Transfer 2 · 串口文件管理

Qt 桌面程序，采用 FileZilla 类似的双栏布局：左侧本地文件，右侧开发板文件；底部为传输队列、日志和进度。使用系统可缩放中文字体，优先 Noto Sans CJK SC，不再使用 Tk。保留独立的 `serial-transfer-gui` 启动命令和 SHA256 校验；原 `serial-transfer` 命令不会被替换。

## 安装与启动

需要 Linux 桌面、Python 3.10–3.13。Ubuntu 22.04/24.04 示例：

```sh
sudo apt install python3 python3-venv psmisc fonts-noto-cjk libxcb-cursor0 libxkbcommon-x11-0 libegl1 libgl1
bash install.sh
~/.local/bin/serial-transfer-gui
```

`~/.local/bin` 在 PATH 中时直接输入 `serial-transfer-gui`，也可以在应用菜单中点击“串口文件传输”。安装脚本会创建应用专属虚拟环境并下载 PySide6，不修改系统 Python，也不需要用 sudo 运行安装脚本。

默认 Python 版本不兼容时，可指定：

```sh
PYTHON_BIN=/path/to/python3.12 bash install.sh
```

Ubuntu 20.04 默认 Python 3.8 不满足要求，需要另备上述版本的 Python。ARM Linux 需要 PySide6 对应架构的 wheel；应在目标机器安装验证。缺少 Qt 系统库时请按发行版安装对应 XCB/OpenGL 库。安装包在 `compat/linux-x86_64/` 附带 Ubuntu 20.04 的 MIT 许可 libxcb-cursor 兼容库及版权文件，仅在系统缺少此库时加载；其他架构仍使用系统库。

## 浏览板端与传输

1. 用串口终端让板端停在已登录的 root shell（`#`），然后退出 tio/minicom 等占用同一串口的程序。
2. 在程序顶部选择串口与波特率，点击连接，获取板端 `/` 的文件列表。USB ACM 通常可使用“保持当前”。
3. 双击目录进入；路径栏可直接输入绝对路径，支持返回上级和刷新。列表包含隐藏文件，可切换显示。
4. 左侧选择本地文件，点击上传，保存到右侧当前目录；右侧选择普通文件，点击下载，保存到左侧当前目录。可多选文件加入队列，按顺序传输。
5. 左右两栏的“新建文件夹”按钮或右键菜单可在当前目录创建文件夹，支持中文、空格和引号；同名文件夹已存在时会报错。板端需要 `mkdir`。
6. 上传、下载遇到同名文件时会询问是否覆盖，默认选择不覆盖；选择覆盖后，校验通过才替换旧文件。目录不能用文件覆盖；覆盖符号链接时替换链接本身，不修改链接指向的文件。
7. 等待队列显示完成；数据收发结束后仍需解压和 SHA256 校验，只有后端成功退出才算完成。

可以从根目录逐层浏览板端所有**当前用户有权访问**的目录；不会一次递归扫描整个系统。每次进入目录只读取该层，避免遍历 `/proc`、`/sys` 等动态文件系统拖慢串口。界面不提供删除、格式化或执行板端文件的操作。上传/下载只支持普通文件；目录可浏览，暂不支持递归传输整个文件夹。

板端无需安装图形程序或 Python。目录读取使用 shell、`stat`、`base64`，以编码记录传递文件名，支持中文、空格及引号；含换行或回车的目录名称可以浏览，含这些控制字符的文件路径暂不支持传输。传输还需要 `gzip`、`sha256sum`、`wc`；上传需要 `mktemp`、`chmod`、`rm`、支持 `-nT` 的 `mv`。元数据读取失败时会报错，而不会伪装成空目录。

连接按钮用于验证串口并读取列表；串口按每次操作打开/关闭，浏览与传输互斥。不自动登录、不切换用户、不主动结束其他串口进程。

## 分发

```sh
bash build-release.sh
```

把 `dist/serial-transfer-gui-2.1.0.tar.gz` 复制给对方。对方准备上述系统依赖后执行：

```sh
tar -xzf serial-transfer-gui-2.1.0.tar.gz
cd serial-transfer-gui-2.1.0
bash install.sh
serial-transfer-gui
```

安装默认需要联网下载 Python 依赖。如需给同架构、兼容 Python/Linux 环境离线部署，可先在有网机器下载 wheel，再打包：

```sh
python3 -m pip download -r requirements.txt -d wheelhouse
bash build-release.sh
```

安装脚本检测到 `wheelhouse/` 后会从本地安装；系统依赖仍需在目标机准备。源码包不会包含开发用 `.venv/`。

卸载：

```sh
bash ~/.local/share/serial-transfer-gui/uninstall.sh
```

## 开发与验证

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python app.py
QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest discover -s tests -v
```

测试使用本地伪终端和 Qt 虚拟显示，覆盖目录读取、新建文件夹、双向传输、覆盖确认、失败保留旧文件、校验、防覆盖、串口恢复及窗口流程；不代表已在真实开发板完成验证。

## 注意事项

- 未明确确认覆盖时，后端仍拒绝覆盖。远端列表可能因其他程序写入而过期：若传输时才出现同名文件，本次操作会失败，刷新后重新传输并确认即可。命令行需显式传入 `--overwrite` 才允许覆盖。
- 覆盖采用同一文件系统内临时文件校验后替换；传输或校验失败时保留旧文件。替换后的文件使用新文件权限（上传沿用源文件权限，下载为本地临时文件权限），不保留旧文件的权限、属主或扩展属性。
- 权限错误请检查本机设备属组，例如 Ubuntu 常用 `dialout`；修改用户组后需重新登录。
- 文件仍采用 gzip + Base64 串口 Shell 传输，速度受串口和板端处理限制；数据解压在内存中进行，大文件需要足够内存。
- 取消或拔掉 USB 后请查看日志，必要时先用终端确认板端回到 shell。已保存文件可能保留；中断时板端也可能遗留 `.serial-transfer.*` 临时目录。
- 板端普通 `/proc`、`/sys` 条目可能是动态文件，大小可为 0，不能按磁盘文件方式保证完整传输。不要在接收期间修改源文件。
