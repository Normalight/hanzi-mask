"""Exercise the actual mixed picker selection and its confirmation button."""

from pathlib import Path

import pytest

pytest.importorskip('PySide6')
from PySide6.QtCore import QItemSelectionModel, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QFileDialog, QListView, QTreeView

from ui.input_dialog import ImageInputDialog
from test_desktop import idle, qapp, wait_for, window, write_rubbing


@pytest.mark.parametrize('view_mode,view_type,name', [
    (QFileDialog.Detail, QTreeView, 'treeView'),
    (QFileDialog.List, QListView, 'listView'),
])
@pytest.mark.parametrize('selection', [('folder',), ('a.png',), ('folder', 'a.png', 'b.jpg')])
def test_picker_accepts_files_folders_and_mixed_selection(qapp, tmp_path, view_mode, view_type, name, selection):
    (tmp_path / 'folder').mkdir()
    for filename in ('a.png', 'b.jpg'):
        write_rubbing(tmp_path / filename)
    dialog = ImageInputDialog()
    dialog.setDirectory(str(tmp_path))
    dialog.setViewMode(view_mode)
    dialog.show()
    try:
        view = dialog.findChild(view_type, name)
        model = view.model()
        wait_for(qapp, lambda: model.rowCount(view.rootIndex()) >= 3)
        for filename in selection:
            index = model.index(str(tmp_path / filename))
            view.selectionModel().select(index, QItemSelectionModel.Select | QItemSelectionModel.Rows)
        box = dialog.findChild(QDialogButtonBox)
        button = next(button for button in box.buttons() if box.buttonRole(button) == QDialogButtonBox.AcceptRole)
        QTest.mouseClick(button, Qt.LeftButton)
        assert dialog.result() == QDialog.Accepted
        assert {Path(path).resolve() for path in dialog.paths} == {(tmp_path / name).resolve() for name in selection}
    finally:
        dialog.close()
        dialog.deleteLater()


def test_merged_input_button_imports_nested_folders_and_files(window, qapp, tmp_path, monkeypatch):
    first = write_rubbing(tmp_path / 'folder' / 'nested' / 'a.png')
    second = write_rubbing(tmp_path / 'b.png')
    monkeypatch.setattr(ImageInputDialog, 'choose', lambda parent: [str(first.parent.parent), str(second), str(second)])
    window.file_buttons[0].click()
    wait_for(qapp, lambda: idle(window))
    assert {item.path for item in window.inputs} == {first, second}
    assert len(window.roots) == 2
    assert window.records[window.current_source().path].options.texture_strength == 0
