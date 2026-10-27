"""Run local image IO and texture operations outside the Qt event loop."""

from pathlib import Path

from PySide6.QtCore import QThread, Signal

from hanzi_mask.batch_service import (
    ScanCancelled, export_batch, load_result, process_batch, replace_originals,
    restore_replacement, scan_inputs,
)
from hanzi_mask.processing import load_image
from hanzi_mask.manual_correction import apply_correction
from hanzi_mask.output_style import DEFAULT_OUTPUT_STYLE


class TaskWorker(QThread):
    progress = Signal(int, int, str)
    item_ready = Signal(object)
    completed = Signal(object)
    failed = Signal(str, object)

    def __init__(self, operation, payload, parent=None):
        super().__init__(parent)
        self.operation = operation
        self.payload = payload

    def run(self):
        def progress(done, total, path):
            self.progress.emit(done, total, str(path))

        try:
            p = self.payload
            if self.operation == "scan":
                result = scan_inputs(p["paths"], p["recursive"], p.get("exclude_paths", ()),
                                     cancel=self.isInterruptionRequested)
            elif self.operation == "process":
                result = process_batch(p["inputs"], p["options"], self.isInterruptionRequested,
                                       progress, p["cache_dir"], self.item_ready.emit,
                                       max_workers=p.get("max_workers"),
                                       per_image_options=p.get("per_image_options"))
            elif self.operation == "export":
                result = export_batch(p["batch"], p["directory"], self.isInterruptionRequested, progress,
                                      output_style=p.get("output_style", DEFAULT_OUTPUT_STYLE))
            elif self.operation == "replace":
                result = replace_originals(p["batch"], p["directory"], self.isInterruptionRequested, progress,
                                           output_style=p.get("output_style", DEFAULT_OUTPUT_STYLE))
            elif self.operation == "restore":
                result = restore_replacement(p["report"], progress)
            elif self.operation == "correct":
                result = apply_correction(p["item"], p["mask"], p["cache_dir"])
            else:
                raise ValueError("未知桌面任务")
            self.completed.emit(result)
        except ScanCancelled:
            self.completed.emit(None)
        except Exception as error:
            self.failed.emit(str(error), getattr(error, "report_path", None))


class PreviewWorker(QThread):
    completed = Signal(int, object)
    failed = Signal(int, str)

    def __init__(self, request_id, path, item, foreground, parent=None):
        super().__init__(parent)
        self.request_id = request_id
        self.path = Path(path)
        self.item = item
        self.foreground = foreground

    def run(self):
        try:
            if self.isInterruptionRequested():
                return
            result = (load_result(self.item, self.foreground) if self.item is not None
                      else load_image(self.path, self.foreground))
            if not self.isInterruptionRequested():
                self.completed.emit(self.request_id, result)
        except Exception as error:
            if not self.isInterruptionRequested():
                self.failed.emit(self.request_id, str(error))
