"""Platform bindings and shortcut routing without intercepting text undo."""

import sys

import pytest

pytest.importorskip('PySide6')
from PySide6.QtGui import QKeySequence
from PySide6.QtTest import QTest

from ui.history_shortcuts import history_key_bindings
from test_desktop import idle, import_and_process, qapp, wait_for, window, write_two_components
from test_desktop_history import brush


KEY_PAIRS = [('Ctrl+Z', 'Ctrl+Shift+Z' if sys.platform == 'darwin' else 'Ctrl+Y')]


@pytest.mark.parametrize('macos', [False, True], ids=['windows', 'macos'])
def test_history_bindings_match_platform_shortcuts(qapp, macos):
    bindings = history_key_bindings(macos=macos)
    undo = [key.toString(QKeySequence.PortableText) for key in bindings[QKeySequence.Undo]]
    redo = [key.toString(QKeySequence.PortableText) for key in bindings[QKeySequence.Redo]]
    assert undo == ['Ctrl+Z']
    assert redo == ['Ctrl+Shift+Z' if macos else 'Ctrl+Y']


@pytest.mark.parametrize('undo,redo', KEY_PAIRS)
@pytest.mark.parametrize('focus', ['canvas', 'history', 'brush_button', 'table'])
def test_shortcuts_undo_and_redo_exactly_one_step_from_workspace_controls(
        window, qapp, tmp_path, focus, undo, redo):
    path = write_two_components(tmp_path / 'glyph.png')
    import_and_process(window, qapp, path)
    brush(window, qapp, (8, 40), (36, 40))
    previous = window.records[path]
    brush(window, qapp, (44, 20), (54, 20), fill=True)
    latest = window.records[path]
    widget = {'canvas': window.preview.mask_canvas, 'history': window.history_list,
              'brush_button': window.background_brush_button, 'table': window.table}[focus]
    widget.setFocus()
    assert qapp.focusWidget() is widget
    QTest.keySequence(widget, QKeySequence(undo))
    wait_for(qapp, lambda: idle(window))
    assert window.records[path].mask_path == previous.mask_path
    assert len(window._correction_history[path]) == 1
    widget.setFocus()
    QTest.keySequence(widget, QKeySequence(redo))
    wait_for(qapp, lambda: idle(window))
    assert window.records[path].mask_path == latest.mask_path
    assert len(window._correction_history[path]) == 2


@pytest.mark.parametrize('undo,redo', KEY_PAIRS)
@pytest.mark.parametrize('editor_name', ['search', 'output'])
def test_text_fields_never_route_history_keys_to_the_image(
        window, qapp, tmp_path, editor_name, undo, redo):
    path = write_two_components(tmp_path / 'glyph.png')
    import_and_process(window, qapp, path)
    brush(window, qapp, (8, 40), (36, 40))
    brush(window, qapp, (44, 20), (54, 20), fill=True)
    window.undo_correction_button.click()
    wait_for(qapp, lambda: idle(window))
    current = window.records[path]
    assert window.undo_correction_button.isEnabled() and window.redo_correction_button.isEnabled()
    editor = getattr(window, editor_name)
    editor.setFocus()
    assert qapp.focusWidget() is editor
    for key in (undo, redo):
        QTest.keySequence(editor, QKeySequence(key))
        qapp.processEvents()
        assert window.records[path] is current
        assert len(window._correction_history[path]) == len(window._correction_redo[path]) == 1


def test_text_input_keeps_native_undo_and_redo_when_image_history_is_available(window, qapp, tmp_path):
    path = write_two_components(tmp_path / 'glyph.png')
    import_and_process(window, qapp, path)
    brush(window, qapp, (8, 40), (36, 40))
    current = window.records[path]
    window.search.setFocus()
    QTest.keyClicks(window.search, 'glyph')
    QTest.keySequence(window.search, QKeySequence(QKeySequence.Undo))
    assert window.search.text() == ''
    QTest.keySequence(window.search, QKeySequence(QKeySequence.Redo))
    assert window.search.text() == 'glyph'
    assert window.records[path] is current
