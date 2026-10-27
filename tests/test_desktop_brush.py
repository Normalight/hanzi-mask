"""Brush edits use the same revision, undo and publication flow as selection."""

import json
from threading import Event

import numpy as np
import pytest
from PIL import Image

pytest.importorskip('PySide6')
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest

from hanzi_mask.batch_service import load_mask
import ui.task_worker as worker_module
from test_canvas import draw_stroke, pixel_position
from test_desktop import (
    click_mask_pixel, idle, import_and_process, qapp, wait_for, window,
    write_two_components,
)


@pytest.mark.parametrize('style', ['black-on-white', 'white-on-black'])
@pytest.mark.parametrize('brush_size,start,end', [
    (3, (8, 40), (36, 40)),
    (1, (8, 26), (36, 54)),
])
def test_default_brush_cut_select_fill_export_and_undo(window, qapp, tmp_path, style, brush_size, start, end):
    path = write_two_components(tmp_path / 'input' / 'glyph.png')
    original_bytes = path.read_bytes()
    import_and_process(window, qapp, path)
    initial = window.records[path]
    original = load_mask(initial).copy()
    canvas = window.preview.mask_canvas
    assert window.brush_tool_button.isChecked() and canvas.tool == 'brush'
    assert window.background_brush_button.isChecked()
    assert window.brush_options.isVisible() and not window.clear_component_button.isVisible()
    window.output_style.setCurrentIndex(window.output_style.findData(style))
    assert ('白色' in window.background_brush_button.accessibleName()) == (style == 'black-on-white')
    window.brush_size_buttons[brush_size].click()
    draw_stroke(canvas, start, end)
    wait_for(qapp, lambda: idle(window))
    split = window.records[path]
    assert split.metrics['components'] == initial.metrics['components'] + 1
    assert split.options == initial.options
    assert load_mask(split)[40, 22] == 0
    assert canvas.tool == 'brush'
    assert window.undo_correction_button.isEnabled()

    click_mask_pixel(window, qapp, 22, 20)
    window.clear_component_button.click()
    wait_for(qapp, lambda: idle(window))
    cleaned = window.records[path]
    assert load_mask(cleaned)[20, 22] == 0
    assert load_mask(cleaned)[60, 22] == 255
    window.brush_tool_button.click()
    window.foreground_brush_button.click()
    window.brush_size_buttons[1].click()
    draw_stroke(canvas, (44, 20), (54, 20))
    wait_for(qapp, lambda: idle(window))
    filled = window.records[path]
    assert filled.metrics['correction_steps'] == 3
    assert filled.metrics['correction_added_pixels'] > 0
    assert np.all(load_mask(filled)[20, 44:55] == 255)
    assert len(window._correction_history[path]) == 3
    assert path.read_bytes() == original_bytes
    np.testing.assert_array_equal(load_mask(initial), original)

    window.save_mode.setCurrentIndex(window.save_mode.findData('export'))
    window.save_results()
    wait_for(qapp, lambda: idle(window))
    exported = next((window._last_output / 'images').rglob('*.png'))
    with Image.open(exported) as image:
        expected = 255 - load_mask(filled) if style == 'black-on-white' else load_mask(filled)
        np.testing.assert_array_equal(np.asarray(image), expected)
    report = json.loads((window._last_output / 'report.json').read_text())
    assert report['images'][0]['metrics']['correction_steps'] == 3
    for previous in (cleaned, split, initial):
        window.undo_correction_button.click()
        wait_for(qapp, lambda: idle(window))
        np.testing.assert_array_equal(load_mask(window.records[path]), load_mask(previous))
    assert not window.undo_correction_button.isEnabled()
    assert not window._test_messages


def test_brush_color_shortcut_tracks_polarity_and_does_not_edit_result(window, qapp, tmp_path):
    path = write_two_components(tmp_path / 'glyph.png')
    import_and_process(window, qapp, path)
    item = window.records[path]
    canvas = window.preview.mask_canvas
    canvas.setFocus()
    QTest.keyClick(canvas, Qt.Key_X)
    assert canvas.brush_foreground and window.foreground_brush_button.isChecked()
    assert '黑色' in window.foreground_brush_button.accessibleName()
    window.preview.set_output_style('white-on-black')
    assert canvas.brush_foreground and window.foreground_brush_button.isChecked()
    assert '白色' in window.foreground_brush_button.accessibleName()
    QTest.keyClick(canvas, Qt.Key_X)
    assert not canvas.brush_foreground and window.background_brush_button.isChecked()
    assert '黑色' in window.background_brush_button.accessibleName()
    assert window.records[path] is item and not window._correction_history


