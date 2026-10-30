"""Qt regressions for per-image precision and serial automatic processing.

Controlled workers return existing candidate pixels so these tests exercise
event ordering and state ownership without depending on solver timing.
"""

from dataclasses import replace
import json
from threading import Event

import pytest

pytest.importorskip("PySide6")
from PySide6.QtCore import QThread, Qt
from PySide6.QtTest import QTest

from hanzi_mask.batch_service import BatchResult, ProcessingError
import ui.task_worker as worker_module
from test_desktop import (
    click_mask_pixel, idle, import_and_process, qapp, wait_for, window,
    write_rubbing, write_two_components,
)


class ControlledProcessing:
    """Keep real QThread signals, but explicitly release each processing call."""

    def __init__(self, records, count=2, outcome="success"):
        self.records = dict(records)
        self.calls = []
        self.entered = [Event() for _ in range(count)]
        self.release = [Event() for _ in range(count)]
        self.outcome = outcome

    def __call__(self, inputs, options, cancel=None, progress=None, cache_dir=None,
                 processed=None, *, max_workers=None, per_image_options=None):
        sources = list(inputs)
        index = len(self.calls)
        self.calls.append((sources, options, per_image_options, max_workers))
        assert index < len(self.entered), "产生了多余的后台处理请求"
        self.entered[index].set()
        assert self.release[index].wait(12), "测试未释放可控后台任务"
        if self.outcome == "exception":
            raise OSError("模拟整批处理失败")
        if self.outcome == "cancelled":
            return BatchResult(options, cancelled=True)
        if self.outcome == "failed":
            return BatchResult(options, errors=[ProcessingError(source.path, "模拟单图失败")
                                                for source in sources])
        items = []
        for done, source in enumerate(sources, 1):
            actual = (per_image_options or {}).get(source.path, options)
            old = self.records[source.path]
            metrics = {"width": old.metrics["width"], "height": old.metrics["height"],
                       "foreground_pixels": old.metrics["foreground_pixels"],
                       "foreground_fraction": .2, "components": 2,
                       "removed_fraction": .1, "texture_strength": actual.texture_strength}
            item = replace(old, source=source, options=actual, metrics=metrics)
            items.append(item)
            # Deliberately send late results even after cancellation. The UI
            # receiver must reject obsolete generations independently.
            if processed:
                processed(item)
            if progress:
                progress(done, len(sources), source.path)
        return BatchResult(options, items=items)

    def release_all(self):
        for event in self.release:
            event.set()


def install_controlled(monkeypatch, window, count=2, outcome="success"):
    runner = ControlledProcessing(window.records, count, outcome)
    monkeypatch.setattr(worker_module, "process_batch", runner)
    return runner


def load_pair(window, qapp, tmp_path):
    folder = tmp_path / "input"
    first = write_two_components(folder / "a.png")
    second = write_two_components(folder / "b.png")
    import_and_process(window, qapp, folder)
    return first, second


def expire_tuning(window):
    """Advance the debounce boundary deterministically, retaining Qt workers."""
    window._tuning_timer.stop()
    window._preview_tuned()


def test_precision_is_debounced_per_image_and_reported(window, qapp, tmp_path, monkeypatch):
    first, second = load_pair(window, qapp, tmp_path)
    old_second = window.records[second]
    runner = install_controlled(monkeypatch, window, count=1)
    try:
        window.strength_input.setValue(12)
        window.strength_input.setValue(24)
        assert window.strength_slider.value() == 240
        assert window.processing_options().texture_strength == .012
        expire_tuning(window)
        wait_for(qapp, runner.entered[0].is_set)
        assert runner.calls[0][0][0].path == first
        assert runner.calls[0][1].texture_strength == .012
        runner.release[0].set()
        wait_for(qapp, lambda: idle(window))
        assert window.records[first].options.texture_strength == .012
        assert window.records[second] is old_second
        window.table.setCurrentCell(1, 1)
        assert window.strength_input.value() == 0
        window.table.setCurrentCell(0, 1)
        assert window.strength_input.value() == 24
        wait_for(qapp, lambda: idle(window))
        window.save_results(mode='export')
        wait_for(qapp, lambda: idle(window))
        report = json.loads((window._last_output / 'report.json').read_text(encoding='utf-8'))
        assert report['parameter_mode'] == 'per_image'
        assert [entry['parameters']['texture_strength'] for entry in report['images']] == [.012, 0]
    finally:
        runner.release_all()


