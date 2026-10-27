"""Real Qt worker, review and publication flows without external services."""

import json
import os
from pathlib import Path
import re
import subprocess
import sys
from threading import Event
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
from PIL import Image, ImageDraw
import pytest

pytest.importorskip("PySide6")
from PySide6.QtCore import QCoreApplication, QEvent, QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox

import ui.task_worker as worker_module
import hanzi_mask.batch_service as batch_module
from hanzi_mask.batch_service import ReplacementError
from ui.window import MainWindow


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    previous = app.quitOnLastWindowClosed()
    app.setQuitOnLastWindowClosed(False)
    yield app
    app.setQuitOnLastWindowClosed(previous)


def wait_for(app, condition, timeout=30):
    # Loaded CI runners can stretch texture solves well past a local 8s;
    # the wait only costs time when a condition is genuinely late.
    deadline = time.monotonic() + timeout
    while not condition() and time.monotonic() < deadline:
        app.processEvents()
        QTest.qWait(5)
    app.processEvents()
    assert condition(), "Qt 任务未在时限内结束或达到预期状态"


def idle(window):
    return window.worker is None and not window._preview_workers and window._pending_preview is None


@pytest.fixture
def window(qapp, monkeypatch, tmp_path):
    messages = []
    for method in ("warning", "information", "critical"):
        monkeypatch.setattr(QMessageBox, method,
                            lambda *args, **kwargs: messages.append(args[1:]) or QMessageBox.Ok)
    widget = MainWindow()
    widget.output.setText(str(tmp_path / "saved"))
    widget._test_messages = messages
    widget.show()
    qapp.processEvents()
    if qapp.platformName() == 'cocoa':
        # A CLI-launched Cocoa test process is not automatically activated.
        # QTest's synthetic mouse events do not perform that OS activation,
        # so establish the Qt active window before checking real focus routing.
        widget.raise_()
        widget.activateWindow()
        widget.windowHandle().requestActivate()
        QApplication.setActiveWindow(widget)
        wait_for(qapp, lambda: qapp.activeWindow() is widget and widget.isActiveWindow()
                 and qapp.focusWidget() is not None)
    yield widget
    widget.cancel_task()
    widget.close()
    wait_for(qapp, lambda: idle(widget) and not widget.isVisible())
    widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    qapp.processEvents()


def write_rubbing(path, dimensions=(72, 64)):
    path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", dimensions, "#181818")
    draw = ImageDraw.Draw(image)
    draw.rectangle((15, 8, 25, 52), fill="#e5e5e5")
    draw.rectangle((15, 42, 58, 52), fill="#e5e5e5")
    draw.rectangle((2, 2, 3, 3), fill="#bbbbbb")
    image.save(path)
    return path


def write_two_components(path):
    """Two substantial, separated strokes that survive ordinary texture cleanup."""
    path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", (96, 80), "#181818")
    draw = ImageDraw.Draw(image)
    draw.rectangle((14, 10, 30, 67), fill="#e5e5e5")
    draw.rectangle((14, 54, 80, 67), fill="#e5e5e5")
    draw.rectangle((62, 10, 81, 28), fill="#e5e5e5")
    image.save(path)
    return path


def click_mask_pixel(window, app, x=70, y=18, modifiers=Qt.NoModifier, *, editable=True):
    """Click a source-image pixel through the fitted viewport transform."""
    window.select_tool_button.click()
    canvas = window.preview.mask_canvas
    rectangle = canvas.image_rect()
    width, height = window.preview.image_size.width(), window.preview.image_size.height()
    point = QPoint(round(rectangle.left() + (x + .5) * rectangle.width() / width),
                   round(rectangle.top() + (y + .5) * rectangle.height() / height))
    assert canvas.rect().contains(point)
    QTest.mouseClick(canvas, Qt.LeftButton, modifiers, pos=point)
    app.processEvents()
    assert window.preview.selection_count > 0
    assert window.clear_component_button.isEnabled() == editable
    return window.preview.selection_count


def import_and_process(window, qapp, source):
    window.import_paths([source])
    wait_for(qapp, lambda: idle(window))
    assert window.inputs
    window.process_all()
    assert window.worker is not None
    wait_for(qapp, lambda: idle(window))
    assert not window.errors
    assert len(window.records) == len(window.inputs)
    assert not window._test_messages