def test_brush_failure_preserves_cache_and_history(window, qapp, tmp_path, monkeypatch):
    path = write_two_components(tmp_path / 'glyph.png')
    import_and_process(window, qapp, path)
    initial = window.records[path]

    def fail(*_):
        raise ValueError('模拟人工矫正缓存写入失败')

    monkeypatch.setattr(worker_module, 'apply_correction', fail)
    draw_stroke(window.preview.mask_canvas, (8, 40), (36, 40))
    wait_for(qapp, lambda: idle(window))
    assert window.records[path] is initial
    assert not window._correction_history
    np.testing.assert_array_equal(window.preview.mask_canvas._mask, load_mask(initial))
    assert window._test_messages


def test_brush_stroke_keeps_cached_image_without_automatic_reprocessing(window, qapp, tmp_path):
    path = write_two_components(tmp_path / 'glyph.png')
    import_and_process(window, qapp, path)
    assert window._pending_preview is None
    canvas = window.preview.mask_canvas
    point = pixel_position(canvas, 22, 40)
    QTest.mousePress(canvas, Qt.LeftButton, pos=point)
    assert window._pending_preview is None and not window._tuning_timer.isActive()
    QTest.qWait(400)
    assert window.worker is None and canvas._stroke_mask is not None
    QTest.mouseRelease(canvas, Qt.LeftButton, pos=point)
    wait_for(qapp, lambda: idle(window))
    assert window.records[path].metrics['manual_correction']
    assert len(window._correction_history[path]) == 1


def test_switch_image_during_stroke_cannot_edit_either_image(window, qapp, tmp_path):
    first = write_two_components(tmp_path / 'input' / 'a.png')
    second = write_two_components(tmp_path / 'input' / 'b.png')
    import_and_process(window, qapp, first.parent)
    initial = dict(window.records)
    canvas = window.preview.mask_canvas
    point = pixel_position(canvas, 22, 40)
    QTest.mousePress(canvas, Qt.LeftButton, pos=point)
    window.table.selectRow(1)
    wait_for(qapp, lambda: idle(window))
    QTest.mouseRelease(canvas, Qt.LeftButton, pos=point)
    assert window.current_source().path == second
    assert window.records == initial
    assert not window._correction_history


def test_brush_can_fill_empty_result_and_keeps_adoption_explicit(window, qapp, tmp_path):
    path = write_two_components(tmp_path / 'glyph.png')
    import_and_process(window, qapp, path)
    click_mask_pixel(window, qapp)
    click_mask_pixel(window, qapp, 22, 60, Qt.ControlModifier)
    window.clear_component_button.click()
    wait_for(qapp, lambda: idle(window))
    assert window.records[path].metrics['foreground_pixels'] == 0
    assert window.table.item(0, 0).checkState() == Qt.Unchecked
    window.brush_tool_button.click()
    assert window.brush_options.isEnabled()
    window.foreground_brush_button.click()
    draw_stroke(window.preview.mask_canvas, (22, 10), (22, 60))
    wait_for(qapp, lambda: idle(window))
    assert window.records[path].metrics['foreground_pixels'] > 0
    assert window.table.item(0, 0).checkState() == Qt.Unchecked
    assert window.table.item(0, 0).flags() & Qt.ItemIsEnabled
    window.table.item(0, 0).setCheckState(Qt.Checked)
    assert window.adopted_items()


def test_pending_correction_blocks_another_stroke(window, qapp, tmp_path, monkeypatch):
    path = write_two_components(tmp_path / 'glyph.png')
    import_and_process(window, qapp, path)
    started, release = Event(), Event()
    original_apply = worker_module.apply_correction

    def paused_apply(*args):
        started.set()
        assert release.wait(10)
        return original_apply(*args)

    monkeypatch.setattr(worker_module, 'apply_correction', paused_apply)
    canvas = window.preview.mask_canvas
    try:
        draw_stroke(canvas, (8, 40), (36, 40))
        wait_for(qapp, started.is_set)
        assert not canvas.editable and window.brush_options.isEnabled()
        draw_stroke(canvas, (60, 18), (83, 18))
        assert canvas._stroke_mask is None
    finally:
        release.set()
    wait_for(qapp, lambda: idle(window))
    assert len(window._correction_history[path]) == 1
    assert load_mask(window.records[path])[18, 70] == 255
