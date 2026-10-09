from copy import deepcopy
from dataclasses import replace
import json

import numpy as np
from PIL import Image
import pytest

from hanzi_mask.batch_service import (
    BatchResult, export_batch, load_mask, load_result, process_batch,
    replace_originals, restore_replacement, scan_inputs,
)
from hanzi_mask.manual_correction import apply_correction
from hanzi_mask import manual_correction
from hanzi_mask.processing import Options


@pytest.fixture
def sample(tmp_path):
    source = tmp_path / "source" / "glyph.png"
    source.parent.mkdir()
    pixels = np.zeros((40, 50), dtype=np.uint8)
    pixels[8:32, 12:20] = 255
    pixels[20:28, 12:38] = 255
    pixels[5:10, 40:45] = 255
    Image.fromarray(pixels).save(source)
    options = Options(texture_strength=0, threshold=127, median_size=1,
                      stroke_radius=0, min_area=0, keep_isolated=True)
    return source, options


def test_cached_revision_preserves_old_mask_candidate_and_source(sample, tmp_path):
    source, options = sample
    item = process_batch(scan_inputs([source]), options, cache_dir=tmp_path / "cache").items[0]
    source_bytes, old_mask_bytes = source.read_bytes(), item.mask_path.read_bytes()
    old_metrics = deepcopy(item.metrics)
    corrected = load_mask(item)
    corrected[5:10, 40:45] = 0
    copy = corrected.copy()
    revision = apply_correction(item, corrected, tmp_path / "corrections")
    assert revision.result is None and revision.mask_path != item.mask_path
    assert revision.candidate_path == item.candidate_path
    assert revision.source_sha256 == item.source_sha256
    assert revision.options == item.options == options
    assert revision.metrics["parameters"] == options.to_dict()
    assert np.array_equal(corrected, copy)
    assert np.array_equal(load_mask(revision), corrected)
    assert item.mask_path.read_bytes() == old_mask_bytes
    assert item.metrics == old_metrics
    assert source.read_bytes() == source_bytes
    assert revision.metrics["manual_correction"]
    assert revision.metrics["components"] == 1
    assert revision.metrics["correction_removed_pixels"] == 25
    assert revision.metrics["correction_added_pixels"] == 0
    assert revision.metrics["removed_pixels"] == 25
    assert revision.metrics["foreground_pixels"] == np.count_nonzero(corrected)
    assert json.loads((revision.mask_path.parent / "metrics.json").read_text()) == revision.metrics
    # Undo is selecting the earlier item; no source or cache mutation is needed.
    assert load_mask(item)[7, 42] == 255
    assert load_mask(revision)[7, 42] == 0


def test_memory_item_gets_cached_candidate_and_roundtrip_preview(sample, tmp_path):
    source, options = sample
    item = process_batch(scan_inputs([source]), options).items[0]
    revised = apply_correction(item, item.result.mask.copy(), tmp_path / "corrections")
    assert revised.result is None
    assert revised.options == options
    assert revised.candidate_path.is_file()
    loaded = load_result(revised)
    assert np.array_equal(loaded.candidate, item.result.candidate)
    assert np.array_equal(loaded.mask, item.result.mask)
    assert loaded.original.size == (50, 40)


def test_successive_edits_have_independent_cache_and_history(sample, tmp_path):
    source, options = sample
    original = process_batch(scan_inputs([source]), options).items[0]
    first_mask = load_mask(original).copy()
    first_mask[5:10, 40:45] = 0
    first = apply_correction(original, first_mask, tmp_path / "corrections")
    second_mask = load_mask(first).copy()
    second_mask[35:37, 2:5] = 255
    second = apply_correction(first, second_mask, tmp_path / "corrections")
    assert first.mask_path != second.mask_path
    assert first.metrics["correction_steps"] == 1
    assert second.metrics["correction_steps"] == 2
    assert second.metrics["correction_total_removed_pixels"] == 25
    assert second.metrics["correction_total_added_pixels"] == 6
    assert second.metrics["added_pixels"] == 6
    assert second.metrics["components"] == 2
    assert len(first.metrics["correction_history"]) == 1
    assert len(second.metrics["correction_history"]) == 2
    assert load_mask(first)[35, 2] == 0


