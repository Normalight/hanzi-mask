"""Standalone texture client: import, preview, review and batch publication."""

from dataclasses import replace
from datetime import datetime
from pathlib import Path
import sys
import tempfile

from PySide6.QtCore import QSignalBlocker, Qt, QTimer, QUrl, Slot
from PySide6.QtGui import QDesktopServices, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDoubleSpinBox,
    QFileDialog, QFormLayout, QGridLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QListWidget, QMainWindow, QMessageBox, QProgressBar,
    QPushButton, QScrollArea, QSlider, QSpinBox, QSplitter, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from hanzi_mask import __version__
from hanzi_mask.batch_service import BatchResult, SaveSummary
from hanzi_mask.concurrency import worker_limits
from hanzi_mask.processing import Options, Result
from hanzi_mask.texture import TEXTURE_PROFILES
from .canvas import PreviewPanel
from .review_hints import review_hints
from .task_worker import PreviewWorker, TaskWorker


PROJECT_ROOT = Path(__file__).resolve().parents[1]
APP_NAME = '字形换底'
IMAGE_FILTER = '图片 (*.png *.jpg *.jpeg *.bmp *.tif *.tiff *.webp)'


class MainWindow(QMainWindow):
    def __init__(self, initial_paths=None, examples_root=None):
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
        self._texture_strength = None
        self._strength_input_editing = False
        self._strength_input_cancelled = False
        self._tuning_paused = 0
        self._tuning_generation = 0
        self._pending_preview = None
        self._selected_preview_path = None
        self._all_options = Options(method='texture', profile='balanced', keep_isolated=True,
                                    edge_refine=True, stroke_recovery=True, tip_recovery=True)
        self._draft_options = {}
        self._processing_snapshots = {}
        self._processing_successful = set()
        self._adoption_controls_locked = None
        self._frozen_adoption = {}
        self._worker_limits = worker_limits()
        self.result_options = self._all_options
        self.worker = None
        self._task_operation = None
        self._platform_services = None
        self._preview_workers = set()
        self._preview_request = 0
        self._closing = False
        self._last_output = None
        self._replacement_report = None
        self._cache = tempfile.TemporaryDirectory(prefix='hanzi-mask-desktop-')
        self._cache_root = Path(self._cache.name)
        self._tuning_timer = QTimer(self)
        self._tuning_timer.setSingleShot(True)
        self._tuning_timer.setInterval(350)
        self._tuning_timer.timeout.connect(self._preview_tuned)
        self.examples_root = Path(examples_root) if examples_root else PROJECT_ROOT / 'output/client-examples/input'
        self._build_ui()
        if initial_paths:
            QTimer.singleShot(0, lambda: self.import_paths(initial_paths))

    def _build_ui(self):
        split = QSplitter(Qt.Horizontal)
        split.setChildrenCollapsible(False)
        self.sidebar = QWidget()
        side = QVBoxLayout(self.sidebar)
        sources = QGroupBox('输入图片')
        source_layout = QVBoxLayout(sources)
        input_buttons = QGridLayout()
        self.file_buttons = []
        for index, (title, callback, shortcut) in enumerate((
            ('添加文件夹…', self.add_folder, 'Ctrl+O'),
            ('添加图片…', self.add_files, 'Ctrl+Shift+O'),
            ('加载示例', self.load_examples, None),
            ('清空列表', self.clear_inputs, None),
        )):
            button = QPushButton(title)
            button.clicked.connect(callback)
            if shortcut:
                button.setShortcut(QKeySequence(shortcut))
            input_buttons.addWidget(button, index // 2, index % 2)
            self.file_buttons.append(button)
        source_layout.addLayout(input_buttons)
        self.root_list = QListWidget()
        self.root_list.setMaximumHeight(95)
        self.root_list.setToolTip('可添加多个文件夹或直接拖入图片')
        source_layout.addWidget(self.root_list)
        self.recursive = QCheckBox('包含子文件夹')
        self.recursive.setChecked(True)
        self.recursive.toggled.connect(self.rescan)
        source_layout.addWidget(self.recursive)
        self.scan_button = QPushButton('重新扫描')
        self.scan_button.clicked.connect(self.rescan)
        source_layout.addWidget(self.scan_button)
        self.input_count = QLabel('添加图片或加载示例开始')
        self.input_count.setWordWrap(True)
        source_layout.addWidget(self.input_count)
        side.addWidget(sources)

        self.parameters = QGroupBox('处理设置')
        form = QFormLayout(self.parameters)
        self.application_scope = QComboBox()
        self.application_scope.addItem('当前图片', 'current')
        self.application_scope.addItem('全部图片', 'all')
        self.application_scope.setToolTip('当前图片：记住每张图的设置，可自动处理当前图。'
                                          '全部图片：设置统一参数，点击“批量处理全部”后应用。')
        form.addRow('应用范围', self.application_scope)
        strength_row = QWidget()
        strength_layout = QHBoxLayout(strength_row)
        strength_layout.setContentsMargins(0, 0, 0, 0)
        self.strength_slider = QSlider(Qt.Horizontal)
        self.strength_slider.setRange(0, 1000)
        self.strength_slider.setSingleStep(10)
        self.strength_slider.setPageStep(100)
        self.strength_slider.setToolTip('向左保留更多细节，向右去除更多纹理；0 关闭纹理平滑')
        self.strength_input = QDoubleSpinBox()
        self.strength_input.setRange(0, 100)
        self.strength_input.setDecimals(1)
        self.strength_input.setSingleStep(1)
        self.strength_input.setSuffix('%')
        self.strength_input.setKeyboardTracking(False)
        self.strength_input.setMinimumWidth(86)
        strength_layout.addWidget(self.strength_slider, 1)
        strength_layout.addWidget(self.strength_input)
        form.addRow('强度', strength_row)
        self.strength_reset = QPushButton('恢复默认强度')
        self.strength_reset.clicked.connect(self._reset_strength)
        form.addRow(self.strength_reset)
        self.keep_isolated = QCheckBox('保留独立点画')
        self.keep_isolated.setChecked(True)
        self.keep_isolated.setToolTip('保留通过宽度和面积筛选、但远离主体的区域；可能留下碎斑。'
                                     '重新处理时应用此设置，不会补回已被其他筛选删除的笔画。')
        form.addRow(self.keep_isolated)
        self.live_preview = QCheckBox('自动处理当前图片')
        self.live_preview.setToolTip('调节强度或切换“保留独立点画”后，自动处理当前图片；'
                                     '仅在“当前图片”范围生效。')
        self.live_preview.setChecked(True)
        self.live_preview.toggled.connect(self._live_preview_changed)
        form.addRow(self.live_preview)
        self.parameter_note = QLabel()
        self.parameter_note.setWordWrap(True)
        form.addRow(self.parameter_note)
        thread_row = QWidget()
        thread_layout = QHBoxLayout(thread_row)
        thread_layout.setContentsMargins(0, 0, 0, 0)
        self.thread_count = QSpinBox()
        self.thread_count.setRange(1, self._worker_limits.maximum)
        self.thread_count.setValue(self._worker_limits.recommended)
        self.thread_count.setMaximumWidth(72)
        self.thread_hint = QLabel(f'推荐 {self._worker_limits.recommended} · 最大 {self._worker_limits.maximum}')
        thread_layout.addWidget(self.thread_count)
        thread_layout.addWidget(self.thread_hint, 1)
        memory = self._worker_limits.total_memory_bytes
        memory_note = f'、{memory / (1024 ** 3):g} GiB 内存' if memory is not None else ''
        thread_row.setToolTip(f'本机 {self._worker_limits.logical_cpus} 个逻辑核心{memory_note}。'
                              f'当前应用最多支持 {self._worker_limits.maximum} 个计算线程；'
                              '增加线程会增加内存占用，未必更快。修改线程数会用于下一次处理，保留已有结果。')
        form.addRow('处理线程', thread_row)
        side.addWidget(self.parameters)

        self.strength_slider.valueChanged.connect(self._strength_slider_changed)
        self.strength_input.valueChanged.connect(self._strength_input_changed)
        self.strength_input.lineEdit().textEdited.connect(self._strength_input_edited)
        self.strength_input.editingFinished.connect(self._strength_input_finished)
        self.strength_slider.sliderReleased.connect(self._strength_slider_released)
        self.keep_isolated.toggled.connect(self._keep_isolated_changed)
        self.application_scope.currentIndexChanged.connect(self._scope_changed)
        self._sync_strength()

        self.process_current_button = QPushButton('处理当前图片')
        self.process_current_button.clicked.connect(self.process_current)
        side.addWidget(self.process_current_button)
        self.process_all_button = QPushButton('批量处理全部')
        self.process_all_button.setStyleSheet('font-weight: 600; padding: 8px;')
        self.process_all_button.clicked.connect(self.process_all)
        side.addWidget(self.process_all_button)

        self.output_group = QGroupBox('保存结果')
        output_form = QFormLayout(self.output_group)
        self.save_mode = QComboBox()
        self.save_mode.addItem('保存到原位置 · 自动备份', 'replace')
        self.save_mode.addItem('导出副本', 'export')
        self.save_mode.setToolTip('保存到原位置仅支持 PNG，先备份全部原图；其他格式请选择“导出副本”。')
        self.save_mode.currentIndexChanged.connect(self._save_mode_changed)
        output_form.addRow('保存方式', self.save_mode)
        output_row = QWidget()
        line = QHBoxLayout(output_row)
        line.setContentsMargins(0, 0, 0, 0)
        self.output = QLineEdit(str(PROJECT_ROOT / 'output' / APP_NAME))
        self.output.setMinimumWidth(80)
        choose_output = QPushButton('…')
        choose_output.setMaximumWidth(32)
        choose_output.clicked.connect(self.choose_output)
        line.addWidget(self.output, 1)
        line.addWidget(choose_output)
        self.output_label = QLabel()
        output_form.addRow(self.output_label, output_row)
        self.save_note = QLabel()
        self.save_note.setWordWrap(True)
        output_form.addRow(self.save_note)
        self.save_button = QPushButton()
        self.save_button.clicked.connect(self.save_results)
        output_form.addRow(self.save_button)
        self._save_mode_changed()
        side.addWidget(self.output_group)
        self.open_output_button = QPushButton('打开保存目录')
        self.open_output_button.setToolTip('打开最近一次导出结果或原图备份所在目录。')
        self.open_output_button.clicked.connect(self.open_output)
        side.addWidget(self.open_output_button)
        note = QLabel('处理结果为候选字形。保存前请检查细笔画、独立点画和残留碎斑。')
        note.setWordWrap(True)
        note.setStyleSheet('color: #696969; padding-top: 5px;')
        side.addWidget(note)
        side.addStretch()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setMinimumWidth(300)
        scroll.setWidget(self.sidebar)
        split.addWidget(scroll)

        workspace = QWidget()
        work = QVBoxLayout(workspace)
        preview_bar = QHBoxLayout()
        self.current_name = QLabel('选择一张图片查看')
        self.current_name.setStyleSheet('font-weight: 600;')
        self.current_name.setTextInteractionFlags(Qt.TextSelectableByMouse)
        preview_bar.addWidget(self.current_name, 1)
        self.adopt_current_button = QPushButton('采用当前图片')
        self.adopt_current_button.clicked.connect(self.toggle_current_adoption)
        preview_bar.addWidget(self.adopt_current_button)
        self.preview = PreviewPanel()
        self.preview.selected_pixels.connect(self._selection_changed)
        self.preview.clear_requested.connect(self.clear_selected_component)
        work.addLayout(preview_bar)
        correction_bar = QHBoxLayout()
        self.correction_note = QLabel('点击结果中的色块选中，清理多余碎斑')
        correction_bar.addWidget(self.correction_note, 1)
        self.clear_component_button = QPushButton('清理选中色块')
        self.clear_component_button.clicked.connect(self.clear_selected_component)
        correction_bar.addWidget(self.clear_component_button)
        self.undo_correction_button = QPushButton('撤销清理')
        self.undo_correction_button.clicked.connect(self.undo_correction)
        correction_bar.addWidget(self.undo_correction_button)
        work.addLayout(correction_bar)
        self.preview.setMinimumHeight(250)
        body = QSplitter(Qt.Vertical)
        body.setChildrenCollapsible(False)
        body.addWidget(self.preview)

        table_panel = QWidget()
        table_layout = QVBoxLayout(table_panel)
        table_layout.setContentsMargins(0, 0, 0, 0)
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
        self.selection_note = QLabel('使用“采用当前图片”或表格勾选决定保存结果；点击中间色块可清理，支持撤销。')
        self.selection_note.setStyleSheet('color: #696969;')
        table_layout.addWidget(self.selection_note)
        body.addWidget(table_panel)
        body.setStretchFactor(0, 3)
        body.setStretchFactor(1, 2)
        body.setSizes([470, 300])
        work.addWidget(body, 1)
        footer = QHBoxLayout()
        self.progress = QProgressBar()
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        footer.addWidget(self.progress, 1)
        self.cancel_button = QPushButton('取消任务')
        self.cancel_button.clicked.connect(self.cancel_task)
        footer.addWidget(self.cancel_button)
        work.addLayout(footer)
        split.addWidget(workspace)
        split.setStretchFactor(1, 1)
        split.setSizes([320, 1080])
        self.setCentralWidget(split)
        self.status = QLabel('就绪 · 可拖入图片或文件夹')
        self.statusBar().addWidget(self.status, 1)
        self.setStyleSheet('QPushButton:pressed { background: #737373; color: white; }')
        self.escape = QShortcut(QKeySequence('Escape'), self)
        self.escape.activated.connect(self.cancel_task)
        self._sync_scope_parameters()
        self._update_buttons()

    def processing_options(self):
        return Options(method='texture', profile='balanced', texture_strength=self._texture_strength,
                       keep_isolated=self.keep_isolated.isChecked(), edge_refine=True,
                       stroke_recovery=True, tip_recovery=True)

    def _platform_allows(self, operation):
        services = self._platform_services
        return services is None or services.permits(operation)

    def _options_for_image(self, path):
        if path in self._draft_options:
            return self._draft_options[path]
        item = self.records.get(path)
        return (item.options if item is not None and item.options is not None
                else self._all_options)

    def _sync_scope_parameters(self):
        self._strength_input_editing = False
        self._strength_input_cancelled = False
        source = self.current_source()
        options = (self._options_for_image(source.path)
                   if self.application_scope.currentData() == 'current' and source is not None
                   else self._all_options)
        self._texture_strength = options.texture_strength
        with QSignalBlocker(self.keep_isolated):
            self.keep_isolated.setChecked(options.keep_isolated)
        self._sync_strength()
        current_scope = self.application_scope.currentData() == 'current'
        self.live_preview.setEnabled(current_scope)
        self.process_all_button.setToolTip(
            '按各图保存的参数或草稿处理全部图片。' if current_scope else
            '使用当前统一参数重新处理全部图片。')
        self._update_parameter_note()

    def _update_parameter_note(self):
        if self.application_scope.currentData() == 'all':
            self.parameter_note.setText('批量处理全部将使用这组统一参数。')
            return
        source = self.current_source()
        item = self.records.get(source.path) if source is not None else None
        if item is None:
            note = '处理当前图片后应用这些参数。' if source else '这些设置将作为新图片的默认参数。'
        elif item.options == self.processing_options():
            note = '当前结果已使用这些参数。'
        else:
            note = '参数尚未应用，预览和保存继续使用已有结果。'
        self.parameter_note.setText(note)

    def _scope_changed(self, *_):
        self._discard_pending_preview()
        self._sync_scope_parameters()
        self.status.setText('可调整当前图片，其他图片保留原结果。'
                            if self.application_scope.currentData() == 'current' else
                            '调整统一参数后，点击“批量处理全部”应用。')

    def _sync_strength(self):
        value = self._texture_strength
        automatic = value is None
        if automatic:
            value = TEXTURE_PROFILES['balanced'][0]
        percent = round(value * 2000, 1)
        with QSignalBlocker(self.strength_slider), QSignalBlocker(self.strength_input):
            self.strength_slider.setValue(round(percent * 10))
            self.strength_input.setValue(percent)
        recommended = round(TEXTURE_PROFILES['balanced'][0] * 2000, 1)
        self.strength_input.setToolTip(
            f'输入 0–100% 的强度，支持一位小数；默认推荐 {recommended:g}%。'
            + ('当前使用默认推荐值。' if automatic else '输入后按回车或移开焦点应用。'))

    def _reset_strength(self):
        self._texture_strength = None
        self._sync_strength()
        self.parameters_changed()
        self._schedule_tuned_preview()

    def _strength_slider_changed(self, value):
        self._texture_strength = value / 20000
        self._sync_strength()
        self.parameters_changed()
        self._schedule_tuned_preview()

    def _strength_slider_released(self):
        # Release finishes an existing gesture; it must not recreate a cancelled request.
        if self._pending_preview_valid():
            self._pending_preview['ready'] = False
            self._tuning_timer.start()

    def _strength_input_changed(self, value):
        self._texture_strength = value / 2000
        self._sync_strength()
        self.parameters_changed()
        self._schedule_tuned_preview()

    def _strength_input_edited(self, *_):
        self._strength_input_editing = True
        self._strength_input_cancelled = False
        self._discard_pending_preview()

    def _strength_input_finished(self):
        edited = self._strength_input_editing
        cancelled = self._strength_input_cancelled
        self._strength_input_editing = False
        self._strength_input_cancelled = False
        if edited:
            if not cancelled:
                self._schedule_tuned_preview()
            self.filter_rows(preserve_editor=True)

    def _keep_isolated_changed(self, *_):
        self.parameters_changed()
        self._schedule_tuned_preview()

    def _schedule_tuned_preview(self):
        self._discard_pending_preview()
        if (self.application_scope.currentData() == 'current' and self.current_source() is not None
                and self.live_preview.isChecked() and not self._closing and not self._tuning_paused
                and (self.worker is None or self._task_operation == 'process')
                and not self._strength_input_editing and self._platform_allows('process')):
            self._pending_preview = {
                'generation': self._tuning_generation,
                'path': self.current_source().path,
                'options': self.processing_options(),
                'ready': False,
            }
            self._tuning_timer.start()

    def _preview_tuned(self):
        if not self._pending_preview_valid():
            return
        if self.strength_slider.isSliderDown():
            self._tuning_timer.start()
        else:
            self._pending_preview['ready'] = True
            self._run_pending_preview()

    def _live_preview_changed(self, checked):
        if not checked:
            self._discard_pending_preview()

    def _discard_pending_preview(self, cancel_running=True):
        self._tuning_timer.stop()
        self._pending_preview = None
        # Queued signals from the old automatic task must become stale immediately.
        self._tuning_generation += 1
        if (cancel_running and self.worker is not None and self._task_operation == 'process'
                and self.worker.payload.get('automatic')):
            self.worker.requestInterruption()

    def _pending_preview_valid(self):
        pending = self._pending_preview
        source = self.current_source()
        return (pending is not None and pending['generation'] == self._tuning_generation
                and source is not None and source.path == pending['path']
                and self.application_scope.currentData() == 'current'
                and self.live_preview.isChecked() and not self._closing
                and not self._tuning_paused and not self._strength_input_editing
                and self._platform_allows('process'))

    def _run_pending_preview(self):
        if (self.worker is not None or not self._pending_preview_valid()
                or not self._pending_preview['ready'] or self.strength_slider.isSliderDown()):
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

    def add_folder(self):
        self._tuning_timer.stop()
        path = self._show_dialog(QFileDialog.getExistingDirectory, '添加单字图片文件夹')
        if path:
            self.import_paths([path])

    def add_files(self):
        self._tuning_timer.stop()
        paths, _ = self._show_dialog(QFileDialog.getOpenFileNames, '添加单字图片', '', IMAGE_FILTER)
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
        self.rescan()

    def load_examples(self):
        self._tuning_timer.stop()
        if not self.examples_root.is_dir():
            self._show_dialog(QMessageBox.information, '加载示例', '当前没有可用示例，请选择示例图片文件夹。')
            self.add_folder()
            return
        self.recursive.blockSignals(True)
        self.recursive.setChecked(True)
        self.recursive.blockSignals(False)
        self.import_paths([self.examples_root], append=False)

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
        self._draft_options.clear()
        self._selected_preview_path = None
        self.table.setRowCount(0)
        self._cancel_previews()
        self.preview.clear()
        self.current_name.setText('选择一张图片查看')
        self.input_count.setText('添加图片或加载示例开始')
        self.status.setText('列表已清空')
        self._sync_scope_parameters()
        self._update_buttons()

    def rescan(self):
        if not self.roots or self.worker:
            return
        excluded = [self._cache_root]
        if self.output.text().strip():
            output = Path(self.output.text()).expanduser().resolve()
            if not self._output_contains_input(output):
                excluded.append(output)
        self._launch('scan', {'paths': self.roots, 'recursive': self.recursive.isChecked(),
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
        self.input_count.setText(f'{len(self.inputs)} 张图片 · {len(self.roots)} 个输入路径')
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

    def parameters_changed(self, *_):
        if (self._closing or not self._platform_allows('process')
                or (self.worker is not None and self._task_operation != 'process')):
            return
        source = self.current_source()
        options = self.processing_options()
        if self.application_scope.currentData() == 'current' and source is not None:
            self._draft_options[source.path] = options
            self.status.setText('已调整当前图片参数，可处理当前图片应用。')
        else:
            self._all_options = options
            self.status.setText('已调整统一参数，点击“批量处理全部”应用。'
                                if self.inputs else '已设置新图片的默认参数。')
        self._update_parameter_note()

    def _invalidate_results(self, note):
        self.records.clear()
        self.errors.clear()
        self._correction_history.clear()
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
        per_image = ({source.path: self._options_for_image(source.path) for source in self.inputs}
                     if self.application_scope.currentData() == 'current' else None)
        self._start_processing(self.inputs, options=self._all_options, per_image_options=per_image)

    def _row_snapshot(self, path):
        row = self._row_by_path[path]
        return {'record': self.records.get(path),
                'draft': self._draft_options.get(path),
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
                                 'cache_dir': cache, 'max_workers': self.thread_count.value(),
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
        self._sync_current_adoption()

    def _on_item(self, item):
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
            self.errors.pop(path, None)
            if (item.options is not None
                    and self._draft_options.get(path) == self._processing_snapshots[path]['draft']):
                self._draft_options[path] = item.options
        notes = review_hints(item.metrics, can_undo=bool(self._correction_history.get(item.source.path)))
        item = replace(item, metrics={**item.metrics, 'review_reasons': notes})
        self.records[item.source.path] = item
        state = '已矫正' if item.metrics.get('manual_correction') else ('待复核' if notes else '已处理')
        self._set_row(item.source.path, state, '；'.join(notes),
                      checked=adopted and item.metrics.get('foreground_pixels', 0) > 0, metrics=item.metrics)
        current = self.current_source()
        if current and current.path == item.source.path:
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
            self._draft_options = {path: options for path, options in self._draft_options.items() if path in by_path}
            self._correction_history = {
                path: [(replace(item, source=by_path[path]), adopted) for item, adopted in history]
                for path, history in self._correction_history.items() if path in by_path}
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
            self._on_item(result)
            notes = self.records[result.source.path].metrics['review_reasons']
            self._set_row(result.source.path, '已矫正', '；'.join(notes))
            self.status.setText('已清理选中色块，保存时应用当前矫正结果')
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
        self._update_parameter_note()
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
        if self._strength_input_editing:
            self._strength_input_cancelled = True
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
        if path != self._selected_preview_path:
            self._selected_preview_path = path
            self._discard_pending_preview()
            self._sync_scope_parameters()
        self._refresh_preview()

    def _refresh_preview(self):
        source = self.current_source()
        self._update_parameter_note()
        self._cancel_previews()
        if source is None or self._closing:
            self.preview.clear()
            return
        self.current_name.setText(source.relative_path.as_posix())
        self.current_name.setToolTip(str(source.path))
        self.preview.clear()
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

    @Slot(int, str)
    def _preview_failed(self, request, message):
        if request == self._preview_request and not self._closing:
            source = self.current_source()
            if source:
                self.records.pop(source.path, None)
                self._correction_history.pop(source.path, None)
                self.errors[source.path] = message
                self._set_row(source.path, '失败', message, checked=False)
            self.status.setText('预览失败：' + message)
            self._update_buttons()
            self._update_parameter_note()

    @Slot()
    def _preview_task_finished(self):
        worker = self.sender()
        if worker is not None:
            self._preview_finished(worker)

    def _preview_finished(self, worker):
        self._preview_workers.discard(worker)
        worker.deleteLater()
        self._finish_close()

    def _selection_changed(self, pixels):
        self.correction_note.setText(f'已选中 {pixels} 个像素 · 退格或 Delete 清理' if pixels else
                                     '点击结果中的色块选中，清理多余碎斑')
        self._update_buttons()

    def clear_selected_component(self):
        source = self.current_source()
        if (self.worker or self._closing or not self._platform_allows('correct')
                or source is None or source.path not in self.records):
            return
        mask = self.preview.remove_selected()
        if mask is None:
            return
        self._launch('correct', {'item': self.records[source.path], 'mask': mask,
                                'cache_dir': self._cache_root / 'corrections',
                                'adopted': self.table.item(self.table.currentRow(), 0).checkState() == Qt.Checked})

    def undo_correction(self):
        source = self.current_source()
        if self.worker or self._closing or source is None or not self._platform_allows('correct'):
            return
        history = self._correction_history.get(source.path, [])
        if not history:
            return
        self._discard_pending_preview()
        previous, adopted = history.pop()
        self._on_item(previous)
        notes = self.records[source.path].metrics['review_reasons']
        state = '已矫正' if previous.metrics.get('manual_correction') else (
            '待复核' if notes else '已处理')
        self._set_row(source.path, state, '；'.join(notes), checked=adopted and self._can_adopt(source.path))
        self.status.setText('已撤销当前图片的上一步清理')
        self._update_buttons()

    def filter_rows(self, *_, preserve_editor=False):
        search = self.search.text().strip().casefold()
        selected_state = self.state_filter.currentText()
        source = self.current_source()
        # Internal result updates must not switch the owner of an edit or queued request.
        held_path = (source.path if preserve_editor and source is not None
                     and (self._strength_input_editing or self._pending_preview_valid()) else None)
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

    def _sync_current_adoption(self):
        source = self.current_source()
        checked = (source is not None
                   and self.table.item(self.table.currentRow(), 0).checkState() == Qt.Checked)
        self.adopt_current_button.setText('取消采用当前图片' if checked else '采用当前图片')
        self.adopt_current_button.setEnabled(
            not self._adoption_locked() and source is not None
            and (checked or self._can_adopt(source.path)))
        self.adopt_current_button.setToolTip(
            '当前图片已采用，保存时将输出；点击取消采用。' if checked else
            '采用当前图片，保存时输出这个结果。' if source and self._can_adopt(source.path) else
            '请先处理图片；只有非空结果可以采用。')

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

    def toggle_current_adoption(self):
        source = self.current_source()
        if self._adoption_locked() or source is None or not self._platform_allows('correct'):
            return
        check = self.table.item(self.table.currentRow(), 0)
        checked = check.checkState() == Qt.Checked
        if checked or self._can_adopt(source.path):
            check.setCheckState(Qt.Unchecked if checked else Qt.Checked)

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
        self.save_note.setText('将采用的 PNG 保存到原文件路径，保持原图尺寸；先备份全部原图。' if replace else
                               '按输入目录结构导出 PNG，保持原图尺寸。')

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
        if mode == 'replace':
            if any(item.source.path.suffix.lower() != '.png' for item in items):
                self._show_dialog(QMessageBox.warning, '保存到原位置', '保存到原位置仅支持 PNG；其他格式请选择“导出副本”。')
                return False
            answer = self._show_dialog(QMessageBox.question, '保存到原位置',
                f'将先备份全部 {len(items)} 张原图，再将采用项的黑底白字结果保存到原文件路径。\n'
                '图片尺寸保持不变。是否继续？',
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if answer != QMessageBox.Yes:
                return False
        directory = output / (
            ('备份-' if mode == 'replace' else '结果-') + datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
        batch = BatchResult(self.result_options, items)
        self._launch(mode, {'batch': batch, 'directory': directory})
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
        processing = self.worker is not None and self._task_operation == 'process'
        editable_settings = not self._closing and (self.worker is None or processing)
        for button in self.file_buttons:
            button.setEnabled(not busy)
        for widget in (self.root_list, self.recursive, self.scan_button):
            widget.setEnabled(not busy)
        self.parameters.setEnabled(editable_settings)
        self.output_group.setEnabled(editable_settings)
        self.process_all_button.setEnabled(not busy and bool(self.inputs))
        self.process_current_button.setEnabled(not busy and self.current_source() is not None)
        count = 0 if busy else len(self.adopted_items())
        self.save_button.setEnabled(not busy and count > 0)
        self.open_output_button.setEnabled(not self._closing and self._last_output is not None)
        adoption_locked = self._adoption_locked()
        if adoption_locked != self._adoption_controls_locked:
            self._adoption_controls_locked = adoption_locked
            self._frozen_adoption = ({source.path: self.table.item(row, 0).checkState()
                                     for row, source in enumerate(self.inputs)} if adoption_locked else {})
            with QSignalBlocker(self.table):
                for row in range(len(self.inputs)):
                    self._update_adoption_cell(row)
        for button in self.adoption_buttons:
            button.setEnabled(not adoption_locked and bool(self.inputs))
        self._sync_current_adoption()
        self.cancel_button.setEnabled(self.worker is not None and not self._closing
                                      and self._task_operation != 'restore'
                                      and not self.worker.payload.get('cancelled_by_user'))
        source = self.current_source()
        editable = not busy and source is not None and source.path in self.records
        self.clear_component_button.setEnabled(editable and self.preview.selection_count > 0)
        self.undo_correction_button.setEnabled(editable and bool(self._correction_history.get(source.path)))

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
    # 完成 hanzi-mask 的共享包与平台产品登记后，成组取消以下启动注释。
    # from .platform_integration import start_platform_session, attach_platform_services
    # session = start_platform_session(app)
    # if session is None:
    #     return 0
    window = MainWindow(initial_paths=paths)
    # attach_platform_services(window, session)
    window.show()
    return app.exec()
