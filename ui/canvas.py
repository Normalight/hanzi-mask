"""Fitted glyph previews with component selection and binary brush editing.

Selections and brush drafts leave committed pixels intact. Completed edits
return a new mask for the window to persist as an undoable correction.
"""

import cv2
import numpy as np
from PIL import Image
from PySide6.QtCore import QEvent, QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QCursor, QImage, QPainter, QPen
from PySide6.QtWidgets import QHBoxLayout, QLabel, QMenu, QSizePolicy, QVBoxLayout, QWidget

from hanzi_mask.output_style import DEFAULT_OUTPUT_STYLE, OUTPUT_STYLES, render_mask, validate_output_style


def cursor_global_position():
    return QCursor.pos()


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


def changes_image(rgb, mask, candidate):
    if mask is None or candidate is None:
        return QImage()
    overlay = rgb.copy()
    removed = (candidate > 0) & (mask == 0)
    added = (candidate == 0) & (mask > 0)
    overlay[removed] = (0.3 * overlay[removed] + 0.7 * np.array([255, 45, 45])).astype(np.uint8)
    overlay[added] = (0.3 * overlay[added] + 0.7 * np.array([20, 220, 255])).astype(np.uint8)
    return rgb_image(overlay)


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
        self._pan_position = None
        self._pan_button = None

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
        self.panel._fit_scale()

    def wheelEvent(self, event):
        if self.panel.has_image:
            pixels = event.pixelDelta()
            if not pixels.isNull():
                self.panel._pan(QPointF(pixels))
            elif event.angleDelta().y():
                self.panel._zoom(1.2 ** (event.angleDelta().y() / 120),
                                 canvas=self, position=event.position())
        event.accept()

    def event(self, event):
        if event.type() == QEvent.NativeGesture and self.panel.has_image:
            if event.gestureType() == Qt.BeginNativeGesture:
                self.panel._gesture_zoom = float(self.panel.zoom_percent)
            elif event.gestureType() == Qt.ZoomNativeGesture:
                # Use the actual pointer rather than the event's reported
                # location, which can differ between native gesture backends.
                target, position = self.panel.pointer_anchor()
                self.panel._zoom(max(0.01, 1.0 + event.value()), canvas=target,
                                 position=position, gesture=True)
            elif event.gestureType() == Qt.EndNativeGesture:
                pass
            else:
                return super().event(event)
            event.accept()
            return True
        return super().event(event)

    def mousePressEvent(self, event):
        if (self.panel.has_image and (event.button() == Qt.MiddleButton or
                (event.button() == Qt.LeftButton and (event.modifiers() & Qt.ShiftModifier or
                 self is self.panel.original_canvas or self.panel.mask_canvas.tool == 'pan')))):
            self._pan_position = event.position()
            self._pan_button = event.button()
            event.accept()
            return
        if event.button() == Qt.LeftButton:
            self.setFocus()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._pan_position is not None:
            delta = event.position() - self._pan_position
            self._pan_position = event.position()
            self.panel._pan(delta)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == self._pan_button and self._pan_position is not None:
            self._pan_position = None
            self._pan_button = None
            event.accept()
            return
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
    """Select components or draw binary strokes in original image coordinates.

    Fitting a small mask uses the painter's local pixel interpolation at edges.
    The owned mask and QImage still contain the original 0/255 pixels; exported
    original-size binary images retain their native pixel-grid resolution.
    """

    def __init__(self, panel, parent=None):
        super().__init__(panel, smooth=True, parent=parent)
        self._mask = None
        self._labels = None
        self._areas = None
        self._selected = set()
        self._selection_overlay = QImage()
        self.inverted = DEFAULT_OUTPUT_STYLE == "black-on-white"
        self.tool = "brush"
        self.brush_size = 3
        self.brush_foreground = False
        self.editable = True
        self._stroke_mask = None
        self._pending_mask = None
        self._stroke_point = None
        self._hover_position = None
        self.setMouseTracking(True)

    def set_tool(self, tool):
        if tool not in ("select", "brush", "pan"):
            raise ValueError("未知精修工具")
        if tool != self.tool:
            self.cancel_stroke()
            self.tool = tool
            self.clear_selection()
            self.panel.changes_canvas.clear_selection()
        self._update_cursor()

    def set_editable(self, editable):
        self.editable = bool(editable)
        if not self.editable:
            self.cancel_stroke()
        self._update_cursor()

    def set_brush_size(self, size):
        self.cancel_stroke()
        self.brush_size = max(1, min(100, int(size)))
        self.update()

    def set_brush_foreground(self, foreground):
        self.cancel_stroke()
        self.brush_foreground = bool(foreground)
        self.panel.brush_foreground_changed.emit(self.brush_foreground)
        self.update()

    def _update_cursor(self):
        if self._mask is None:
            cursor = Qt.ArrowCursor
        elif self.tool == "brush":
            cursor = Qt.CrossCursor if self.editable else Qt.ArrowCursor
        elif self.tool == "pan":
            cursor = Qt.OpenHandCursor
        else:
            cursor = Qt.PointingHandCursor
        self.setCursor(cursor)
        self.update()

    def _render_mask(self, mask):
        self.image = gray_image(render_mask(mask, self.panel.output_style))
        self.update()

    def cancel_stroke(self):
        if self._stroke_mask is not None:
            self._stroke_mask = None
            self._stroke_point = None
            if self._mask is not None:
                self._render_mask(self._mask)

    def show_pending_mask(self, mask):
        """Keep the submitted edit visible until the window commits or rejects it."""
        self._stroke_mask = None
        self._stroke_point = None
        self._pending_mask = mask.copy()
        self.clear_selection()
        self._render_mask(self._pending_mask)

    def discard_pending_mask(self):
        if self._pending_mask is not None:
            self._pending_mask = None
            if self._mask is not None:
                self._render_mask(self._mask)

    def _brush_point(self, position):
        point = self.image_point(position)
        return int(np.floor(point.x())), int(np.floor(point.y()))

    def _inside_mask(self, point):
        if self._mask is None:
            return False
        x, y = point
        return 0 <= x < self._mask.shape[1] and 0 <= y < self._mask.shape[0]

    def _draw_stroke(self, position):
        point = self._brush_point(position)
        if self._stroke_point is not None or self._inside_mask(point):
            # A 4-connected background path blocks 8-connected foreground even
            # along a one-pixel diagonal. Filled strokes retain 8-connectivity.
            line_type = cv2.LINE_4 if not self.brush_foreground and self.brush_size == 1 else cv2.LINE_8
            cv2.line(self._stroke_mask, self._stroke_point or point, point,
                     255 if self.brush_foreground else 0,
                     thickness=self.brush_size, lineType=line_type)
            self._render_mask(self._stroke_mask)
        # Re-entry starts a new segment, never a bridge across the letterbox.
        self._stroke_point = point if self._inside_mask(point) else None

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
        self.cancel_stroke()
        self._pending_mask = None
        self._mask, self._labels, self._areas = mask, labels, areas
        self._selected.clear()
        self._selection_overlay = QImage()
        self._hover_position = None
        self._update_cursor()
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
        self.cancel_stroke()
        changed = self.inverted != bool(inverted)
        self.inverted = bool(inverted)
        if self._mask is not None:
            self._render_mask(self._pending_mask if self._pending_mask is not None else self._mask)
        self.panel.mask_title.setText(OUTPUT_STYLES[self.panel.output_style])
        self.update()
        if changed:
            self.panel.output_style_changed.emit(self.panel.output_style)

    def contextMenuEvent(self, event):
        if not self.panel.output_style_editable:
            event.accept()
            return
        menu = QMenu(self)
        inverse = menu.addAction("白底黑字")
        normal = menu.addAction("黑底白字")
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
        self.panel.changes_canvas.update()

    def _select_position(self, position, modifiers, canvas=None):
        if self._labels is None or self.image.isNull() or self._pending_mask is not None:
            return
        self.panel.changes_canvas.clear_selection()
        point = (canvas or self).image_point(position)
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
        if event.button() == Qt.LeftButton and (event.modifiers() & Qt.ShiftModifier or self.tool == 'pan'):
            super().mousePressEvent(event)
            return
        if event.button() == Qt.LeftButton:
            self.setFocus(Qt.MouseFocusReason)
            if self.tool == "select":
                self._select_position(event.position(), event.modifiers())
            elif (self.editable and self._pending_mask is None and not self.image.isNull()
                  and self._inside_mask(self._brush_point(event.position()))):
                self.panel.stroke_started.emit()
                self._stroke_mask = self._mask.copy()
                self._stroke_point = None
                self._draw_stroke(event.position())
            self._hover_position = event.position()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._pan_position is not None:
            super().mouseMoveEvent(event)
            return
        self._hover_position = event.position()
        if self._stroke_mask is not None:
            self._draw_stroke(event.position())
        self.update()
        event.accept()

    def mouseReleaseEvent(self, event):
        if self._pan_position is not None:
            super().mouseReleaseEvent(event)
            return
        if event.button() == Qt.LeftButton and self._stroke_mask is not None:
            self._draw_stroke(event.position())
            mask = self._stroke_mask
            changed = not np.array_equal(mask, self._mask)
            if changed:
                self.show_pending_mask(mask)
                self.panel.brush_finished.emit(mask)
            else:
                self.cancel_stroke()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.mousePressEvent(event)
            return
        super().mouseDoubleClickEvent(event)

    def focusOutEvent(self, event):
        self.cancel_stroke()
        super().focusOutEvent(event)

    def leaveEvent(self, event):
        self._hover_position = None
        self.update()
        super().leaveEvent(event)

    def keyPressEvent(self, event):
        if (self.tool == "select" and event.key() in (Qt.Key_Delete, Qt.Key_Backspace)
                and self.selection_count):
            self.panel.clear_requested.emit()
            event.accept()
            return
        super().keyPressEvent(event)

    def _paint_overlay(self, painter):
        if not self._selection_overlay.isNull():
            painter.setRenderHint(QPainter.SmoothPixmapTransform, self.smooth)
            painter.drawImage(self.image_rect(), self._selection_overlay)
        if (self.tool == "brush" and self.editable and self._hover_position is not None
                and self._inside_mask(self._brush_point(self._hover_position))):
            radius = max(2.0, self.brush_size * self.scale / 2)
            painter.setRenderHint(QPainter.Antialiasing, True)
            painter.setPen(QPen(Qt.black, 3))
            painter.drawEllipse(self._hover_position, radius, radius)
            painter.setPen(QPen(Qt.white, 1))
            painter.drawEllipse(self._hover_position, radius, radius)

    def _background_color(self):
        return Qt.white if self.inverted else Qt.black