def test_standalone_launch_and_recursive_import(window, qapp, tmp_path):
    first = write_rubbing(tmp_path / "examples" / "安" / "small.png")
    second = write_rubbing(tmp_path / "examples" / "水" / "wide.png", (83, 65))
    assert window.isVisible()
    assert '字形换底' in window.windowTitle()
    assert window.save_mode.currentData() == 'replace'
    assert window.output_style.currentData() == 'black-on-white'
    assert window.preview.output_style == 'black-on-white'
    options = window.processing_options()
    assert options.method == "texture"
    assert options.keep_isolated
    assert options.edge_refine and options.stroke_recovery and options.tip_recovery
    assert window.worker is None
    window.import_paths([tmp_path / "examples"])
    wait_for(qapp, lambda: idle(window))
    assert {source.path for source in window.inputs} == {first, second}
    assert {source.relative_path.as_posix() for source in window.inputs} == {"安/small.png", "水/wide.png"}
    assert window.table.rowCount() == 2
    assert options.texture_strength == 0
    assert set(window.records) == {first}
    assert window.history_panel.isVisible()
    assert window.preview.has_image
    assert not window._test_messages


def test_client_launch_in_clean_process_does_not_import_login_or_training():
    project = Path(__file__).resolve().parents[1]
    script = """
import sys
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication
from ui.window import MainWindow
app = QApplication([])
app.setQuitOnLastWindowClosed(False)
window = MainWindow()
window.show()
app.processEvents()
assert window.isVisible()
assert 'hanzi_auth' not in sys.modules
assert 'hanzi_update' not in sys.modules
assert 'torch' not in sys.modules
window.close()
window.deleteLater()
QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
app.processEvents()
"""
    completed = subprocess.run([sys.executable, "-c", script], cwd=project,
                               capture_output=True, text=True, timeout=20)
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_background_processing_review_and_export_only_adopted(window, qapp, tmp_path):
    source = tmp_path / "input"
    paths = [write_rubbing(source / char / "crop.png") for char in ("安", "水")]
    originals = {path: path.read_bytes() for path in paths}
    import_and_process(window, qapp, source)
    assert len(window.adopted_items()) == 2
    # Both results came from the worker's on-disk cache, not retained image arrays.
    assert all(item.result is None and item.mask_path.is_file() for item in window.records.values())
    window.save_mode.setCurrentIndex(window.save_mode.findData('export'))
    window.table.item(1, 0).setCheckState(Qt.Unchecked)
    adopted = window.adopted_items()[0].source
    window.save_results()
    wait_for(qapp, lambda: idle(window))
    output = window._last_output
    report = json.loads((output / "report.json").read_text())
    exported = list((output / "images").rglob("*.png"))
    assert len(exported) == 1
    assert exported[0].relative_to(output / "images") == adopted.relative_path
    assert report["summary"]["exported"] == 1
    assert report["images"][0]["source"] == str(adopted.path)
    with Image.open(exported[0]) as image:
        assert image.size == (72, 64)
        assert set(np.unique(np.asarray(image))) == {0, 255}
    assert all(path.read_bytes() == originals[path] for path in paths)
    assert not window._test_messages


def test_export_keeps_fixed_processing_parameters_and_adoption(window, qapp, tmp_path):
    path = write_rubbing(tmp_path / "input" / "crop.png")
    import_and_process(window, qapp, path)
    assert window.save_button.isEnabled()
    assert window.adopted_items()
    previous = window.records[path]
    previous_state = window.table.item(0, 3).text()
    wait_for(qapp, lambda: idle(window))
    assert window.processing_options().profile == "balanced"
    assert window.processing_options().texture_strength == 0
    assert window.records[path] is previous
    assert window.adopted_items() == [previous]
    assert window.table.item(0, 0).checkState() == Qt.Checked
    assert window.table.item(0, 3).text() == previous_state
    assert window.save_button.isEnabled()
    assert not window.preview.mask_canvas.image.isNull()
    window.save_mode.setCurrentIndex(window.save_mode.findData('export'))
    window.save_results()
    wait_for(qapp, lambda: idle(window))
    report = json.loads((window._last_output / "report.json").read_text())
    assert report["images"][0]["parameters"] == previous.options.to_dict()


