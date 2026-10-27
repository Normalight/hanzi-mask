"""Per-image undo/redo and stable previews while a correction is persisted."""

import json
import sys
from threading import Event

import numpy as np
import pytest
from PIL import Image

pytest.importorskip('PySide6')
from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence
from PySide6.QtTest import QTest

from hanzi_mask.batch_service import load_mask
import ui.task_worker as worker_module
from test_canvas import draw_stroke
from test_desktop import (
    click_mask_pixel, idle, import_and_process, qapp, wait_for, window,
    write_two_components,
)


def brush(window, qapp, start, end, *, fill=False):
    window.brush_tool_button.click()
    (window.foreground_brush_button if fill else window.background_brush_button).click()
    draw_stroke(window.preview.mask_canvas, start, end)
    wait_for(qapp, lambda: idle(window))


def choose_history(window, qapp, row):
    item = window.history_list.item(row)
    QTest.mouseClick(window.history_list.viewport(), Qt.LeftButton,
                     pos=window.history_list.visualItemRect(item).center())
    wait_for(qapp, lambda: idle(window))


def test_history_labels_jump_undo_redo_and_export_current_state(window, qapp, tmp_path):
    path = write_two_components(tmp_path / 'glyph.png')
    source_bytes = path.read_bytes()
    import_and_process(window, qapp, path)
    states = [load_mask(window.records[path]).copy()]
    brush(window, qapp, (8, 40), (36, 40))
    states.append(load_mask(window.records[path]).copy())
    click_mask_pixel(window, qapp, 22, 20)
    window.clear_component_button.click()
    wait_for(qapp, lambda: idle(window))
    states.append(load_mask(window.records[path]).copy())
    brush(window, qapp, (44, 20), (54, 20), fill=True)
    states.append(load_mask(window.records[path]).copy())
    assert window.history_list.count() == 4
    for row, label in enumerate(('初始处理结果', '画笔分割', '清理色块', '画笔填补')):
        assert label in window.history_list.item(row).text()
    assert window.history_list.currentRow() == 3

    choose_history(window, qapp, 1)
    np.testing.assert_array_equal(load_mask(window.records[path]), states[1])
    assert window.redo_correction_button.isEnabled() and window.undo_correction_button.isEnabled()
    window.redo_correction_button.click()
    wait_for(qapp, lambda: idle(window))
    np.testing.assert_array_equal(load_mask(window.records[path]), states[2])
    choose_history(window, qapp, 3)
    np.testing.assert_array_equal(load_mask(window.records[path]), states[3])
    assert not window.redo_correction_button.isEnabled()
    choose_history(window, qapp, 0)
    assert not window.undo_correction_button.isEnabled()
    np.testing.assert_array_equal(load_mask(window.records[path]), states[0])
    choose_history(window, qapp, 2)
    window.save_mode.setCurrentIndex(window.save_mode.findData('export'))
    window.save_results()
    wait_for(qapp, lambda: idle(window))
    image_path = next((window._last_output / 'images').rglob('*.png'))
    with Image.open(image_path) as image:
        np.testing.assert_array_equal(np.asarray(image), 255 - states[2])
    report = json.loads((window._last_output / 'report.json').read_text())
    assert report['images'][0]['metrics']['correction_steps'] == 2
    assert report['images'][0]['metrics']['correction_operation'] == 'select'
    assert path.read_bytes() == source_bytes


def test_failed_branch_keeps_redo_but_successful_edit_replaces_future(window, qapp, tmp_path, monkeypatch):
    path = write_two_components(tmp_path / 'glyph.png')
    import_and_process(window, qapp, path)
    brush(window, qapp, (8, 40), (36, 40))
    first = window.records[path]
    brush(window, qapp, (44, 20), (54, 20), fill=True)
    future = window.records[path]
    window.undo_correction_button.click()
    wait_for(qapp, lambda: idle(window))
    original_apply = worker_module.apply_correction

    def fail(*_):
        raise ValueError('测试保存失败')

    monkeypatch.setattr(worker_module, 'apply_correction', fail)
    brush(window, qapp, (44, 30), (54, 30), fill=True)
    np.testing.assert_array_equal(load_mask(window.records[path]), load_mask(first))
    assert window.redo_correction_button.isEnabled()
    assert window._correction_redo[path][-1][0].mask_path == future.mask_path
    assert window.history_list.count() == 3
    monkeypatch.setattr(worker_module, 'apply_correction', original_apply)
    brush(window, qapp, (44, 30), (54, 30), fill=True)
    assert not window.redo_correction_button.isEnabled()
    assert not window._correction_redo.get(path)
    assert window.history_list.count() == 3
    assert load_mask(window.records[path])[20, 48] == 0
    assert load_mask(window.records[path])[30, 48] == 255


