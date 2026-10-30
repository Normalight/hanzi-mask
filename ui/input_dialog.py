"""Platform file and directory pickers using Qt's native dialog helpers."""

from PySide6.QtCore import QLibraryInfo, QTranslator
from PySide6.QtWidgets import QFileDialog


def install_dialog_translations(app):
    """Keep Qt's fallback dialog in Chinese when no native helper is available."""
    if getattr(app, '_hanzi_dialog_translator', None) is not None:
        return True
    translator = QTranslator(app)
    if translator.load('qtbase_zh_CN', QLibraryInfo.path(QLibraryInfo.TranslationsPath)):
        app.installTranslator(translator)
        app._hanzi_dialog_translator = translator
        return True
    translator.deleteLater()
    return False


class ImageInputDialog:
    @staticmethod
    def choose(parent=None):
        paths, _ = QFileDialog.getOpenFileNames(
            parent, '添加图片', '',
            '图片 (*.png *.jpg *.jpeg *.bmp *.tif *.tiff *.webp)')
        return paths

    @staticmethod
    def choose_directory(parent=None):
        path = QFileDialog.getExistingDirectory(parent, '添加文件夹')
        return [path] if path else []