@pytest.mark.parametrize('output_style', ['black-on-white', 'white-on-black'])
def test_component_cleanup_updates_only_cached_mask_supports_undo_and_exports_correction(
        window, qapp, tmp_path, output_style):
    path = write_two_components(tmp_path / "input" / "glyph.png")
    original_bytes = path.read_bytes()
    import_and_process(window, qapp, path)
    original_item = window.records[path]
    original_mask = batch_module.load_mask(original_item).copy()
    original_cached_bytes = original_item.mask_path.read_bytes()
    assert original_mask[18, 70] == original_mask[60, 22] == 255

    selected_pixels = click_mask_pixel(window, qapp)
    window.output_style.setCurrentIndex(window.output_style.findData(output_style))
    assert window.preview.output_style == output_style
    assert window.preview.selection_count == selected_pixels
    assert window.worker is None
    assert window.records[path] is original_item
    window.clear_component_button.click()
    wait_for(qapp, lambda: idle(window))
    corrected_item = window.records[path]
    corrected_mask = batch_module.load_mask(corrected_item).copy()
    assert corrected_item.result is None and corrected_item.mask_path.is_file()
    assert corrected_item.mask_path != original_item.mask_path
    assert corrected_item.source_sha256 == original_item.source_sha256
    assert corrected_mask[18, 70] == 0
    assert corrected_mask[60, 22] == 255
    assert np.count_nonzero(original_mask) - np.count_nonzero(corrected_mask) == selected_pixels
    assert corrected_item.metrics["foreground_pixels"] == np.count_nonzero(corrected_mask)
    assert window.table.item(0, 3).text() == "已矫正"
    assert window.preview.selection_count == 0
    assert window.undo_correction_button.isEnabled()
    assert path.read_bytes() == original_bytes
    assert original_item.mask_path.read_bytes() == original_cached_bytes

    window.undo_correction_button.click()
    wait_for(qapp, lambda: idle(window))
    np.testing.assert_array_equal(batch_module.load_mask(window.records[path]), original_mask)
    assert not window.undo_correction_button.isEnabled()
    assert not window._correction_history.get(path)

    click_mask_pixel(window, qapp)
    window.clear_component_button.click()
    wait_for(qapp, lambda: idle(window))
    np.testing.assert_array_equal(batch_module.load_mask(window.records[path]), corrected_mask)
    assert window.undo_correction_button.isEnabled()
    window.preview.set_output_style('white-on-black' if output_style == 'black-on-white' else 'black-on-white')
    assert window.output_style.currentData() == window.preview.output_style
    assert window.undo_correction_button.isEnabled()
    window.preview.set_output_style(output_style)
    window.save_mode.setCurrentIndex(window.save_mode.findData('export'))
    window.save_results()
    wait_for(qapp, lambda: idle(window))
    exported = list((window._last_output / "images").rglob("*.png"))
    assert len(exported) == 1
    with Image.open(exported[0]) as image:
        assert image.size == (96, 80)
        expected = 255 - corrected_mask if output_style == 'black-on-white' else corrected_mask
        np.testing.assert_array_equal(np.asarray(image), expected)
    report = json.loads((window._last_output / "report.json").read_text())
    assert report['output_style'] == output_style
    assert report["images"][0]["metrics"]["foreground_pixels"] == np.count_nonzero(corrected_mask)
    assert path.read_bytes() == original_bytes
    assert not window._test_messages


