"""Fitted glyph previews with selection of foreground components for cleanup.

Painting keeps original pixels intact. Selection highlights are a separate
overlay, and removal returns a new mask for the window to save explicitly.
"""

import cv2
import numpy as np
from PIL import Image
from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QHBoxLayout, QLabel, QMenu, QSizePolicy, QVBoxLayout, QWidget


def rgb_image(pixels: np.ndarray) -> QImage:
    """Own the image bytes, including non-four-byte-aligned scanlines."""
    rgb = np.ascontiguousarray(pixels, dtype=np.uint8)
    if rgb.ndim != 3 or rgb.shape[2] != 3:
        raise ValueError("预览图片必须包含三个颜色通道")
    height, width = rgb.shape[:2]
    return QImage(rgb.data, width, height, rgb.strides[0], QImage.Format_RGB888).copy()


def gray_image(pixels: np.ndarray) -> QImage:
    gray = np.ascontiguousarray(pixels, dtype=np.uint8)
    if gray.ndim != 2:
        raise ValueError("预览掩膜必须是二维图片")
    height, width = gray.shape
    return QImage(gray.data, width, height, gray.strides[0], QImage.Format_Grayscale8).copy()


class ImageCanvas(QWidget):
    """A viewport into the parent panel's shared image-coordinate transform."""

    def __init__(self, panel, *, smooth=True, placeholder="未处理", parent=None):
        super().__init__(parent)
        self.panel = panel
        self.image = QImage()
        self.smooth = smooth
        self.placeholder = placeholder
        self.setMinimumSize(160, 180)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setAttribute(Qt.WA_OpaquePaintEvent, True)
        self.setCursor(Qt.ArrowCursor)

    def sizeHint(self):
        return QSize(300, 360)

    @property
    def scale(self):
        return self.panel.scale

    @property
    def image_center(self):
        return QPointF(self.panel.image_center)

    def viewport_center(self):
        return QPointF(self.width() / 2, self.height() / 2)

    def image_point(self, position):
        return self.image_center + (QPointF(position) - self.viewport_center()) / self.scale

    def image_rect(self):
        origin = self.viewport_center() - self.image_center * self.scale
        return QRectF(origin.x(), origin.y(),
                      self.panel.image_size.width() * self.scale,
                      self.panel.image_size.height() * self.scale)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), self._background_color())
        if self.image.isNull():
            painter.setPen(QColor("#b4b4b4"))
            painter.drawText(self.rect().adjusted(12, 12, -12, -12),
                             Qt.AlignCenter | Qt.TextWordWrap, self.placeholder)
            return
        painter.setRenderHint(QPainter.SmoothPixmapTransform, self.smooth)
        painter.drawImage(self.image_rect(), self.image)
        self._paint_overlay(painter)

    def _paint_overlay(self, painter):
        pass

    def _background_color(self):
        return Qt.black

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.panel.fit()

    def wheelEvent(self, event):
        # The client always shows the complete glyph. Accept the wheel event so
        # it cannot zoom or unexpectedly scroll a surrounding input panel.
        event.accept()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.setFocus()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.LeftButton:
            event.accept()
            return
        super().mouseDoubleClickEvent(event)