def test_redo_stays_with_its_image_and_rescan_but_clears_after_regeneration(window, qapp, tmp_path):
    first = write_two_components(tmp_path / 'input' / 'a.png')
    second = write_two_components(tmp_path / 'input' / 'b.png')
    import_and_process(window, qapp, first.parent)
    brush(window, qapp, (8, 40), (36, 40))
    revision = window.records[first]
    window.undo_correction_button.click()
    wait_for(qapp, lambda: idle(window))
    window.table.selectRow(1)
    wait_for(qapp, lambda: idle(window))
    assert window.current_source().path == second
    assert window.history_list.count() == 1 and not window.redo_correction_button.isEnabled()
    window.rescan()
    wait_for(qapp, lambda: idle(window))
    window.table.selectRow(0)
    wait_for(qapp, lambda: idle(window))
    assert window.redo_correction_button.isEnabled()
    assert window._correction_redo[first][-1][0].source == window.records[first].source
    window.redo_correction_button.click()
    wait_for(qapp, lambda: idle(window))
    np.testing.assert_array_equal(load_mask(window.records[first]), load_mask(revision))
    window.undo_correction_button.click()
    wait_for(qapp, lambda: idle(window))
    window.process_current()
    wait_for(qapp, lambda: idle(window))
    assert not window._correction_redo.get(first)
    assert window.history_list.count() == 1
    assert not window.undo_correction_button.isEnabled() and not window.redo_correction_button.isEnabled()


def test_history_shortcuts_apply_in_canvas_and_preserve_text_field_undo(window, qapp, tmp_path):
    path = write_two_components(tmp_path / 'glyph.png')
    import_and_process(window, qapp, path)
    original = window.records[path]
    brush(window, qapp, (8, 40), (36, 40))
    edited = window.records[path]
    window.search.setFocus()
    QTest.keyClicks(window.search, 'glyph')
    QTest.keySequence(window.search, QKeySequence(QKeySequence.Undo))
    assert window.search.text() == ''
    assert window.records[path].mask_path == edited.mask_path
    canvas = window.preview.mask_canvas
    canvas.setFocus()
    QTest.keySequence(canvas, QKeySequence(QKeySequence.Undo))
    wait_for(qapp, lambda: idle(window))
    assert window.records[path].mask_path == original.mask_path
    QTest.keySequence(canvas, QKeySequence('Ctrl+Shift+Z' if sys.platform == 'darwin' else 'Ctrl+Y'))
    wait_for(qapp, lambda: idle(window))
    assert window.records[path].mask_path == edited.mask_path


def test_pending_stroke_stays_visible_and_commits_without_clearing_or_reloading_preview(
        window, qapp, tmp_path, monkeypatch):
    path = write_two_components(tmp_path / 'glyph.png')
    import_and_process(window, qapp, path)
    entered, release = Event(), Event()
    original_apply = worker_module.apply_correction
    preview = window.preview
    original_key = preview.original_canvas.image.cacheKey()
    previous_request = window._preview_request
    previous_progress = (window.progress.minimum(), window.progress.maximum(), window.progress.value())
    cleared = []
    actual_clear = preview.clear

    def observed_clear():
        cleared.append(True)
        actual_clear()

    def paused_apply(*args):
        entered.set()
        assert release.wait(10)
        return original_apply(*args)

    monkeypatch.setattr(preview, 'clear', observed_clear)
    monkeypatch.setattr(worker_module, 'apply_correction', paused_apply)
    try:
        draw_stroke(preview.mask_canvas, (8, 40), (36, 40))
        wait_for(qapp, entered.is_set)
        assert preview.mask_canvas._mask[40, 22] == 255
        assert preview.mask_canvas.image.pixelColor(22, 40).red() == 255
        assert all(not canvas.image.isNull() for canvas in preview.canvases)
        assert preview.original_canvas.image.cacheKey() == original_key
        assert window.output_group.isEnabled()
        assert window.brush_options.isEnabled()
        assert not preview.mask_canvas.editable
        assert previous_progress == (window.progress.minimum(), window.progress.maximum(), window.progress.value())
        QTest.qWait(50)
        assert preview.mask_canvas.image.pixelColor(22, 40).red() == 255
    finally:
        release.set()
    wait_for(qapp, lambda: idle(window))
    assert not cleared and window._preview_request == previous_request
    assert preview.original_canvas.image.cacheKey() == original_key
    assert preview.mask_canvas._mask[40, 22] == 0
    assert preview.mask_canvas._pending_mask is None
    assert preview.changes_canvas.image.pixelColor(22, 40).red() > preview.changes_canvas.image.pixelColor(22, 40).green()