def test_switching_images_keeps_component_correction_and_undo_independent(
        window, qapp, tmp_path):
    folder = tmp_path / "input"
    first = write_two_components(folder / "a.png")
    second = write_two_components(folder / "b.png")
    originals = {path: path.read_bytes() for path in (first, second)}
    import_and_process(window, qapp, folder)
    initial_masks = {path: batch_module.load_mask(window.records[path]).copy()
                     for path in (first, second)}

    click_mask_pixel(window, qapp)
    window.clear_component_button.click()
    wait_for(qapp, lambda: idle(window))
    first_corrected = batch_module.load_mask(window.records[first]).copy()
    window.table.selectRow(1)
    wait_for(qapp, lambda: idle(window))
    assert window.current_source().path == second
    assert window.preview.selection_count == 0
    assert not window.undo_correction_button.isEnabled()
    window.undo_correction()
    np.testing.assert_array_equal(batch_module.load_mask(window.records[second]), initial_masks[second])

    click_mask_pixel(window, qapp)
    # Delete exercises the canvas clear_requested signal through the same UI action.
    QTest.keyClick(window.preview.mask_canvas, Qt.Key_Delete)
    wait_for(qapp, lambda: idle(window))
    second_corrected = batch_module.load_mask(window.records[second]).copy()
    assert second_corrected[18, 70] == 0
    np.testing.assert_array_equal(batch_module.load_mask(window.records[first]), first_corrected)
    assert window._correction_history.get(first) and window._correction_history.get(second)

    window.table.selectRow(0)
    wait_for(qapp, lambda: idle(window))
    assert window.undo_correction_button.isEnabled()
    window.undo_correction_button.click()
    wait_for(qapp, lambda: idle(window))
    np.testing.assert_array_equal(batch_module.load_mask(window.records[first]), initial_masks[first])
    np.testing.assert_array_equal(batch_module.load_mask(window.records[second]), second_corrected)
    assert not window._correction_history.get(first)
    assert window._correction_history.get(second)
    window.table.selectRow(1)
    wait_for(qapp, lambda: idle(window))
    window.undo_correction_button.click()
    wait_for(qapp, lambda: idle(window))
    np.testing.assert_array_equal(batch_module.load_mask(window.records[second]), initial_masks[second])
    assert all(path.read_bytes() == originals[path] for path in (first, second))
    assert not window._test_messages


@pytest.mark.parametrize('key', [Qt.Key_Backspace, Qt.Key_Delete])
@pytest.mark.parametrize('inverted', [False, True])
@pytest.mark.parametrize('multiselect', [False, True])
def test_focused_mask_keyboard_cleanup_supports_both_polarities_multiselect_and_undo(
        window, qapp, tmp_path, key, inverted, multiselect):
    path = write_two_components(tmp_path / 'input' / 'glyph.png')
    original_bytes = path.read_bytes()
    import_and_process(window, qapp, path)
    initial = window.records[path]
    initial_mask = batch_module.load_mask(initial).copy()
    window.preview.set_mask_inverted(inverted)
    selected = click_mask_pixel(window, qapp)
    if multiselect:
        selected = click_mask_pixel(window, qapp, x=22, y=60, modifiers=Qt.ControlModifier)
    assert QApplication.focusWidget() is window.preview.mask_canvas
    assert window.preview.mask_canvas.hasFocus()
    QTest.keyClick(QApplication.focusWidget(), key)
    assert window.worker is not None
    wait_for(qapp, lambda: idle(window))
    corrected = batch_module.load_mask(window.records[path])
    assert np.count_nonzero(initial_mask) - np.count_nonzero(corrected) == selected
    assert corrected[18, 70] == 0
    assert corrected[60, 22] == (0 if multiselect else 255)
    assert window.preview.mask_canvas.inverted == inverted
    assert window.preview.selection_count == 0
    assert window.undo_correction_button.isEnabled()
    current = window.records[path]
    # The same focused key has no cleanup action once the selection is empty.
    QTest.keyClick(QApplication.focusWidget(), key)
    qapp.processEvents()
    assert window.worker is None
    assert window.records[path] is current
    assert len(window._correction_history[path]) == 1
    window.undo_correction_button.click()
    wait_for(qapp, lambda: idle(window))
    np.testing.assert_array_equal(batch_module.load_mask(window.records[path]), initial_mask)
    assert path.read_bytes() == original_bytes
    assert not window._test_messages


@pytest.mark.parametrize('key', [Qt.Key_Backspace, Qt.Key_Delete])
@pytest.mark.parametrize('editor', ['output', 'search'])
def test_text_editor_delete_keys_edit_text_without_removing_selected_component(
        window, qapp, tmp_path, key, editor):
    path = write_two_components(tmp_path / 'input' / '12345-1245-1235.png')
    import_and_process(window, qapp, path)
    previous = window.records[path]
    pixels = click_mask_pixel(window, qapp)
    line = getattr(window, editor)
    line.setText('12345')
    line.setCursorPosition(3)
    expected = '1245' if key == Qt.Key_Backspace else '1235'
    line.setFocus()
    qapp.processEvents()
    focused = QApplication.focusWidget()
    assert focused is line
    assert focused is not window.preview.mask_canvas
    QTest.keyClick(focused, key)
    qapp.processEvents()
    assert line.text() == expected
    assert window.preview.selection_count == pixels
    assert window.worker is None
    assert window.records[path] is previous
    assert not window._correction_history.get(path)
    assert not window._test_messages


