"""Platform-specific shortcuts for image undo and redo."""

import sys

from PySide6.QtGui import QKeySequence


def history_key_bindings(*, macos=None):
    if macos is None:
        macos = sys.platform == 'darwin'
    # Qt's portable Ctrl modifier maps to Command on macOS.
    return {
        QKeySequence.Undo: [QKeySequence('Ctrl+Z')],
        QKeySequence.Redo: [QKeySequence('Ctrl+Shift+Z' if macos else 'Ctrl+Y')],
    }