class MaskCanvas(ImageCanvas):
    """Select complete 8-connected white components using image coordinates."""

    def __init__(self, panel, parent=None):
        super().__init__(panel, smooth=False, parent=parent)
        self._mask = None
        self._labels = None
        self._areas = None
        self._selected = set()
        self._selection_overlay = QImage()
        self.inverted = False

    @property
    def selection_count(self):
        if self._areas is None:
            return 0
        return sum(int(self._areas[label]) for label in self._selected)

    def set_mask(self, pixels):
        if pixels is None:
            mask, labels, areas = None, None, None
        else:
            mask = (np.asarray(pixels) > 0).astype(np.uint8) * 255
            _, labels, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
            areas = stats[:, cv2.CC_STAT_AREA]
        self._mask, self._labels, self._areas = mask, labels, areas
        self._selected.clear()
        self._selection_overlay = QImage()
        self.setCursor(Qt.PointingHandCursor if mask is not None else Qt.ArrowCursor)
        self.panel.selected_pixels.emit(0)
        self.update()

    def clear_selection(self):
        self._selected.clear()
        self._update_selection()

    def remove_selected(self):
        if self._mask is None or not self._selected:
            return None
        result = self._mask.copy()
        result[np.isin(self._labels, list(self._selected))] = 0
        return result

    def set_inverted(self, inverted):
        self.inverted = bool(inverted)
        if self._mask is not None:
            self.image = gray_image(255 - self._mask if self.inverted else self._mask)
        self.panel.mask_title.setText("白底黑字" if self.inverted else "黑底白字")
        self.update()

    def contextMenuEvent(self, event):
        menu = QMenu(self)
        normal = menu.addAction("黑底白字")
        inverse = menu.addAction("白底黑字")
        for action, checked in ((normal, not self.inverted), (inverse, self.inverted)):
            action.setCheckable(True)
            action.setChecked(checked)
        chosen = menu.exec(event.globalPos())
        if chosen is normal:
            self.set_inverted(False)
        elif chosen is inverse:
            self.set_inverted(True)
        menu.deleteLater()
        event.accept()

    def _update_selection(self):
        if self._selected:
            selected = np.isin(self._labels, list(self._selected))
            height, width = selected.shape
            rgba = np.zeros((height, width, 4), dtype=np.uint8)
            rgba[selected] = (255, 145, 15, 175)
            self._selection_overlay = QImage(
                rgba.data, width, height, rgba.strides[0], QImage.Format_RGBA8888).copy()
        else:
            self._selection_overlay = QImage()
        self.panel.selected_pixels.emit(self.selection_count)
        self.update()

    def _select_position(self, position, modifiers):
        if self._labels is None or self.image.isNull():
            return
        point = self.image_point(position)
        x, y = int(np.floor(point.x())), int(np.floor(point.y()))
        height, width = self._labels.shape
        label = int(self._labels[y, x]) if 0 <= x < width and 0 <= y < height else 0
        if not label:
            self._selected.clear()
        elif modifiers & (Qt.ControlModifier | Qt.MetaModifier):
            if label in self._selected:
                self._selected.remove(label)
            else:
                self._selected.add(label)
        else:
            self._selected = {label}
        self._update_selection()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.setFocus()
            self._select_position(event.position(), event.modifiers())
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._select_position(event.position(), event.modifiers())
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Delete, Qt.Key_Backspace) and self.selection_count:
            self.panel.clear_requested.emit()
            event.accept()
            return
        if event.key() == Qt.Key_Escape:
            self.clear_selection()
            event.accept()
            return
        super().keyPressEvent(event)

    def _paint_overlay(self, painter):
        if not self._selection_overlay.isNull():
            painter.setRenderHint(QPainter.SmoothPixmapTransform, False)
            painter.drawImage(self.image_rect(), self._selection_overlay)

    def _background_color(self):
        return Qt.white if self.inverted else Qt.black