def test_all_precision_waits_for_explicit_batch_and_updates_each_image(window, qapp, tmp_path, monkeypatch):
    first, second = load_pair(window, qapp, tmp_path)
    runner = install_controlled(monkeypatch, window, count=1)
    try:
        window.application_scope.setCurrentIndex(window.application_scope.findData('all'))
        window.strength_slider.setValue(480)
        assert window.strength_input.value() == 48
        assert window._pending_preview is None and window.worker is None
        window.process_all()
        wait_for(qapp, runner.entered[0].is_set)
        assert runner.calls[0][1].texture_strength == .024
        runner.release[0].set()
        wait_for(qapp, lambda: idle(window))
        assert all(item.options.texture_strength == .024 for item in window.records.values())
        window.application_scope.setCurrentIndex(window.application_scope.findData('current'))
        assert window.strength_input.value() == 48
        window.table.setCurrentCell(1, 1)
        assert window.strength_input.value() == 48
    finally:
        runner.release_all()


@pytest.mark.parametrize("outcome", ["failed", "exception", "cancelled"])
def test_unsuccessful_recompute_preserves_old_result_history_and_adoption(
        window, qapp, tmp_path, monkeypatch, outcome):
    path = write_two_components(tmp_path / "input" / "a.png")
    import_and_process(window, qapp, path)
    click_mask_pixel(window, qapp)
    window.clear_component_button.click()
    wait_for(qapp, lambda: idle(window))
    old = window.records[path]
    history = list(window._correction_history[path])
    state = window.table.item(0, 3).text()
    runner = install_controlled(monkeypatch, window, count=1, outcome=outcome)
    try:
        window.process_current()
        wait_for(qapp, lambda: runner.entered[0].is_set())
        window.table.item(0, 0).setCheckState(Qt.Unchecked)
        runner.release[0].set()
        wait_for(qapp, lambda: idle(window))
        assert window.records[path] is old
        assert window._correction_history[path] == history
        assert window.table.item(0, 3).text() == state
        assert window.table.item(0, 0).checkState() == Qt.Unchecked
        assert window.undo_correction_button.isEnabled()
    finally:
        runner.release_all()


def test_unprocessed_and_empty_results_cannot_be_adopted(window, qapp, tmp_path):
    path = write_rubbing(tmp_path / "input" / "a.png")
    window.import_paths([path])
    wait_for(qapp, lambda: window._pending_preview is not None)
    window.cancel_task()
    wait_for(qapp, lambda: idle(window))
    assert window.current_source().path == path
    assert window.preview.has_image
    check = window.table.item(0, 0)
    assert not check.flags() & Qt.ItemIsEnabled
    check.setCheckState(Qt.Checked)
    window.check_all(True)
    assert check.checkState() == Qt.Unchecked
    assert not window.adopted_items()
    window.process_current()
    wait_for(qapp, lambda: idle(window))
    click_mask_pixel(window, qapp, x=22, y=30)
    window.clear_component_button.click()
    wait_for(qapp, lambda: idle(window))
    assert window.records[path].metrics["foreground_pixels"] == 0
    check.setCheckState(Qt.Checked)
    window.check_all(True)
    assert check.checkState() == Qt.Unchecked
    assert not check.flags() & Qt.ItemIsEnabled
    assert not window.save_button.isEnabled()
    window.undo_correction_button.click()
    wait_for(qapp, lambda: idle(window))
    assert check.checkState() == Qt.Checked
    assert check.flags() & Qt.ItemIsEnabled


@pytest.mark.parametrize('mode', ['export', 'copy'])
def test_adoption_is_frozen_while_saving_captures_existing_results(
        window, qapp, tmp_path, monkeypatch, mode):
    first, second = load_pair(window, qapp, tmp_path)
    window.table.item(1, 0).setCheckState(Qt.Unchecked)
    old = window.records[first]
    entered, release = Event(), Event()
    real = worker_module.export_batch

    def paused_export(*args, **kwargs):
        entered.set()
        assert release.wait(12)
        return real(*args, **kwargs)

    monkeypatch.setattr(worker_module, "export_batch", paused_export)
    try:
        window.save_results(mode=mode)
        wait_for(qapp, lambda: entered.is_set())
        report_directory = window.worker.payload['directory']
        assert not window.output_group.isEnabled()
        assert not window.output_style.isEnabled()
        assert not window.preview.output_style_editable
        for row, wanted in ((0, Qt.Unchecked), (1, Qt.Checked)):
            check = window.table.item(row, 0)
            assert not check.flags() & Qt.ItemIsEnabled
            check.setCheckState(wanted)
        window.check_all(False)
        assert window.table.item(0, 0).checkState() == Qt.Checked
        assert window.table.item(1, 0).checkState() == Qt.Unchecked
        assert window.worker.payload["batch"].items == [old]
        release.set()
        wait_for(qapp, lambda: idle(window))
        assert window.adopted_items() == [old]
        assert window.output_group.isEnabled()
        assert window.output_style.isEnabled()
        assert window.preview.output_style_editable
        assert window.table.item(0, 0).flags() & Qt.ItemIsEnabled
        report = json.loads(((report_directory if mode == 'copy' else window._last_output)
                             / "report.json").read_text(encoding='utf-8'))
        assert [row["source"] for row in report["images"]] == [str(first)]
        assert second in window.records
    finally:
        release.set()