def test_corrected_item_exports_replaces_and_restores_exact_original(sample, tmp_path):
    source, options = sample
    original_bytes = source.read_bytes()
    original = process_batch(scan_inputs([source]), options).items[0]
    mask = load_mask(original).copy()
    mask[5:10, 40:45] = 0
    revised = apply_correction(original, mask, tmp_path / "corrections")
    batch = BatchResult(options, [revised])
    exported = export_batch(batch, tmp_path / "export")
    with Image.open(exported.outputs[0]) as image:
        assert image.size == (50, 40)
        assert set(np.unique(np.asarray(image))) == {0, 255}
        assert np.array_equal(np.asarray(image), mask)
    report = json.loads(exported.report_path.read_text())
    assert report["schema_version"] == 2
    assert report["parameters"] == report["images"][0]["parameters"] == options.to_dict()
    assert report["images"][0]["metrics"]["manual_correction"]
    replacement = replace_originals(batch, tmp_path / "backup")
    with Image.open(source) as image:
        assert np.array_equal(np.asarray(image), mask)
    restore_replacement(replacement.report_path)
    assert source.read_bytes() == original_bytes


@pytest.mark.parametrize("mask", [
    np.zeros((40, 50), dtype=np.float32),
    np.zeros((40, 50, 3), dtype=np.uint8),
    np.full((40, 50), 127, dtype=np.uint8),
    np.zeros((20, 25), dtype=np.uint8),
    np.empty((0, 0), dtype=np.uint8),
    [[0, 255]],
])
def test_invalid_mask_rejected_without_new_cache(sample, tmp_path, mask):
    source, options = sample
    item = process_batch(scan_inputs([source]), options).items[0]
    original_bytes = source.read_bytes()
    with pytest.raises(ValueError):
        apply_correction(item, mask, tmp_path / "corrections")
    assert not (tmp_path / "corrections").exists()
    assert source.read_bytes() == original_bytes


def test_stale_source_refuses_correction(sample, tmp_path):
    source, options = sample
    item = process_batch(scan_inputs([source]), options).items[0]
    source.write_bytes(source.read_bytes() + b"external modification")
    with pytest.raises(ValueError, match="变化"):
        apply_correction(item, load_mask(item), tmp_path / "corrections")
    assert not (tmp_path / "corrections").exists()


def test_clear_all_updates_empty_review_and_does_not_repeat_static_reasons(sample, tmp_path):
    source, options = sample
    item = process_batch(scan_inputs([source]), options).items[0]
    cleared = apply_correction(item, np.zeros((40, 50), dtype=np.uint8), tmp_path / "corrections")
    assert cleared.metrics["foreground_pixels"] == cleared.metrics["components"] == 0
    assert any("误删了文字主体" in r for r in cleared.metrics["review_reasons"])
    repeated = apply_correction(cleared, load_mask(item), tmp_path / "corrections")
    assert not any("结果没有白色区域" in r for r in repeated.metrics["review_reasons"])
    assert sum("已人工矫正" in r for r in repeated.metrics["review_reasons"]) == 1


def test_source_changed_while_cache_is_written_refuses_revision_and_cleans_partial_cache(sample, tmp_path, monkeypatch):
    source, options = sample
    item = process_batch(scan_inputs([source]), options).items[0]
    original_save = Image.Image.save
    changed = False
    def save_then_change(image, file, *args, **kwargs):
        nonlocal changed
        original_save(image, file, *args, **kwargs)
        if not changed:
            source.write_bytes(source.read_bytes() + b"external modification")
            changed = True
    monkeypatch.setattr(Image.Image, "save", save_then_change)
    with pytest.raises(ValueError, match="变化"):
        apply_correction(item, load_mask(item), tmp_path / "corrections")
    assert list((tmp_path / "corrections").iterdir()) == []
    assert source.read_bytes().endswith(b"external modification")


def test_correction_cache_cannot_be_source_ancestor(sample, tmp_path):
    source, options = sample
    item = process_batch(scan_inputs([source]), options).items[0]
    original_bytes = source.read_bytes()
    with pytest.raises(ValueError, match="缓存目录"):
        apply_correction(item, load_mask(item), source.parent)
    assert source.read_bytes() == original_bytes
    assert list(source.parent.iterdir()) == [source]