class PreviewPanel(QWidget):
    zoom_changed = Signal(str)
    selected_pixels = Signal(int)
    clear_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.scale = 1.0
        self.fit_mode = True
        self.image_center = QPointF()
        self.image_size = QSize()
        self.canvases = ()
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        canvases = []
        for index, title in enumerate(("原图", "黑底白字", "变化对照")):
            column = QWidget(self)
            layout = QVBoxLayout(column)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(5)
            label = QLabel(title)
            font = label.font()
            font.setBold(True)
            label.setFont(font)
            if index == 2:
                label.setToolTip("红色：相对原始前景候选删除；青色：增加")
            layout.addWidget(label)
            if index == 1:
                self.mask_title = label
                label.setToolTip("点击字形或杂斑选中整块；Ctrl/Command 可多选；右键切换底色；Delete 清理")
                canvas = MaskCanvas(self, parent=column)
            else:
                canvas = ImageCanvas(self, placeholder="请选择图片" if index == 0 else "未处理",
                                     parent=column)
            layout.addWidget(canvas, 1)
            row.addWidget(column, 1)
            canvases.append(canvas)
        self.canvases = tuple(canvases)
        self.original_canvas, self.mask_canvas, self.changes_canvas = self.canvases

    @property
    def has_image(self):
        return bool(self.canvases) and not self.original_canvas.image.isNull()

    @property
    def selection_count(self):
        return self.mask_canvas.selection_count

    def clear_selection(self):
        self.mask_canvas.clear_selection()

    def remove_selected(self):
        return self.mask_canvas.remove_selected()

    def set_mask_inverted(self, inverted):
        self.mask_canvas.set_inverted(inverted)

    def set_images(self, original: Image.Image | None, mask: np.ndarray | None,
                   candidate: np.ndarray | None):
        if original is None:
            self.clear()
            return
        rgb = np.asarray(original.convert("RGB"))
        height, width = rgb.shape[:2]
        shape = (height, width)
        for name, pixels in (("字形", mask), ("明暗候选", candidate)):
            if pixels is not None and np.asarray(pixels).shape != shape:
                raise ValueError(f"{name}预览尺寸与原图不一致")
        original_qimage = rgb_image(rgb)
        mask_pixels = None if mask is None else (np.asarray(mask) > 0).astype(np.uint8) * 255
        mask_qimage = (QImage() if mask_pixels is None else gray_image(
            255 - mask_pixels if self.mask_canvas.inverted else mask_pixels))
        changes_qimage = QImage()
        if mask is not None and candidate is not None:
            overlay = rgb.copy()
            removed = (np.asarray(candidate) > 0) & (np.asarray(mask) == 0)
            added = (np.asarray(candidate) == 0) & (np.asarray(mask) > 0)
            overlay[removed] = (0.3 * overlay[removed] + 0.7 * np.array([255, 45, 45])).astype(np.uint8)
            overlay[added] = (0.3 * overlay[added] + 0.7 * np.array([20, 220, 255])).astype(np.uint8)
            changes_qimage = rgb_image(overlay)
        # Build every QImage before replacing the selection, so invalid data
        # cannot leave a mixture of the previous and current glyph.
        self.mask_canvas.set_mask(mask)
        for canvas, image in zip(self.canvases, (original_qimage, mask_qimage, changes_qimage)):
            canvas.image = image
        self.image_size = QSize(width, height)
        self.fit()

    def clear(self):
        self.mask_canvas.set_mask(None)
        for canvas in self.canvases:
            canvas.image = QImage()
            canvas.update()
        self.image_size = QSize()
        self.image_center = QPointF()
        self.scale = 1.0
        self.fit_mode = True
        self.zoom_changed.emit("适配")

    def fit(self):
        self.fit_mode = True
        if self.has_image:
            self.image_center = QPointF(self.image_size.width() / 2, self.image_size.height() / 2)
        self._fit_scale()

    def _fit_scale(self):
        if self.has_image:
            self.scale = min(min(max(1, canvas.width() - 24) / self.image_size.width(),
                                 max(1, canvas.height() - 24) / self.image_size.height())
                             for canvas in self.canvases)
        self._refresh()

    def actual_size(self):
        self.fit()

    def zoom_in(self):
        self.fit()

    def zoom_out(self):
        self.fit()

    def _zoom(self, factor, *, canvas=None, position=None):
        self.fit()

    def _pan(self, delta):
        self.fit()

    def _refresh(self):
        for canvas in self.canvases:
            canvas.update()
        self.zoom_changed.emit("适配" if self.fit_mode else f"{self.scale * 100:.0f}%")
