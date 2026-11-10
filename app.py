#!/usr/bin/env python3
"""Qt desktop file manager for serial-shell file transfer."""
import codecs
import ctypes
import glob
import json
import os
from pathlib import Path, PurePosixPath
import platform
import signal
import stat
import sys
import time

from PySide6.QtCore import QDir, QProcess, QSettings, Qt, QTimer
from PySide6.QtGui import QAction, QFont, QFontDatabase, QIcon
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFileDialog, QFileIconProvider,
    QFileSystemModel, QFrame, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QInputDialog, QMainWindow, QMenu, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton,
    QSizePolicy, QSplitter, QStyle, QTabWidget, QTableWidget, QTableWidgetItem,
    QTreeView, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

ROOT = Path(__file__).resolve().parent


def serial_ports():
    return sorted(set(p for pattern in ('/dev/serial/by-id/*', '/dev/ttyACM*',
                                       '/dev/ttyUSB*', '/dev/ttyS*')
                      for p in glob.glob(pattern)))


def backend_command(action, port, baud):
    if not port.startswith('/dev/'):
        raise ValueError('请选择 /dev/ 开头的串口设备。')
    command = [sys.executable, '-u', str(ROOT / 'serial_transfer.py'), action,
               '--events', '--port', port]
    if baud != '保持当前':
        if not baud.isdigit() or int(baud) <= 0:
            raise ValueError('波特率必须是正整数。')
        command += ['--baud', baud]
    return command


def transfer_command(direction, port, baud, local, remote, overwrite=False):
    if direction not in ('send', 'receive'):
        raise ValueError('请选择传输方向。')
    if not remote.startswith('/') or remote.endswith('/') or any(c in remote for c in '\r\n\x00'):
        raise ValueError('板端文件必须是完整的绝对路径。')
    if not local:
        raise ValueError('请选择本地文件。')
    path = Path(local).expanduser().absolute()
    if direction == 'send' and not path.is_file():
        raise ValueError('要上传的本地文件不存在。')
    if direction == 'receive':
        if os.path.lexists(path) and not overwrite:
            raise ValueError('本地目标已存在，请更换文件名。')
        if path.is_dir():
            raise ValueError('目标是文件夹，不能用文件覆盖。')
        if not path.parent.is_dir():
            raise ValueError('本地保存目录不存在。')
    source, output = (str(path), remote) if direction == 'send' else (remote, str(path))
    return backend_command(direction, port, baud) + ['--source', source, '--output', output] + (['--overwrite'] if overwrite else [])


def human_size(size):
    size = float(size)
    for suffix in ('B', 'KiB', 'MiB', 'GiB', 'TiB'):
        if size < 1024 or suffix == 'TiB':
            return f'{int(size):,} B' if suffix == 'B' else f'{size:.1f} {suffix}'
        size /= 1024


def display_time(timestamp):
    try:
        return time.strftime('%Y-%m-%d %H:%M', time.localtime(timestamp))
    except (ValueError, OverflowError, OSError, TypeError):
        return ''


def display_name(value):
    """Keep control characters visible without changing filesystem paths."""
    return ''.join({'\n': r'\n', '\r': r'\r', '\t': r'\t'}.get(c, f'\\x{ord(c):02x}' if ord(c) < 32 or ord(c) == 127 else c)
                   for c in value)


def select_font(application):
    for pattern in ('fonts/*.ttc', 'fonts/*.ttf', 'fonts/*.otf'):
        for filename in ROOT.glob(pattern):
            QFontDatabase.addApplicationFont(str(filename))
    families = set(QFontDatabase.families())
    family = next((name for name in ('Noto Sans CJK SC', 'Source Han Sans SC',
                                     'Microsoft YaHei', 'WenQuanYi Micro Hei',
                                     'Droid Sans Fallback', 'DejaVu Sans') if name in families), 'sans-serif')
    font = QFont(family, 10)
    font.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
    application.setFont(font)
    return family


class LocalFileModel(QFileSystemModel):
    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return ['文件名', '大小', '类型', '修改时间'][section] if 0 <= section < 4 else ''
        return super().headerData(section, orientation, role)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if index.isValid():
            info = self.fileInfo(index)
            if role == Qt.ItemDataRole.DecorationRole and index.column() == 0:
                icon = QStyle.StandardPixmap.SP_DirIcon if info.isDir() else QStyle.StandardPixmap.SP_FileIcon
                return QApplication.style().standardIcon(icon)
            if role == Qt.ItemDataRole.DisplayRole:
                if index.column() == 0:
                    return display_name(info.fileName())
                if index.column() == 1:
                    return '' if info.isDir() else human_size(info.size())
                if index.column() == 2:
                    return '目录链接' if info.isSymLink() and info.isDir() else ('符号链接' if info.isSymLink() else ('文件夹' if info.isDir() else '文件'))
                if index.column() == 3:
                    return info.lastModified().toString('yyyy-MM-dd HH:mm')
        return super().data(index, role)


