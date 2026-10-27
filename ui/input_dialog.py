"""One cross-platform picker for image files and recursively scanned folders."""

from pathlib import Path

from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QDialogButtonBox, QFileDialog, QListView, QTreeView,
)


class ImageInputDialog(QFileDialog):
    def __init__(self, parent=None):
        super().__init__(parent, '添加图片或文件夹')
        # Native QFileDialog APIs cannot select files and folders in one session.
        self.setOption(QFileDialog.DontUseNativeDialog, True)
        self.setFileMode(QFileDialog.ExistingFiles)
        self.setNameFilter('图片 (*.png *.jpg *.jpeg *.bmp *.tif *.tiff *.webp)')
        self.setLabelText(QFileDialog.Accept, '添加所选')
        self.setLabelText(QFileDialog.Reject, '取消')
        self.setLabelText(QFileDialog.LookIn, '位置：')
        self.setLabelText(QFileDialog.FileName, '图片或文件夹：')
        self.setLabelText(QFileDialog.FileType, '文件类型：')
        self.resize(850, 560)
        for view_type, name in ((QListView, 'listView'), (QTreeView, 'treeView')):
            view = self.findChild(view_type, name)
            if view is not None:
                view.setSelectionMode(QAbstractItemView.ExtendedSelection)
                view.selectionModel().selectionChanged.connect(self._update_accept_label)
        self.paths = []

    def _update_accept_label(self, *_):
        # Qt changes this label to "Open" for a selected folder; our action adds it.
        box = self.findChild(QDialogButtonBox)
        for button in box.buttons():
            if box.buttonRole(button) == QDialogButtonBox.AcceptRole:
                button.setText('添加所选')

    def accept(self):
        paths = [Path(path) for path in self.selectedFiles()]
        if paths and all(path.is_file() or path.is_dir() for path in paths):
            self.paths = [str(path) for path in paths]
            # QFileDialog.accept rejects directories in ExistingFiles mode.
            QDialog.accept(self)

    @classmethod
    def choose(cls, parent=None):
        dialog = cls(parent)
        try:
            return dialog.paths if dialog.exec() == QDialog.Accepted else []
        finally:
            dialog.deleteLater()
