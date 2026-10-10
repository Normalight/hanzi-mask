"""Writable defaults for source runs and frozen desktop applications."""
from pathlib import Path
import sys

from PySide6.QtCore import QStandardPaths


def default_output(project_root: Path, app_name: str) -> Path:
    if getattr(sys, 'frozen', False):
        documents = QStandardPaths.writableLocation(QStandardPaths.DocumentsLocation)
        return (Path(documents) if documents else Path.home()) / app_name
    return project_root / 'output' / app_name
