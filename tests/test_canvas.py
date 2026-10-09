import gc
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest
from PIL import Image

pytest.importorskip("PySide6")
from PySide6.QtCore import QCoreApplication, QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QContextMenuEvent, QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMenu

import ui.canvas as canvas_module
from ui.canvas import PreviewPanel, gray_image, rgb_image


@pytest.fixture
def panel():
    app = QApplication.instance() or QApplication([])
    preview = PreviewPanel()
    preview.resize(900, 400)
    preview.show()
    app.processEvents()
    yield preview
    preview.close()
    preview.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    app.processEvents()


def test_qimages_own_pixels_with_unaligned_scanlines():
    rgb = np.full((4, 5, 3), (13, 77, 201), dtype=np.uint8)
    gray = np.full((4, 5), 255, dtype=np.uint8)
    color_image = rgb_image(rgb)
    mask_image = gray_image(gray)
    rgb[:] = 0
    gray[:] = 0
    del rgb, gray
    gc.collect()
    assert color_image.pixelColor(4, 3).getRgb()[:3] == (13, 77, 201)
    assert mask_image.pixelColor(4, 3).red() == 255


def test_original_without_mask_and_clear_release_selection(panel):
    original = Image.new("RGB", (90, 100), "white")
    panel.set_images(original, None, None)
    assert panel.has_image
    assert panel.mask_canvas.image.isNull()
    assert panel.changes_canvas.image.isNull()
    assert panel.mask_canvas.placeholder == "未处理"
    panel.actual_size()
    panel.clear()
    assert not panel.has_image
    assert all(canvas.image.isNull() for canvas in panel.canvases)
    assert panel.image_size.isEmpty()
    assert panel.fit_mode
    assert panel.image_center == QPointF()


def test_same_pixels_and_colored_changes_are_independent_of_input(panel):
    original = Image.new("RGB", (5, 4), (100, 100, 100))
    mask = np.zeros((4, 5), dtype=np.uint8)
    candidate = mask.copy()
    candidate[1, 1] = 255
    mask[2, 2] = 255
    panel.set_images(original, mask, candidate)
    assert panel.changes_canvas.image.pixelColor(1, 1).getRgb()[:3] == (208, 61, 61)
    assert panel.changes_canvas.image.pixelColor(2, 2).getRgb()[:3] == (44, 184, 208)
    assert not panel.mask_canvas.smooth
    original.paste((0, 0, 0), (0, 0, 5, 4))
    mask[:] = 0
    candidate[:] = 0
    assert panel.original_canvas.image.pixelColor(0, 0).red() == 100
    assert panel.mask_canvas.image.pixelColor(2, 2).red() == 255


def test_always_fitted_previews_keep_original_pixels_and_fit_after_resize(panel):
    original = Image.new("RGB", (400, 300), "white")
    mask = np.zeros((300, 400), dtype=np.uint8)
    mask[10:100, 15:80] = 255
    before = mask.copy()
    zoom_labels = []
    panel.zoom_changed.connect(zoom_labels.append)
    panel.set_images(original, mask, mask.copy())
    assert zoom_labels[-1] == "适配"
    for canvas in panel.canvases:
        assert canvas.image.width() == 400
        assert canvas.image.height() == 300
    previous_scale = panel.scale
    panel.resize(1050, 450)
    QApplication.instance().processEvents()
    assert panel.scale != previous_scale
    assert panel.fit_mode
    assert all(view.image.size().width() == 400 for view in panel.canvases)
    np.testing.assert_array_equal(mask, before)
    assert panel.image_center == QPointF(200, 150)
    panel.resize(630, 300)
    QApplication.instance().processEvents()
    assert zoom_labels[-1] == "适配"
    for view in panel.canvases:
        rectangle = view.image_rect()
        assert rectangle.left() >= 11
        assert rectangle.top() >= 11
        assert rectangle.right() <= view.width() - 11
        assert rectangle.bottom() <= view.height() - 11
        assert view.image.width() == 400
        assert view.image.height() == 300