class Application(QMainWindow):
    def __init__(self):
        super().__init__()
        self.settings = QSettings('SerialTransfer', 'Desktop')
        self.connected = False
        self.remote_dir = '/'
        self.remote_entries = []
        self.local_dir = str(Path.home())
        self.process = None
        self.current_job = None
        self.jobs = []
        self.stdout_buffer = ''
        self.output_decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
        self.cancel_requested = False
        self.refresh_after_queue = False
        self.listing_received = False
        self.started = 0.0
        self.setWindowTitle('Serial Transfer — 串口文件管理')
        self.setWindowIcon(QIcon(str(ROOT / 'icon.svg')))
        self.resize(1220, 850)
        self.setMinimumSize(900, 650)
        self.setStyleSheet('''
            QMainWindow, QWidget { background: #f5f6f8; color: #243247; }
            QMenuBar, QMenu { background: #fff; }
            QMenuBar::item:selected, QMenu::item:selected { background: #e7effc; }
            QLineEdit, QComboBox { background: white; border: 1px solid #cad2dd;
                border-radius: 4px; padding: 5px 7px; min-height: 20px; }
            QLineEdit:focus, QComboBox:focus { border-color: #3a7add; }
            QPushButton { background: #fff; border: 1px solid #cad2dd; border-radius: 4px;
                padding: 6px 12px; min-height: 20px; }
            QPushButton:hover { background: #edf3fc; border-color: #94b6e5; }
            QPushButton:pressed { background: #dce9fa; }
            QPushButton:disabled { color: #96a0af; background: #f1f3f6; }
            QPushButton#primary { background: #2869c8; border-color: #2869c8; color: white; }
            QPushButton#primary:hover { background: #205bb0; }
            QPushButton#primary:disabled { background: #a6bad8; border-color: #a6bad8; }
            QTreeView, QTreeWidget, QTableWidget { background: #fff; alternate-background-color: #f8fafc;
                border: 1px solid #d5dce5; selection-background-color: #e3edfc; selection-color: #173c72;
                outline: 0; }
            QTreeView::item, QTreeWidget::item { min-height: 28px; }
            QHeaderView::section { background: #f0f3f7; padding: 7px 8px;
                border: 0; border-bottom: 1px solid #d5dce5; border-right: 1px solid #e0e5ec; }
            QPlainTextEdit { background: white; border: 1px solid #d5dce5; padding: 7px; }
            QTabWidget::pane { border: 0; }
            QTabBar::tab { background: #e9edf3; padding: 7px 18px; border: 1px solid #d5dce5; }
            QTabBar::tab:selected { background: white; border-bottom-color: white; }
            QSplitter::handle { background: #e0e5ec; }
            QProgressBar { border: 1px solid #d1d9e4; border-radius: 3px;
                background: #e9edf4; text-align: center; min-height: 16px; }
            QProgressBar::chunk { background: #3d80df; }
            QStatusBar { background: #eef1f6; border-top: 1px solid #d5dce5; }
        ''')
        self.build_menu()
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(10)
        layout.addLayout(self.build_connection_bar())
        self.connection_hint = QLabel('先让板端停在 root shell（#），退出 tio / minicom，然后连接串口。')
        self.connection_hint.setStyleSheet('color: #64748b; padding: 2px 0 4px 0;')
        layout.addWidget(self.connection_hint)
        self.vertical_split = QSplitter(Qt.Orientation.Vertical)
        self.file_split = QSplitter(Qt.Orientation.Horizontal)
        self.file_split.addWidget(self.build_local_panel())
        self.file_split.addWidget(self.build_remote_panel())
        self.file_split.setSizes([600, 600])
        self.file_split.setChildrenCollapsible(False)
        self.vertical_split.addWidget(self.file_split)
        self.tabs = QTabWidget()
        self.queue_table = QTableWidget(0, 5)
        self.queue_table.setHorizontalHeaderLabels(['方向', '文件', '目标目录', '大小', '状态'])
        self.queue_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.queue_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.queue_table.setAlternatingRowColors(True)
        self.queue_table.verticalHeader().hide()
        self.queue_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.queue_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.queue_table.setColumnWidth(0, 78)
        self.queue_table.setColumnWidth(3, 100)
        self.queue_table.setColumnWidth(4, 140)
        self.tabs.addTab(self.queue_table, '传输队列')
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.document().setMaximumBlockCount(3000)
        self.tabs.addTab(self.log, '连接与传输日志')
        self.vertical_split.addWidget(self.tabs)
        self.vertical_split.setSizes([520, 210])
        layout.addWidget(self.vertical_split, 1)
        bottom = QHBoxLayout()
        self.status_label = QLabel('未连接')
        bottom.addWidget(self.status_label, 1)
        self.progress = QProgressBar()
        self.progress.setFixedWidth(240)
        self.progress.setValue(0)
        bottom.addWidget(self.progress)
        self.cancel_button = QPushButton('取消当前操作')
        self.cancel_button.clicked.connect(self.cancel)
        self.cancel_button.setEnabled(False)
        bottom.addWidget(self.cancel_button)
        layout.addLayout(bottom)
        self.statusBar().showMessage('SHA256 校验 · 覆盖前确认 · 按需读取板端目录')
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.update_elapsed)
        self.timer.start(1000)
        saved_local = self.settings.value('local_dir', str(Path.home()))
        self.navigate_local(saved_local if Path(saved_local).is_dir() else str(Path.home()))
        self.refresh_ports()
        self.update_controls()

    def build_menu(self):
        menu = self.menuBar().addMenu('文件')
        save = menu.addAction('保存日志…')
        save.triggered.connect(self.save_log)
        menu.addSeparator()
        menu.addAction('退出', self.close)
        view = self.menuBar().addMenu('查看')
        self.hidden_action = QAction('显示隐藏文件', self, checkable=True, checked=True)
        self.hidden_action.triggered.connect(self.toggle_hidden)
        view.addAction(self.hidden_action)
        view.addAction('刷新本地目录', lambda: self.navigate_local(self.local_dir))
        view.addAction('刷新板端目录', lambda: self.request_listing(self.remote_dir))
        help_menu = self.menuBar().addMenu('帮助')
        help_menu.addAction('使用说明', lambda: QMessageBox.information(self, '串口文件管理',
            '1. 板端登录 root，并退出其他串口终端。\n2. 选择串口，点击连接。\n'
            '3. 左边是本地文件，右边是板端文件；双击目录进入。\n'
            '4. 选择普通文件，点击上传或下载。支持多选文件排队传输。\n\n'
            '板端目录按需读取，包含隐藏文件；不会递归扫描整个根目录。\n'
            '暂不递归传输文件夹；同名目标会拒绝覆盖。'))

    def build_connection_bar(self):
        bar = QHBoxLayout()
        label = QLabel('串口文件管理')
        label.setStyleSheet('font-weight: 600; font-size: 16px; padding-right: 14px;')
        bar.addWidget(label)
        bar.addWidget(QLabel('串口'))
        self.port_box = QComboBox()
        self.port_box.setEditable(True)
        self.port_box.setMinimumWidth(225)
        self.port_box.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.port_box.setEditText(self.settings.value('port', '/dev/ttyACM0'))
        bar.addWidget(self.port_box, 1)
        refresh = QPushButton('刷新')
        refresh.clicked.connect(self.refresh_ports)
        bar.addWidget(refresh)
        bar.addSpacing(10)
        bar.addWidget(QLabel('波特率'))
        self.baud_box = QComboBox()
        self.baud_box.addItems(['保持当前', '9600', '57600', '115200', '460800', '921600', '1500000'])
        self.baud_box.setCurrentText(self.settings.value('baud', '保持当前'))
        bar.addWidget(self.baud_box)
        self.connect_button = QPushButton('连接')
        self.connect_button.setObjectName('primary')
        self.connect_button.clicked.connect(self.connect_board)
        bar.addWidget(self.connect_button)
        self.disconnect_button = QPushButton('断开')
        self.disconnect_button.clicked.connect(self.disconnect_board)
        bar.addWidget(self.disconnect_button)
        return bar

    def panel_header(self, title, action, action_slot):
        row = QHBoxLayout()
        label = QLabel(title)
        label.setStyleSheet('font-weight: 600; font-size: 14px; padding: 3px 0;')
        row.addWidget(label)
        row.addStretch()
        button = QPushButton(action)
        button.clicked.connect(action_slot)
        row.addWidget(button)
        return row, button

    def build_local_panel(self):
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 5, 0)
        row, self.upload_button = self.panel_header('本地站点', '上传 →', self.upload_selected)
        mkdir_button = QPushButton('新建文件夹')
        mkdir_button.clicked.connect(self.create_local_directory)
        row.insertWidget(2, mkdir_button)
        layout.addLayout(row)
        navigation = QHBoxLayout()
        up = QPushButton('上一级')
        up.clicked.connect(lambda: self.navigate_local(str(Path(self.local_dir).parent)))
        navigation.addWidget(up)
        self.local_path = QLineEdit()
        self.local_path.returnPressed.connect(lambda: self.navigate_local(self.local_path.text()))
        navigation.addWidget(self.local_path, 1)
        refresh = QPushButton('刷新')
        refresh.clicked.connect(lambda: self.navigate_local(self.local_path.text()))
        navigation.addWidget(refresh)
        layout.addLayout(navigation)
        self.local_model = LocalFileModel(self)
        self.local_model.setReadOnly(True)
        self.local_model.setFilter(QDir.Filter.AllEntries | QDir.Filter.NoDotAndDotDot | QDir.Filter.Hidden)
        self.local_model.setRootPath('/')
        self.local_tree = QTreeView()
        self.local_tree.setModel(self.local_model)
        self.local_tree.setRootIsDecorated(False)
        self.local_tree.setItemsExpandable(False)
        self.local_tree.setAlternatingRowColors(True)
        self.local_tree.setSelectionMode(QTreeView.SelectionMode.ExtendedSelection)
        self.local_tree.setSelectionBehavior(QTreeView.SelectionBehavior.SelectRows)
        self.local_tree.setSortingEnabled(True)
        self.local_tree.sortByColumn(0, Qt.SortOrder.AscendingOrder)
        self.local_tree.setEditTriggers(QTreeView.EditTrigger.NoEditTriggers)
        self.local_tree.doubleClicked.connect(self.local_double_click)
        self.local_tree.setColumnWidth(0, 245)
        self.local_tree.setColumnWidth(1, 100)
        self.local_tree.setColumnWidth(2, 85)
        self.local_tree.setColumnWidth(3, 145)
        self.local_model.setHeaderData(0, Qt.Orientation.Horizontal, '文件名')
        self.local_model.setHeaderData(1, Qt.Orientation.Horizontal, '大小')
        self.local_model.setHeaderData(2, Qt.Orientation.Horizontal, '类型')
        self.local_model.setHeaderData(3, Qt.Orientation.Horizontal, '修改时间')
        layout.addWidget(self.local_tree, 1)
        self.local_info = QLabel('双击目录进入；选择文件后上传。')
        self.local_info.setStyleSheet('color: #64748b; padding-top: 3px;')
        layout.addWidget(self.local_info)
        self.local_tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.local_tree.customContextMenuRequested.connect(self.local_context_menu)
        return panel

    def build_remote_panel(self):
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(5, 0, 0, 0)
        row, self.download_button = self.panel_header('远程站点 · 开发板', '← 下载', self.download_selected)
        self.remote_mkdir_button = QPushButton('新建文件夹')
        self.remote_mkdir_button.clicked.connect(self.create_remote_directory)
        row.insertWidget(2, self.remote_mkdir_button)
        layout.addLayout(row)
        navigation = QHBoxLayout()
        self.remote_up = QPushButton('上一级')
        self.remote_up.clicked.connect(lambda: self.request_listing(str(PurePosixPath(self.remote_dir).parent)))
        navigation.addWidget(self.remote_up)
        self.remote_path = QLineEdit('/')
        self.remote_path.returnPressed.connect(lambda: self.request_listing(self.remote_path.text()))
        navigation.addWidget(self.remote_path, 1)
        self.remote_refresh = QPushButton('刷新')
        self.remote_refresh.clicked.connect(lambda: self.request_listing(self.remote_path.text()))
        navigation.addWidget(self.remote_refresh)
        layout.addLayout(navigation)
        self.remote_tree = QTreeWidget()
        self.remote_tree.setColumnCount(4)
        self.remote_tree.setHeaderLabels(['文件名', '大小', '类型', '修改时间'])
        self.remote_tree.setRootIsDecorated(False)
        self.remote_tree.setAlternatingRowColors(True)
        self.remote_tree.setSelectionMode(QTreeWidget.SelectionMode.ExtendedSelection)
        self.remote_tree.setSelectionBehavior(QTreeWidget.SelectionBehavior.SelectRows)
        self.remote_tree.setEditTriggers(QTreeWidget.EditTrigger.NoEditTriggers)
        self.remote_tree.setColumnWidth(0, 245)
        self.remote_tree.setColumnWidth(1, 95)
        self.remote_tree.setColumnWidth(2, 85)
        self.remote_tree.setColumnWidth(3, 145)
        self.remote_tree.itemDoubleClicked.connect(self.remote_double_click)
        self.remote_tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.remote_tree.customContextMenuRequested.connect(self.remote_context_menu)
        layout.addWidget(self.remote_tree, 1)
        self.remote_info = QLabel('尚未连接。连接后可从 / 浏览整个板端文件系统。')
        self.remote_info.setStyleSheet('color: #64748b; padding-top: 3px;')
        layout.addWidget(self.remote_info)
        return panel

    def local_context_menu(self, position):
        menu = QMenu(self)
        action = menu.addAction('上传所选文件 →')
        action.setEnabled(self.connected and self.process is None)
        action.triggered.connect(self.upload_selected)
        menu.addAction('新建文件夹…', self.create_local_directory)
        menu.exec(self.local_tree.viewport().mapToGlobal(position))

    def remote_context_menu(self, position):
        menu = QMenu(self)
        action = menu.addAction('← 下载所选文件')
        action.setEnabled(self.connected and self.process is None)
        action.triggered.connect(self.download_selected)
        mkdir = menu.addAction('新建文件夹…', self.create_remote_directory)
        mkdir.setEnabled(self.connected and self.process is None)
        menu.exec(self.remote_tree.viewport().mapToGlobal(position))

    def directory_name(self):
        name, accepted = QInputDialog.getText(self, '新建文件夹', '文件夹名称：')
        if not accepted:
            return None
        if not name or name in ('.', '..') or ('/' in name or any(ord(c) < 32 or ord(c) == 127 for c in name)):
            QMessageBox.warning(self, '名称无效', '请输入单个文件夹名称，不能包含 /、换行或控制字符。')
            return None
        return name

    def create_local_directory(self):
        name = self.directory_name()
        if name is None:
            return
        try:
            (Path(self.local_dir) / name).mkdir()
        except OSError as error:
            QMessageBox.warning(self, '创建失败', str(error))
            return
        self.navigate_local(self.local_dir)
        self.status_label.setText('本地文件夹已创建：' + name)

    def create_remote_directory(self):
        if not self.connected or self.process is not None:
            return
        name = self.directory_name()
        if name is None:
            return
        path = str(PurePosixPath(self.remote_dir) / name)
        try:
            command = backend_command('mkdir', self.port_box.currentText().strip(), self.baud_box.currentText())
        except ValueError as error:
            QMessageBox.warning(self, '创建失败', str(error))
            return
        self.launch(command + ['--path', path], {'action': 'mkdir', 'path': path})

    def append_log(self, message):
        self.log.appendPlainText(time.strftime('%H:%M:%S  ') + message.rstrip())

    def refresh_ports(self):
        current = self.port_box.currentText()
        self.port_box.clear()
        self.port_box.addItems(serial_ports())
        self.port_box.setEditText(current)

    def navigate_local(self, path):
        candidate = Path(path).expanduser().absolute()
        if not candidate.is_dir():
            QMessageBox.warning(self, '无法打开目录', f'本地目录不存在或不可访问：\n{candidate}')
            self.local_path.setText(self.local_dir)
            return
        self.local_dir = str(candidate)
        self.local_path.setText(self.local_dir)
        self.local_tree.setRootIndex(self.local_model.index(self.local_dir))
        self.settings.setValue('local_dir', self.local_dir)

    def local_double_click(self, index):
        path = self.local_model.filePath(index)
        if self.local_model.isDir(index):
            self.navigate_local(path)
        elif self.connected:
            self.upload_selected()

    def connect_board(self):
        if self.process is not None:
            return
        self.request_listing('/', connecting=True)

    def disconnect_board(self):
        if self.process is not None:
            return
        self.connected = False
        self.remote_entries = []
        self.remote_tree.clear()
        self.remote_info.setText('已断开。')
        self.connection_hint.setText('串口已释放。可在其他串口终端中继续操作板子。')
        self.status_label.setText('未连接')
        self.update_controls()

    def request_listing(self, path, connecting=False):
        if self.process is not None or (not self.connected and not connecting):
            return
        if not path.startswith('/') or '\x00' in path:
            QMessageBox.warning(self, '路径无效', '请输入板端绝对目录路径，例如 / 或 /userdata。')
            return
        try:
            command = backend_command('list', self.port_box.currentText().strip(), self.baud_box.currentText())
        except ValueError as error:
            QMessageBox.warning(self, '无法连接', str(error))
            return
        self.listing_received = False
        self.launch(command + ['--path', path], {'action': 'list', 'path': path})

    def set_listing(self, event):
        self.remote_dir = event['path']
        self.remote_path.setText(self.remote_dir)
        self.remote_entries = event['entries']
        self.render_remote()
        self.listing_received = True

    def render_remote(self):
        self.remote_tree.clear()
        folders = 0
        files = 0
        hidden = self.hidden_action.isChecked()
        entries = sorted(self.remote_entries, key=lambda e: (not self.is_directory(e), e['name'].casefold()))
        for entry in entries:
            if not hidden and entry['name'].startswith('.'):
                continue
            kind = entry['kind']
            directory = self.is_directory(entry)
            label = {'directory': '文件夹', 'file': '文件', 'symlink': '符号链接', 'other': '特殊文件'}.get(kind, kind)
            if kind == 'symlink' and directory:
                label = '目录链接'
            item = QTreeWidgetItem([display_name(entry['name']), '' if directory else human_size(entry.get('size', 0)),
                                    label, display_time(entry.get('mtime'))])
            item.setData(0, Qt.ItemDataRole.UserRole, entry)
            item.setIcon(0, self.style().standardIcon(QStyle.StandardPixmap.SP_DirIcon if directory else QStyle.StandardPixmap.SP_FileIcon))
            item.setToolTip(0, str(PurePosixPath(self.remote_dir) / entry['name']))
            item.setToolTip(2, entry.get('mode', label))
            item.setTextAlignment(1, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self.remote_tree.addTopLevelItem(item)
            folders += int(directory)
            files += int(not directory)
        self.remote_info.setText(f'{folders} 个目录，{files} 个文件 / 链接 · 双击目录进入')

    @staticmethod
    def is_directory(entry):
        return entry['kind'] == 'directory' or entry.get('is_dir', False)

    def toggle_hidden(self):
        filters = QDir.Filter.AllEntries | QDir.Filter.NoDotAndDotDot
        if self.hidden_action.isChecked():
            filters |= QDir.Filter.Hidden
        self.local_model.setFilter(filters)
        self.render_remote()

    def remote_double_click(self, item, column):
        entry = item.data(0, Qt.ItemDataRole.UserRole)
        if self.is_directory(entry):
            self.request_listing(str(PurePosixPath(self.remote_dir) / entry['name']))
        else:
            self.download_selected()

    def upload_selected(self):
        if not self.connected:
            return
        indexes = self.local_tree.selectionModel().selectedRows(0)
        paths = [Path(self.local_model.filePath(index)) for index in indexes]
        skipped = []
        for path in paths:
            try:
                regular = stat.S_ISREG(path.stat().st_mode)
            except OSError:
                regular = False
            if not regular:
                skipped.append(path.name)
                continue
            self.enqueue('send', str(path), str(PurePosixPath(self.remote_dir) / path.name), path.stat().st_size)
        if skipped:
            self.append_log('已跳过目录或特殊文件：' + '、'.join(skipped))
        if not paths:
            self.status_label.setText('请先在左侧选择要上传的文件。')
        self.start_next()

    def download_selected(self):
        if not self.connected:
            return
        items = self.remote_tree.selectedItems()
        skipped = []
        for item in items:
            entry = item.data(0, Qt.ItemDataRole.UserRole)
            if entry['kind'] != 'file':
                skipped.append(entry['name'])
                continue
            self.enqueue('receive', str(Path(self.local_dir) / entry['name']),
                         str(PurePosixPath(self.remote_dir) / entry['name']), entry.get('size', 0))
        if skipped:
            self.append_log('已跳过目录、符号链接或特殊文件：' + '、'.join(skipped))
        if not items:
            self.status_label.setText('请先在右侧选择要下载的普通文件。')
        self.start_next()

    def enqueue(self, action, local, remote, size):
        row = self.queue_table.rowCount()
        self.queue_table.insertRow(row)
        values = ['上传 →' if action == 'send' else '← 下载', Path(local).name,
                  str(PurePosixPath(remote).parent) if action == 'send' else str(Path(local).parent),
                  human_size(size), '等待中']
        for column, text in enumerate(values):
            item = QTableWidgetItem(text)
            item.setToolTip(text)
            self.queue_table.setItem(row, column, item)
        self.jobs.append(dict(action=action, local=local, remote=remote, row=row))
        self.tabs.setCurrentIndex(0)

    def start_next(self):
        if self.process is not None:
            return
        if not self.jobs:
            if self.refresh_after_queue and self.connected:
                self.refresh_after_queue = False
                self.request_listing(self.remote_dir)
            return
        job = self.jobs.pop(0)
        if job['action'] == 'receive':
            exists = os.path.lexists(job['local'])
            directory = Path(job['local']).is_dir()
            target = job['local']
        else:
            entry = next((e for e in self.remote_entries
                          if str(PurePosixPath(self.remote_dir) / e['name']) == job['remote']), None)
            exists = entry is not None
            directory = exists and self.is_directory(entry)
            target = job['remote']
        overwrite = False
        if exists:
            if directory:
                self.queue_table.item(job['row'], 4).setText('已跳过：目标是文件夹')
                self.append_log('不能用文件覆盖文件夹：' + target)
                QTimer.singleShot(0, self.start_next)
                return
            answer = QMessageBox.question(self, '覆盖同名文件？',
                '目标文件已存在：\n' + display_name(target) +
                '\n\n是否覆盖？传输并校验成功后才会替换原文件。符号链接将替换链接本身。',
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                self.queue_table.item(job['row'], 4).setText('已跳过：保留原文件')
                QTimer.singleShot(0, self.start_next)
                return
            overwrite = True
        try:
            command = transfer_command(job['action'], self.port_box.currentText().strip(),
                                       self.baud_box.currentText(), job['local'], job['remote'], overwrite=overwrite)
        except ValueError as error:
            self.queue_table.item(job['row'], 4).setText('失败：目标已存在' if '已存在' in str(error) else '无法开始')
            self.append_log(str(error))
            QTimer.singleShot(0, self.start_next)
            return
        self.queue_table.item(job['row'], 4).setText('传输中')
        self.launch(command, job)

    def launch(self, command, job):
        self.current_job = job
        self.stdout_buffer = ''
        self.output_decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
        self.cancel_requested = False
        self.started = time.monotonic()
        self.progress.setRange(0, 0)
        self.process = QProcess(self)
        self.process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        self.process.readyReadStandardOutput.connect(self.read_output)
        self.process.started.connect(self.process_started)
        self.process.finished.connect(self.operation_finished)
        self.process.errorOccurred.connect(self.process_error)
        self.append_log((('读取板端目录：' if job['action'] == 'list' else '新建板端文件夹：') + job['path']) if job['action'] in ('list', 'mkdir') else
                        ('上传：' if job['action'] == 'send' else '下载：') + job['remote'])
        self.status_label.setText('正在读取目录…' if job['action'] == 'list' else ('正在创建文件夹…' if job['action'] == 'mkdir' else '正在传输…'))
        self.update_controls()
        self.process.start(command[0], command[1:])

    def read_output(self):
        if self.process is None:
            return
        self.stdout_buffer += self.output_decoder.decode(bytes(self.process.readAllStandardOutput()))
        while '\n' in self.stdout_buffer:
            line, self.stdout_buffer = self.stdout_buffer.split('\n', 1)
            self.handle_line(line)

    def handle_line(self, line):
        if not line.strip():
            return
        try:
            event = json.loads(line)
        except ValueError:
            event = None
        if isinstance(event, dict) and event.get('type') == 'listing':
            self.set_listing(event)
        elif isinstance(event, dict) and event.get('type') == 'progress':
            current, total = event.get('current', 0), event.get('total')
            if total:
                self.progress.setRange(0, 1000)
                self.progress.setValue(int(current * 1000 / total))
                self.progress.setFormat(f'{current * 100 / total:.0f}% · 等待校验')
            else:
                self.status_label.setText(f'已接收编码数据 {human_size(current)}')
        elif isinstance(event, dict) and event.get('type') == 'status':
            message = event.get('message', '')
            self.status_label.setText(message)
            self.append_log(message)
        else:
            self.append_log(line)

    def process_error(self, error):
        if error == QProcess.ProcessError.FailedToStart and self.process is not None:
            self.append_log('无法启动后端：' + self.process.errorString())
            self.operation_finished(1, QProcess.ExitStatus.NormalExit)

    def operation_finished(self, code, exit_status=None):
        if self.process is None:
            return
        self.read_output()
        self.stdout_buffer += self.output_decoder.decode(b'', final=True)
        if self.stdout_buffer:
            self.handle_line(self.stdout_buffer)
            self.stdout_buffer = ''
        job = self.current_job
        process = self.process
        self.process = None
        self.current_job = None
        process.deleteLater()
        success = code == 0 and (job['action'] != 'list' or self.listing_received)
        self.progress.setRange(0, 100)
        self.progress.setValue(100 if success else 0)
        self.progress.setFormat('%p%')
        if job['action'] == 'list':
            if success:
                self.connected = True
                self.settings.setValue('port', self.port_box.currentText())
                self.settings.setValue('baud', self.baud_box.currentText())
                self.connection_hint.setText('已连接 · 每次目录读取和文件传输完成后释放串口；请勿同时打开其他串口终端。')
                self.status_label.setText('目录读取完成')
            else:
                self.remote_path.setText(self.remote_dir)
                self.status_label.setText('操作已取消' if self.cancel_requested else '目录读取失败，请查看日志')
                self.tabs.setCurrentIndex(1)
        elif job['action'] == 'mkdir':
            self.status_label.setText('文件夹创建成功' if success else ('操作已取消' if self.cancel_requested else '创建失败，请查看日志'))
            if success:
                self.refresh_after_queue = True
            else:
                self.tabs.setCurrentIndex(1)
        else:
            text = '完成 · 校验通过' if success else ('已取消' if self.cancel_requested else '失败 · 查看日志')
            self.queue_table.item(job['row'], 4).setText(text)
            self.status_label.setText(text)
            if success and job['action'] == 'send':
                self.refresh_after_queue = True
            if success and job['action'] == 'receive':
                self.navigate_local(self.local_dir)
            if not success:
                self.append_log(f'文件操作退出码：{code}；请查看以上日志确认结果。')
        self.update_controls()
        QTimer.singleShot(0, self.start_next)

    def update_controls(self):
        busy = self.process is not None
        self.port_box.setEnabled(not busy and not self.connected)
        self.baud_box.setEnabled(not busy and not self.connected)
        self.connect_button.setEnabled(not busy and not self.connected)
        self.disconnect_button.setEnabled(not busy and self.connected)
        self.upload_button.setEnabled(not busy and self.connected)
        self.download_button.setEnabled(not busy and self.connected)
        self.remote_up.setEnabled(not busy and self.connected)
        self.remote_path.setEnabled(not busy and self.connected)
        self.remote_refresh.setEnabled(not busy and self.connected)
        self.remote_mkdir_button.setEnabled(not busy and self.connected)
        self.cancel_button.setEnabled(busy and not self.cancel_requested)

    def update_elapsed(self):
        if self.process is not None:
            elapsed = int(time.monotonic() - self.started)
            self.statusBar().showMessage(f'操作进行中  {elapsed // 60:02d}:{elapsed % 60:02d}   ·   上传与下载完成后核对 SHA256')
        else:
            self.statusBar().showMessage('SHA256 校验 · 覆盖前确认 · 按需读取板端目录')

    def cancel(self):
        for job in self.jobs:
            self.queue_table.item(job['row'], 4).setText('已取消')
        self.jobs.clear()
        self.refresh_after_queue = False
        if self.process is not None:
            self.cancel_requested = True
            self.status_label.setText('正在取消，等待串口恢复…')
            self.append_log('已请求取消，等待当前清理操作结束。')
            self.deliver_cancel()
            self.update_controls()

    def process_started(self):
        if self.cancel_requested:
            self.deliver_cancel()

    def deliver_cancel(self):
        if self.process is not None and self.process.processId():
            try:
                os.kill(self.process.processId(), signal.SIGINT)
            except ProcessLookupError:
                pass

    def save_log(self):
        name, _ = QFileDialog.getSaveFileName(self, '保存传输日志', 'serial-transfer.log', '日志文件 (*.log);;所有文件 (*)')
        if name:
            try:
                Path(name).write_text(self.log.toPlainText(), encoding='utf-8')
            except OSError as error:
                QMessageBox.warning(self, '保存失败', str(error))

    def closeEvent(self, event):
        if self.process is not None:
            QMessageBox.information(self, '操作进行中', '请先取消当前操作，等待串口恢复后再关闭窗口。')
            event.ignore()
        else:
            event.accept()


def main():
    try:
        ctypes.CDLL('libxcb-cursor.so.0', mode=ctypes.RTLD_GLOBAL)
    except OSError:
        compatibility_library = ROOT / 'compat' / ('linux-' + platform.machine()) / 'libxcb-cursor.so.0'
        if compatibility_library.is_file():
            ctypes.CDLL(str(compatibility_library), mode=ctypes.RTLD_GLOBAL)
    application = QApplication(sys.argv)
    application.setApplicationName('Serial Transfer')
    application.setStyle('Fusion')
    select_font(application)
    window = Application()
    window.show()
    return application.exec()


if __name__ == '__main__':
    sys.exit(main())