@pytest.mark.parametrize("adopted", [True, False])
def test_clearing_all_components_excludes_empty_mask_and_undo_restores_adoption(
        window, qapp, tmp_path, adopted):
    path = write_two_components(tmp_path / "input" / "glyph.png")
    original_bytes = path.read_bytes()
    import_and_process(window, qapp, path)
    initial_mask = batch_module.load_mask(window.records[path]).copy()
    window.table.item(0, 0).setCheckState(Qt.Checked if adopted else Qt.Unchecked)
    click_mask_pixel(window, qapp)
    selected = click_mask_pixel(window, qapp, x=22, y=60, modifiers=Qt.ControlModifier)
    assert selected == np.count_nonzero(initial_mask)
    window.clear_component_button.click()
    wait_for(qapp, lambda: idle(window))
    assert not np.any(batch_module.load_mask(window.records[path]))
    assert window.table.item(0, 0).checkState() == Qt.Unchecked
    assert not window.save_button.isEnabled()
    assert window.undo_correction_button.isEnabled()
    window.undo_correction_button.click()
    wait_for(qapp, lambda: idle(window))
    np.testing.assert_array_equal(batch_module.load_mask(window.records[path]), initial_mask)
    assert window.table.item(0, 0).checkState() == (Qt.Checked if adopted else Qt.Unchecked)
    assert window.save_button.isEnabled() == adopted
    assert path.read_bytes() == original_bytes


@pytest.mark.parametrize("change", ["reprocess", "rescan"])
def test_only_successful_regeneration_clears_component_undo_history(window, qapp, tmp_path, change):
    path = write_two_components(tmp_path / "input" / "glyph.png")
    import_and_process(window, qapp, path)
    initial_mask = batch_module.load_mask(window.records[path]).copy()
    click_mask_pixel(window, qapp)
    window.clear_component_button.click()
    wait_for(qapp, lambda: idle(window))
    assert window._correction_history.get(path)
    corrected = window.records[path]
    if change == "reprocess":
        window.process_current()
        wait_for(qapp, lambda: idle(window))
        np.testing.assert_array_equal(batch_module.load_mask(window.records[path]), initial_mask)
    else:
        window.rescan()
        wait_for(qapp, lambda: idle(window))
        assert window.records[path].mask_path == corrected.mask_path
    assert bool(window._correction_history.get(path)) == (change == "rescan")
    assert window.undo_correction_button.isEnabled() == (change == "rescan")
    window.undo_correction()
    if change == "rescan":
        np.testing.assert_array_equal(batch_module.load_mask(window.records[path]), initial_mask)


def test_cancelled_scan_worker_does_not_publish_a_new_input_list(
        window, qapp, tmp_path, monkeypatch):
    first = write_rubbing(tmp_path / "old" / "a.png")
    new = write_rubbing(tmp_path / "new" / "b.png")
    import_and_process(window, qapp, first)
    previous_inputs = list(window.inputs)
    previous_records = dict(window.records)
    entered, release = Event(), Event()
    cancellation_states = []
    real_scan = worker_module.scan_inputs

    def paused_scan(paths, recursive=False, exclude_paths=(), cancel=None):
        assert callable(cancel), "扫描任务必须传入 QThread 中断检查"

        def pause_at_first_check():
            if not entered.is_set():
                entered.set()
                assert release.wait(5)
            state = cancel()
            cancellation_states.append(state)
            return state

        return real_scan(paths, recursive, exclude_paths, cancel=pause_at_first_check)

    monkeypatch.setattr(worker_module, "scan_inputs", paused_scan)
    window.import_paths([new.parent], append=False)
    try:
        wait_for(qapp, lambda: entered.is_set())
        assert window.worker.isRunning()
        window.cancel_task()
        assert window.worker.isInterruptionRequested()
    finally:
        release.set()
    wait_for(qapp, lambda: idle(window))

    assert cancellation_states and cancellation_states[0] is True
    assert window.inputs == previous_inputs
    assert window.records == previous_records
    assert window.table.rowCount() == 1
    assert window.current_source().path == first
    assert all(source.path != new for source in window.inputs)
    assert window.status.text() == "已取消扫描"
    assert not window._test_messages