def test_wheel_and_drag_do_not_zoom_or_pan_any_preview(panel):
    panel.set_images(Image.new("RGB", (400, 300), "white"), None, None)
    scale, center = panel.scale, QPointF(panel.image_center)
    canvas = panel.original_canvas
    point = canvas.rect().center()
    wheel = QWheelEvent(QPointF(point), QPointF(canvas.mapToGlobal(point)),
                        QPoint(), QPoint(0, 120),
                        Qt.NoButton, Qt.NoModifier, Qt.NoScrollPhase, False)
    QApplication.sendEvent(canvas, wheel)
    assert panel.scale == scale
    QTest.mousePress(canvas, Qt.LeftButton, pos=point)
    QTest.mouseRelease(canvas, Qt.LeftButton, pos=point + QPoint(25, 10))
    assert panel.image_center == center
    assert all(view.image_center == panel.image_center for view in panel.canvases)
    assert panel.fit_mode


def test_invalid_mask_preserves_previous_selection(panel):
    panel.set_images(Image.new("RGB", (30, 40), "white"), None, None)
    with pytest.raises(ValueError, match="尺寸"):
        panel.set_images(Image.new("RGB", (30, 40), "black"), np.zeros((4, 3)), None)
    assert panel.original_canvas.image.pixelColor(0, 0).red() == 255


def component_mask():
    mask = np.zeros((70, 80), dtype=np.uint8)
    mask[10, 10] = 255
    mask[11, 11] = 255  # The diagonal pair is one component under 8-connectivity.
    mask[10:15, 35:41] = 255
    return mask


def pixel_position(canvas, x, y):
    return (canvas.viewport_center() +
            (QPointF(x + .5, y + .5) - canvas.image_center) * canvas.scale).toPoint()


def select_pixel(panel, x, y, modifiers=Qt.NoModifier):
    canvas = panel.mask_canvas
    QTest.mouseClick(canvas, Qt.LeftButton, modifiers, pixel_position(canvas, x, y))
    QApplication.instance().processEvents()


def test_click_selects_eight_connected_foreground_and_overlay_keeps_mask_intact(panel):
    mask = component_mask()
    panel.set_images(Image.new("RGB", (80, 70), "gray"), mask, mask)
    counts = []
    panel.selected_pixels.connect(counts.append)
    select_pixel(panel, 10, 10)

    assert panel.selection_count == 2
    assert counts[-1] == 2
    canvas = panel.mask_canvas
    assert canvas.image.pixelColor(10, 10).red() == 255
    painted = canvas.grab().toImage().pixelColor(pixel_position(canvas, 10, 10))
    assert painted.getRgb()[:3] != (255, 255, 255)
    assert painted.red() > painted.blue()
    result = panel.remove_selected()
    assert result.dtype == np.uint8
    assert result[10, 10] == result[11, 11] == 0
    assert np.count_nonzero(result) == 30
    assert np.count_nonzero(mask) == 32
    assert canvas.image.pixelColor(10, 10).red() == 255
    result[:] = 0
    assert np.count_nonzero(panel.remove_selected()) == 30


def test_modifier_multiselection_background_and_letterbox_clicks(panel):
    mask = component_mask()
    panel.set_images(Image.new("RGB", (80, 70), "gray"), mask, mask)
    select_pixel(panel, 10, 10)
    select_pixel(panel, 35, 10, Qt.ControlModifier)
    assert panel.selection_count == 32
    assert not np.count_nonzero(panel.remove_selected())
    select_pixel(panel, 10, 10, Qt.MetaModifier)
    assert panel.selection_count == 30
    select_pixel(panel, 1, 1)
    assert panel.selection_count == 0
    assert panel.remove_selected() is None
    select_pixel(panel, 35, 10)
    assert panel.selection_count == 30
    QTest.mouseClick(panel.mask_canvas, Qt.LeftButton, pos=QPoint(1, 1))
    assert panel.selection_count == 0