def test_undo_keeps_previews_visible_until_cached_revision_is_ready(window, qapp, tmp_path, monkeypatch):
    path = write_two_components(tmp_path / 'glyph.png')
    import_and_process(window, qapp, path)
    brush(window, qapp, (8, 40), (36, 40))
    entered, release = Event(), Event()
    actual_load = worker_module.load_result

    def paused_load(*args):
        entered.set()
        assert release.wait(10)
        return actual_load(*args)

    monkeypatch.setattr(worker_module, 'load_result', paused_load)
    try:
        window.undo_correction_button.click()
        wait_for(qapp, entered.is_set)
        assert window._preview_loading
        assert all(not canvas.image.isNull() for canvas in window.preview.canvases)
        assert not window.preview.mask_canvas.editable
        assert window.preview.mask_canvas.image.pixelColor(22, 40).red() == 255
    finally:
        release.set()
    wait_for(qapp, lambda: idle(window))
    assert window.preview.mask_canvas.image.pixelColor(22, 40).red() == 0
    assert window.preview.mask_canvas.editable


def test_twenty_step_history_limit_applies_across_undo_and_redo(window, qapp, tmp_path):
    path = write_two_components(tmp_path / 'glyph.png')
    import_and_process(window, qapp, path)
    for step in range(22):
        brush(window, qapp, (70, 18), (70, 18), fill=bool(step % 2))
    assert len(window._correction_history[path]) == 20
    assert window.history_list.count() == 21
    assert window.history_list.item(0).text().startswith('2 ·')
    choose_history(window, qapp, 0)
    assert len(window._correction_redo[path]) == 20
    assert not window.undo_correction_button.isEnabled()
    assert window.records[path].metrics['correction_steps'] == 2
    choose_history(window, qapp, 20)
    assert window.records[path].metrics['correction_steps'] == 22
    assert not window.redo_correction_button.isEnabled()


def test_switch_to_unprocessed_image_during_correction_runs_after_commit(window, qapp, tmp_path, monkeypatch):
    path = write_two_components(tmp_path / 'a.png')
    second = write_two_components(tmp_path / 'b.png')
    window.import_paths([tmp_path])
    wait_for(qapp, lambda: idle(window))
    entered, release = Event(), Event()
    actual_apply = worker_module.apply_correction

    def paused_apply(*args):
        entered.set()
        assert release.wait(10)
        return actual_apply(*args)

    monkeypatch.setattr(worker_module, 'apply_correction', paused_apply)
    try:
        draw_stroke(window.preview.mask_canvas, (8, 40), (36, 40))
        wait_for(qapp, entered.is_set)
        window.table.setCurrentCell(1, 1)
        assert window._pending_preview is not None
        QTest.qWait(400)
        assert window._task_operation == 'correct' and window.worker is not None
    finally:
        release.set()
    wait_for(qapp, lambda: idle(window) and window._pending_preview is None)
    assert set(window.records) == {path, second}
    assert window.records[path].metrics.get('manual_correction')
    assert len(window._correction_history[path]) == 1
    assert window.current_source().path == second
    window.table.setCurrentCell(0, 1)
    wait_for(qapp, lambda: idle(window))
    assert window.undo_correction_button.isEnabled()