def test_filtering_moves_selection_off_hidden_rows_and_clears_empty_preview(
        window, qapp, tmp_path):
    folder = tmp_path / "input"
    first = write_rubbing(folder / "first.png")
    second = write_rubbing(folder / "second.png", (85, 67))
    window.import_paths([folder])
    wait_for(qapp, lambda: idle(window))
    assert window.current_source().path == first

    window.search.setText("second")
    wait_for(qapp, lambda: idle(window))

    assert window.table.isRowHidden(0)
    assert window.current_source().path == second
    assert not window.table.isRowHidden(window.table.currentRow())
    assert window.preview.original_canvas.image.size().width() == 85
    window.search.setText("no-matching-image")
    wait_for(qapp, lambda: idle(window))
    assert window.current_source() is None
    assert not window.preview.has_image
    assert not window.process_current_button.isEnabled()
    window.process_current()
    assert window.worker is None


def test_export_partial_failure_preserves_successes_and_reports_failure_count(
        window, qapp, tmp_path):
    source = tmp_path / "input"
    first = write_rubbing(source / "a.png")
    second = write_rubbing(source / "b.png")
    originals = {path: path.read_bytes() for path in (first, second)}
    import_and_process(window, qapp, source)
    # Corrupt only the session cache: publishing must report that one item failed
    # while retaining the successful export and all original image bytes.
    window.records[second].mask_path.write_bytes(b"damaged cached image")
    window.save_mode.setCurrentIndex(window.save_mode.findData('export'))
    window.save_results()
    wait_for(qapp, lambda: idle(window))

    output = window._last_output
    report = json.loads((output / "report.json").read_text())
    assert report["summary"]["exported"] == 1
    assert report["summary"]["errors"] == 1
    assert len(list((output / "images").rglob("*.png"))) == 1
    assert {row["status"] for row in report["images"]} == {"exported", "error"}
    assert re.search(r"(?:失败[：:\s]*1|1\s*张?\s*失败)", window.status.text())
    assert all(path.read_bytes() == originals[path] for path in (first, second))


def track_processing(monkeypatch):
    calls = []
    real = worker_module.process_batch

    def tracked(inputs, options, *args, **kwargs):
        sources = list(inputs)
        calls.append(([source.path for source in sources], options.texture_strength))
        return real(sources, options, *args, **kwargs)

    monkeypatch.setattr(worker_module, "process_batch", tracked)
    return calls


def test_reprocessing_failure_preserves_previous_mask_and_adoption(
        window, qapp, tmp_path, monkeypatch):
    path = write_rubbing(tmp_path / "input" / "crop.png")
    import_and_process(window, qapp, path)
    assert not window.preview.mask_canvas.image.isNull()
    previous = window.records[path]
    previous_state = window.table.item(0, 3).text()

    def failed_image(*args, **kwargs):
        raise OSError("模拟原图暂时无法读取")

    monkeypatch.setattr(batch_module, "process_single", failed_image)
    window.process_current()
    wait_for(qapp, lambda: idle(window))

    assert window.records[path] is previous
    assert window.adopted_items() == [previous]
    assert window.table.item(0, 3).text() == previous_state
    assert "模拟原图暂时无法读取" in window.table.item(0, 4).toolTip()
    assert window.table.item(0, 0).checkState() == Qt.Checked
    assert not window.preview.mask_canvas.image.isNull()
    assert window.save_button.isEnabled()
    assert not window._test_messages


