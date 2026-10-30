"""Native picker dispatch, cancellation, and recursive import regressions."""

import pytest

pytest.importorskip('PySide6')
from PySide6.QtWidgets import QFileDialog

from ui.input_dialog import ImageInputDialog, install_dialog_translations
from test_desktop import idle, qapp, wait_for, window, write_rubbing


@pytest.mark.parametrize('cancelled', [False, True])
def test_native_image_picker_imports_multiple_files(window, qapp, tmp_path, monkeypatch, cancelled):
    paths = [write_rubbing(tmp_path / name) for name in ('a.png', 'b.jpg')]

    def choose(parent, caption, directory, filters):
        assert parent is window
        assert caption == '添加图片'
        assert '*.png' in filters and '*.tiff' in filters
        assert window._tuning_paused == 1
        return ([] if cancelled else [str(path) for path in paths]), filters

    # The common Qt API dispatches to the installed Windows, Cocoa or Linux
    # platform helper with its default native options.
    monkeypatch.setattr(QFileDialog, 'getOpenFileNames', choose)
    window.file_buttons[0].click()
    wait_for(qapp, lambda: idle(window))
    assert {item.path for item in window.inputs} == (set() if cancelled else set(paths))
    assert window._tuning_paused == 0


@pytest.mark.parametrize('cancelled', [False, True])
def test_native_folder_picker_appends_and_scans_recursively(window, qapp, tmp_path, monkeypatch, cancelled):
    first = write_rubbing(tmp_path / 'first' / 'a.png')
    second = write_rubbing(tmp_path / 'folder' / 'nested' / 'b.png')
    window.import_paths([first])
    wait_for(qapp, lambda: idle(window))

    def choose(parent, caption):
        assert parent is window and caption == '添加文件夹'
        assert window._tuning_paused == 1
        return '' if cancelled else str(second.parent.parent)

    monkeypatch.setattr(QFileDialog, 'getExistingDirectory', choose)
    window.file_buttons[1].click()
    wait_for(qapp, lambda: idle(window))
    assert {item.path for item in window.inputs} == ({first} if cancelled else {first, second})
    assert window._tuning_paused == 0


def test_qt_fallback_translation(qapp):
    assert install_dialog_translations(qapp)
    assert '文件' in qapp.translate('QFileDialog', 'File &name:')