class ChangesCanvas(ImageCanvas):
    """Select connected red and cyan differences for correction."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.labels = {}
        self.selected = set()
        self.overlay = QImage()

    def set_changes(self, mask, candidate):
        self.labels = {}
        self.clear_selection()
        if mask is not None and candidate is not None:
            for name, pixels in (("red", (candidate > 0) & (mask == 0)),
                                 ("cyan", (candidate == 0) & (mask > 0))):
                self.labels[name] = cv2.connectedComponents(pixels.astype(np.uint8), connectivity=8)[1]
        self.panel.selected_pixels.emit(self.panel.selection_count)

    @property
    def selection_count(self):
        return sum(int(np.count_nonzero(self.labels[name] == label)) for name, label in self.selected)

    def clear_selection(self):
        self.selected.clear()
        self.overlay = QImage()
        self.update()

    def corrected_mask(self):
        if not self.selected:
            return None
        result = self.panel.mask_canvas._mask.copy()
        for name, label in self.selected:
            result[self.labels[name] == label] = 255 if name == "red" else 0
        return result

    def _select(self, position, modifiers):
        mask = self.panel.mask_canvas
        if mask._pending_mask is not None or not self.labels:
            return
        mask.clear_selection()
        point = self.image_point(position)
        x, y = int(np.floor(point.x())), int(np.floor(point.y()))
        selected = None
        for name, labels in self.labels.items():
            if 0 <= y < labels.shape[0] and 0 <= x < labels.shape[1] and labels[y, x]:
                selected = (name, int(labels[y, x]))
                break
        if not selected:
            self.selected.clear()
        elif modifiers & (Qt.ControlModifier | Qt.MetaModifier):
            self.selected.symmetric_difference_update({selected})
        else:
            self.selected = {selected}
        if self.selected:
            shape = next(iter(self.labels.values())).shape
            rgba = np.zeros((*shape, 4), dtype=np.uint8)
            for name, label in self.selected:
                rgba[self.labels[name] == label] = (255, 145, 15, 175)
            self.overlay = QImage(rgba.data, shape[1], shape[0], rgba.strides[0], QImage.Format_RGBA8888).copy()
        else:
            self.overlay = QImage()
        self.panel.selected_pixels.emit(self.selection_count)
        self.panel.mask_canvas.update()
        self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and (event.modifiers() & Qt.ShiftModifier or self.panel.mask_canvas.tool == 'pan'):
            super().mousePressEvent(event)
            return
        if event.button() == Qt.LeftButton:
            self.setFocus(Qt.MouseFocusReason)
            mask = self.panel.mask_canvas
            if mask.tool == "select":
                self._select(event.position(), event.modifiers())
            event.accept()
            return
        super().mousePressEvent(event)

    def keyPressEvent(self, event):
        mask = self.panel.mask_canvas
        if (mask.tool == "select" and event.key() in (Qt.Key_Delete, Qt.Key_Backspace)
                and self.panel.selection_count):
            self.panel.clear_requested.emit()
            event.accept()
            return
        super().keyPressEvent(event)

    def _paint_overlay(self, painter):
        overlay = self.overlay
        if not overlay.isNull():
            painter.setRenderHint(QPainter.SmoothPixmapTransform, self.smooth)
            painter.drawImage(self.image_rect(), overlay)


class PreviewPanel(QWidget):
    zoom_changed = Signal(str)
    selected_pixels = Signal(int)
    clear_requested = Signal()
    output_style_changed = Signal(str)
    stroke_started = Signal()
    brush_finished = Signal(object)
    brush_foreground_changed = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.scale = 1.0
        self.fit_mode = True
        self.zoom_percent = 100
        self._gesture_zoom = 100.0
        self.image_center = QPointF()
        self.image_size = QSize()
        self.canvases = ()
        self._original_pixels = None
        self._candidate_pixels = None
        self.output_style_editable = True
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        canvases = []
        for index, title in enumerate(("原图", OUTPUT_STYLES[DEFAULT_OUTPUT_STYLE], "变化对照")):
            column = QWidget(self)
            layout = QVBoxLayout(column)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(5)
            label = QLabel(title)
            font = label.font()
            font.setBold(True)
            label.setFont(font)
            if index == 2:
                label.setToolTip("红色：已删除；青色：已增加。选中变化区域后按 Delete，红色恢复、青色移除")
            layout.addWidget(label)
            if index == 1:
                self.mask_title = label
                label.setToolTip("画笔：背景色擦除，字形色填补；选中色块：Ctrl/Command 多选，退格/Delete 清理；右键切换输出底色")
                canvas = MaskCanvas(self, parent=column)
            elif index == 2:
                canvas = ChangesCanvas(self, parent=column)
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
        return self.mask_canvas.selection_count + self.changes_canvas.selection_count

    def clear_selection(self):
        self.mask_canvas.cancel_stroke()
        self.mask_canvas.clear_selection()
        self.changes_canvas.clear_selection()
        self.selected_pixels.emit(0)

    def remove_selected(self):
        return self.changes_canvas.corrected_mask() if self.changes_canvas.selected else self.mask_canvas.remove_selected()

    def set_mask_inverted(self, inverted):
        self.mask_canvas.set_inverted(inverted)

    @property
    def output_style(self):
        return "black-on-white" if self.mask_canvas.inverted else "white-on-black"

    def set_output_style(self, output_style):
        validate_output_style(output_style)
        self.set_mask_inverted(output_style == "black-on-white")

    def set_images(self, original: Image.Image | None, mask: np.ndarray | None,
                   candidate: np.ndarray | None):
        if original is None:
            self.clear()
            return
        rgb = np.asarray(original.convert("RGB")).copy()
        height, width = rgb.shape[:2]
        shape = (height, width)
        for name, pixels in (("字形", mask), ("明暗候选", candidate)):
            if pixels is not None and np.asarray(pixels).shape != shape:
                raise ValueError(f"{name}预览尺寸与原图不一致")
        original_qimage = rgb_image(rgb)
        mask_pixels = None if mask is None else (np.asarray(mask) > 0).astype(np.uint8) * 255
        mask_qimage = (QImage() if mask_pixels is None else gray_image(
            render_mask(mask_pixels, self.output_style)))
        candidate_pixels = None if candidate is None else np.asarray(candidate).copy()
        changes_qimage = changes_image(rgb, mask_pixels, candidate_pixels)
        # Build every QImage before replacing the selection, so invalid data
        # cannot leave a mixture of the previous and current glyph.
        self.mask_canvas.set_mask(mask)
        self.changes_canvas.set_changes(mask_pixels, candidate_pixels)
        for canvas, image in zip(self.canvases, (original_qimage, mask_qimage, changes_qimage)):
            canvas.image = image
        self._original_pixels, self._candidate_pixels = rgb, candidate_pixels
        self.image_size = QSize(width, height)
        self.fit()

    def update_mask(self, mask):
        """Commit a correction without clearing, reloading or refitting the previews."""
        pixels = np.asarray(mask)
        if self._original_pixels is None or pixels.shape != self._original_pixels.shape[:2]:
            raise ValueError("字形预览尺寸与原图不一致")
        rendered = gray_image(render_mask(pixels, self.output_style))
        changes = changes_image(self._original_pixels, pixels, self._candidate_pixels)
        self.mask_canvas.set_mask(pixels)
        self.changes_canvas.set_changes(pixels, self._candidate_pixels)
        self.mask_canvas.image = rendered
        self.changes_canvas.image = changes
        self.mask_canvas.update()
        self.changes_canvas.update()

    def clear(self):
        self._original_pixels = self._candidate_pixels = None
        self.mask_canvas.set_mask(None)
        self.changes_canvas.set_changes(None, None)
        for canvas in self.canvases:
            canvas.image = QImage()
            canvas.update()
        self.image_size = QSize()
        self.image_center = QPointF()
        self.scale = 1.0
        self.fit_mode = True
        self.zoom_percent = 100
        self._gesture_zoom = 100.0
        self.zoom_changed.emit("适配")

    def fit(self):
        self.fit_mode = True
        self.zoom_percent = 100
        self._gesture_zoom = 100.0
        if self.has_image:
            self.image_center = QPointF(self.image_size.width() / 2, self.image_size.height() / 2)
        self._fit_scale()

    def _fit_scale(self, *, refresh=True):
        if self.has_image:
            self.scale = self.zoom_percent / 100 * min(min(max(1, canvas.width() - 24) / self.image_size.width(),
                                 max(1, canvas.height() - 24) / self.image_size.height())
                             for canvas in self.canvases)
        if refresh:
            self._refresh()

    def set_zoom_percent(self, percent):
        self.zoom_percent = max(25, min(800, int(percent)))
        self._gesture_zoom = float(self.zoom_percent)
        self.fit_mode = self.zoom_percent == 100
        self._fit_scale()

    def pointer_anchor(self):
        global_position = cursor_global_position()
        for canvas in self.canvases:
            position = canvas.mapFromGlobal(global_position)
            if canvas.rect().contains(position):
                return canvas, QPointF(position)
        return None, None

    def actual_size(self):
        self.fit()

    def zoom_in(self):
        self.fit()

    def zoom_out(self):
        self.fit()

    def _zoom(self, factor, *, canvas=None, position=None, gesture=False):
        if not self.has_image:
            return
        anchor = canvas.image_point(position) if canvas is not None and position is not None else None
        value = (self._gesture_zoom if gesture else self.zoom_percent) * factor
        self._gesture_zoom = max(25.0, min(800.0, value))
        self.zoom_percent = round(self._gesture_zoom)
        self.fit_mode = self.zoom_percent == 100
        self._fit_scale(refresh=False)
        if anchor is not None:
            self.image_center = anchor - (QPointF(position) - canvas.viewport_center()) / self.scale
        self._refresh()

    def _pan(self, delta):
        if not self.has_image:
            return
        self.image_center = self.image_center - QPointF(delta) / self.scale
        self._refresh()

    def _refresh(self):
        for canvas in self.canvases:
            canvas.update()
        self.zoom_changed.emit("适配" if self.fit_mode else f"{self.zoom_percent}%")