@pytest.mark.parametrize('output_style', ['black-on-white', 'white-on-black'])
def test_replacement_requires_confirmation_backs_up_then_restores_original_bytes(
        window, qapp, tmp_path, monkeypatch, output_style):
    path = write_rubbing(tmp_path / "input" / "crop.png")
    original = path.read_bytes()
    import_and_process(window, qapp, path)
    mask = batch_module.load_mask(window.records[path]).copy()
    window.output_style.setCurrentIndex(window.output_style.findData(output_style))
    assert window.save_mode.currentData() == 'replace'
    assert '原位置' in window.save_button.text()
    calls = []

    def refuse(*args, **kwargs):
        calls.append(args[1])
        assert window.output_style.currentText() in args[2]
        return QMessageBox.No

    monkeypatch.setattr(QMessageBox, "question", refuse)
    window.save_button.click()
    assert window.worker is None
    assert path.read_bytes() == original
    assert window._replacement_report is None
    assert calls == ['保存到原位置']
    monkeypatch.setattr(QMessageBox, "question", lambda *args, **kwargs: QMessageBox.Yes)
    window.save_button.click()
    wait_for(qapp, lambda: idle(window))
    report_path = window._replacement_report
    report = json.loads(report_path.read_text())
    assert report["status"] == "completed"
    assert report['output_style'] == output_style
    assert len(report["images"]) == 1
    backup = report_path.parent / report["images"][0]["backup"]
    assert backup.read_bytes() == original
    assert path.read_bytes() != original
    with Image.open(path) as image:
        assert set(np.unique(np.asarray(image))) == {0, 255}
        np.testing.assert_array_equal(np.asarray(image), mask if output_style == 'white-on-black' else 255 - mask)
    assert not window.records
    assert report_path.is_file()
    window.restore_last()
    wait_for(qapp, lambda: idle(window))
    assert path.read_bytes() == original
    assert json.loads(report_path.read_text())["status"] == "restored"
    assert not window._test_messages


@pytest.mark.parametrize("mode", ["export", "replace"])
@pytest.mark.parametrize("destination", ["same", "ancestor"])
def test_save_rejects_directory_that_contains_import_root(
        window, qapp, tmp_path, monkeypatch, mode, destination):
    source = tmp_path / "input"
    path = write_rubbing(source / "crop.png")
    original = path.read_bytes()
    import_and_process(window, qapp, source)
    window.save_mode.setCurrentIndex(window.save_mode.findData(mode))
    window.output.setText(str(source if destination == "same" else tmp_path))
    monkeypatch.setattr(QMessageBox, "question", lambda *args, **kwargs: QMessageBox.Yes)
    before = set(tmp_path.rglob("*"))

    window.save_results()

    assert window.worker is None
    assert window._last_output is None
    assert window.adopted_items()
    assert path.read_bytes() == original
    assert set(tmp_path.rglob("*")) == before
    assert window._test_messages


@pytest.mark.parametrize("operation", ["replace", "restore"])
def test_failed_mutating_task_invalidates_reviewed_results_and_preserves_recovery(
        window, qapp, tmp_path, monkeypatch, operation):
    path = write_rubbing(tmp_path / "input" / "crop.png")
    import_and_process(window, qapp, path)
    assert window.save_mode.currentData() == 'replace'
    monkeypatch.setattr(QMessageBox, "question", lambda *args, **kwargs: QMessageBox.Yes)
    if operation == "restore":
        window.save_results()
        wait_for(qapp, lambda: idle(window))
        # Review the replaced file so failure of recovery must invalidate that
        # specific processed state, even if disk bytes have already changed.
        window.process_all()
        wait_for(qapp, lambda: idle(window))
        assert window.records
    real = getattr(worker_module, "replace_originals" if operation == "replace" else "restore_replacement")

    def fail_after_disk_change(*args, **kwargs):
        result = real(*args, **kwargs)
        raise ReplacementError("模拟保存恢复状态失败", result.report_path, [])

    monkeypatch.setattr(worker_module,
                        "replace_originals" if operation == "replace" else "restore_replacement",
                        fail_after_disk_change)
    if operation == "replace":
        window.save_results()
    else:
        window.restore_last()
    wait_for(qapp, lambda: idle(window))

    assert not window.records
    assert not window.adopted_items()
    assert not window.save_button.isEnabled()
    assert window.table.item(0, 0).checkState() == Qt.Unchecked
    assert window.table.item(0, 3).text() == "未处理"
    assert window.preview.mask_canvas.image.isNull()
    assert window._replacement_report.is_file()
    assert window._test_messages


