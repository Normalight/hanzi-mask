"""Optional platform bridge lifecycle using fake SDKs and temporary work.

No login, session transport, native update engine, or real input is used.
"""

import os
from pathlib import Path
import subprocess
import sys
from types import ModuleType, SimpleNamespace

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest

pytest.importorskip('PySide6')
from PySide6.QtCore import QCoreApplication, QEvent, QObject
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QComboBox, QLabel, QMainWindow, QMessageBox, QPushButton, QWidget

from hanzi_mask.batch_service import ProcessingError, SaveSummary
from ui.platform_integration import (
    PRODUCT, PlatformServices, attach_platform_services, start_platform_session,
)
from test_desktop import (
    click_mask_pixel, idle, import_and_process, qapp, wait_for, window,
    write_rubbing, write_two_components,
)
from test_desktop_tuning import ControlledProcessing, expire_tuning
import ui.task_worker as worker_module


class FakeWorker:
    def __init__(self):
        self.interruptions = 0
        self.payload = {}

    def requestInterruption(self):
        self.interruptions += 1


class FakePreview:
    def __init__(self):
        self.selection = 12

    def clear_selection(self):
        self.selection = 0


class FakeGuard:
    def __init__(self):
        self.locked = False

    def check_native_update(self, *args):
        raise AssertionError('测试不能调用更新前置网络检查')


class FakeUpdater(QObject):
    """Model only the native adapter's close probe, with no backend/network."""
    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.restart_requested = False
        self.preparing = False
        self.approved = False
        self.window.installEventFilter(self)

    def eventFilter(self, watched, event):
        if self.preparing and not self.restart_requested:
            # The real adapter's preparation timer observes this cancellation.
            self.preparing = False
        if watched is self.window and event.type() == QEvent.Close and self.preparing:
            self.window.closeEvent(event)
            if event.isAccepted():
                self.approved = True
                self.preparing = False
            event.ignore()
            return True
        return False


class FakeWindow(QMainWindow):
    def __init__(self, directory):
        super().__init__()
        self.setCentralWidget(QWidget())
        self.status = QLabel()
        self.check_updates_button = QPushButton('检查更新')
        self.preview = FakePreview()
        self.worker = None
        self._task_operation = None
        self._preview_workers = set()
        self._closing = False
        self._platform_services = None
        self.records = {'glyph': object()}
        self._correction_history = {'glyph': [('previous', True)]}
        self.items = [object()]
        self.pending_preview = object()
        self.discarded = 0
        self.discard_modes = []
        self.save_modes = []
        self.save_mode = 'replace'
        self.output_style = QComboBox(self)
        self.output_style.addItem('白底黑字', 'black-on-white')
        self.output_style.addItem('黑底白字', 'white-on-black')
        self.fail_save_start = False
        self.close_calls = 0
        self.cleanup_calls = 0
        self.cache_file = directory / 'candidate.png'
        self.cache_file.write_bytes(b'temporary candidate bytes')

    def _discard_pending_preview(self, cancel_running=True):
        self.discarded += 1
        self.discard_modes.append(cancel_running)
        self.pending_preview = None
        if (cancel_running and self.worker is not None
                and self._task_operation == 'process' and self.worker.payload.get('automatic')):
            self.worker.requestInterruption()

    def adopted_items(self):
        return list(self.items)

    def save_results(self, *, mode=None):
        mode = self.save_mode if mode is None else mode
        self.save_modes.append(mode)
        if self.fail_save_start or not self.items:
            return False
        assert self.worker is None
        self.worker = FakeWorker()
        self._task_operation = mode
        return True

    def close(self):
        self.close_calls += 1
        return super().close()

    def closeEvent(self, event):
        if self._platform_services and not self._platform_services.before_close(event):
            return
        self.cleanup_calls += 1
        self.cache_file.unlink(missing_ok=True)
        event.accept()


