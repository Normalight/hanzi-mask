from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
import threading

import numpy as np
from PIL import Image, PngImagePlugin
import pytest

from hanzi_mask import batch_service as service
from hanzi_mask.processing import Options


@pytest.fixture
def options():
    # Bypass the expensive smooth solver: tests verify desktop I/O semantics.
    return Options(texture_strength=0, stroke_radius=0, min_area=0, keep_isolated=True)


def make_image(path, *, size=(31, 23), format=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    pixels = np.full((size[1], size[0]), 15, dtype=np.uint8)
    pixels[3:19, 6:12] = 220
    pixels[12:17, 6:25] = 240
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text("source-note", "原始图片")
    Image.fromarray(pixels).save(path, format=format, pnginfo=metadata)
    return path


def batch_for(tmp_path, options, count=2, cached=False):
    inputs = [make_image(tmp_path / "source" / f"glyph-{i}.png") for i in range(count)]
    batch = service.process_batch(service.scan_inputs(inputs), options,
                                  cache_dir=tmp_path / "cache" if cached else None)
    return inputs, batch


def read_report(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_scan_stable_recursive_dedup_and_relative_paths(tmp_path):
    root = tmp_path / "字形"
    make_image(root / "10.png")
    make_image(root / "2.png")
    nested = make_image(root / "安" / "1.png")
    assert [i.relative_path.as_posix() for i in service.scan_inputs([root])] == ["2.png", "10.png"]
    scanned = service.scan_inputs([nested, root, root], recursive=True)
    assert len(scanned) == 3
    assert len({i.path for i in scanned}) == 3
    assert any(i.relative_path == Path("字形/安/1.png") for i in scanned)
    assert scanned == service.scan_inputs([root, nested], recursive=True)


def test_scan_collision_names_are_stable(tmp_path):
    root_a = tmp_path / "a" / "root"
    root_b = tmp_path / "b" / "root"
    make_image(root_a / "安.png")
    make_image(root_a / "安.jpg")
    make_image(root_b / "安.png")
    items = service.scan_inputs([root_b, root_a], recursive=True)
    names = [i.relative_path.as_posix().casefold() for i in items]
    assert len(set(names)) == 3
    assert all("root/安-" in n for n in names)
    assert items == service.scan_inputs([root_a, root_b], recursive=True)


def test_scan_excludes_generated_hidden_and_symlink_directories(tmp_path):
    root = tmp_path / "source"
    good = make_image(root / "安" / "one.png")
    make_image(root / ".cache" / "cached.png")
    make_image(root / ".hidden.png")
    make_image(root / "exports" / "result.png")
    outside = tmp_path / "outside"
    make_image(outside / "not-input.png")
    (root / "linked").symlink_to(outside, target_is_directory=True)
    items = service.scan_inputs([root], recursive=True, exclude_paths=[root / "exports"])
    assert [item.path for item in items] == [good]
    with pytest.raises(ValueError, match="排除"):
        service.scan_inputs([root], exclude_paths=[tmp_path])


def test_scan_invalid_inputs_and_traversal(tmp_path):
    with pytest.raises(ValueError):
        service.scan_inputs([tmp_path / "missing"])
    file = tmp_path / "file.txt"
    file.write_text("no image")
    with pytest.raises(ValueError):
        service.scan_inputs([file])
    with pytest.raises(ValueError):
        service.InputImage(file, Path("../outside.png"))


def test_process_partial_error_progress_and_texture_only(tmp_path, options):
    good = make_image(tmp_path / "good.png")
    bad = tmp_path / "bad.png"
    bad.write_bytes(b"not an image")
    events, records = [], []
    batch = service.process_batch(service.scan_inputs([good, bad]), options,
                                  progress=lambda *args: events.append(args),
                                  processed=records.append)
    assert len(batch.items) == len(records) == 1
    assert len(batch.errors) == 1
    assert not batch.cancelled
    assert [event[:2] for event in events] == [(1, 2), (2, 2)]
    with pytest.raises(ValueError, match="texture"):
        service.process_batch([], Options(method="morphology"))


def test_cached_results_load_on_demand_and_export(tmp_path, options):
    inputs, batch = batch_for(tmp_path, options, cached=True)
    assert all(item.result is None for item in batch.items)
    item = batch.items[0]
    assert item.mask_path.exists() and item.candidate_path.exists()
    assert read_report(item.mask_path.parent / "metrics.json") == item.metrics
    loaded = service.load_result(item)
    assert loaded.original.size == (31, 23)
    assert np.array_equal(loaded.mask, service.load_mask(item))
    hashes = [sha256(p.read_bytes()).hexdigest() for p in inputs]
    summary = service.export_batch(batch, tmp_path / "export")
    assert len(summary.outputs) == 2
    for file in summary.outputs:
        with Image.open(file) as image:
            assert image.size == (31, 23)
            assert set(np.unique(np.asarray(image))) <= {0, 255}
    report = read_report(summary.report_path)
    assert report["parameters"]["method"] == "texture"
    assert report["summary"] == {"exported": 2, "errors": 0, "remaining": 0}
    assert [sha256(p.read_bytes()).hexdigest() for p in inputs] == hashes
    with pytest.raises(FileExistsError):
        service.export_batch(batch, tmp_path / "export")


def test_process_cancellation_returns_partial_cache_without_resident_images(tmp_path, options):
    files = [make_image(tmp_path / f"{i}.png") for i in range(3)]
    stopped = False
    def progress(*_):
        nonlocal stopped
        stopped = True
    batch = service.process_batch(service.scan_inputs(files), options, cancel=lambda: stopped,
                                  progress=progress, cache_dir=tmp_path / "cache")
    assert batch.cancelled
    assert len(batch.items) == 1
    assert batch.items[0].result is None
    summary = service.export_batch(batch, tmp_path / "partial-export")
    assert len(summary.outputs) == 1
    assert read_report(summary.report_path)["processing_cancelled"]


def test_source_changed_during_processing_is_rejected(tmp_path, options, monkeypatch):
    file = make_image(tmp_path / "glyph.png")
    original_process = service.process_image
    def changing(path, opts):
        result = original_process(path, opts)
        path.write_bytes(path.read_bytes() + b"changed")
        return result
    monkeypatch.setattr(service, "process_image", changing)
    batch = service.process_batch(service.scan_inputs([file]), options)
    assert not batch.items
    assert "变化" in batch.errors[0].error


def test_cached_preview_refuses_stale_original(tmp_path, options):
    sources, batch = batch_for(tmp_path, options, cached=True)
    sources[0].write_bytes(sources[0].read_bytes() + b"changed")
    with pytest.raises(ValueError, match="变化"):
        service.load_result(batch.items[0])


def test_export_cancel_keeps_completed_files_and_report(tmp_path, options):
    _, batch = batch_for(tmp_path, options)
    stopped = False
    def progress(*_):
        nonlocal stopped
        stopped = True
    summary = service.export_batch(batch, tmp_path / "export", cancel=lambda: stopped, progress=progress)
    assert summary.cancelled and len(summary.outputs) == 1
    assert read_report(summary.report_path)["status"] == "cancelled"


def test_invalid_mask_is_recorded_and_not_published(tmp_path, options):
    _, batch = batch_for(tmp_path, options, count=1)
    batch.items[0].result.mask[0, 0] = 127
    summary = service.export_batch(batch, tmp_path / "export")
    assert not summary.outputs
    assert len(summary.errors) == 1
    assert summary.errors[0].path == batch.items[0].source.path
    assert "0/255" in summary.errors[0].error
    assert read_report(summary.report_path)["images"][0]["status"] == "error"


def test_export_partial_failure_returns_errors_for_the_client(tmp_path, options):
    _, batch = batch_for(tmp_path, options, count=2, cached=True)
    batch.items[1].mask_path.unlink()
    summary = service.export_batch(batch, tmp_path / "export")
    assert len(summary.outputs) == 1
    assert len(summary.errors) == 1
    assert summary.errors[0].path == batch.items[1].source.path
    assert summary.errors[0].error
    assert read_report(summary.report_path)["summary"]["errors"] == len(summary.errors)
    assert not summary.cancelled


def test_parallel_cached_processing_is_bounded_and_callbacks_stay_in_caller(
        tmp_path, options, monkeypatch):
    files = [make_image(tmp_path / f"glyph-{index}.png") for index in range(4)]
    sources = service.scan_inputs(files)
    caller = threading.get_ident()
    active = peak = 0
    lock = threading.Lock()
    second_entered = threading.Event()
    real_process = service.process_single
    callback_threads = []

    def concurrent_process(source, opts):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        try:
            if source == sources[0]:
                assert second_entered.wait(3)
            elif source == sources[1]:
                second_entered.set()
            return real_process(source, opts)
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(service, "process_single", concurrent_process)
    batch = service.process_batch(
        sources, options, cache_dir=tmp_path / "cache", max_workers=2,
        processed=lambda item: callback_threads.append(threading.get_ident()),
        progress=lambda *args: callback_threads.append(threading.get_ident()))
    assert peak == 2 and active == 0
    assert all(thread == caller for thread in callback_threads)
    assert len(callback_threads) == 8
    assert [item.source for item in batch.items] == sources
    assert all(item.result is None and item.mask_path.is_file() for item in batch.items)
    assert not batch.errors and not batch.cancelled
    for item in batch.items:
        expected = real_process(item.source, options)
        np.testing.assert_array_equal(service.load_mask(item), expected.result.mask)


def test_parallel_cancel_discards_running_results_and_stops_submitting(
        tmp_path, options, monkeypatch):
    sources = service.scan_inputs([make_image(tmp_path / f"{index}.png") for index in range(5)])
    second_entered, stopped = threading.Event(), threading.Event()
    started, published, threads = [], [], []
    real_process = service.process_single

    def blocked_process(source, opts):
        started.append(source)
        threads.append(threading.current_thread())
        if source == sources[0]:
            assert second_entered.wait(3)
        elif source == sources[1]:
            second_entered.set()
            assert stopped.wait(3)
        return real_process(source, opts)

    monkeypatch.setattr(service, "process_single", blocked_process)
    batch = service.process_batch(
        sources, options, cache_dir=tmp_path / "cache", max_workers=2,
        cancel=stopped.is_set, progress=lambda *args: stopped.set(), processed=published.append)
    assert batch.cancelled
    assert len(batch.items) == len(published) == 1
    assert batch.items[0].source == sources[0]
    assert set(started) == set(sources[:2])
    assert all(not thread.is_alive() for thread in threads)


def test_parallel_image_failure_does_not_hide_other_results(tmp_path, options):
    good = make_image(tmp_path / "good.png")
    bad = tmp_path / "bad.png"
    bad.write_bytes(b"invalid png")
    progress = []
    batch = service.process_batch(service.scan_inputs([good, bad]), options,
                                  cache_dir=tmp_path / "cache", max_workers=2,
                                  progress=lambda *args: progress.append(args))
    assert len(batch.items) == len(batch.errors) == 1
    assert batch.items[0].source.path == good
    assert batch.errors[0].path == bad
    assert [record[:2] for record in progress] == [(1, 2), (2, 2)]


@pytest.mark.parametrize("workers", [0, 5, 1.5, True])
def test_invalid_parallel_count_does_not_create_cache(tmp_path, options, workers):
    source = make_image(tmp_path / "source.png")
    cache = tmp_path / "cache"
    with pytest.raises(ValueError, match="并行"):
        service.process_batch(service.scan_inputs([source]), options,
                              cache_dir=cache, max_workers=workers)
    assert not cache.exists()


def test_replace_all_backed_up_before_first_write_and_restore_exact_bytes(tmp_path, options, monkeypatch):
    sources, batch = batch_for(tmp_path, options, cached=True)
    originals = {p: p.read_bytes() for p in sources}
    backup = tmp_path / "backup"
    atomic = service._atomic_write
    def checked_write(path, data, exclusive=False):
        if path in originals:
            assert len(list((backup / "originals").glob("*.png"))) == len(sources)
            assert all((backup / "originals" / f"{i:06d}.png").read_bytes() == originals[p]
                       for i, p in enumerate(sources, 1))
        atomic(path, data, exclusive)
    monkeypatch.setattr(service, "_atomic_write", checked_write)
    summary = service.replace_originals(batch, backup)
    assert summary.outputs == sources
    assert all(p.read_bytes() != originals[p] for p in sources)
    assert read_report(summary.report_path)["status"] == "completed"
    restored = service.restore_replacement(summary.report_path)
    assert len(restored.outputs) == len(sources)
    assert all(p.read_bytes() == originals[p] for p in sources)
    assert read_report(summary.report_path)["status"] == "restored"
    service.restore_replacement(summary.report_path)  # Idempotent retry.


def test_replace_refuses_stale_sources_before_any_original_changes(tmp_path, options):
    sources, batch = batch_for(tmp_path, options)
    originals = sources[1].read_bytes()
    sources[0].write_bytes(sources[0].read_bytes() + b"external edit")
    with pytest.raises(ValueError, match="变化"):
        service.replace_originals(batch, tmp_path / "backup")
    assert sources[1].read_bytes() == originals
    assert not (tmp_path / "backup").exists()


def test_replace_rejects_non_png_and_fake_png(tmp_path, options):
    for name in ["glyph.jpg", "fake.png"]:
        image = make_image(tmp_path / name, format="JPEG")
        batch = service.process_batch(service.scan_inputs([image]), options)
        with pytest.raises(ValueError, match="PNG"):
            service.replace_originals(batch, tmp_path / f"backup-{name}")


def test_replace_cancel_after_first_original_rolls_back_all(tmp_path, options):
    sources, batch = batch_for(tmp_path, options)
    originals = [p.read_bytes() for p in sources]
    stopped = False
    def progress(*_):
        nonlocal stopped
        stopped = True
    summary = service.replace_originals(batch, tmp_path / "backup", cancel=lambda: stopped, progress=progress)
    assert summary.cancelled and not summary.outputs
    assert [p.read_bytes() for p in sources] == originals
    report = read_report(summary.report_path)
    assert len(report["images"]) == len(sources)
    assert report["status"] == "cancelled"
    assert report["images"][0]["status"] == "rolled_back"


def test_replace_io_failure_rolls_back_and_reports_recovery(tmp_path, options, monkeypatch):
    sources, batch = batch_for(tmp_path, options)
    originals = [p.read_bytes() for p in sources]
    atomic = service._atomic_write
    def failing_write(path, data, exclusive=False):
        if path == sources[1]:
            raise OSError("磁盘写入失败")
        atomic(path, data, exclusive)
    monkeypatch.setattr(service, "_atomic_write", failing_write)
    with pytest.raises(service.ReplacementError) as failure:
        service.replace_originals(batch, tmp_path / "backup")
    assert not failure.value.rollback_errors
    assert [p.read_bytes() for p in sources] == originals
    assert read_report(failure.value.report_path)["status"] == "rolled_back"


def test_replace_callback_failure_rolls_back(tmp_path, options):
    sources, batch = batch_for(tmp_path, options)
    originals = [p.read_bytes() for p in sources]
    def fail_callback(*_):
        raise KeyError("worker update failed")
    with pytest.raises(service.ReplacementError):
        service.replace_originals(batch, tmp_path / "backup", progress=fail_callback)
    assert [p.read_bytes() for p in sources] == originals


def test_restore_rejects_corrupted_backup_before_touching_any_source(tmp_path, options):
    sources, batch = batch_for(tmp_path, options)
    summary = service.replace_originals(batch, tmp_path / "backup")
    current = [p.read_bytes() for p in sources]
    (summary.output_dir / "originals" / "000002.png").write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="校验"):
        service.restore_replacement(summary.report_path)
    assert [p.read_bytes() for p in sources] == current


def test_restore_rejects_external_edit_before_touching_other_source(tmp_path, options):
    sources, batch = batch_for(tmp_path, options)
    summary = service.replace_originals(batch, tmp_path / "backup")
    first_current = sources[0].read_bytes()
    sources[1].write_bytes(sources[1].read_bytes() + b"external edit")
    with pytest.raises(ValueError, match="修改"):
        service.restore_replacement(summary.report_path)
    assert sources[0].read_bytes() == first_current


def test_rollback_failure_is_visible_and_backup_remains_usable(tmp_path, options):
    sources, batch = batch_for(tmp_path, options)
    def mutate_and_fail(*_):
        sources[0].write_bytes(sources[0].read_bytes() + b"external")
        raise ValueError("failure")
    with pytest.raises(service.ReplacementError) as failure:
        service.replace_originals(batch, tmp_path / "backup", progress=mutate_and_fail)
    assert failure.value.rollback_errors
    report = read_report(failure.value.report_path)
    assert report["status"] == "rollback_failed"
    assert (failure.value.report_path.parent / report["images"][0]["backup"]).exists()


def test_save_refuses_duplicate_outputs_and_incomplete_replacement(tmp_path, options):
    _, batch = batch_for(tmp_path, options)
    batch.items[1] = replace(batch.items[1], source=service.InputImage(batch.items[1].source.path,
                                                                     batch.items[0].source.relative_path))
    with pytest.raises(ValueError, match="冲突"):
        service.export_batch(batch, tmp_path / "export")
    _, partial = batch_for(tmp_path / "partial", options)
    partial.cancelled = True
    with pytest.raises(ValueError, match="完整"):
        service.replace_originals(partial, tmp_path / "backup")


def test_cache_root_can_be_reused_without_overwriting_earlier_results(tmp_path, options):
    source = make_image(tmp_path / "source.png")
    inputs = service.scan_inputs([source])
    first = service.process_batch(inputs, options, cache_dir=tmp_path / "cache")
    second = service.process_batch(inputs, replace(options, threshold=250), cache_dir=tmp_path / "cache")
    assert len(first.items) == len(second.items) == 1
    assert first.items[0].mask_path != second.items[0].mask_path
    assert not np.array_equal(service.load_mask(first.items[0]), service.load_mask(second.items[0]))


def test_replace_cancel_during_preflight_creates_recoverable_noop_report(tmp_path, options):
    sources, batch = batch_for(tmp_path, options)
    originals = [p.read_bytes() for p in sources]
    summary = service.replace_originals(batch, tmp_path / "backup", cancel=lambda: True)
    assert summary.cancelled and summary.report_path.exists()
    assert [p.read_bytes() for p in sources] == originals
    assert read_report(summary.report_path)["status"] == "cancelled"


def test_restore_write_failure_is_recorded_and_can_be_retried(tmp_path, options, monkeypatch):
    sources, batch = batch_for(tmp_path, options)
    originals = [p.read_bytes() for p in sources]
    summary = service.replace_originals(batch, tmp_path / "backup")
    atomic = service._atomic_write
    def fail_second(path, data, exclusive=False):
        if path == sources[1]:
            raise OSError("恢复写入失败")
        atomic(path, data, exclusive)
    monkeypatch.setattr(service, "_atomic_write", fail_second)
    with pytest.raises(service.ReplacementError, match="恢复未完成"):
        service.restore_replacement(summary.report_path)
    report = read_report(summary.report_path)
    assert report["status"] == "restore_failed"
    assert report["restored_count"] == 1
    assert sources[0].read_bytes() == originals[0]
    monkeypatch.setattr(service, "_atomic_write", atomic)
    service.restore_replacement(summary.report_path)
    assert [p.read_bytes() for p in sources] == originals


def test_scan_cancellation_happens_before_walking_and_during_nested_walk(tmp_path, monkeypatch):
    root = tmp_path / "source"
    make_image(root / "nested" / "one.png")
    original_walk = service.os.walk
    called = False
    def observed_walk(*args, **kwargs):
        nonlocal called
        called = True
        return original_walk(*args, **kwargs)
    monkeypatch.setattr(service.os, "walk", observed_walk)
    with pytest.raises(service.ScanCancelled):
        service.scan_inputs([root], recursive=True, cancel=lambda: True)
    assert not called
    checks = 0
    def cancel_midway():
        nonlocal checks
        checks += 1
        return checks >= 6
    with pytest.raises(service.ScanCancelled):
        service.scan_inputs([root], recursive=True, cancel=cancel_midway)
    assert called