def test_selection_uses_owned_pixels_and_tracks_fit_coordinates_after_resize(panel):
    mask = component_mask()
    panel.set_images(Image.new("RGB", (80, 70), "gray"), mask, mask.copy())
    mask[:] = 0
    panel.resize(1080, 520)
    QApplication.instance().processEvents()
    select_pixel(panel, 39, 13)
    assert panel.selection_count == 30
    result = panel.remove_selected()
    assert np.count_nonzero(result) == 2
    panel.clear_selection()
    assert panel.selection_count == 0
    assert panel.remove_selected() is None


def test_delete_requests_cleanup_and_does_not_modify_pixels_itself(panel):
    mask = component_mask()
    panel.set_images(Image.new("RGB", (80, 70), "gray"), mask, mask)
    requests = []
    panel.clear_requested.connect(lambda: requests.append(True))
    select_pixel(panel, 35, 10)
    QTest.keyClick(panel.mask_canvas, Qt.Key_Delete)
    QTest.keyClick(panel.mask_canvas, Qt.Key_Backspace)
    assert requests == [True, True]
    assert panel.selection_count == 30
    assert panel.mask_canvas.image.pixelColor(35, 10).red() == 255
    panel.clear_selection()
    QTest.keyClick(panel.mask_canvas, Qt.Key_Delete)
    assert len(requests) == 2


def test_new_images_and_clear_drop_selection_but_invalid_input_preserves_it(panel):
    mask = component_mask()
    panel.set_images(Image.new("RGB", (80, 70), "gray"), mask, mask)
    select_pixel(panel, 10, 10)
    with pytest.raises(ValueError, match="尺寸"):
        panel.set_images(Image.new("RGB", (30, 40), "black"), np.zeros((4, 3)), None)
    assert panel.selection_count == 2
    assert panel.image_size.width() == 80
    panel.set_images(Image.new("RGB", (80, 70), "white"), mask, mask)
    assert panel.selection_count == 0
    select_pixel(panel, 35, 10)
    panel.clear()
    assert panel.selection_count == 0
    assert panel.remove_selected() is None


@pytest.mark.parametrize("inverted", [False, True])
def test_both_preview_polarities_select_the_same_foreground_and_return_canonical_mask(panel, inverted):
    mask = component_mask()
    panel.set_images(Image.new("RGB", (80, 70), "gray"), mask, mask)
    panel.set_mask_inverted(inverted)
    assert panel.mask_title.text() == ("白底黑字" if inverted else "黑底白字")
    assert panel.mask_canvas.image.pixelColor(35, 10).red() == (0 if inverted else 255)
    assert panel.mask_canvas.image.pixelColor(1, 1).red() == (255 if inverted else 0)
    select_pixel(panel, 35, 10)
    assert panel.selection_count == 30
    result = panel.remove_selected()
    assert set(np.unique(result)) == {0, 255}
    assert result[10, 10] == 255
    assert result[10, 35] == 0
    select_pixel(panel, 1, 1)
    assert panel.selection_count == 0


def test_mask_context_menu_changes_only_display_and_keeps_selected_component(panel, monkeypatch):
    mask = component_mask()
    panel.set_images(Image.new("RGB", (80, 70), "gray"), mask, mask)
    select_pixel(panel, 35, 10)
    canvas = panel.mask_canvas
    def menu_factory(parent):
        menu = QMenu(parent)
        # PySide supplies an instance-bound exec which shadows class patches.
        # Replace only this menu's modal runner while keeping its real actions.
        menu.exec = lambda *args: next(action for action in menu.actions()
                                      if action.text() == "白底黑字")
        return menu

    monkeypatch.setattr(canvas_module, "QMenu", menu_factory)
    point = pixel_position(canvas, 35, 10)
    event = QContextMenuEvent(QContextMenuEvent.Mouse, point, canvas.mapToGlobal(point))
    QApplication.sendEvent(canvas, event)
    assert canvas.inverted
    assert canvas.image.pixelColor(35, 10).red() == 0
    assert panel.selection_count == 30
    assert np.count_nonzero(panel.remove_selected()) == 2
    panel.set_images(Image.new("RGB", (80, 70), "gray"), mask, mask)
    assert panel.selection_count == 0
    assert canvas.inverted
    assert canvas.image.pixelColor(35, 10).red() == 0