@pytest.mark.parametrize("cached", [False, True])
def test_correction_and_undo_keep_actual_options_under_changed_batch_context(sample, tmp_path, cached):
    source, options = sample
    actual = replace(options, edge_refine=True, stroke_recovery=True, tip_recovery=True)
    original = process_batch(scan_inputs([source]), actual,
                             cache_dir=tmp_path / "cache" if cached else None).items[0]
    source_bytes = source.read_bytes()
    mask = load_mask(original).copy()
    mask[5:10, 40:45] = 0
    first = apply_correction(original, mask, tmp_path / "corrections")
    second_mask = load_mask(first).copy()
    second_mask[35:37, 2:5] = 255
    second = apply_correction(first, second_mask, tmp_path / "corrections")
    changed_context = replace(actual, threshold=250, keep_isolated=False, tip_recovery=False)
    for index, item in enumerate([original, first, second]):
        assert item.options == actual
        assert item.metrics["parameters"] == actual.to_dict()
        if item.mask_path is not None:
            cache_metrics = json.loads((item.mask_path.parent / "metrics.json").read_text())
            assert cache_metrics["parameters"] == actual.to_dict()
        # Undo chooses an older immutable item, including its actual parameters.
        summary = export_batch(BatchResult(changed_context, [item]), tmp_path / f"export-{index}")
        report = json.loads(summary.report_path.read_text())
        assert report["parameter_mode"] == "uniform"
        assert report["parameters"] == report["images"][0]["parameters"] == actual.to_dict()
        assert report["batch_context_parameters"] == changed_context.to_dict()
        with Image.open(summary.outputs[0]) as image:
            assert np.array_equal(np.asarray(image), load_mask(item))
    assert not np.array_equal(load_mask(original), load_mask(first))
    assert not np.array_equal(load_mask(first), load_mask(second))
    assert source.read_bytes() == source_bytes


def test_cached_dark_foreground_correction_uses_actual_foreground_without_solver(tmp_path, monkeypatch):
    source = tmp_path / "dark.png"
    rgba = np.zeros((30, 30, 4), dtype=np.uint8)
    rgba[8:22, 12:18, 3] = 255
    Image.fromarray(rgba).save(source)
    options = Options(foreground="dark", texture_strength=0, threshold=127,
                      median_size=1, stroke_radius=0, min_area=0, keep_isolated=True)
    item = process_batch(scan_inputs([source]), options, cache_dir=tmp_path / "cache").items[0]
    calls = []
    actual_load = manual_correction.load_result

    def observe_load(record, foreground="light"):
        calls.append(foreground)
        return actual_load(record, foreground)

    def unexpected_solver(*_):
        pytest.fail("manual correction must not run texture processing")

    monkeypatch.setattr(manual_correction, "load_result", observe_load)
    monkeypatch.setattr("hanzi_mask.batch_service.process_image", unexpected_solver)
    revision = apply_correction(item, load_mask(item), tmp_path / "corrections")
    assert calls == ["dark"]
    assert revision.options == options
    loaded = load_result(revision, revision.options.foreground)
    assert np.all(np.asarray(loaded.original)[0, 0] == 255)
    assert np.all(np.asarray(loaded.original)[15, 15] == 0)
    assert np.array_equal(loaded.mask, load_mask(item))


def test_unknown_legacy_options_remain_unknown_after_manual_correction(sample, tmp_path):
    source, options = sample
    original = process_batch(scan_inputs([source]), options).items[0]
    metrics = {key: value for key, value in original.metrics.items() if key != "parameters"}
    legacy = replace(original, options=None, metrics=metrics,
                     result=replace(original.result, metrics=dict(metrics)))
    revision = apply_correction(legacy, load_mask(legacy), tmp_path / "corrections")
    assert revision.options is None
    assert "parameters" not in revision.metrics
    summary = export_batch(BatchResult(options, [revision]), tmp_path / "export")
    report = json.loads(summary.report_path.read_text())
    assert report["parameters"] is None and report["parameters_complete"] is False
    assert report["images"][0]["parameters"] is None