@pytest.mark.parametrize("operation", ["process", "preview", "correct"])
def test_close_waits_for_running_worker_and_keeps_cache_until_finish(
        window, qapp, tmp_path, monkeypatch, operation):
    writer = write_two_components if operation == "correct" else write_rubbing
    path = writer(tmp_path / "input" / "crop.png")
    entered, release = Event(), Event()
    if operation == "process":
        window.import_paths([path])
        wait_for(qapp, lambda: idle(window))
        real = worker_module.process_batch

        def blocked(*args, **kwargs):
            entered.set()
            assert release.wait(5), "测试没有释放后台处理"
            return real(*args, **kwargs)

        monkeypatch.setattr(worker_module, "process_batch", blocked)
        window.process_all()
    elif operation == "correct":
        import_and_process(window, qapp, path)
        real = worker_module.apply_correction

        def blocked(*args, **kwargs):
            entered.set()
            assert release.wait(5), "测试没有释放后台矫正"
            return real(*args, **kwargs)

        monkeypatch.setattr(worker_module, "apply_correction", blocked)
        click_mask_pixel(window, qapp)
        window.clear_component_button.click()
    else:
        real = worker_module.load_image

        def blocked(*args, **kwargs):
            entered.set()
            assert release.wait(5), "测试没有释放后台预览"
            return real(*args, **kwargs)

        monkeypatch.setattr(worker_module, "load_image", blocked)
        window.import_paths([path])
    try:
        wait_for(qapp, lambda: entered.is_set() and
                 (operation != "preview" or window.worker is None))
        thread = window.worker if operation != "preview" else next(iter(window._preview_workers))
        assert thread.isRunning()
        cache = window._cache_root
        window.close()
        assert window._closing
        assert window.isVisible()
        assert thread.isRunning()
        assert thread.isInterruptionRequested()
        assert cache.is_dir()
    finally:
        release.set()
    wait_for(qapp, lambda: idle(window) and not window.isVisible())
    assert not cache.exists()


def test_late_preview_cannot_overwrite_new_selection(window, qapp, tmp_path, monkeypatch):
    directory = tmp_path / "input"
    directory.mkdir()
    first, second = directory / "a.png", directory / "b.png"
    Image.new("RGB", (31, 33), (201, 10, 20)).save(first)
    Image.new("RGB", (37, 39), (10, 20, 201)).save(second)
    entered, release = Event(), Event()
    real = worker_module.load_image

    def delay_first(path, foreground):
        if Path(path) == first:
            entered.set()
            assert release.wait(5)
        return real(path, foreground)

    monkeypatch.setattr(worker_module, "load_image", delay_first)
    window.import_paths([directory])
    try:
        wait_for(qapp, lambda: entered.is_set() and window.worker is None)
        old_request = window._preview_request
        window.table.selectRow(1)
        wait_for(qapp, lambda: window.preview.has_image and
                 window.preview.original_canvas.image.size().width() == 37)
        assert window.current_source().path == second
        assert window._preview_request != old_request
        # A queued completion from the previous selection can arrive even after
        # cancellation. Exercise that receiver boundary explicitly.
        window._preview_completed(old_request, Image.new("RGB", (31, 33), "red"))
        assert window.preview.original_canvas.image.size().width() == 37
        assert window.preview.original_canvas.image.pixelColor(0, 0).getRgb()[:3] == (10, 20, 201)
    finally:
        release.set()
    wait_for(qapp, lambda: idle(window))
    assert window.preview.original_canvas.image.size().width() == 37
    assert not window._test_messages


def test_app_arguments_keep_cli_batch_compatibility_without_qt_import(tmp_path):
    source = write_rubbing(tmp_path / "input" / "crop.png")
    output = tmp_path / "output"
    project = Path(__file__).resolve().parents[1]
    script = """
import sys
import app
result = app.main(sys.argv[1:])
assert 'PySide6' not in sys.modules
assert 'ui.window' not in sys.modules
raise SystemExit(result)
"""
    completed = subprocess.run(
        [sys.executable, "-c", script, str(source), "-o", str(output), "--preview"],
        cwd=project, capture_output=True, text=True, timeout=20)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert (output / "crop.png").is_file()
    assert len(list(output.glob("report-*.json"))) == 1
    assert len(list(output.glob("preview-*.jpg"))) == 1