def test_adoption_changed_during_correction_is_retained_by_result_and_undo(
        window, qapp, tmp_path, monkeypatch):
    path = write_two_components(tmp_path / "input" / "a.png")
    import_and_process(window, qapp, path)
    original = window.records[path]
    entered, release = Event(), Event()
    real = worker_module.apply_correction

    def paused_correction(*args, **kwargs):
        entered.set()
        assert release.wait(12)
        return real(*args, **kwargs)

    monkeypatch.setattr(worker_module, "apply_correction", paused_correction)
    try:
        click_mask_pixel(window, qapp)
        window.clear_component_button.click()
        wait_for(qapp, lambda: entered.is_set())
        assert window.output_group.isEnabled()
        assert window.table.item(0, 0).flags() & Qt.ItemIsEnabled
        window.table.item(0, 0).setCheckState(Qt.Unchecked)
        assert window.table.item(0, 0).checkState() == Qt.Unchecked
        assert not window.worker.payload["adopted"]
        release.set()
        wait_for(qapp, lambda: idle(window))
        assert window.table.item(0, 0).checkState() == Qt.Unchecked
        assert window.records[path].mask_path != original.mask_path
        window.undo_correction_button.click()
        wait_for(qapp, lambda: idle(window))
        assert window.table.item(0, 0).checkState() == Qt.Unchecked
        assert window.records[path].mask_path == original.mask_path
    finally:
        release.set()


def test_rescan_rebinds_sources_without_losing_result_or_undo(
        window, qapp, tmp_path):
    path = write_two_components(tmp_path / "input" / "a.png")
    import_and_process(window, qapp, path.parent)
    click_mask_pixel(window, qapp)
    window.clear_component_button.click()
    wait_for(qapp, lambda: idle(window))
    old = window.records[path]
    old_history = window._correction_history[path][0][0]
    other = write_rubbing(tmp_path / "other" / "b.png")
    window.import_paths([other.parent])
    wait_for(qapp, lambda: idle(window))
    assert window.current_source().path == path
    assert window.records[path].mask_path == old.mask_path
    assert window.records[path].source.relative_path.as_posix() == "input/a.png"
    assert window._correction_history[path][0][0].mask_path == old_history.mask_path
    assert window._correction_history[path][0][0].source == window.records[path].source
    assert window.preview.has_image
    assert window.undo_correction_button.isEnabled()



def test_import_and_selection_automatically_process_only_uncached_images(
        window, qapp, tmp_path, monkeypatch):
    from test_desktop import track_processing
    first = write_two_components(tmp_path / 'input' / 'a.png')
    second = write_two_components(tmp_path / 'input' / 'nested' / 'b.png')
    calls = track_processing(monkeypatch)
    window.import_paths([first.parent])
    wait_for(qapp, lambda: idle(window))
    assert calls == [([first], 0)]
    assert set(window.records) == {first}
    click_mask_pixel(window, qapp)
    window.clear_component_button.click()
    wait_for(qapp, lambda: idle(window))
    corrected = window.records[first]
    history = list(window._correction_history[first])
    window.table.setCurrentCell(1, 1)
    wait_for(qapp, lambda: idle(window))
    assert calls == [([first], 0), ([second], 0)]
    window.table.setCurrentCell(0, 1)
    wait_for(qapp, lambda: idle(window))
    QTest.qWait(400)
    assert len(calls) == 2
    assert window.records[first] is corrected
    assert window._correction_history[first] == history
    assert window.history_panel.isVisible() and window.undo_correction_button.isEnabled()


def test_quick_selection_changes_process_only_latest_image(window, qapp, tmp_path, monkeypatch):
    from test_desktop import track_processing
    paths = [write_rubbing(tmp_path / 'input' / f'{name}.png') for name in ('a', 'b', 'c')]
    calls = track_processing(monkeypatch)
    window.import_paths([paths[0].parent])
    wait_for(qapp, lambda: window._pending_preview is not None)
    window.table.setCurrentCell(1, 1)
    window.table.setCurrentCell(2, 1)
    wait_for(qapp, lambda: idle(window))
    assert calls == [([paths[2]], 0)]
    assert set(window.records) == {paths[2]}


