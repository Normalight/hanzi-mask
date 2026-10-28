"""Standalone texture client: import, preview, review and batch publication."""

from dataclasses import replace
from datetime import datetime
from pathlib import Path
import sys
import os
import tempfile

from PySide6.QtCore import QEvent, QRectF, QSignalBlocker, QSize, Qt, QTimer, QUrl, Slot
from PySide6.QtGui import QColor, QDesktopServices, QIcon, QKeySequence, QPainter, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QButtonGroup, QComboBox,
    QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMessageBox, QProgressBar,
    QPushButton, QScrollArea, QSizePolicy, QSplitter, QTableWidget,
    QTableWidgetItem, QToolButton, QVBoxLayout, QWidget,
)

from hanzi_mask import __version__
from hanzi_mask.batch_service import BatchResult, SaveSummary
from hanzi_mask.output_style import DEFAULT_OUTPUT_STYLE, OUTPUT_STYLES
from hanzi_mask.processing import Options, Result
from .canvas import PreviewPanel
from .history_shortcuts import history_key_bindings
from .input_dialog import ImageInputDialog
from .paths import default_output
from .review_hints import review_hints
from .task_worker import PreviewWorker, TaskWorker


PROJECT_ROOT = Path(__file__).resolve().parents[1]
APP_NAME = '字形换底'
CORRECTION_NAMES = {'brush_cut': '画笔分割', 'brush_fill': '画笔填补', 'select': '清理色块'}


