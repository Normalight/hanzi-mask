"""Qt regressions for editable drafts and serial latest-only processing.

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


def type_strength(window, text):
    line = window.strength_input.lineEdit()
    line.setFocus()
    line.selectAll()
    QTest.keyClicks(line, text)
    assert window._strength_input_editing
    return line


def test_strength_numeric_precision_and_recommended_semantics(window, qapp, tmp_path):
    path = write_rubbing(tmp_path / "input" / "a.png")
    import_and_process(window, qapp, path)
    window.live_preview.setChecked(False)
    assert window.processing_options().texture_strength is None
    type_strength(window, "57.3")
    assert window.processing_options().texture_strength is None
    QTest.keyClick(window.strength_input, Qt.Key_Return)
    assert window.processing_options().texture_strength == pytest.approx(.02865)
    assert window.strength_slider.value() == 573
    assert window._draft_options[path].texture_strength == pytest.approx(.02865)
    window.strength_slider.setValue(731)
    assert window.strength_input.value() == pytest.approx(73.1)
    assert window.processing_options().texture_strength == pytest.approx(.03655)
    window.strength_reset.click()
    assert window.processing_options().texture_strength is None
    assert window.records[path].options.texture_strength is None
    assert not window._pending_preview


def test_current_scope_keeps_per_image_drafts_and_all_scope_uses_uniform_options(
        window, qapp, tmp_path, monkeypatch):
    first, second = load_pair(window, qapp, tmp_path)
    previous = dict(window.records)
    window.live_preview.setChecked(False)
    window.strength_slider.setValue(200)
    window.keep_isolated.setChecked(False)
    window.table.setCurrentCell(1, 1)
    wait_for(qapp, lambda: idle(window))
    assert window.processing_options().texture_strength is None
    assert window.keep_isolated.isChecked()
    window.strength_input.setValue(60)
    window.table.setCurrentCell(0, 1)
    wait_for(qapp, lambda: idle(window))
    assert window.strength_input.value() == 20
    assert not window.keep_isolated.isChecked()
    assert window.records == previous

    runner = install_controlled(monkeypatch, window)
    try:
        window.process_all()
        wait_for(qapp, lambda: runner.entered[0].is_set())
        per_image = runner.calls[0][2]
        assert per_image[first].texture_strength == .01
        assert not per_image[first].keep_isolated
        assert per_image[second].texture_strength == .03
        runner.release[0].set()
        wait_for(qapp, lambda: idle(window))
        assert window.records[first].options == per_image[first]
        assert window.records[second].options == per_image[second]
        window.application_scope.setCurrentIndex(window.application_scope.findData("all"))
        window.strength_slider.setValue(800)
        assert not window.live_preview.isEnabled()
        assert not window._pending_preview
        window.table.setCurrentCell(1, 1)
        wait_for(qapp, lambda: idle(window))
        assert window.strength_input.value() == 80
        window.process_all()
        wait_for(qapp, lambda: runner.entered[1].is_set())
        assert runner.calls[1][2] is None
        assert runner.calls[1][1].texture_strength == .04
        runner.release[1].set()
        wait_for(qapp, lambda: idle(window))
        assert {item.options.texture_strength for item in window.records.values()} == {.04}
    finally:
        runner.release_all()


def test_latest_auto_request_waits_for_finished_and_rejects_old_callbacks(
        window, qapp, tmp_path, monkeypatch):
    first, second = load_pair(window, qapp, tmp_path)
    old = dict(window.records)
    runner = install_controlled(monkeypatch, window)
    receivers = []
    real_item = window._on_item

    def record_receiver(item):
        receivers.append((item.options.texture_strength, QThread.currentThread()))
        real_item(item)

    monkeypatch.setattr(window, "_on_item", record_receiver)
    try:
        window.strength_slider.setValue(200)
        expire_tuning(window)
        wait_for(qapp, lambda: runner.entered[0].is_set())
        old_worker = window.worker
        assert window.parameters.isEnabled()
        assert window.strength_input.isEnabled()
        assert window.keep_isolated.isEnabled()
        assert window.application_scope.isEnabled()
        assert window.thread_count.isEnabled()
        assert window.output_group.isEnabled()
        assert not window.file_buttons[0].isEnabled()
        assert not window.process_all_button.isEnabled()
        window.strength_slider.setValue(400)
        window.strength_slider.setValue(600)
        window.keep_isolated.setChecked(False)
        expire_tuning(window)
        assert window.worker is old_worker
        assert old_worker.isInterruptionRequested()
        assert len(runner.calls) == 1
        assert window._pending_preview["ready"]
        runner.release[0].set()
        wait_for(qapp, lambda: runner.entered[1].is_set())
        assert window.worker is not old_worker
        assert runner.calls[1][0][0].path == first
        assert runner.calls[1][1].texture_strength == .03
        assert not runner.calls[1][1].keep_isolated
        assert window.records == old
        assert receivers == []
        runner.release[1].set()
        wait_for(qapp, lambda: idle(window))
        assert len(runner.calls) == 2
        assert receivers == [(.03, qapp.thread())]
        assert window.records[first].options.texture_strength == .03
        assert window.records[second] is old[second]
        assert window._pending_preview is None
    finally:
        runner.release_all()


def test_manual_batch_keeps_frozen_parameters_and_later_draft_and_adoption(
        window, qapp, tmp_path, monkeypatch):
    first, second = load_pair(window, qapp, tmp_path)
    runner = install_controlled(monkeypatch, window)
    try:
        window.process_all()
        wait_for(qapp, lambda: runner.entered[0].is_set())
        manual = window.worker
        assert not manual.payload["automatic"]
        window.strength_slider.setValue(740)
        window.table.item(0, 0).setCheckState(Qt.Unchecked)
        expire_tuning(window)
        assert not manual.isInterruptionRequested()
        assert window.records[first].options.texture_strength is None
        runner.release[0].set()
        wait_for(qapp, lambda: runner.entered[1].is_set())
        assert window.records[first].options.texture_strength is None
        assert window._draft_options[first].texture_strength == .037
        assert window.strength_input.value() == 74
        assert window.table.item(0, 0).checkState() == Qt.Unchecked
        assert window.records[second].options.texture_strength is None
        runner.release[1].set()
        wait_for(qapp, lambda: idle(window))
        assert window.records[first].options.texture_strength == .037
        assert window.table.item(0, 0).checkState() == Qt.Unchecked
        assert window.table.item(1, 0).checkState() == Qt.Checked
    finally:
        runner.release_all()


def test_cancel_running_auto_drops_late_results_and_finishes_status(
        window, qapp, tmp_path, monkeypatch):
    path = write_rubbing(tmp_path / "input" / "a.png")
    import_and_process(window, qapp, path)
    old = window.records[path]
    runner = install_controlled(monkeypatch, window, count=1)
    try:
        window.strength_slider.setValue(600)
        expire_tuning(window)
        wait_for(qapp, lambda: runner.entered[0].is_set())
        window.cancel_task()
        assert not window._pending_preview
        assert not window.cancel_button.isEnabled()
        runner.release[0].set()
        wait_for(qapp, lambda: idle(window))
        assert window.records[path] is old
        assert window.table.item(0, 3).text() != "处理中"
        assert "已取消处理" in window.status.text()
        assert len(runner.calls) == 1
        assert not window._tuning_timer.isActive()
    finally:
        runner.release_all()


@pytest.mark.parametrize("action", ["switch", "scope", "live", "cancel", "modal"])
def test_pending_preview_is_discarded_by_explicit_context_changes(
        window, qapp, tmp_path, action):
    load_pair(window, qapp, tmp_path)
    old = dict(window.records)
    window.strength_slider.setValue(640)
    assert window._pending_preview is not None
    if action == "switch":
        window.table.setCurrentCell(1, 1)
    elif action == "scope":
        window.application_scope.setCurrentIndex(window.application_scope.findData("all"))
    elif action == "live":
        window.live_preview.setChecked(False)
    elif action == "cancel":
        window.cancel_task()
    else:
        window._show_dialog(lambda *args: None)
    assert window._pending_preview is None
    assert not window._tuning_timer.isActive()
    expire_tuning(window)
    wait_for(qapp, lambda: idle(window))
    assert window.records == old


def test_cancelled_slider_release_cannot_recreate_request(window, qapp, tmp_path):
    path = write_rubbing(tmp_path / "input" / "a.png")
    import_and_process(window, qapp, path)
    window.strength_slider.setSliderDown(True)
    window.strength_slider.setValue(690)
    window.cancel_task()
    window.strength_slider.setSliderDown(False)
    assert window._pending_preview is None
    assert not window._tuning_timer.isActive()
    window.strength_slider.setValue(700)
    assert window._pending_preview["options"].texture_strength == .035


def test_uncommitted_numeric_edit_survives_internal_refresh_and_cancelled_submit(
        window, qapp, tmp_path):
    path = write_rubbing(tmp_path / "input" / "a.png")
    import_and_process(window, qapp, path)
    window.strength_slider.setValue(600)
    line = type_strength(window, "57.3")
    assert window._pending_preview is None
    assert not window._tuning_timer.isActive()
    window._refresh_preview()
    wait_for(qapp, lambda: idle(window))
    assert line.text().startswith("57.3")
    assert window._strength_input_editing
    window.cancel_task()
    QTest.keyClick(window.strength_input, Qt.Key_Return)
    assert not window._strength_input_editing
    assert window._draft_options[path].texture_strength == pytest.approx(.02865)
    assert window._pending_preview is None
    assert not window._tuning_timer.isActive()
    type_strength(window, "58.4")
    QTest.keyClick(window.strength_input, Qt.Key_Return)
    assert window._pending_preview["options"].texture_strength == pytest.approx(.0292)


def test_same_numeric_value_commit_still_schedules_latest_request(window, qapp, tmp_path):
    path = write_rubbing(tmp_path / "input" / "a.png")
    import_and_process(window, qapp, path)
    window.strength_input.setValue(57.3)
    type_strength(window, "57.3")
    assert window._pending_preview is None
    QTest.keyClick(window.strength_input, Qt.Key_Return)
    assert window._pending_preview["options"].texture_strength == pytest.approx(.02865)


def test_result_filter_does_not_switch_owner_of_uncommitted_or_pending_edit(
        window, qapp, tmp_path, monkeypatch):
    first, _ = load_pair(window, qapp, tmp_path)
    window._set_row(first, "已矫正", "人工复核")
    window.state_filter.setCurrentText("已矫正")
    runner = install_controlled(monkeypatch, window)
    try:
        window.process_current()
        wait_for(qapp, lambda: runner.entered[0].is_set())
        line = type_strength(window, "57.3")
        runner.release[0].set()
        wait_for(qapp, lambda: idle(window))
        assert window.current_source().path == first
        assert not window.table.isRowHidden(0)
        assert line.text().startswith("57.3")
        assert window._strength_input_editing
        QTest.keyClick(window.strength_input, Qt.Key_Return)
        assert window.current_source().path == first
        assert window._pending_preview["path"] == first
        expire_tuning(window)
        wait_for(qapp, lambda: runner.entered[1].is_set())
        assert runner.calls[1][0][0].path == first
        assert runner.calls[1][1].texture_strength == pytest.approx(.02865)
        runner.release[1].set()
        wait_for(qapp, lambda: idle(window))
        assert window.records[first].options.texture_strength == pytest.approx(.02865)
        assert window.current_source() is None
        assert not window.preview.has_image
    finally:
        runner.release_all()


@pytest.mark.parametrize("outcome", ["failed", "exception", "cancelled"])
def test_unsuccessful_recompute_preserves_old_result_history_and_new_draft(
        window, qapp, tmp_path, monkeypatch, outcome):
    path = write_two_components(tmp_path / "input" / "a.png")
    import_and_process(window, qapp, path)
    window.live_preview.setChecked(False)
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
        window.strength_slider.setValue(740)
        window.table.item(0, 0).setCheckState(Qt.Unchecked)
        runner.release[0].set()
        wait_for(qapp, lambda: idle(window))
        assert window.records[path] is old
        assert window._correction_history[path] == history
        assert window.table.item(0, 3).text() == state
        assert window.table.item(0, 0).checkState() == Qt.Unchecked
        assert window._draft_options[path].texture_strength == .037
        assert window.undo_correction_button.isEnabled()
    finally:
        runner.release_all()


def test_unprocessed_and_empty_results_cannot_be_adopted(window, qapp, tmp_path):
    path = write_rubbing(tmp_path / "input" / "a.png")
    window.import_paths([path])
    wait_for(qapp, lambda: idle(window))
    assert window.current_source().path == path
    assert window.preview.has_image
    check = window.table.item(0, 0)
    assert not check.flags() & Qt.ItemIsEnabled
    assert not window.adopt_current_button.isEnabled()
    check.setCheckState(Qt.Checked)
    window.check_all(True)
    window.toggle_current_adoption()
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
    window.toggle_current_adoption()
    assert check.checkState() == Qt.Unchecked
    assert not check.flags() & Qt.ItemIsEnabled
    assert not window.adopt_current_button.isEnabled()
    assert not window.save_button.isEnabled()
    window.undo_correction_button.click()
    wait_for(qapp, lambda: idle(window))
    assert check.checkState() == Qt.Checked
    assert check.flags() & Qt.ItemIsEnabled
    assert window.adopt_current_button.isEnabled()


def test_adoption_is_frozen_while_export_captures_existing_results(
        window, qapp, tmp_path, monkeypatch):
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
        window.save_results(mode="export")
        wait_for(qapp, lambda: entered.is_set())
        assert not window.parameters.isEnabled()
        assert not window.adopt_current_button.isEnabled()
        for row, wanted in ((0, Qt.Unchecked), (1, Qt.Checked)):
            check = window.table.item(row, 0)
            assert not check.flags() & Qt.ItemIsEnabled
            check.setCheckState(wanted)
        window.check_all(False)
        window.toggle_current_adoption()
        assert window.table.item(0, 0).checkState() == Qt.Checked
        assert window.table.item(1, 0).checkState() == Qt.Unchecked
        assert window.worker.payload["batch"].items == [old]
        release.set()
        wait_for(qapp, lambda: idle(window))
        assert window.adopted_items() == [old]
        assert window.parameters.isEnabled()
        assert window.table.item(0, 0).flags() & Qt.ItemIsEnabled
        report = json.loads((window._last_output / "report.json").read_text())
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
        assert not window.parameters.isEnabled()
        assert window.adopt_current_button.isEnabled()
        assert window.table.item(0, 0).flags() & Qt.ItemIsEnabled
        window.adopt_current_button.click()
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


def test_rescan_rebinds_sources_without_losing_draft_result_or_undo(
        window, qapp, tmp_path):
    path = write_two_components(tmp_path / "input" / "a.png")
    import_and_process(window, qapp, path.parent)
    window.live_preview.setChecked(False)
    click_mask_pixel(window, qapp)
    window.clear_component_button.click()
    wait_for(qapp, lambda: idle(window))
    old = window.records[path]
    old_history = window._correction_history[path][0][0]
    window.strength_slider.setValue(710)
    other = write_rubbing(tmp_path / "other" / "b.png")
    window.import_paths([other.parent])
    wait_for(qapp, lambda: idle(window))
    assert window.current_source().path == path
    assert window.strength_input.value() == 71
    assert window._draft_options[path].texture_strength == .0355
    assert window.records[path].mask_path == old.mask_path
    assert window.records[path].source.relative_path.as_posix() == "input/a.png"
    assert window._correction_history[path][0][0].mask_path == old_history.mask_path
    assert window._correction_history[path][0][0].source == window.records[path].source
    assert window.preview.has_image
    assert window.undo_correction_button.isEnabled()


def test_undo_discards_pending_recompute_and_preserves_current_draft(window, qapp, tmp_path):
    path = write_two_components(tmp_path / "input" / "a.png")
    import_and_process(window, qapp, path)
    original = window.records[path]
    click_mask_pixel(window, qapp)
    window.clear_component_button.click()
    wait_for(qapp, lambda: idle(window))
    window.strength_slider.setValue(720)
    assert window._pending_preview
    window.undo_correction_button.click()
    wait_for(qapp, lambda: idle(window))
    assert window._pending_preview is None
    assert not window._tuning_timer.isActive()
    assert window.records[path].mask_path == original.mask_path
    assert window.records[path].options == original.options
    assert window._draft_options[path].texture_strength == .036


def test_modal_depth_guards_nested_events_and_restores_after_exception(window, qapp, tmp_path):
    path = write_rubbing(tmp_path / "input" / "a.png")
    import_and_process(window, qapp, path)
    window.strength_slider.setValue(650)

    def nested_dialog(*args):
        assert window._tuning_paused == 2
        window.strength_input.setValue(80)
        assert window._pending_preview is None
        raise RuntimeError("模拟弹窗退出")

    def outer_dialog(*args):
        assert window._tuning_paused == 1
        assert window._pending_preview is None
        type_strength(window, "57.3")
        QTest.keyClick(window.strength_input, Qt.Key_Return)
        assert window._pending_preview is None
        return window._show_dialog(nested_dialog)

    with pytest.raises(RuntimeError, match="模拟弹窗退出"):
        window._show_dialog(outer_dialog)
    assert window._tuning_paused == 0
    assert window._pending_preview is None
    assert not window._tuning_timer.isActive()
    assert window.worker is None


def test_thread_count_change_does_not_schedule_or_change_glyph_draft(window, qapp, tmp_path):
    path = write_rubbing(tmp_path / "input" / "a.png")
    import_and_process(window, qapp, path)
    before = window.processing_options()
    assert window.thread_count.maximum() == window._worker_limits.maximum
    assert window.thread_count.value() == window._worker_limits.recommended
    generation = window._tuning_generation
    window.thread_count.setValue(1)
    assert window.processing_options() == before
    assert window._tuning_generation == generation
    assert window._pending_preview is None
    assert path in window.records