def test_latest_selection_waits_for_finished_and_rejects_old_callbacks(
        window, qapp, tmp_path, monkeypatch):
    first, second = load_pair(window, qapp, tmp_path)
    runner = install_controlled(monkeypatch, window)
    window.records.clear()
    receivers = []
    real_on_item = window._on_item

    def track_item(item, **kwargs):
        receivers.append((item.source.path, QThread.currentThread()))
        return real_on_item(item, **kwargs)

    monkeypatch.setattr(window, '_on_item', track_item)
    try:
        window._schedule_tuned_preview()
        expire_tuning(window)
        wait_for(qapp, runner.entered[0].is_set)
        running = window.worker
        window.table.setCurrentCell(1, 1)
        expire_tuning(window)
        assert running.isInterruptionRequested()
        assert len(runner.calls) == 1
        assert window.worker is running
        runner.release[0].set()
        wait_for(qapp, runner.entered[1].is_set)
        assert first not in window.records
        assert runner.calls[1][0][0].path == second
        assert runner.calls[1][1] == window.processing_options()
        assert runner.calls[1][3] == 2
        runner.release[1].set()
        wait_for(qapp, lambda: idle(window))
        assert receivers == [(second, qapp.thread())]
        assert set(window.records) == {second}
    finally:
        runner.release_all()


def test_batch_completion_does_not_duplicate_queued_current_image(
        window, qapp, tmp_path, monkeypatch):
    first, second = load_pair(window, qapp, tmp_path)
    runner = install_controlled(monkeypatch, window, count=1)
    window.records.clear()
    try:
        window.process_all()
        wait_for(qapp, runner.entered[0].is_set)
        window.table.setCurrentCell(1, 1)
        expire_tuning(window)
        assert window._pending_preview['path'] == second
        window.table.item(0, 0).setCheckState(Qt.Unchecked)
        runner.release[0].set()
        wait_for(qapp, lambda: idle(window))
        QTest.qWait(400)
        assert len(runner.calls) == 1
        assert set(window.records) == {first, second}
        assert runner.calls[0][3] == 2
        assert all(item.options.texture_strength == 0 for item in window.records.values())
    finally:
        runner.release_all()


@pytest.mark.parametrize('outcome', ['failed', 'exception', 'cancelled', 'user_cancel'])
def test_automatic_failure_or_cancel_does_not_retry_or_publish_late_results(
        window, qapp, tmp_path, monkeypatch, outcome):
    path = write_rubbing(tmp_path / 'a.png')
    import_and_process(window, qapp, path)
    runner = install_controlled(monkeypatch, window, count=1,
                                outcome='success' if outcome == 'user_cancel' else outcome)
    window.records.clear()
    try:
        window._schedule_tuned_preview()
        expire_tuning(window)
        wait_for(qapp, runner.entered[0].is_set)
        if outcome == 'user_cancel':
            window.cancel_task()
        runner.release[0].set()
        wait_for(qapp, lambda: idle(window))
        QTest.qWait(400)
        assert len(runner.calls) == 1
        assert path not in window.records
        assert window._pending_preview is None
        assert not window._tuning_timer.isActive()
    finally:
        runner.release_all()


@pytest.mark.parametrize('action', ['cancel', 'modal'])
def test_pending_automatic_processing_can_be_cancelled_before_it_starts(
        window, qapp, tmp_path, monkeypatch, action):
    from test_desktop import track_processing
    path = write_rubbing(tmp_path / 'a.png')
    calls = track_processing(monkeypatch)
    window.import_paths([path])
    wait_for(qapp, lambda: window._pending_preview is not None)
    if action == 'cancel':
        wait_for(qapp, lambda: window.worker is None)
        assert window.cancel_button.isEnabled()
        window.cancel_button.click()
    else:
        def dialog(*args):
            assert window._tuning_paused == 1
            window._schedule_tuned_preview()
            assert window._pending_preview is None
        window._show_dialog(dialog)
    QTest.qWait(400)
    assert not calls
    assert window._pending_preview is None
    window.process_current()
    wait_for(qapp, lambda: idle(window))
    assert calls == [([path], 0)]


def test_modal_depth_restores_after_exception(window, qapp, tmp_path):
    def nested_dialog(*args):
        assert window._tuning_paused == 2
        raise RuntimeError('模拟弹窗退出')

    def outer_dialog(*args):
        assert window._tuning_paused == 1
        return window._show_dialog(nested_dialog)

    with pytest.raises(RuntimeError, match='模拟弹窗退出'):
        window._show_dialog(outer_dialog)
    assert window._tuning_paused == 0