class MainWindow(QMainWindow):
    def __init__(self, initial_paths=None):
        super().__init__()
        self.setWindowTitle(f'{APP_NAME} · {__version__}')
        self.resize(1400, 900)
        self.setAcceptDrops(True)
        self.roots = []
        self.inputs = []
        self._row_by_path = {}
        self.records = {}
        self.errors = {}
        self._correction_history = {}
        self._correction_redo = {}
        self._history_view_key = None
        self._tuning_paused = 0
        self._tuning_generation = 0
        self._pending_preview = None
        self._selected_preview_path = None
        self._all_options = Options(method='texture', profile='balanced', texture_strength=0, keep_isolated=True,
                                    edge_refine=True, stroke_recovery=True, tip_recovery=True)
        self._processing_snapshots = {}
        self._processing_successful = set()
        self._adoption_controls_locked = None
        self._frozen_adoption = {}
        self.result_options = self._all_options
        self.worker = None
        self._task_operation = None
        self._platform_services = None
        self._preview_workers = set()
        self._preview_request = 0
        self._displayed_preview_path = None
        self._preview_loading = False
        self._closing = False
        self._last_output = None
        self._replacement_report = None
        self._cache = tempfile.TemporaryDirectory(prefix='hanzi-mask-desktop-')
        self._cache_root = Path(self._cache.name)
        self._tuning_timer = QTimer(self)
        self._tuning_timer.setSingleShot(True)
        self._tuning_timer.setInterval(350)
        self._tuning_timer.timeout.connect(self._preview_tuned)
        self._build_ui()
        if initial_paths:
            QTimer.singleShot(0, lambda: self.import_paths(initial_paths))

    def _build_ui(self):
        split = QSplitter(Qt.Horizontal)
        split.setChildrenCollapsible(False)
        split.setHandleWidth(6)
        self.sidebar = QWidget()
        side = QVBoxLayout(self.sidebar)
        side.setContentsMargins(10, 8, 4, 8)
        side.setSpacing(8)
        sources = QGroupBox('输入图片')
        source_layout = QVBoxLayout(sources)
        input_buttons = QVBoxLayout()
        source_actions = QHBoxLayout()
        self.file_buttons = []
        for index, (title, callback, shortcut) in enumerate((
            ('添加图片或文件夹…', self.add_inputs, 'Ctrl+O'),
            ('清空列表', self.clear_inputs, None),
        )):
            button = QPushButton(title)
            button.clicked.connect(callback)
            if shortcut:
                button.setShortcut(QKeySequence(shortcut))
            if index == 0:
                button.setObjectName('primaryAction')
                input_buttons.addWidget(button)
            else:
                source_actions.addWidget(button)
            self.file_buttons.append(button)
        source_layout.addLayout(input_buttons)
        self.root_list = QListWidget()
        self.root_list.setTextElideMode(Qt.ElideMiddle)
        self.root_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.root_list.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        for signal in (self.root_list.model().rowsInserted, self.root_list.model().rowsRemoved,
                       self.root_list.model().modelReset):
            signal.connect(self._fit_input_list)
        self._fit_input_list()
        self.root_list.setToolTip('可添加多个文件夹或直接拖入图片')
        source_layout.addWidget(self.root_list)
        self.scan_button = QPushButton('重新扫描')
        self.scan_button.clicked.connect(self.rescan)
        source_actions.addWidget(self.scan_button)
        source_layout.addLayout(source_actions)
        side.addWidget(sources)

        process_row = QHBoxLayout()
        self.process_current_button = QPushButton('处理当前图片')
        self.process_current_button.clicked.connect(self.process_current)
        process_row.addWidget(self.process_current_button)
        self.process_all_button = QPushButton('批量处理全部')
        self.process_all_button.clicked.connect(self.process_all)
        process_row.addWidget(self.process_all_button)
        side.addLayout(process_row)

        self.output_group = QGroupBox('保存结果')
        output_form = QFormLayout(self.output_group)
        output_form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        output_form.setContentsMargins(8, 10, 8, 8)
        output_form.setHorizontalSpacing(6)
        self.output_style = QComboBox()
        for value, title in OUTPUT_STYLES.items():
            self.output_style.addItem(title, value)
        self.output_style.setCurrentIndex(self.output_style.findData(DEFAULT_OUTPUT_STYLE))
        self.output_style.setToolTip('用于全部图片的预览、导出副本和原位置保存；切换后无需重新处理。')
        self.output_style.currentIndexChanged.connect(self._output_style_changed)
        self.output_style.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.output_style.setMinimumContentsLength(5)
        output_form.addRow('底色', self.output_style)
        self.save_mode = QComboBox()
        self.save_mode.addItem('原位置 · 备份', 'replace')
        self.save_mode.addItem('导出副本', 'export')
        self.save_mode.setToolTip('保存到原位置仅支持 PNG，先备份全部原图；其他格式请选择“导出副本”。')
        self.save_mode.currentIndexChanged.connect(self._save_mode_changed)
        self.save_mode.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.save_mode.setMinimumContentsLength(8)
        output_form.addRow('方式', self.save_mode)
        output_row = QWidget()
        line = QHBoxLayout(output_row)
        line.setContentsMargins(0, 0, 0, 0)
        self.output = QLineEdit(str(default_output(PROJECT_ROOT, APP_NAME)))
        self.output.setMinimumWidth(80)
        choose_output = QPushButton('…')
        choose_output.setMaximumWidth(32)
        choose_output.clicked.connect(self.choose_output)
        line.addWidget(self.output, 1)
        line.addWidget(choose_output)
        self.output_label = QLabel()
        output_form.addRow(self.output_label, output_row)
        self.save_button = QPushButton()
        self.save_button.setObjectName('primaryAction')
        self.save_button.clicked.connect(self.save_results)
        output_form.addRow(self.save_button)
        self._save_mode_changed()
        side.addWidget(self.output_group)
        self.open_output_button = QPushButton('打开保存目录')
        self.open_output_button.setToolTip('打开最近一次导出结果或原图备份所在目录。')
        self.open_output_button.clicked.connect(self.open_output)
        side.addWidget(self.open_output_button)
        self.check_updates_button = QPushButton('检查更新')
        side.addWidget(self.check_updates_button)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setMinimumWidth(250)
        scroll.setMaximumWidth(300)
        scroll.setFrameShape(QScrollArea.NoFrame)
        scroll.setWidget(self.sidebar)
        split.addWidget(scroll)

        workspace = QWidget()
        work = QVBoxLayout(workspace)
        work.setContentsMargins(4, 8, 10, 8)
        work.setSpacing(6)
        preview_bar = QHBoxLayout()
        self.current_name = QLabel('选择一张图片查看')
        self.current_name.setStyleSheet('font-weight: 600;')
        self.current_name.setMinimumWidth(0)
        self.current_name.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.current_name.setTextInteractionFlags(Qt.TextSelectableByMouse)
        preview_bar.addWidget(self.current_name, 1)
        self.preview = PreviewPanel()
        self.preview.output_style_changed.connect(self._preview_style_changed)
        self.preview.selected_pixels.connect(self._selection_changed)
        self.preview.clear_requested.connect(self.clear_selected_component)
        self.preview.stroke_started.connect(self._discard_pending_preview)
        self.preview.brush_finished.connect(self.apply_brush_correction)
        self.preview.brush_foreground_changed.connect(self._sync_brush_controls)
        work.addLayout(preview_bar)
        correction_bar = QHBoxLayout()
        correction_bar.setSpacing(4)
        self.tool_group = QButtonGroup(self)
        self.select_tool_button = QPushButton('选中色块')
        self.brush_tool_button = QPushButton('画笔工具')
        for button, tool in ((self.select_tool_button, 'select'), (self.brush_tool_button, 'brush')):
            button.setCheckable(True)
            self.tool_group.addButton(button)
            button.clicked.connect(lambda _=False, value=tool: self._set_correction_tool(value))
            correction_bar.addWidget(button)
        self.brush_tool_button.setChecked(True)
        self.brush_options = QWidget()
        brush_bar = QHBoxLayout(self.brush_options)
        brush_bar.setContentsMargins(4, 0, 0, 0)
        brush_bar.setSpacing(4)
        self.brush_color_group = QButtonGroup(self)
        self.background_brush_button = QPushButton()
        self.foreground_brush_button = QPushButton()
        for button, foreground in ((self.background_brush_button, False),
                                   (self.foreground_brush_button, True)):
            button.setCheckable(True)
            self.brush_color_group.addButton(button)
            button.clicked.connect(lambda _=False, value=foreground:
                                   self.preview.mask_canvas.set_brush_foreground(value))
            brush_bar.addWidget(button)
        brush_bar.addWidget(QLabel('大小'))
        size_choices = QWidget()
        size_bar = QHBoxLayout(size_choices)
        size_bar.setContentsMargins(0, 0, 0, 0)
        size_bar.setSpacing(4)
        size_choices.setStyleSheet(
            'QToolButton { background: #ffffff; border: 1px solid #b8bec7; border-radius: 4px; padding: 0; }'
            'QToolButton:hover { background: #edf4ff; }'
            'QToolButton:checked { background: #dbeafe; border: 2px solid #6c9ddd; }'
            'QToolButton:disabled { background: #eeeeee; border-color: #d4d4d4; }')
        self.brush_size_group = QButtonGroup(self)
        self.brush_size_buttons = {}
        for size, diameter, name in (
            (1, 3, '最细'), (3, 5, '细'), (5, 8, '较细'), (9, 11, '中等'),
            (15, 15, '较粗'), (25, 20, '粗'), (40, 26, '最粗'),
        ):
            button = QToolButton()
            button.setCheckable(True)
            button.setFixedSize(30, 30)
            button.setIconSize(QSize(28, 28))
            button.setToolTip(f'{name}画笔')
            button.setAccessibleName(f'{name}画笔')
            dot = QPixmap(56, 56)
            dot.setDevicePixelRatio(2)
            dot.fill(Qt.transparent)
            painter = QPainter(dot)
            painter.setRenderHint(QPainter.Antialiasing, True)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor('#202020'))
            painter.drawEllipse(QRectF((28 - diameter) / 2, (28 - diameter) / 2,
                                      diameter, diameter))
            painter.end()
            button.setIcon(QIcon(dot))
            self.brush_size_group.addButton(button, size)
            self.brush_size_buttons[size] = button
            button.setChecked(size == self.preview.mask_canvas.brush_size)
            size_bar.addWidget(button)
        self.brush_size_group.idClicked.connect(self.preview.mask_canvas.set_brush_size)
        brush_bar.addWidget(size_choices)
        correction_bar.addWidget(self.brush_options)
        self.clear_component_button = QPushButton('清理选中色块')
        self.clear_component_button.clicked.connect(self.clear_selected_component)
        correction_bar.addWidget(self.clear_component_button)
        correction_bar.addStretch()
        work.addLayout(correction_bar)
        self.correction_note = QLabel()
        self.correction_note.setWordWrap(True)
        self.correction_note.setStyleSheet('color: #6b7280; font-size: 11px;')
        work.addWidget(self.correction_note)
        self.preview.setMinimumHeight(250)
        self.body_splitter = body = QSplitter(Qt.Vertical)
        body.setHandleWidth(6)
        body.setChildrenCollapsible(False)
        body.addWidget(self.preview)

        table_panel = QWidget()
        table_layout = QVBoxLayout(table_panel)
        table_layout.setContentsMargins(0, 0, 0, 0)
        table_layout.setSpacing(6)
        table_panel.setMinimumHeight(140)
        filter_line = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText('筛选文件名或路径…')
        self.search.textChanged.connect(self.filter_rows)
        filter_line.addWidget(self.search, 1)
        self.state_filter = QComboBox()
        self.state_filter.addItems(['全部图片', '待复核', '已矫正', '未处理', '失败'])
        self.state_filter.currentIndexChanged.connect(self.filter_rows)
        filter_line.addWidget(self.state_filter)
        self.adoption_buttons = []
        for title, checked in [('全部采用', True), ('全部排除', False)]:
            button = QPushButton(title)
            button.clicked.connect(lambda _=False, value=checked: self.check_all(value))
            filter_line.addWidget(button)
            self.adoption_buttons.append(button)
        table_layout.addLayout(filter_line)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(['采用', '图片 / 相对目录', '尺寸', '状态', '复核提示'])
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.verticalHeader().setDefaultSectionSize(28)
        self.table.setMinimumHeight(92)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch)
        self.table.itemSelectionChanged.connect(self.preview_selection)
        self.table.itemChanged.connect(self._table_item_changed)
        table_layout.addWidget(self.table, 1)
        self.history_panel = QGroupBox('操作历史')
        self.history_panel.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)
        history_layout = QVBoxLayout(self.history_panel)
        history_actions = QHBoxLayout()
        self.undo_correction_button = QPushButton('撤销修改')
        self.undo_correction_button.clicked.connect(self.undo_correction)
        history_actions.addWidget(self.undo_correction_button)
        self.redo_correction_button = QPushButton('重做修改')
        self.redo_correction_button.clicked.connect(self.redo_correction)
        history_actions.addWidget(self.redo_correction_button)
        history_layout.addLayout(history_actions)
        self.history_list = QListWidget()
        self.history_list.setMinimumHeight(100)
        self.history_list.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Expanding)
        self.history_list.setToolTip('点击任一步回到该状态；每张图片保留最近 20 步修改。')
        self.history_list.setTextElideMode(Qt.ElideRight)
        self.history_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.history_list.itemClicked.connect(self._history_item_activated)
        self.history_list.itemActivated.connect(self._history_item_activated)
        history_layout.addWidget(self.history_list, 1)
        side.addWidget(self.history_panel, 1)
        body.addWidget(table_panel)
        body.setStretchFactor(0, 1)
        body.setStretchFactor(1, 0)
        body.setSizes([600, 180])
        work.addWidget(body, 1)
        footer = QHBoxLayout()
        self.progress = QProgressBar()
        self.progress.setFixedHeight(18)
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        footer.addWidget(self.progress, 1)
        self.cancel_button = QPushButton('取消任务')
        self.cancel_button.clicked.connect(self.cancel_task)
        footer.addWidget(self.cancel_button)
        work.addLayout(footer)
        split.addWidget(workspace)
        split.setStretchFactor(1, 1)
        split.setSizes([250, 1150])
        self.setCentralWidget(split)
        self.status = QLabel('就绪 · 可拖入图片或文件夹')
        self.status.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.statusBar().addWidget(self.status, 1)
        self.setStyleSheet(
            'QMainWindow { background: #f5f6f8; }'
            'QGroupBox { border: 1px solid #d8dde5; border-radius: 6px; margin-top: 8px; padding: 6px; }'
            'QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 3px; color: #4b5563; }'
            'QPushButton { min-height: 24px; padding: 2px 8px; border: 1px solid #cdd3dc; border-radius: 4px; background: #ffffff; }'
            'QPushButton:hover { background: #eef3f9; border-color: #a8b8cf; }'
            'QPushButton:pressed, QPushButton:checked { background: #e0edff; color: #184d93; border-color: #79a6e3; }'
            'QPushButton:disabled { color: #9ca3af; background: #f4f5f7; border-color: #e0e3e8; }'
            'QPushButton#primaryAction:enabled { background: #3478cf; color: white; border-color: #3478cf; }'
            'QPushButton#primaryAction:enabled:hover { background: #2867b5; }'
            'QLineEdit, QComboBox { min-height: 24px; }'
            'QListWidget, QTableWidget { border: 1px solid #d8dde5; border-radius: 3px; background: #ffffff; alternate-background-color: #f7f9fc; }'
            'QHeaderView::section { background: #eef1f5; color: #4b5563; border: none; border-bottom: 1px solid #d8dde5; padding: 5px; }'
            'QSplitter::handle { background: #e5e8ed; }')
        self.escape = QShortcut(QKeySequence('Escape'), self)
        self.escape.activated.connect(self.cancel_task)
        self.history_shortcuts = []
        bindings = history_key_bindings()
        self._history_key_sequences = {sequence.toString(QKeySequence.PortableText)
                                       for sequences in bindings.values() for sequence in sequences}
        for key, callback in ((QKeySequence.Undo, self.undo_correction),
                              (QKeySequence.Redo, self.redo_correction)):
            shortcut = QShortcut(self)
            shortcut.setKeys(bindings[key])
            shortcut.setContext(Qt.WindowShortcut)
            shortcut.activated.connect(callback)
            self.history_shortcuts.append((key, shortcut))
        for editor in self.findChildren(QLineEdit):
            editor.installEventFilter(self)
        undo_keys = 'Command+Z' if sys.platform == 'darwin' else 'Ctrl+Z'
        redo_keys = 'Command+Shift+Z' if sys.platform == 'darwin' else 'Ctrl+Y'
        self.undo_correction_button.setToolTip('撤销当前图片的上一步修改：' + undo_keys)
        self.redo_correction_button.setToolTip('恢复刚撤销的修改：' + redo_keys)
        self._set_correction_tool('brush')
        self._update_buttons()

    def _fit_input_list(self, *_):
        rows = max(1, min(2, self.root_list.count()))
        self.root_list.setFixedHeight(rows * (self.fontMetrics().height() + 6) + 6)

    def eventFilter(self, watched, event):
        if (isinstance(watched, QLineEdit) and event.type() == QEvent.ShortcutOverride
                and QKeySequence(event.keyCombination()).toString(QKeySequence.PortableText)
                in self._history_key_sequences):
            # Reserve these keys for text editing even when its undo stack is empty.
            event.accept()
            return True
        return super().eventFilter(watched, event)

    def processing_options(self):
        return self._all_options

    def _platform_allows(self, operation):
        services = self._platform_services
        return services is None or services.permits(operation)

    def _schedule_tuned_preview(self):
        source = self.current_source()
        if (source is None or source.path in self.records or source.path in self.errors
                or self._closing or self._tuning_paused or not self._platform_allows('process')
                or (self.worker is not None and self._task_operation not in ('scan', 'process', 'correct'))):
            return
        self._pending_preview = {
            'generation': self._tuning_generation, 'path': source.path,
            'options': self.processing_options(), 'ready': False,
        }
        self._tuning_timer.start()
        self._update_buttons()

    def _preview_tuned(self):
        if not self._pending_preview_valid():
            self._pending_preview = None
            self._update_buttons()
            return
        self._pending_preview['ready'] = True
        self._run_pending_preview()

    def _discard_pending_preview(self, cancel_running=True):
        self._tuning_timer.stop()
        self._pending_preview = None
        # Queued signals from the old automatic task must become stale immediately.
        self._tuning_generation += 1
        if (cancel_running and self.worker is not None and self._task_operation == 'process'
                and self.worker.payload.get('automatic')):
            self.worker.requestInterruption()
        self._update_buttons()

    def _pending_preview_valid(self):
        pending = self._pending_preview
        source = self.current_source()
        return (pending is not None and pending['generation'] == self._tuning_generation
                and source is not None and source.path == pending['path']
                and source.path not in self.records and source.path not in self.errors
                and not self._closing and not self._tuning_paused
                and self._platform_allows('process'))

    def _run_pending_preview(self):
        if not self._pending_preview_valid():
            self._pending_preview = None
            self._update_buttons()
            return
        if self.worker is not None or not self._pending_preview['ready']:
            return
        pending = self._pending_preview
        self._pending_preview = None
        self._start_processing([self.current_source()], options=pending['options'],
                               automatic=True, generation=pending['generation'])

    def _automatic_task_obsolete(self):
        return (self.worker is not None and self._task_operation == 'process'
                and self.worker.payload.get('automatic')
                and self.worker.payload['generation'] != self._tuning_generation)

    def _show_dialog(self, dialog, *args):
        self._discard_pending_preview()
        self._tuning_paused += 1
        try:
            return dialog(self, *args)
        finally:
            self._tuning_paused -= 1

    def add_inputs(self):
        paths = self._show_dialog(ImageInputDialog.choose)
        if paths:
            self.import_paths(paths)

    def import_paths(self, paths, append=True):
        if self.worker or self._closing or not self._platform_allows('scan'):
            return
        roots = list(self.roots) if append else []
        for path in paths:
            p = Path(path).expanduser().resolve()
            if p not in roots:
                roots.append(p)
        self.roots = roots
        self.root_list.clear()
        self.root_list.addItems([str(p) for p in roots])
        for row, path in enumerate(roots):
            self.root_list.item(row).setToolTip(str(path))
        self.rescan()

    def clear_inputs(self):
        if self.worker or not self._platform_allows('scan'):
            return
        self._discard_pending_preview()
        self.roots.clear()
        self.root_list.clear()
        self.inputs.clear()
        self._row_by_path.clear()
        self.records.clear()
        self.errors.clear()
        self._correction_history.clear()
        self._correction_redo.clear()
        self._selected_preview_path = None
        self.table.setRowCount(0)
        self._cancel_previews()
        self.preview.clear()
        self._displayed_preview_path = None
        self._preview_loading = False
        self.current_name.setText('选择一张图片查看')
        self.status.setText('列表已清空')
        self._update_buttons()

    def rescan(self):
        if not self.roots or self.worker:
            return
        excluded = [self._cache_root]
        if self.output.text().strip():
            output = Path(self.output.text()).expanduser().resolve()
            if not self._output_contains_input(output):
                excluded.append(output)
        self._launch('scan', {'paths': self.roots, 'recursive': True,
                              'exclude_paths': excluded})

    def _populate(self, previous=None, selected_path=None):
        previous = previous or {}
        self.table.blockSignals(True)
        self._row_by_path = {source.path: row for row, source in enumerate(self.inputs)}
        self.table.setRowCount(len(self.inputs))
        for row, source in enumerate(self.inputs):
            check = QTableWidgetItem()
            check.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable | Qt.ItemIsSelectable)
            old = previous.get(source.path)
            check.setCheckState(old['checked'] if old else Qt.Unchecked)
            self.table.setItem(row, 0, check)
            name = QTableWidgetItem(source.relative_path.as_posix())
            name.setToolTip(str(source.path))
            self.table.setItem(row, 1, name)
            for column, value in ((2, '—'), (3, '未处理'), (4, '')):
                self.table.setItem(row, column, QTableWidgetItem(value))
            if old:
                self.table.item(row, 2).setText(old['size'])
                self.table.item(row, 3).setText(old['state'])
                self.table.item(row, 4).setText(old['note'])
                self.table.item(row, 4).setToolTip(old['note'])
            self._update_adoption_cell(row)
        self.table.blockSignals(False)
        self.filter_rows()
        if self.inputs:
            selected = self._row_by_path.get(selected_path)
            if selected is not None and not self.table.isRowHidden(selected):
                self.table.setCurrentCell(selected, 1)
            elif self.current_source() is None:
                visible = next((row for row in range(len(self.inputs)) if not self.table.isRowHidden(row)), None)
                if visible is not None:
                    self.table.setCurrentCell(visible, 1)
        else:
            self.preview.clear()
            self.current_name.setText('未找到支持的图片')
        self.preview_selection()
        if self._pending_preview is None:
            self._schedule_tuned_preview()

    def _invalidate_results(self, note):
        self.records.clear()
        self.errors.clear()
        self._correction_history.clear()
        self._correction_redo.clear()
        with QSignalBlocker(self.table):
            for row in range(self.table.rowCount()):
                self.table.item(row, 0).setCheckState(Qt.Unchecked)
                self.table.item(row, 3).setText('未处理')
                self.table.item(row, 4).setText(note)
                self.table.item(row, 4).setToolTip(note)
        self.filter_rows()
        self.preview_selection()
        self._update_buttons()

    def current_source(self):
        row = self.table.currentRow()
        return self.inputs[row] if 0 <= row < len(self.inputs) and not self.table.isRowHidden(row) else None

    def process_current(self):
        source = self.current_source()
        if source:
            self._start_processing([source], options=self.processing_options())

    def process_all(self):
        self._start_processing(self.inputs, options=self.processing_options())

    def _row_snapshot(self, path):
        row = self._row_by_path[path]
        return {'record': self.records.get(path),
                'checked': self.table.item(row, 0).checkState(),
                'size': self.table.item(row, 2).text(),
                'state': self.table.item(row, 3).text(),
                'note': self.table.item(row, 4).text()}

    def _restore_processing_row(self, path, error=None):
        snapshot = self._processing_snapshots[path]
        row = self._row_by_path.get(path)
        if row is None:
            return
        if snapshot['record'] is not None and path not in self.records:
            # Preview validation may have found a changed source or broken cache.
            # Such a record cannot be restored merely because processing failed.
            message = self.errors.get(path) or error or '原结果无法读取，请重新处理。'
            self.errors[path] = message
            self._set_row(path, '失败', message, checked=False)
            return
        if error is not None and snapshot['record'] is None:
            self.errors[path] = error
            self._set_row(path, '失败', error, checked=False)
            return
        self._set_row(path, snapshot['state'], snapshot['note'])
        self.table.item(row, 2).setText(snapshot['size'])
        if error is not None:
            self.table.item(row, 4).setToolTip(
                (snapshot['note'] + '\n' if snapshot['note'] else '')
                + '本次处理失败，已保留原结果：' + error)

    def _start_processing(self, sources, *, options=None, per_image_options=None,
                          automatic=False, generation=None):
        sources = list(sources)
        if not sources or self.worker or self._closing or not self._platform_allows('process'):
            return
        self.result_options = options or self.processing_options()
        self._processing_snapshots = {source.path: self._row_snapshot(source.path) for source in sources}
        self._processing_successful = set()
        for source in sources:
            self._set_row(source.path, '处理中', self._processing_snapshots[source.path]['note'])
        cache = self._cache_root / f'run-{datetime.now().strftime("%Y%m%d-%H%M%S-%f")}'
        self._launch('process', {'inputs': list(sources), 'options': self.result_options,
                                 'cache_dir': cache, 'max_workers': 2,
                                 'per_image_options': dict(per_image_options) if per_image_options else None,
                                 'automatic': automatic, 'generation': generation})

    def _launch(self, operation, payload):
        if self.worker or self._closing or not self._platform_allows(operation):
            return
        self._tuning_timer.stop()
        if not payload.get('automatic'):
            self._discard_pending_preview(cancel_running=False)
        self._task_operation = operation
        worker = TaskWorker(operation, payload, self)
        self.worker = worker
        worker.progress.connect(self._task_progress, Qt.QueuedConnection)
        worker.item_ready.connect(self._task_item, Qt.QueuedConnection)
        worker.completed.connect(self._task_completed, Qt.QueuedConnection)
        worker.failed.connect(self._task_failed, Qt.QueuedConnection)
        worker.finished.connect(self._task_finished, Qt.QueuedConnection)
        if operation != 'correct':
            self.progress.setRange(0, 0)
        self.status.setText({'scan': '正在扫描图片…', 'process': '正在处理图片…',
                             'export': '正在导出结果…', 'replace': '正在备份并替换…',
                             'restore': '正在恢复原始图片…', 'correct': '正在保存人工矫正…'}[operation])
        self._update_buttons()
        worker.start()

    def _task_event(self, worker, callback, *args):
        if worker is not None and worker is self.worker and not self._automatic_task_obsolete():
            callback(*args)

    @Slot(int, int, str)
    def _task_progress(self, done, total, path):
        self._task_event(self.sender(), self._on_progress, done, total, path)

    @Slot(object)
    def _task_item(self, item):
        self._task_event(self.sender(), self._on_item, item)

    @Slot(object)
    def _task_completed(self, result):
        self._task_event(self.sender(), self._on_completed, result)

    @Slot(str, object)
    def _task_failed(self, message, report):
        self._task_event(self.sender(), self._on_error, message, report)

    @Slot()
    def _task_finished(self):
        worker = self.sender()
        if worker is not None:
            self._on_finished(worker)

    def _on_progress(self, done, total, path):
        self.progress.setRange(0, total)
        self.progress.setValue(done)
        self.status.setText(f'{done} / {total} · {Path(path).name}')

    def _set_row(self, path, state, note, checked=None, metrics=None):
        row = self._row_by_path.get(path)
        if row is None:
            return
        with QSignalBlocker(self.table):
            self.table.item(row, 3).setText(state)
            self.table.item(row, 4).setText(note)
            self.table.item(row, 4).setToolTip(note)
            if metrics:
                self.table.item(row, 2).setText(f'{metrics["width"]} × {metrics["height"]}')
            if checked is not None:
                self.table.item(row, 0).setCheckState(Qt.Checked if checked else Qt.Unchecked)
            self._update_adoption_cell(row)

    def _on_item(self, item, *, corrected_mask=None):
        path = item.source.path
        previous = self.records.get(path)
        row = self._row_by_path.get(path)
        had_result = (previous is not None
                      or self._processing_snapshots.get(path, {}).get('record') is not None)
        adopted = (self.table.item(row, 0).checkState() == Qt.Checked
                   if had_result and row is not None else True)
        if self.worker is not None and self._task_operation == 'process' and path in self._processing_snapshots:
            self._processing_successful.add(path)
            self._correction_history.pop(path, None)
            self._correction_redo.pop(path, None)
            self.errors.pop(path, None)
        notes = review_hints(item.metrics, can_undo=bool(self._correction_history.get(item.source.path)))
        item = replace(item, metrics={**item.metrics, 'review_reasons': notes})
        self.records[item.source.path] = item
        state = '已矫正' if item.metrics.get('manual_correction') else ('待复核' if notes else '已处理')
        self._set_row(item.source.path, state, '；'.join(notes),
                      checked=adopted and item.metrics.get('foreground_pixels', 0) > 0, metrics=item.metrics)
        current = self.current_source()
        if current and current.path == item.source.path:
            if (corrected_mask is not None and not self._preview_loading
                    and self._displayed_preview_path == current.path and self.preview.has_image):
                self.preview.update_mask(corrected_mask)
            else:
                self._refresh_preview()

    def _on_completed(self, result):
        operation = self._task_operation
        if operation == 'scan':
            if result is None:
                self.status.setText('已取消扫描')
                return
            self._cancel_previews()
            current = self.current_source()
            selected_path = current.path if current is not None else None
            previous = {source.path: self._row_snapshot(source.path) for source in self.inputs}
            by_path = {source.path: source for source in result}
            self.records = {path: replace(item, source=by_path[path]) for path, item in self.records.items()
                            if path in by_path}
            self.errors = {path: error for path, error in self.errors.items() if path in by_path}
            self._correction_history = {
                path: [(replace(item, source=by_path[path]), adopted) for item, adopted in history]
                for path, history in self._correction_history.items() if path in by_path}
            self._correction_redo = {
                path: [(replace(item, source=by_path[path]), adopted) for item, adopted in future]
                for path, future in self._correction_redo.items() if path in by_path}
            self.inputs = result
            self._populate(previous, selected_path)
            self.status.setText(f'扫描完成：{len(result)} 张图片')
        elif operation == 'process':
            for item in result.items:
                if item.source.path not in self._processing_successful:
                    self._on_item(item)
            failures = {error.path: error.error for error in result.errors}
            for path in self._processing_snapshots:
                if path not in self._processing_successful:
                    self._restore_processing_row(path, failures.get(path))
            self._processing_snapshots.clear()
            state = '已取消' if result.cancelled else '处理完成'
            self.status.setText(f'{state}：{len(result.items)} 张成功，{len(result.errors)} 张失败；'
                                '未成功重算的图片保留原结果')
            self.filter_rows(preserve_editor=True)
            self._refresh_preview()
        elif operation == 'correct':
            previous = self.worker.payload['item']
            history = self._correction_history.setdefault(previous.source.path, [])
            history.append((previous, self.worker.payload['adopted']))
            del history[:-20]
            self._correction_redo.pop(previous.source.path, None)
            result = replace(result, metrics={**result.metrics,
                                             'correction_operation': self.worker.payload.get('action', 'select')})
            self._on_item(result, corrected_mask=self.worker.payload['mask'])
            notes = self.records[result.source.path].metrics['review_reasons']
            self._set_row(result.source.path, '已矫正', '；'.join(notes))
            action = '已完成画笔精修' if self.worker.payload.get('tool') == 'brush' else '已清理选中色块'
            self.status.setText(action + '，保存时应用当前矫正结果')
        elif isinstance(result, SaveSummary):
            if self._platform_services is not None:
                self._platform_services.on_saved(result)
            self._last_output = result.output_dir
            if operation == 'replace':
                self._replacement_report = result.report_path
            if operation in ('replace', 'restore'):
                self._invalidate_results('已恢复原图' if operation == 'restore' else
                                         ('保存已取消，原图已回滚' if result.cancelled else '已换底，备份可恢复'))
            verb = {'export': '导出', 'replace': '保存到原位置', 'restore': '恢复'}[operation]
            self.status.setText(f'{verb}{"已取消" if result.cancelled else "完成"}：{len(result.outputs)} 张成功，'
                                f'{len(result.errors)} 张失败 · {result.output_dir}')
            if result.errors and not self._closing:
                details = '\n'.join(f'{error.path.name}：{error.error}' for error in result.errors[:5])
                self._show_dialog(QMessageBox.warning, '部分结果保存失败',
                                  f'{len(result.errors)} 张保存失败，成功结果已保留。\n'
                                  f'{details}\n完整记录：{result.report_path}')
        self._update_buttons()

    def _on_error(self, message, report=None):
        if report:
            self._replacement_report = Path(report)
            self._last_output = Path(report).parent
        if self._task_operation in ('replace', 'restore'):
            self._invalidate_results('请检查恢复清单并重新处理')
        elif self._task_operation == 'process':
            for path in self._processing_snapshots:
                if path not in self._processing_successful:
                    self._restore_processing_row(path, message)
            self._processing_snapshots.clear()
            self.filter_rows(preserve_editor=True)
            self._refresh_preview()
        elif self._task_operation == 'correct':
            self.preview.mask_canvas.discard_pending_mask()
            self._refresh_preview()
        self.status.setText('失败：' + message)
        if not self._closing:
            self._show_dialog(QMessageBox.warning, '任务失败', message)

    def _on_finished(self, worker=None):
        worker = worker or self.worker
        if worker is not self.worker:
            return
        obsolete = self._automatic_task_obsolete()
        self.worker = None
        if worker:
            worker.deleteLater()
        if self._task_operation == 'process':
            unsettled = bool(self._processing_snapshots)
            for path in self._processing_snapshots:
                if path not in self._processing_successful:
                    self._restore_processing_row(path)
            self._processing_snapshots.clear()
            self._processing_successful.clear()
            if unsettled:
                self.filter_rows(preserve_editor=True)
                self._refresh_preview()
        if self.progress.maximum() == 0:
            self.progress.setRange(0, 1)
            self.progress.setValue(0)
        self._update_buttons()
        if obsolete and not self._closing and self._pending_preview is None:
            self.status.setText('已取消处理，保留已有结果。' if worker.payload.get('cancelled_by_user')
                                else '已结束上一轮自动处理，保留已有结果。')
        self._finish_close()
        if self._platform_services is not None:
            self._platform_services.on_task_finished(self._task_operation)
        if self._pending_preview is not None and not self._closing:
            QTimer.singleShot(0, self._run_pending_preview)

    def cancel_task(self):
        self._discard_pending_preview(cancel_running=False)
        self.preview.clear_selection()
        if self.worker and self._task_operation != 'restore':
            self.worker.payload['cancelled_by_user'] = True
            self.worker.requestInterruption()
            self.cancel_button.setEnabled(False)
            self.status.setText('正在结束当前图片或回滚替换，请稍候…')

    def _cancel_previews(self):
        self._preview_request += 1
        for worker in self._preview_workers:
            worker.requestInterruption()

    def preview_selection(self):
        source = self.current_source()
        path = source.path if source is not None else None
        changed = path != self._selected_preview_path
        if changed:
            self._selected_preview_path = path
            self._discard_pending_preview()
        self._refresh_preview()
        if changed:
            self._schedule_tuned_preview()

    def _refresh_preview(self):
        source = self.current_source()
        self._cancel_previews()
        if source is None or self._closing:
            self.preview.clear()
            self._displayed_preview_path = None
            self._preview_loading = False
            return
        self.current_name.setText(source.relative_path.as_posix())
        self.current_name.setToolTip(str(source.path))
        # Retain the current image while a same-image revision loads. Actual
        # image switches clear immediately so pixels cannot acquire a new owner.
        if source.path != self._displayed_preview_path:
            self.preview.clear()
            self._displayed_preview_path = None
        self._preview_loading = True
        request = self._preview_request
        item = self.records.get(source.path)
        foreground = (item.options.foreground if item is not None and item.options is not None
                      else self.processing_options().foreground)
        worker = PreviewWorker(request, source.path, item, foreground, self)
        self._preview_workers.add(worker)
        worker.completed.connect(self._preview_completed, Qt.QueuedConnection)
        worker.failed.connect(self._preview_failed, Qt.QueuedConnection)
        worker.finished.connect(self._preview_task_finished, Qt.QueuedConnection)
        worker.start()
        self._update_buttons()

    @Slot(int, object)
    def _preview_completed(self, request, result):
        if request != self._preview_request or self._closing:
            return
        if isinstance(result, Result):
            self.preview.set_images(result.original, result.mask, result.candidate)
        else:
            self.preview.set_images(result, None, None)
        source = self.current_source()
        self._displayed_preview_path = source.path if source else None
        self._preview_loading = False
        self._update_buttons()

    @Slot(int, str)
    def _preview_failed(self, request, message):
        if request == self._preview_request and not self._closing:
            source = self.current_source()
            if source:
                self.records.pop(source.path, None)
                self._correction_history.pop(source.path, None)
                self._correction_redo.pop(source.path, None)
                self.errors[source.path] = message
                self._set_row(source.path, '失败', message, checked=False)
            self.preview.clear()
            self._displayed_preview_path = None
            self._preview_loading = False
            self.status.setText('预览失败：' + message)
            self._update_buttons()

    @Slot()
    def _preview_task_finished(self):
        worker = self.sender()
        if worker is not None:
            self._preview_finished(worker)

    def _preview_finished(self, worker):
        self._preview_workers.discard(worker)
        worker.deleteLater()
        self._finish_close()

    def _set_correction_tool(self, tool):
        self.preview.mask_canvas.set_tool(tool)
        self.select_tool_button.setChecked(tool == 'select')
        self.brush_tool_button.setChecked(tool == 'brush')
        self.brush_options.setVisible(tool == 'brush')
        self.clear_component_button.setVisible(tool == 'select')
        self._sync_brush_controls()
        self._selection_changed(self.preview.selection_count)

    def _sync_brush_controls(self, *_):
        canvas = self.preview.mask_canvas
        for button, foreground in ((self.background_brush_button, False),
                                   (self.foreground_brush_button, True)):
            white = foreground != canvas.inverted
            color = '白色' if white else '黑色'
            action = '填补' if foreground else '分割'
            swatch = QPixmap(14, 14)
            swatch.fill(QColor('white' if white else 'black'))
            painter = QPainter(swatch)
            painter.setPen(QColor('#9ca3af'))
            painter.drawRect(0, 0, 13, 13)
            painter.end()
            button.setIcon(QIcon(swatch))
            button.setText(action)
            button.setAccessibleName(f'{color} · {action}')
            button.setChecked(canvas.brush_foreground == foreground)
            help_text = ('填补缺失笔画' if foreground else '用背景色画线，分离粘连色块')
            button.setToolTip(f'{color} · {action}：{help_text}；在画布按 X 切换双色')

    def _selection_changed(self, pixels):
        if self.preview.mask_canvas.tool == 'brush':
            self.correction_note.setText('拖动精修 · X 切换双色 · Esc 取消当前一笔')
        else:
            self.correction_note.setText(f'已选中 {pixels} 个像素 · 退格或 Delete 清理' if pixels else
                                         '点击选块 · Ctrl/Command 多选 · Delete 清理')
        self._update_buttons()

    def clear_selected_component(self):
        if self.preview.mask_canvas.tool != 'select':
            return
        mask = self.preview.remove_selected()
        if mask is not None:
            self._apply_manual_correction(mask, 'select')

    def apply_brush_correction(self, mask):
        self._apply_manual_correction(mask, 'brush')

    def _apply_manual_correction(self, mask, tool):
        source = self.current_source()
        if (self.worker or self._closing or not self._platform_allows('correct')
                or self._preview_loading or source is None or source.path not in self.records):
            self.preview.mask_canvas.discard_pending_mask()
            return
        action = ('brush_fill' if self.preview.mask_canvas.brush_foreground else 'brush_cut') if tool == 'brush' else 'select'
        self.preview.mask_canvas.show_pending_mask(mask)
        self._launch('correct', {'item': self.records[source.path], 'mask': mask,
                                'tool': tool, 'action': action,
                                'cache_dir': self._cache_root / 'corrections',
                                'adopted': self.table.item(self.table.currentRow(), 0).checkState() == Qt.Checked})

    def undo_correction(self):
        source = self.current_source()
        if source is not None:
            self._restore_history_step(len(self._correction_history.get(source.path, [])) - 1)

    def redo_correction(self):
        source = self.current_source()
        if source is not None:
            self._restore_history_step(len(self._correction_history.get(source.path, [])) + 1)

    def _history_item_activated(self, item):
        self._restore_history_step(self.history_list.row(item))

    def _history_states(self, source):
        if source is None or source.path not in self.records:
            return [], 0
        past = self._correction_history.get(source.path, [])
        future = self._correction_redo.get(source.path, [])
        adopted = self.table.item(self._row_by_path[source.path], 0).checkState() == Qt.Checked
        return [*past, (self.records[source.path], adopted), *reversed(future)], len(past)

    def _restore_history_step(self, step):
        source = self.current_source()
        if self.worker or self._closing or source is None or not self._platform_allows('correct'):
            return
        states, current = self._history_states(source)
        if not 0 <= step < len(states) or step == current:
            return
        self._discard_pending_preview()
        self.preview.mask_canvas.cancel_stroke()
        self._correction_history[source.path] = states[:step]
        self._correction_redo[source.path] = list(reversed(states[step + 1:]))
        previous, adopted = states[step]
        self._on_item(previous)
        notes = self.records[source.path].metrics['review_reasons']
        state = '已矫正' if previous.metrics.get('manual_correction') else (
            '待复核' if notes else '已处理')
        self._set_row(source.path, state, '；'.join(notes), checked=adopted and self._can_adopt(source.path))
        self.status.setText('已回到：' + self._history_label(previous))
        self._update_buttons()

    @staticmethod
    def _history_label(item):
        step = item.metrics.get('correction_steps', 0)
        if not step:
            return '初始处理结果'
        action = CORRECTION_NAMES.get(item.metrics.get('correction_operation'), '人工修改')
        return f'{step} · {action}'

    def _sync_history_panel(self, editable):
        source = self.current_source()
        states, current = self._history_states(source)
        key = (source.path if source else None, tuple(id(item) for item, _ in states), current)
        if key != self._history_view_key:
            self._history_view_key = key
            with QSignalBlocker(self.history_list):
                self.history_list.clear()
                for index, (record, _) in enumerate(states):
                    text = self._history_label(record)
                    if index == current:
                        text += '  ← 当前'
                    item = QListWidgetItem(text)
                    if index > current:
                        item.setForeground(QColor('#808080'))
                    if record.metrics.get('manual_correction'):
                        item.setToolTip(f'删除 {record.metrics.get("correction_removed_pixels", 0)} 个像素，'
                                        f'填补 {record.metrics.get("correction_added_pixels", 0)} 个像素；点击恢复此状态')
                    self.history_list.addItem(item)
                if states:
                    self.history_list.setCurrentRow(current)
                    self.history_list.scrollToItem(self.history_list.item(current))
                else:
                    self.history_list.addItem('处理图片后在这里查看修改记录')
        self.history_list.setEnabled(editable and bool(states))
        undo = editable and current > 0
        redo = editable and current + 1 < len(states)
        self.undo_correction_button.setEnabled(undo)
        self.redo_correction_button.setEnabled(redo)
        for key, shortcut in self.history_shortcuts:
            shortcut.setEnabled(undo if key == QKeySequence.Undo else redo)

    def filter_rows(self, *_, preserve_editor=False):
        search = self.search.text().strip().casefold()
        selected_state = self.state_filter.currentText()
        source = self.current_source()
        # Internal result updates must not switch the owner of an edit or queued request.
        held_path = (source.path if preserve_editor and source is not None
                     and self._pending_preview_valid() else None)
        for row, source in enumerate(self.inputs):
            state = self.table.item(row, 3).text()
            matches = (not search or search in str(source.path).casefold())
            if selected_state != '全部图片':
                matches = matches and state == selected_state
            self.table.setRowHidden(row, not (matches or source.path == held_path))
        if self.current_source() is None:
            visible = next((row for row in range(len(self.inputs)) if not self.table.isRowHidden(row)), None)
            if visible is None:
                self.table.clearSelection()
                self.table.setCurrentCell(-1, -1)
                self._selected_preview_path = None
                self._discard_pending_preview()
                self._cancel_previews()
                self.preview.clear()
                self.current_name.setText('没有符合筛选条件的图片')
            else:
                self.table.setCurrentCell(visible, 1)
        self._update_buttons()

    def _adoption_locked(self):
        return self._closing or (self.worker is not None
                                 and self._task_operation in ('export', 'replace', 'restore'))

    def _can_adopt(self, path):
        item = self.records.get(path)
        return item is not None and item.metrics.get('foreground_pixels', 0) > 0

    def _update_adoption_cell(self, row):
        check = self.table.item(row, 0)
        flags = Qt.ItemIsSelectable | Qt.ItemIsUserCheckable
        if (not self._adoption_locked()
                and (self._can_adopt(self.inputs[row].path) or check.checkState() == Qt.Checked)):
            flags |= Qt.ItemIsEnabled
        check.setFlags(flags)

    def _remember_correction_adoption(self, path, checked):
        if (self.worker is not None and self._task_operation == 'correct'
                and self.worker.payload['item'].source.path == path):
            self.worker.payload['adopted'] = bool(checked)

    def _table_item_changed(self, item):
        if item.column() == 0 and 0 <= item.row() < len(self.inputs):
            path = self.inputs[item.row()].path
            if self._adoption_locked():
                with QSignalBlocker(self.table):
                    item.setCheckState(self._frozen_adoption.get(path, Qt.Unchecked))
            elif item.checkState() == Qt.Checked and not self._can_adopt(path):
                with QSignalBlocker(self.table):
                    item.setCheckState(Qt.Unchecked)
                    self._update_adoption_cell(item.row())
            else:
                self._remember_correction_adoption(path, item.checkState() == Qt.Checked)
                with QSignalBlocker(self.table):
                    self._update_adoption_cell(item.row())
        self._update_buttons()

    def check_all(self, checked):
        if self._adoption_locked() or not self._platform_allows('correct'):
            return
        with QSignalBlocker(self.table):
            for row, source in enumerate(self.inputs):
                if not self.table.isRowHidden(row):
                    adopt = checked and self._can_adopt(source.path)
                    self.table.item(row, 0).setCheckState(Qt.Checked if adopt else Qt.Unchecked)
                    self._update_adoption_cell(row)
                    self._remember_correction_adoption(source.path, adopt)
        self._update_buttons()

    def adopted_items(self):
        return [self.records[source.path] for row, source in enumerate(self.inputs)
                if self._can_adopt(source.path) and self.table.item(row, 0).checkState() == Qt.Checked]

    def choose_output(self):
        self._tuning_timer.stop()
        path = self._show_dialog(QFileDialog.getExistingDirectory, '选择结果或备份保存目录', self.output.text())
        if path:
            self.output.setText(path)

    def _save_mode_changed(self):
        replace = self.save_mode.currentData() == 'replace'
        self.output_label.setText('备份目录' if replace else '保存目录')
        self.save_button.setText('保存到原位置' if replace else '导出采用项')

    def _output_style_changed(self):
        self.preview.set_output_style(self.output_style.currentData())

    def _preview_style_changed(self, output_style):
        self.output_style.setCurrentIndex(self.output_style.findData(output_style))
        self._sync_brush_controls()

    def _output_contains_input(self, output):
        paths = self.roots or [source.path for source in self.inputs]
        return any(path == output or output in path.parents for path in paths)

    def save_results(self, *, mode=None):
        self._tuning_timer.stop()
        if self.worker:
            return False
        items = self.adopted_items()
        if not items:
            return False
        if not self.output.text().strip():
            self._show_dialog(QMessageBox.warning, '选择保存目录', '请选择结果或备份保存目录。')
            return False
        output = Path(self.output.text()).expanduser().resolve()
        if self._output_contains_input(output):
            self._show_dialog(QMessageBox.warning, '选择保存目录',
                              '保存目录不能与输入目录相同或包含输入路径。请选择独立目录或输入目录中的子文件夹。')
            return False
        mode = self.save_mode.currentData() if mode is None else mode
        if mode not in ('export', 'replace'):
            raise ValueError('保存方式必须为导出或替换')
        if not self._platform_allows(mode):
            return False
        output_style = self.output_style.currentData()
        if mode == 'replace':
            if any(item.source.path.suffix.lower() != '.png' for item in items):
                self._show_dialog(QMessageBox.warning, '保存到原位置', '保存到原位置仅支持 PNG；其他格式请选择“导出副本”。')
                return False
            answer = self._show_dialog(QMessageBox.question, '保存到原位置',
                f'将先备份全部 {len(items)} 张原图，再将采用项的{OUTPUT_STYLES[output_style]}结果保存到原文件路径。\n'
                '图片尺寸保持不变。是否继续？',
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if answer != QMessageBox.Yes:
                return False
        directory = output / (
            ('备份-' if mode == 'replace' else '结果-') + datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
        batch = BatchResult(self.result_options, items)
        self._launch(mode, {'batch': batch, 'directory': directory, 'output_style': output_style})
        return self.worker is not None

    def choose_restore(self):
        self._tuning_timer.stop()
        report, _ = self._show_dialog(QFileDialog.getOpenFileName, '选择替换备份清单',
                                      self.output.text(), '替换清单 (replacement.json)')
        if report:
            self._start_restore(Path(report))

    def restore_last(self):
        if self._replacement_report:
            self._start_restore(self._replacement_report)

    def _start_restore(self, report):
        self._tuning_timer.stop()
        if self.worker:
            return
        answer = self._show_dialog(QMessageBox.question, '恢复原图', '按这份备份清单恢复原始图片，是否继续？',
                                      QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer == QMessageBox.Yes:
            self._launch('restore', {'report': report})

    def open_output(self):
        path = self._last_output or Path(self.output.text()).expanduser()
        if path.exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.resolve())))

    def _update_buttons(self, *_):
        if not hasattr(self, 'save_button'):
            return
        busy = self.worker is not None or self._closing
        correcting = self.worker is not None and self._task_operation == 'correct' and not self._closing
        editing_task = self.worker is not None and self._task_operation in ('process', 'correct')
        editable_settings = not self._closing and (self.worker is None or editing_task)
        for button in self.file_buttons:
            button.setEnabled(not busy)
        for widget in (self.root_list, self.scan_button):
            widget.setEnabled(not busy)
        self.output_group.setEnabled(editable_settings)
        self.process_all_button.setEnabled(not busy and bool(self.inputs))
        self.process_current_button.setEnabled(not busy and self.current_source() is not None)
        count = 0 if busy else len(self.adopted_items())
        self.save_button.setEnabled(not busy and count > 0)
        self.open_output_button.setEnabled(not self._closing and self._last_output is not None)
        adoption_locked = self._adoption_locked()
        self.preview.output_style_editable = not adoption_locked
        if adoption_locked != self._adoption_controls_locked:
            self._adoption_controls_locked = adoption_locked
            self._frozen_adoption = ({source.path: self.table.item(row, 0).checkState()
                                     for row, source in enumerate(self.inputs)} if adoption_locked else {})
            with QSignalBlocker(self.table):
                for row in range(len(self.inputs)):
                    self._update_adoption_cell(row)
        for button in self.adoption_buttons:
            button.setEnabled(not adoption_locked and bool(self.inputs))
        can_cancel_worker = (self.worker is not None
                             and self._task_operation not in ('restore', 'correct')
                             and not self.worker.payload.get('cancelled_by_user'))
        can_cancel_pending = self.worker is None and self._pending_preview_valid()
        self.cancel_button.setEnabled(not self._closing and (can_cancel_worker or can_cancel_pending))
        source = self.current_source()
        editable = (not busy and source is not None and source.path in self.records
                    and self._platform_allows('correct'))
        canvas_editable = editable and not self._preview_loading
        self.preview.mask_canvas.set_editable(canvas_editable)
        self.select_tool_button.setEnabled(not busy or correcting)
        self.brush_tool_button.setEnabled(not busy or correcting)
        self.brush_options.setEnabled((canvas_editable or (correcting and not self._preview_loading))
                                     and not self.preview.mask_canvas.image.isNull())
        self.clear_component_button.setEnabled(canvas_editable and self.preview.mask_canvas.tool == 'select'
                                               and self.preview.selection_count > 0)
        self._sync_history_panel(editable)

    def dragEnterEvent(self, event):
        if not self.worker and event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        paths = [url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()]
        if paths:
            self.import_paths(paths)
            event.acceptProposedAction()

    def closeEvent(self, event):
        if self._platform_services is not None and not self._platform_services.before_close(event):
            return
        self._discard_pending_preview(cancel_running=False)
        if self.worker or self._preview_workers:
            self._closing = True
            if self.worker and self._task_operation != 'restore':
                self.worker.requestInterruption()
            self._cancel_previews()
            self.status.setText('正在结束任务并保存恢复状态…')
            self._update_buttons()
            event.ignore()
            return
        self._closing = True
        self._cache.cleanup()
        event.accept()

    def _finish_close(self):
        if self._closing and self.worker is None and not self._preview_workers:
            QTimer.singleShot(0, self.close)


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    paths = []
    if args:
        if args[0] == '--examples':
            paths = [PROJECT_ROOT / 'output/client-examples/input']
        else:
            paths = args
    app = QApplication.instance() or QApplication([sys.argv[0]])
    app.setApplicationName(APP_NAME)
    app.setOrganizationName('HanZi')
    app.setStyle('Fusion')
    # Local source runs may opt out of account services for development.
    # Frozen release builds always require the normal authenticated flow.
    local_dev = (os.environ.get('HANZI_MASK_LOCAL_DEV') == '1'
                 and not getattr(sys, 'frozen', False))
    if local_dev:
        session = None
    else:
        from .platform_integration import start_platform_session, attach_platform_services
        session = start_platform_session(app)
        if session is None:
            return 0
    window = MainWindow(initial_paths=paths)
    if not local_dev:
        attach_platform_services(window, session)
    window.show()
    return app.exec()
