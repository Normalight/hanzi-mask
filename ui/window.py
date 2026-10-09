"""Standalone texture client: import, preview, review and batch publication."""

from datetime import datetime
from pathlib import Path
import sys
import tempfile

from PySide6.QtCore import QSignalBlocker, Qt, QTimer, QUrl
from PySide6.QtGui import QAction, QDesktopServices, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox,
    QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QListWidget, QMainWindow, QMessageBox, QProgressBar,
    QPushButton, QScrollArea, QSlider, QSplitter, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from hanzi_mask import __version__
from hanzi_mask.batch_service import BatchResult, SaveSummary
from hanzi_mask.processing import Options, Result
from hanzi_mask.texture import TEXTURE_PROFILES
from .canvas import PreviewPanel
from .task_worker import PreviewWorker, TaskWorker


PROJECT_ROOT = Path(__file__).resolve().parents[1]
IMAGE_FILTER = '图片 (*.png *.jpg *.jpeg *.bmp *.tif *.tiff *.webp)'


class MainWindow(QMainWindow):
    def __init__(self, initial_paths=None, examples_root=None):
        super().__init__()
        self.setWindowTitle(f'字形替换 · {__version__}')
        self.resize(1400, 900)
        self.setAcceptDrops(True)
        self.roots = []
        self.inputs = []
        self._row_by_path = {}
        self.records = {}
        self.errors = {}
        self._correction_history = {}
        self._texture_strength = None
        self.result_options = Options()
        self.worker = None
        self._task_operation = None
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
        toolbar = self.addToolBar('文件')
        toolbar.setMovable(False)
        for title, callback, shortcut in (
            ('添加文件夹…', self.add_folder, 'Ctrl+O'),
            ('添加图片…', self.add_files, 'Ctrl+Shift+O'),
            ('加载示例', self.load_examples, None),
            ('清空列表', self.clear_inputs, None),
        ):
            action = QAction(title, self)
            action.triggered.connect(callback)
            if shortcut:
                action.setShortcut(QKeySequence(shortcut))
            toolbar.addAction(action)
        self.file_actions = list(toolbar.actions())
        toolbar.addSeparator()
        self.restore_action = QAction('恢复替换…', self)
        self.restore_action.triggered.connect(self.choose_restore)
        toolbar.addAction(self.restore_action)

        split = QSplitter(Qt.Horizontal)
        split.setChildrenCollapsible(False)
        self.sidebar = QWidget()
        side = QVBoxLayout(self.sidebar)
        heading = QLabel('批量字形替换')
        heading.setStyleSheet('font-size: 20px; font-weight: 600;')
        side.addWidget(heading)
        sub = QLabel('纹理去噪 · 黑底白字')
        sub.setStyleSheet('color: #6b6b6b;')
        side.addWidget(sub)

        sources = QGroupBox('输入图片')
        source_layout = QVBoxLayout(sources)
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

        self.parameters = QGroupBox('纹理去噪')
        form = QFormLayout(self.parameters)
        strength_row = QWidget()
        strength_layout = QHBoxLayout(strength_row)
        strength_layout.setContentsMargins(0, 0, 0, 0)
        self.strength_slider = QSlider(Qt.Horizontal)
        self.strength_slider.setRange(0, 50)
        self.strength_slider.setToolTip('向左保留更多细节，向右去除更多纹理；0 关闭纹理平滑')
        self.strength_value = QLabel()
        self.strength_value.setMinimumWidth(64)
        strength_layout.addWidget(self.strength_slider, 1)
        strength_layout.addWidget(self.strength_value)
        form.addRow('强度', strength_row)
        self.strength_reset = QPushButton('恢复默认强度')
        self.strength_reset.clicked.connect(self._reset_strength)
        form.addRow(self.strength_reset)
        self.live_preview = QCheckBox('调节强度后处理当前图')
        self.live_preview.setChecked(True)
        self.live_preview.toggled.connect(lambda checked: None if checked else self._tuning_timer.stop())
        form.addRow(self.live_preview)
        side.addWidget(self.parameters)

        self.strength_slider.valueChanged.connect(self._strength_slider_changed)
        self.strength_slider.sliderReleased.connect(self._schedule_tuned_preview)
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
        self.save_mode.addItem('导出副本', 'export')
        self.save_mode.addItem('原位替换 PNG · 自动备份', 'replace')
        self.save_mode.currentIndexChanged.connect(self._save_mode_changed)
        output_form.addRow('保存方式', self.save_mode)
        output_row = QWidget()
        line = QHBoxLayout(output_row)
        line.setContentsMargins(0, 0, 0, 0)
        self.output = QLineEdit(str(PROJECT_ROOT / 'output/字形替换'))
        self.output.setMinimumWidth(80)
        choose_output = QPushButton('…')
        choose_output.setMaximumWidth(32)
        choose_output.clicked.connect(self.choose_output)
        line.addWidget(self.output, 1)
        line.addWidget(choose_output)
        output_form.addRow('保存目录', output_row)
        self.save_note = QLabel('按输入目录结构导出 PNG，保持原图尺寸。')
        self.save_note.setWordWrap(True)
        output_form.addRow(self.save_note)
        self.save_button = QPushButton('导出采用项')
        self.save_button.clicked.connect(self.save_results)
        output_form.addRow(self.save_button)
        side.addWidget(self.output_group)
        self.open_output_button = QPushButton('打开结果目录')
        self.open_output_button.clicked.connect(self.open_output)
        side.addWidget(self.open_output_button)
        self.restore_last_button = QPushButton('恢复上次替换')
        self.restore_last_button.clicked.connect(self.restore_last)
        side.addWidget(self.restore_last_button)
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
        for title, checked in [('全部采用', True), ('全部排除', False)]:
            button = QPushButton(title)
            button.clicked.connect(lambda _=False, value=checked: self.check_all(value))
            filter_line.addWidget(button)
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
        self.table.itemChanged.connect(self._update_buttons)
        table_layout.addWidget(self.table, 1)
        self.selection_note = QLabel('勾选“采用”决定保存哪些结果；点击中间色块后可清理，支持撤销。')
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
        self._update_buttons()

    def processing_options(self):
        return Options(method='texture', profile='balanced', texture_strength=self._texture_strength)

    def _sync_strength(self):
        value = self._texture_strength
        automatic = value is None
        if automatic:
            value = TEXTURE_PROFILES['balanced'][0]
        with QSignalBlocker(self.strength_slider):
            self.strength_slider.setValue(round(value * 1000))
        self.strength_value.setText(('默认 ' if automatic else '') + f'{round(value / .05 * 100)}%')

    def _reset_strength(self):
        self._texture_strength = None
        self._sync_strength()
        self.parameters_changed()
        self._schedule_tuned_preview()

    def _strength_slider_changed(self, value):
        self._texture_strength = value / 1000
        self._sync_strength()
        self.parameters_changed()
        self._schedule_tuned_preview()

    def _schedule_tuned_preview(self):
        if self.live_preview.isChecked() and not self.worker and not self._closing:
            self._tuning_timer.start()

    def _preview_tuned(self):
        if not self.live_preview.isChecked():
            return
        if self.strength_slider.isSliderDown():
            self._tuning_timer.start()
        elif not self.worker and not self._closing:
            self.process_current()

    def add_folder(self):
        path = QFileDialog.getExistingDirectory(self, '添加单字图片文件夹')
        if path:
            self.import_paths([path])

    def add_files(self):
        paths, _ = QFileDialog.getOpenFileNames(self, '添加单字图片', '', IMAGE_FILTER)
        if paths:
            self.import_paths(paths)

    def import_paths(self, paths, append=True):
        if self.worker or self._closing:
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
        if not self.examples_root.is_dir():
            QMessageBox.information(self, '加载示例', '当前没有可用示例，请选择示例图片文件夹。')
            self.add_folder()
            return
        self.recursive.blockSignals(True)
        self.recursive.setChecked(True)
        self.recursive.blockSignals(False)
        self.import_paths([self.examples_root], append=False)

    def clear_inputs(self):
        if self.worker:
            return
        self._tuning_timer.stop()
        self.roots.clear()
        self.root_list.clear()
        self.inputs.clear()
        self._row_by_path.clear()
        self.records.clear()
        self.errors.clear()
        self._correction_history.clear()
        self.table.setRowCount(0)
        self._cancel_previews()
        self.preview.clear()
        self.current_name.setText('选择一张图片查看')
        self.input_count.setText('添加图片或加载示例开始')
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
        self._launch('scan', {'paths': self.roots, 'recursive': self.recursive.isChecked(),
                              'exclude_paths': excluded})

    def _populate(self):
        self.table.blockSignals(True)
        self._row_by_path = {source.path: row for row, source in enumerate(self.inputs)}
        self.table.setRowCount(len(self.inputs))
        for row, source in enumerate(self.inputs):
            check = QTableWidgetItem()
            check.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable | Qt.ItemIsSelectable)
            check.setCheckState(Qt.Unchecked)
            self.table.setItem(row, 0, check)
            name = QTableWidgetItem(source.relative_path.as_posix())
            name.setToolTip(str(source.path))
            self.table.setItem(row, 1, name)
            for column, value in ((2, '—'), (3, '未处理'), (4, '')):
                self.table.setItem(row, column, QTableWidgetItem(value))
        self.table.blockSignals(False)
        self.input_count.setText(f'{len(self.inputs)} 张图片 · {len(self.roots)} 个输入路径')
        self.filter_rows()
        if self.inputs:
            self.table.selectRow(0)
        else:
            self.preview.clear()
            self.current_name.setText('未找到支持的图片')

    def parameters_changed(self, *_):
        if self.worker or not (self.records or self.errors):
            return
        self._invalidate_results('参数已修改')
        self.status.setText('参数已修改，请重新处理图片')

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
            self._start_processing([source])

    def process_all(self):
        self._start_processing(self.inputs)

    def _start_processing(self, sources):
        if not sources or self.worker:
            return
        self.result_options = self.processing_options()
        for source in sources:
            self.records.pop(source.path, None)
            self.errors.pop(source.path, None)
            self._correction_history.pop(source.path, None)
            self._set_row(source.path, '处理中', '', checked=False)
        current = self.current_source()
        if current and any(source.path == current.path for source in sources):
            self.preview_selection()
        cache = self._cache_root / f'run-{datetime.now().strftime("%Y%m%d-%H%M%S-%f")}'
        self._launch('process', {'inputs': list(sources), 'options': self.result_options, 'cache_dir': cache})

    def _launch(self, operation, payload):
        if self.worker or self._closing:
            return
        self._tuning_timer.stop()
        self._task_operation = operation
        worker = TaskWorker(operation, payload, self)
        self.worker = worker
        worker.progress.connect(self._on_progress)
        worker.item_ready.connect(self._on_item)
        worker.completed.connect(self._on_completed)
        worker.failed.connect(self._on_error)
        worker.finished.connect(self._on_finished)
        self.progress.setRange(0, 0)
        self.status.setText({'scan': '正在扫描图片…', 'process': '正在处理图片…',
                             'export': '正在导出结果…', 'replace': '正在备份并替换…',
                             'restore': '正在恢复原始图片…', 'correct': '正在保存人工矫正…'}[operation])
        self._update_buttons()
        worker.start()

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

    def _on_item(self, item):
        self.records[item.source.path] = item
        notes = [reason.split('；可用 --keep-isolated')[0]
                 for reason in item.metrics.get('review_reasons', [])]
        state = '已矫正' if item.metrics.get('manual_correction') else ('待复核' if notes else '已处理')
        self._set_row(item.source.path, state, '；'.join(notes),
                      checked=item.metrics.get('foreground_pixels', 0) > 0, metrics=item.metrics)
        current = self.current_source()
        if current and current.path == item.source.path:
            self.preview_selection()

    def _on_completed(self, result):
        operation = self._task_operation
        if operation == 'scan':
            if result is None:
                self.status.setText('已取消扫描')
                return
            self._cancel_previews()
            self.records.clear()
            self.errors.clear()
            self._correction_history.clear()
            self.inputs = result
            self._populate()
            self.status.setText(f'扫描完成：{len(result)} 张图片')
        elif operation == 'process':
            for error in result.errors:
                self.errors[error.path] = error.error
                self._set_row(error.path, '失败', error.error, checked=False)
            for source in self.inputs:
                if source.path not in self.records and source.path not in self.errors:
                    self._set_row(source.path, '未处理', '', checked=False)
            state = '已取消' if result.cancelled else '处理完成'
            self.status.setText(f'{state}：{len(result.items)} 张成功，{len(result.errors)} 张失败；请复核后保存')
            self.filter_rows()
            self.preview_selection()
        elif operation == 'correct':
            previous = self.worker.payload['item']
            history = self._correction_history.setdefault(previous.source.path, [])
            history.append((previous, self.worker.payload['adopted']))
            del history[:-20]
            self._on_item(result)
            self._set_row(result.source.path, '已矫正', '已人工清理色块；可撤销',
                          checked=self.worker.payload['adopted'] and result.metrics['foreground_pixels'] > 0)
            self.status.setText('已清理选中色块，保存时应用当前矫正结果')
        elif isinstance(result, SaveSummary):
            self._last_output = result.output_dir
            if operation == 'replace':
                self._replacement_report = result.report_path
            if operation in ('replace', 'restore'):
                self._invalidate_results('已恢复原图' if operation == 'restore' else
                                         ('替换已取消，原图已回滚' if result.cancelled else '已替换，备份可恢复'))
            verb = {'export': '导出', 'replace': '替换', 'restore': '恢复'}[operation]
            self.status.setText(f'{verb}{"已取消" if result.cancelled else "完成"}：{len(result.outputs)} 张成功，'
                                f'{len(result.errors)} 张失败 · {result.output_dir}')
            if result.errors and not self._closing:
                details = '\n'.join(f'{error.path.name}：{error.error}' for error in result.errors[:5])
                QMessageBox.warning(self, '部分结果保存失败', f'{len(result.errors)} 张保存失败，成功结果已保留。\n'
                                    f'{details}\n完整记录：{result.report_path}')
        self._update_buttons()

    def _on_error(self, message, report=None):
        if report:
            self._replacement_report = Path(report)
            self._last_output = Path(report).parent
        if self._task_operation in ('replace', 'restore'):
            self._invalidate_results('请检查恢复清单并重新处理')
        elif self._task_operation == 'process':
            for source in self.inputs:
                if source.path not in self.records:
                    self._set_row(source.path, '未处理', message, checked=False)
        elif self._task_operation == 'correct':
            self.preview_selection()
        self.status.setText('失败：' + message)
        if not self._closing:
            QMessageBox.warning(self, '任务失败', message)

    def _on_finished(self):
        worker = self.worker
        self.worker = None
        if worker:
            worker.deleteLater()
        if self.progress.maximum() == 0:
            self.progress.setRange(0, 1)
            self.progress.setValue(0)
        self._update_buttons()
        self._finish_close()

    def cancel_task(self):
        self._tuning_timer.stop()
        self.preview.clear_selection()
        if self.worker and self._task_operation != 'restore':
            self.worker.requestInterruption()
            self.cancel_button.setEnabled(False)
            self.status.setText('正在结束当前图片或回滚替换，请稍候…')

    def _cancel_previews(self):
        self._preview_request += 1
        for worker in self._preview_workers:
            worker.requestInterruption()

    def preview_selection(self):
        self._tuning_timer.stop()
        source = self.current_source()
        self._cancel_previews()
        if source is None or self._closing:
            self.preview.clear()
            return
        self.current_name.setText(source.relative_path.as_posix())
        self.current_name.setToolTip(str(source.path))
        self.preview.clear()
        request = self._preview_request
        worker = PreviewWorker(request, source.path, self.records.get(source.path),
                               self.processing_options().foreground, self)
        self._preview_workers.add(worker)
        worker.completed.connect(self._preview_completed)
        worker.failed.connect(self._preview_failed)
        worker.finished.connect(lambda current=worker: self._preview_finished(current))
        worker.start()
        self._update_buttons()

    def _preview_completed(self, request, result):
        if request != self._preview_request or self._closing:
            return
        if isinstance(result, Result):
            self.preview.set_images(result.original, result.mask, result.candidate)
        else:
            self.preview.set_images(result, None, None)

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

    def _preview_finished(self, worker):
        self._preview_workers.discard(worker)
        worker.deleteLater()
        self._finish_close()

    def _selection_changed(self, pixels):
        self.correction_note.setText(f'已选中 {pixels} 个像素 · 可清理或按 Delete' if pixels else
                                     '点击结果中的色块选中，清理多余碎斑')
        self._update_buttons()

    def clear_selected_component(self):
        source = self.current_source()
        if self.worker or self._closing or source is None or source.path not in self.records:
            return
        mask = self.preview.remove_selected()
        if mask is None:
            return
        self._launch('correct', {'item': self.records[source.path], 'mask': mask,
                                'cache_dir': self._cache_root / 'corrections',
                                'adopted': self.table.item(self.table.currentRow(), 0).checkState() == Qt.Checked})

    def undo_correction(self):
        source = self.current_source()
        if self.worker or self._closing or source is None:
            return
        history = self._correction_history.get(source.path, [])
        if not history:
            return
        previous, adopted = history.pop()
        self._on_item(previous)
        state = '已矫正' if previous.metrics.get('manual_correction') else (
            '待复核' if previous.metrics.get('review_reasons') else '已处理')
        self._set_row(source.path, state, '已撤销清理', checked=adopted)
        self.status.setText('已撤销当前图片的上一步清理')
        self._update_buttons()

    def filter_rows(self, *_):
        search = self.search.text().strip().casefold()
        selected_state = self.state_filter.currentText()
        for row, source in enumerate(self.inputs):
            state = self.table.item(row, 3).text()
            matches = (not search or search in str(source.path).casefold())
            if selected_state != '全部图片':
                matches = matches and state == selected_state
            self.table.setRowHidden(row, not matches)
        if self.current_source() is None:
            visible = next((row for row in range(len(self.inputs)) if not self.table.isRowHidden(row)), None)
            if visible is None:
                self.table.clearSelection()
                self.table.setCurrentCell(-1, -1)
                self._cancel_previews()
                self.preview.clear()
                self.current_name.setText('没有符合筛选条件的图片')
            else:
                self.table.selectRow(visible)
        self._update_buttons()

    def check_all(self, checked):
        if self.worker and self._task_operation in ('export', 'replace', 'restore'):
            return
        with QSignalBlocker(self.table):
            for row, source in enumerate(self.inputs):
                if source.path in self.records and not self.table.isRowHidden(row):
                    self.table.item(row, 0).setCheckState(Qt.Checked if checked else Qt.Unchecked)
        self._update_buttons()

    def adopted_items(self):
        return [self.records[source.path] for row, source in enumerate(self.inputs)
                if source.path in self.records and self.table.item(row, 0).checkState() == Qt.Checked]

    def choose_output(self):
        path = QFileDialog.getExistingDirectory(self, '选择结果或备份保存目录', self.output.text())
        if path:
            self.output.setText(path)

    def _save_mode_changed(self):
        replace = self.save_mode.currentData() == 'replace'
        self.save_button.setText('备份并替换采用项' if replace else '导出采用项')
        self.save_note.setText('仅替换 PNG；全部原图备份后再保存，可恢复。' if replace else
                               '按输入目录结构导出 PNG，保持原图尺寸。')

    def _output_contains_input(self, output):
        paths = self.roots or [source.path for source in self.inputs]
        return any(path == output or output in path.parents for path in paths)

    def save_results(self):
        if self.worker:
            return
        items = self.adopted_items()
        if not items:
            return
        if not self.output.text().strip():
            QMessageBox.warning(self, '选择保存目录', '请选择结果或备份保存目录。')
            return
        output = Path(self.output.text()).expanduser().resolve()
        if self._output_contains_input(output):
            QMessageBox.warning(self, '选择保存目录',
                                '保存目录不能与输入目录相同或包含输入路径。请选择独立目录或输入目录中的子文件夹。')
            return
        mode = self.save_mode.currentData()
        if mode == 'replace':
            if any(item.source.path.suffix.lower() != '.png' for item in items):
                QMessageBox.warning(self, '原位替换', '原位替换仅支持 PNG；请将其他格式导出为 PNG 副本。')
                return
            answer = QMessageBox.question(self, '备份并替换字形',
                f'将先备份全部 {len(items)} 张原图，再将采用项原位替换为黑底白字。\n是否继续？',
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if answer != QMessageBox.Yes:
                return
        directory = output / (
            ('备份-' if mode == 'replace' else '结果-') + datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
        batch = BatchResult(self.result_options, items)
        self._launch(mode, {'batch': batch, 'directory': directory})

    def choose_restore(self):
        report, _ = QFileDialog.getOpenFileName(self, '选择替换备份清单', self.output.text(), '替换清单 (replacement.json)')
        if report:
            self._start_restore(Path(report))

    def restore_last(self):
        if self._replacement_report:
            self._start_restore(self._replacement_report)

    def _start_restore(self, report):
        if self.worker:
            return
        answer = QMessageBox.question(self, '恢复原图', '按这份备份清单恢复原始图片，是否继续？',
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
        for action in self.file_actions:
            action.setEnabled(not busy)
        self.restore_action.setEnabled(not busy)
        for widget in (self.root_list, self.recursive, self.scan_button,
                       self.parameters, self.output_group):
            widget.setEnabled(not busy)
        self.process_all_button.setEnabled(not busy and bool(self.inputs))
        self.process_current_button.setEnabled(not busy and self.current_source() is not None)
        count = 0 if busy else len(self.adopted_items())
        self.save_button.setEnabled(not busy and count > 0)
        self.open_output_button.setEnabled(not busy and self._last_output is not None)
        self.restore_last_button.setEnabled(not busy and self._replacement_report is not None)
        self.cancel_button.setEnabled(self.worker is not None and not self._closing and self._task_operation != 'restore')
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
        self._tuning_timer.stop()
        if self.worker or self._preview_workers:
            self._closing = True
            if self.worker and self._task_operation != 'restore':
                self.worker.requestInterruption()
            self._cancel_previews()
            self.status.setText('正在结束任务并保存恢复状态…')
            self._update_buttons()
            event.ignore()
            return
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
    app.setApplicationName('字形替换')
    app.setOrganizationName('HanZi')
    app.setStyle('Fusion')
    window = MainWindow(initial_paths=paths)
    window.show()
    return app.exec()