@pytest.fixture
def bridge(qapp, tmp_path):
    window = FakeWindow(tmp_path)
    services = PlatformServices(window)
    services.guard = FakeGuard()
    services.updater = FakeUpdater(window)
    window._platform_services = services
    window._update_controller = services.updater
    window.show()
    qapp.processEvents()
    yield window, services
    window._platform_services = None
    services.updater.preparing = False
    window.close()
    window.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    qapp.processEvents()


def summary(window, *, cancelled=False, errors=False):
    return SaveSummary(window.cache_file.parent / 'export',
                       window.cache_file.parent / 'export/report.json',
                       outputs=[window.cache_file.parent / 'export/glyph.png'],
                       cancelled=cancelled,
                       errors=[ProcessingError(Path('glyph.png'), '模拟保存失败')] if errors else [])


def request_update(services):
    services.updater.restart_requested = True
    services.updater.preparing = True


def finish_export(window, services, result=None):
    if result is not None:
        services.on_saved(result)
    window.worker = None
    services.on_task_finished('export')


def test_import_does_not_load_shared_sdks_in_clean_process():
    script = """
import sys
import ui.platform_integration
assert not any(name == 'hanzi_auth' or name.startswith('hanzi_auth.') for name in sys.modules)
assert not any(name == 'hanzi_update' or name.startswith('hanzi_update.') for name in sys.modules)
"""
    project = Path(__file__).resolve().parents[1]
    result = subprocess.run([sys.executable, '-c', script], cwd=project,
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr


def test_start_session_uses_independent_product_via_lazy_import(monkeypatch):
    calls = []
    module = ModuleType('hanzi_auth')
    expected = SimpleNamespace(product=PRODUCT)
    module.start_session = lambda app, product: calls.append((app, product)) or expected
    monkeypatch.setitem(sys.modules, 'hanzi_auth', module)
    app = object()
    assert start_platform_session(app) is expected
    assert calls == [(app, 'hanzi-mask')]


def test_start_session_keeps_cancelled_or_secondary_instance_result(monkeypatch):
    module = ModuleType('hanzi_auth')
    module.start_session = lambda *args: None
    monkeypatch.setitem(sys.modules, 'hanzi_auth', module)
    assert start_platform_session(object()) is None


def test_attach_services_uses_save_lock_and_update_precheck_callbacks(bridge, monkeypatch):
    window, _ = bridge
    auth, update = ModuleType('hanzi_auth'), ModuleType('hanzi_update')
    guard = FakeGuard()
    updater = FakeUpdater(window)
    calls = []

    def protect(host, session, save, lock):
        calls.append(('protect', host, session, save, lock))
        return guard

    def attach(host, product, **kwargs):
        calls.append(('update', host, product, kwargs))
        return updater

    auth.protect_window, update.attach_updates = protect, attach
    monkeypatch.setitem(sys.modules, 'hanzi_auth', auth)
    monkeypatch.setitem(sys.modules, 'hanzi_update', update)
    session = SimpleNamespace(product=PRODUCT)
    services = attach_platform_services(window, session)
    assert services is window._platform_services
    assert services.guard is guard and services.updater is updater
    assert services.parent() is window
    assert calls[0][1:3] == (window, session)
    assert calls[0][3] == services.export_preserved_results
    assert calls[0][4] == services.lock_session
    assert calls[1][1:3] == (window, PRODUCT)
    kwargs = calls[1][3]
    assert kwargs['button'] is window.check_updates_button
    assert kwargs['precheck'] == guard.check_native_update
    assert not kwargs['is_locked']()
    guard.locked = True
    assert kwargs['is_locked']()


@pytest.mark.parametrize('product', [None, 'char-detection-main', 'group-by-character'])
def test_wrong_product_is_rejected_before_loading_sdks(bridge, monkeypatch, product):
    window, previous = bridge
    monkeypatch.setitem(sys.modules, 'hanzi_auth', None)
    monkeypatch.setitem(sys.modules, 'hanzi_update', None)
    with pytest.raises(ValueError, match='不属于'):
        attach_platform_services(window, SimpleNamespace(product=product))
    assert window._platform_services is previous


@pytest.mark.parametrize('operation', ['scan', 'process', 'correct', 'export', 'copy', 'replace', 'restore'])
def test_locked_gate_allows_only_preserved_export(bridge, operation):
    _, services = bridge
    assert services.permits(operation)
    services.guard.locked = True
    assert services.permits(operation) == (operation == 'export')


@pytest.mark.parametrize('operation', ['scan', 'process', 'correct', 'export', 'copy', 'replace', 'restore'])
def test_lock_preserves_cached_work_and_does_not_interrupt_save_transactions(bridge, operation):
    window, services = bridge
    records = dict(window.records)
    history = dict(window._correction_history)
    worker = window.worker = FakeWorker()
    window._task_operation = operation
    request_update(services)
    services.guard.locked = True
    services.lock_session()
    assert worker.interruptions == (1 if operation in {'scan', 'process', 'correct'} else 0)
    assert window.records == records
    assert window._correction_history == history
    assert window.cache_file.is_file()
    assert not window._closing
    assert window.centralWidget().isEnabled()
    assert window.preview.selection == 0
    assert window.pending_preview is None
    assert not services.updater.restart_requested


def test_rescue_export_busy_request_is_coalesced_and_deferred(bridge, qapp):
    window, services = bridge
    window.worker = FakeWorker()
    window._task_operation = 'process'
    services.guard.locked = True
    services.export_preserved_results()
    services.export_preserved_results()
    assert services._rescue_pending
    assert not window.save_modes
    window.worker = None
    services.on_task_finished('process')
    services.on_task_finished('process')
    assert not window.save_modes
    wait_for(qapp, lambda: window.save_modes == ['export'])
    assert not services._rescue_pending
    assert window.worker is not None
    assert window.cache_file.is_file()


@pytest.mark.parametrize('operation', ['export', 'copy', 'replace', 'restore'])
def test_busy_rescue_leaves_safe_save_running(bridge, qapp, operation):
    window, services = bridge
    worker = window.worker = FakeWorker()
    window._task_operation = operation
    services.export_preserved_results()
    assert worker.interruptions == 0
    assert not window.save_modes
    window.worker = None
    services.on_task_finished(operation)
    wait_for(qapp, lambda: window.save_modes == ['export'])


def test_idle_rescue_forces_export_without_changing_replace_preference(bridge):
    window, services = bridge
    assert window.save_mode == 'replace'
    services.guard.locked = True
    services.export_preserved_results()
    assert window.save_modes == ['export']
    assert window.save_mode == 'replace'


def test_rescue_request_does_not_restart_work_after_window_is_closing(bridge, qapp):
    window, services = bridge
    window.worker = FakeWorker()
    window._task_operation = 'process'
    services.export_preserved_results()
    window._closing = True
    window.worker = None
    services.on_task_finished('process')
    qapp.processEvents()
    assert not services._rescue_pending
    assert not window.save_modes
    services.export_preserved_results()
    assert not window.save_modes


@pytest.mark.parametrize('operation', ['scan', 'process', 'correct', 'export', 'copy', 'replace', 'restore', 'preview'])
def test_update_rejects_unrelated_busy_work_without_cancelling_it(bridge, operation):
    window, services = bridge
    if operation == 'preview':
        window._preview_workers.add(object())
    else:
        worker = window.worker = FakeWorker()
        window._task_operation = operation
    request_update(services)
    pending = window.pending_preview
    event = QCloseEvent()
    assert not services.before_close(event)
    assert not event.isAccepted()
    assert not services.updater.restart_requested
    assert window.pending_preview is pending
    assert not window._closing
    assert not window.save_modes
    assert window.cache_file.is_file()
    if operation != 'preview':
        assert worker.interruptions == 0


def test_successful_update_export_accepts_probe_but_keeps_cache_until_real_close(bridge, qapp):
    window, services = bridge
    assert window.save_mode == 'replace'
    request_update(services)
    window.close()
    assert window.save_modes == ['export']
    assert window.save_mode == 'replace'
    own_worker = window.worker
    assert not own_worker.interruptions
    assert services._update_export_active
    assert not services._update_save_ready
    assert not window._closing
    assert window.isVisible()
    window.close()
    assert window.worker is own_worker
    assert window.save_modes == ['export']
    assert services.updater.restart_requested
    finish_export(window, services, summary(window))
    assert window.close_calls == 2
    wait_for(qapp, lambda: services.updater.approved)
    assert window.close_calls == 3
    assert window.isVisible()
    assert window.cleanup_calls == 0
    assert window.cache_file.is_file()
    # The fake native adapter now performs its real finish_close continuation.
    window.close()
    assert not window.isVisible()
    assert window.cleanup_calls == 1
    assert not window.cache_file.exists()


@pytest.mark.parametrize('outcome', ['cancelled', 'errors', 'missing-summary'])
def test_failed_update_export_does_not_authorize_installation_or_close(bridge, qapp, outcome):
    window, services = bridge
    request_update(services)
    event = QCloseEvent()
    services.before_close(event)
    result = None if outcome == 'missing-summary' else summary(
        window, cancelled=outcome == 'cancelled', errors=outcome == 'errors')
    finish_export(window, services, result)
    qapp.processEvents()
    assert not services.updater.restart_requested
    assert not services._update_save_ready
    assert not services.updater.approved
    assert window.close_calls == 0
    assert window.cache_file.is_file()


def test_update_save_validation_refusal_clears_restart_and_retains_cache(bridge):
    window, services = bridge
    window.fail_save_start = True
    request_update(services)
    event = QCloseEvent()
    assert not services.before_close(event)
    assert not event.isAccepted()
    assert not services.updater.restart_requested
    assert not services._update_export_active
    assert not services._update_save_ready
    assert window.cache_file.is_file()


def test_regular_export_success_is_not_update_save_proof(bridge):
    window, services = bridge
    services.export_preserved_results()
    services.on_saved(summary(window))
    assert not services._update_save_ready
    window.worker = None
    request_update(services)
    assert not services.before_close(QCloseEvent())
    assert window.save_modes == ['export', 'export']
    assert not services._update_save_ready


def test_late_save_summary_from_a_different_worker_cannot_authorize_update(bridge, qapp):
    window, services = bridge
    request_update(services)
    services.before_close(QCloseEvent())
    window.worker = FakeWorker()
    services.on_saved(summary(window))
    finish_export(window, services)
    qapp.processEvents()
    assert not services._update_save_ready
    assert not services.updater.restart_requested
    assert not window.close_calls


def test_lock_during_update_export_preserves_save_but_aborts_restart(bridge, qapp):
    window, services = bridge
    request_update(services)
    services.before_close(QCloseEvent())
    worker = window.worker
    services.guard.locked = True
    services.lock_session()
    assert worker.interruptions == 0
    assert not services.updater.restart_requested
    finish_export(window, services, summary(window))
    qapp.processEvents()
    assert not services._update_save_ready
    assert window.close_calls == 0
    assert window.cache_file.is_file()


def test_update_no_adopted_items_preserves_cache_during_probe(bridge):
    window, services = bridge
    window.items.clear()
    request_update(services)
    event = QCloseEvent()
    assert not services.before_close(event)
    assert event.isAccepted()
    assert not services._update_save_ready
    assert not window.save_modes
    assert window.cache_file.is_file()


@pytest.mark.parametrize('change', ['selection', 'output_style'])
def test_changed_save_settings_require_new_update_export(bridge, qapp, change):
    window, services = bridge
    request_update(services)
    services.before_close(QCloseEvent())
    services.on_saved(summary(window))
    window.worker = None
    if change == 'selection':
        window.items.append(object())
    else:
        window.output_style.setCurrentIndex(1)
    services.on_task_finished('export')
    wait_for(qapp, lambda: len(window.save_modes) == 2)
    assert window.isVisible()
    assert not services.updater.approved
    assert not services._update_save_ready
    assert window.cache_file.is_file()


def test_normal_close_uses_original_host_path(bridge):
    window, services = bridge
    event = QCloseEvent()
    assert services.before_close(event)
    assert not services._update_save_ready
    window.close()
    assert window.cleanup_calls == 1
    assert not window.isVisible()


def test_busy_automatic_task_is_not_interrupted_when_update_is_refused(bridge):
    window, services = bridge
    worker = window.worker = FakeWorker()
    worker.payload['automatic'] = True
    window._task_operation = 'process'
    request_update(services)
    event = QCloseEvent()
    assert not services.before_close(event)
    assert not services.updater.restart_requested
    assert not worker.interruptions
    assert window.discard_modes == []
    assert window.pending_preview is not None


@pytest.mark.parametrize('entry', ['window', 'app-gui', 'app-cli'])
def test_entry_points_activate_platform_session_as_configured(tmp_path, entry):
    source = write_rubbing(tmp_path / 'input.png')
    script = """
import importlib.abc
import sys
import types

class BlockRealSharedSDK(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'hanzi_auth', 'hanzi_update'}:
            raise AssertionError('不应加载真实共享SDK: ' + fullname)

sys.meta_path.insert(0, BlockRealSharedSDK())
calls = []

def fake_start_session(app, product):
    calls.append(product)
    return None

entry, source, output = sys.argv[1:]
if entry != 'app-cli':
    stub = types.ModuleType('hanzi_auth')
    stub.start_session = fake_start_session
    sys.modules['hanzi_auth'] = stub
if entry == 'app-cli':
    import app
    code = app.main([source, '-o', output])
    assert 'PySide6' not in sys.modules
    assert not calls
else:
    from PySide6.QtWidgets import QApplication
    application = QApplication([])
    if entry == 'window':
        from ui.window import main
        code = main([])
    else:
        import app
        code = app.main(['--gui'])
    assert calls == ['hanzi-mask']
    assert not [w for w in application.topLevelWidgets() if w.__class__.__name__ == 'MainWindow']
assert code == 0
assert not any(name.split('.')[0] == 'hanzi_update' for name in sys.modules)
"""
    project = Path(__file__).resolve().parents[1]
    result = subprocess.run([sys.executable, '-c', script, entry, str(source), str(tmp_path / 'output')],
                            cwd=project, capture_output=True, text=True, timeout=90)
    assert result.returncode == 0, result.stdout + result.stderr


def test_explicit_local_development_mode_opens_gui_without_shared_services(tmp_path):
    script = """
import sys
from PySide6.QtWidgets import QApplication
QApplication.exec = lambda self: 0
from ui.window import main
assert main([]) == 0
windows = [w for w in QApplication.topLevelWidgets() if w.__class__.__name__ == 'MainWindow']
assert len(windows) == 1 and windows[0].isVisible()
assert windows[0]._platform_services is None
"""
    project = Path(__file__).resolve().parents[1]
    env = dict(os.environ, HANZI_MASK_LOCAL_DEV='1', QT_QPA_PLATFORM='offscreen')
    result = subprocess.run([sys.executable, '-c', script], cwd=project, env=env,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


def test_real_window_lock_gates_work_but_preserves_explicit_and_clicked_export(
        window, qapp, tmp_path):
    assert window.save_mode.currentData() == 'copy'
    path = write_two_components(tmp_path / 'input' / 'glyph.png')
    originals = path.read_bytes()
    import_and_process(window, qapp, path)
    click_mask_pixel(window, qapp)
    window.clear_component_button.click()
    wait_for(qapp, lambda: idle(window))
    previous = window.records[path]
    history = list(window._correction_history[path])
    services = PlatformServices(window)
    services.guard = FakeGuard()
    services.updater = FakeUpdater(window)
    window._platform_services = services
    window._update_controller = services.updater
    services.guard.locked = True
    services.lock_session()
    window.process_current()
    window.process_all()
    window._launch('process', {})
    window._launch('correct', {})
    click_mask_pixel(window, qapp, x=22, y=60, editable=False)
    window.clear_selected_component()
    window.undo_correction()
    assert window.worker is None
    assert window.records[path] is previous
    assert window._correction_history[path] == history
    assert previous.mask_path.is_file()
    assert window.save_results() is False
    assert window.worker is None
    assert window.save_results(mode='export') is True
    wait_for(qapp, lambda: idle(window))
    first_export = window._last_output
    assert len(list(first_export.rglob('*.png'))) == 1
    assert window.save_mode.currentData() == 'copy'
    assert path.read_bytes() == originals
    # QPushButton.clicked has a bool argument; the keyword-only mode entry
    # must still work through the actual signal connection.
    window.save_mode.setCurrentIndex(window.save_mode.findData('export'))
    assert window.save_button.isEnabled()
    window.save_button.click()
    assert window.worker is not None
    wait_for(qapp, lambda: idle(window))
    assert window._last_output != first_export
    assert len(list(window._last_output.rglob('*.png'))) == 1
    assert path.read_bytes() == originals
    assert not window._test_messages


@pytest.mark.parametrize('suffix', ['png', 'jpg'])
def test_real_lock_rescue_forces_copy_export_when_default_saves_in_place(
        window, qapp, tmp_path, monkeypatch, suffix):
    assert window.save_mode.currentData() == 'copy'
    path = write_two_components(tmp_path / 'input' / f'glyph.{suffix}')
    original_bytes = path.read_bytes()
    import_and_process(window, qapp, path)
    previous = window.records[path]
    services = PlatformServices(window)
    services.guard = FakeGuard()
    services.updater = FakeUpdater(window)
    window._platform_services = services
    window._update_controller = services.updater

    def unexpected_replacement(*args, **kwargs):
        pytest.fail('锁定救援必须导出副本，不能请求替换或修改原图')

    monkeypatch.setattr(worker_module, 'replace_originals', unexpected_replacement)
    monkeypatch.setattr(QMessageBox, 'question', unexpected_replacement)
    services.guard.locked = True
    services.lock_session()
    services.export_preserved_results()
    assert window.worker is not None and window._task_operation == 'export'
    wait_for(qapp, lambda: idle(window))
    assert window.save_mode.currentData() == 'copy'
    assert window.records[path] is previous
    assert previous.mask_path.is_file()
    assert path.read_bytes() == original_bytes
    assert len(list(window._last_output.rglob('*.png'))) == 1
    assert (window._last_output / 'report.json').is_file()
    assert window._replacement_report is None
    assert not window._test_messages


@pytest.mark.parametrize('source_state', ['changed', 'missing'])
def test_real_lock_rescues_cached_result_after_source_changes(window, qapp, tmp_path, source_state):
    path = write_two_components(tmp_path / 'input' / 'glyph.png')
    import_and_process(window, qapp, path)
    previous = window.records[path]
    if source_state == 'missing':
        path.unlink()
    else:
        path.write_bytes(b'changed source')
    services = PlatformServices(window)
    services.guard = FakeGuard()
    services.updater = FakeUpdater(window)
    window._platform_services = services
    services.guard.locked = True
    services.lock_session()
    services.export_preserved_results()
    wait_for(qapp, lambda: idle(window))
    assert window.records[path] is previous
    assert len(list(window._last_output.rglob('*.png'))) == 1
    assert (window._last_output / 'report.json').is_file()
    assert not window._test_messages


@pytest.mark.parametrize('has_pending', [False, True])
def test_refused_update_keeps_real_automatic_generation_and_queued_latest_work(
        window, qapp, tmp_path, monkeypatch, has_pending):
    path = write_two_components(tmp_path / 'input' / 'a.png')
    second = write_two_components(tmp_path / 'input' / 'b.png')
    import_and_process(window, qapp, path.parent)
    services = PlatformServices(window)
    services.guard = FakeGuard()
    services.updater = FakeUpdater(window)
    window._platform_services = services
    window._update_controller = services.updater
    runner = ControlledProcessing(window.records, count=2 if has_pending else 1)
    monkeypatch.setattr(worker_module, 'process_batch', runner)
    try:
        window.records.clear()
        window._schedule_tuned_preview()
        expire_tuning(window)
        wait_for(qapp, lambda: runner.entered[0].is_set())
        running = window.worker
        if has_pending:
            window.table.setCurrentCell(1, 1)
            expire_tuning(window)
        pending = window._pending_preview
        generation = window._tuning_generation
        obsolete = window._automatic_task_obsolete()
        interrupted = running.isInterruptionRequested()
        request_update(services)
        assert not services.before_close(QCloseEvent())
        assert not services.updater.restart_requested
        assert running.isInterruptionRequested() == interrupted
        assert window._tuning_generation == generation
        assert window._pending_preview is pending
        assert window._automatic_task_obsolete() == obsolete
        runner.release[0].set()
        if has_pending:
            wait_for(qapp, lambda: runner.entered[1].is_set())
            assert runner.calls[1][0][0].path == second
            runner.release[1].set()
        wait_for(qapp, lambda: idle(window))
        assert window.records[second if has_pending else path].options.texture_strength == 0
        assert len(runner.calls) == (2 if has_pending else 1)
    finally:
        runner.release_all()


def test_real_idle_close_prevents_already_queued_rescue_export_from_using_deleted_cache(
        window, qapp, tmp_path, monkeypatch):
    path = write_rubbing(tmp_path / 'input' / 'glyph.png')
    import_and_process(window, qapp, path)
    services = PlatformServices(window)
    services.guard = FakeGuard()
    services.updater = FakeUpdater(window)
    window._platform_services = services
    window._update_controller = services.updater
    cache = window._cache_root
    exports = []
    monkeypatch.setattr(window, 'save_results', lambda **kwargs: exports.append(kwargs))
    services._rescue_pending = True
    services._schedule_dispatch()
    assert services._dispatch_scheduled
    assert exports == []
    window.close()
    wait_for(qapp, lambda: not window.isVisible())
    assert window._closing
    assert not cache.exists()
    qapp.processEvents()
    assert exports == []
    assert not services._rescue_pending
    assert not services._dispatch_scheduled
    assert window.worker is None
    assert not window._preview_workers
    assert not window._test_messages


def test_real_native_close_probe_keeps_window_open_and_cache_until_final_close(
        window, qapp, tmp_path):
    assert window.save_mode.currentData() == 'copy'
    path = write_rubbing(tmp_path / 'input' / 'glyph.png')
    original = path.read_bytes()
    import_and_process(window, qapp, path)
    services = PlatformServices(window)
    services.guard = FakeGuard()
    services.updater = FakeUpdater(window)
    window._platform_services = services
    window._update_controller = services.updater
    cache = window._cache_root
    request_update(services)
    assert not window.close()
    wait_for(qapp, lambda: idle(window) and services.updater.approved)
    assert window.isVisible()
    assert not window._closing
    assert cache.is_dir()
    assert window.records[path].mask_path.is_file()
    assert services._update_save_ready
    assert window.save_mode.currentData() == 'copy'
    assert path.read_bytes() == original
    window.close()
    wait_for(qapp, lambda: not window.isVisible())
    assert window._closing
    assert not window.isVisible()
    assert not cache.exists()
    assert path.read_bytes() == original
    assert not window._test_messages
