"""Desktop copy saving and responsive cache shutdown."""

from threading import Event

import pytest

pytest.importorskip('PySide6')
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QMessageBox

from test_desktop import idle, import_and_process, qapp, wait_for, window, write_rubbing


def test_default_save_creates_beside_copy_and_retains_review(window, qapp, tmp_path, monkeypatch):
    source = write_rubbing(tmp_path / 'input' / '字.jpg')
    before = source.read_bytes()
    import_and_process(window, qapp, source.parent)
    record = window.records[source]
    monkeypatch.setattr(QMessageBox, 'question', lambda *args: pytest.fail('Copy saving needs no replacement prompt'))
    assert window.save_mode.currentData() == 'copy'
    window.save_button.click()
    wait_for(qapp, lambda: idle(window))
    assert source.read_bytes() == before
    assert (source.parent / '副本-字.png').is_file()
    assert window._last_output == source.parent
    assert window.records[source] is record
    assert window.adopted_items() == [record]
    assert window.background_brush_button.text() == '擦除'
    window.rescan()
    wait_for(qapp, lambda: idle(window))
    assert [item.path for item in window.inputs] == [source]
    copy = source.parent / '副本-字.png'
    window.clear_inputs()
    window.import_paths([copy])
    wait_for(qapp, lambda: idle(window))
    assert [item.path for item in window.inputs] == [copy]


def test_close_cleans_cache_in_worker_after_processing_stops(window, qapp, tmp_path, monkeypatch):
    source = write_rubbing(tmp_path / 'input' / '字.png')
    import_and_process(window, qapp, source)
    entered, release = Event(), Event()
    cleanup = window._cache.cleanup

    def paused_cleanup():
        entered.set()
        assert release.wait(12)
        cleanup()

    monkeypatch.setattr(window._cache, 'cleanup', paused_cleanup)
    cache = window._cache_root
    try:
        window.close()
        wait_for(qapp, entered.is_set)
        assert window._closing and window.isVisible()
        assert cache.exists()
        heartbeat = []
        QTimer.singleShot(0, lambda: heartbeat.append(True))
        wait_for(qapp, lambda: bool(heartbeat))
        assert not window.parameters.isEnabled()
    finally:
        release.set()
    wait_for(qapp, lambda: not window.isVisible())
    assert not cache.exists()
