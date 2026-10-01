import json

import numpy as np
from PIL import Image
import pytest

from hanzi_mask.supervised_data import GlyphPairs, check_checkpoint_sources, letterbox, normalize, read_manifest


def manifest_files(tmp_path):
    mask = np.zeros((70, 90), dtype=np.uint8)
    mask[10:60, 20:30] = 255
    mask[30:40, 10:80] = 255
    Image.fromarray(mask).save(tmp_path / "mask.png")
    Image.fromarray(mask).save(tmp_path / "train.png")
    test = np.roll(mask, 10, axis=0)
    Image.fromarray(test).save(tmp_path / "test.png")
    manifest = {"train": [{"image": "train.png", "mask": "mask.png", "group": "page-a"}],
                "test": [{"image": "test.png", "group": "page-b"}]}
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest))
    return path, manifest


def test_split_rejects_duplicate_contents_despite_different_names(tmp_path):
    path, manifest = manifest_files(tmp_path)
    (tmp_path / "test.png").write_bytes((tmp_path / "train.png").read_bytes())
    with pytest.raises(ValueError, match="重复"):
        read_manifest(path)


def test_split_rejects_shared_page_even_for_different_images(tmp_path):
    path, manifest = manifest_files(tmp_path)
    manifest["test"][0]["group"] = "page-a"
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="来源组"):
        read_manifest(path)


def test_masks_must_be_binary_and_match_the_source(tmp_path):
    path, _ = manifest_files(tmp_path)
    Image.new("L", (90, 70), 127).save(tmp_path / "mask.png")
    with pytest.raises(ValueError, match="0/255"):
        read_manifest(path)
    Image.new("L", (4, 4), 255).save(tmp_path / "mask.png")
    with pytest.raises(ValueError, match="同尺寸"):
        read_manifest(path)


def test_augmentation_is_seeded_binary_and_does_not_read_test_images(tmp_path):
    path, _ = manifest_files(tmp_path)
    manifest = read_manifest(path)
    # Removing held-out data after manifest validation must not affect training.
    (tmp_path / "test.png").unlink()
    pairs = GlyphPairs(manifest["train"], 96)
    a, gt = pairs.batch(np.random.default_rng(17), 3)
    b, gt2 = pairs.batch(np.random.default_rng(17), 3)
    np.testing.assert_array_equal(a, b)
    np.testing.assert_array_equal(gt, gt2)
    assert a.shape == gt.shape == (3, 1, 96, 96)
    assert np.isfinite(a).all() and a.min() >= 0 and a.max() <= 1
    assert set(np.unique(gt)) == {0, 1}
    assert np.count_nonzero((a > .5) & (gt == 0)) > 0


def test_letterbox_preserves_aspect_and_crop_coordinates():
    image = np.ones((50, 100), dtype=np.float32)
    padded, (x, y, w, h) = letterbox(image, 96)
    assert (x, y, w, h) == (0, 24, 96, 48)
    np.testing.assert_array_equal(padded[y:y+h, x:x+w], 1)
    assert padded[:y].sum() == 0


def test_constant_images_normalize_without_nan():
    assert np.isfinite(normalize(np.full((10, 10), 42))).all()


def test_loaded_checkpoint_cannot_relabel_training_data_as_test(tmp_path):
    path, _ = manifest_files(tmp_path)
    manifest = read_manifest(path)
    original = manifest["train"].copy()
    check_checkpoint_sources(original, manifest)
    manifest["test"][0]["sha256"] = original[0]["sha256"]
    with pytest.raises(ValueError, match="训练来源"):
        check_checkpoint_sources(original, manifest)


def test_loaded_checkpoint_rejects_changed_training_labels(tmp_path):
    path, _ = manifest_files(tmp_path)
    manifest = read_manifest(path)
    original = [dict(item) for item in manifest["train"]]
    manifest["train"][0]["mask_sha256"] = "changed"
    with pytest.raises(ValueError, match="掩膜"):
        check_checkpoint_sources(original, manifest)
