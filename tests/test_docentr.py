import json
import pickle

import numpy as np
from PIL import Image
import pytest

from hanzi_mask import docentr


@pytest.mark.parametrize("shape", [(100, 180), (256, 256), (257, 513), (1, 1)])
@pytest.mark.parametrize("foreground", ["light", "dark"])
def test_tile_roundtrip_preserves_every_pixel_and_polarity(shape, foreground):
    rng = np.random.default_rng(8)
    gray = rng.integers(0, 256, shape, dtype=np.uint8)
    scores = docentr.infer_scores(Image.fromarray(gray), lambda patch: patch, "tile", foreground)
    expected = gray / 255 if foreground == "light" else 1 - gray / 255
    np.testing.assert_allclose(scores, expected, atol=2e-7)


def test_fit_preserves_aspect_ratio_and_uses_white_padding():
    patches = []

    def capture(patch):
        patches.append(patch.copy())
        return patch

    original = Image.new("RGB", (300, 150), "white")
    scores = docentr.infer_scores(original, capture, "fit", "light")
    assert scores.shape == (150, 300)
    np.testing.assert_allclose(scores, 1)
    assert np.all(patches[0][:128] == 0)
    assert np.all(patches[0][128:] == 1)


def test_model_receives_bgr_and_output_uses_luminance():
    seen = []

    def capture(patch):
        seen.append(patch[0, 0].copy())
        return patch

    scores = docentr.infer_scores(Image.new("RGB", (1, 1), "red"), capture, "tile", "dark")
    np.testing.assert_allclose(seen[0], [0, 0, 1])
    assert scores[0, 0] == pytest.approx(1 - 0.299)


def test_smaller_fit_size_keeps_character_in_single_white_padded_patch():
    patches = []

    def capture(patch):
        patches.append(patch.copy())
        return patch

    scores = docentr.infer_scores(Image.new("RGB", (400, 200), "white"), capture, "fit", "light", 128)
    assert scores.shape == (200, 400)
    assert np.all(patches[0][:64, :128] == 0)
    assert np.all(patches[0][64:] == 1)
    assert np.all(patches[0][:, 128:] == 1)
    np.testing.assert_allclose(scores, 1)


def test_nonfinite_model_output_is_rejected():
    with pytest.raises(ValueError, match="非有限"):
        docentr.infer_scores(Image.new("RGB", (10, 10)),
                             lambda p: np.full_like(p, np.nan), "tile", "light")


def setup_cli(tmp_path, monkeypatch):
    source = tmp_path / "input"
    source.mkdir()
    Image.new("RGB", (20, 30), "white").save(source / "a.png")
    repo = tmp_path / "repo"
    (repo / "models").mkdir(parents=True)
    (repo / "models" / "binae.py").write_text("")
    weights = tmp_path / "weights.pt"
    weights.write_bytes(b"fake checkpoint for mocked model")

    class FakeModel:
        device = "fake"
        metadata = {"device": "fake"}

        def __init__(self, *args):
            pass

        def __call__(self, patch):
            return patch

    monkeypatch.setattr(docentr, "DocEnTr", FakeModel)
    output = tmp_path / "result"
    return source, output, [str(source), "-o", str(output), "--repo", str(repo), "--weights", str(weights)]


def test_cli_exports_both_modes_and_preserves_inputs(tmp_path, monkeypatch):
    source, output, argv = setup_cli(tmp_path, monkeypatch)
    before = (source / "a.png").read_bytes()
    assert docentr.main(argv) == 0
    assert (source / "a.png").read_bytes() == before
    for mode in ["tile", "fit"]:
        with Image.open(output / mode / "a.png") as mask:
            assert mask.size == (20, 30)
            assert set(np.unique(mask)) == {255}
    report = json.loads((output / "report.json").read_text())
    assert report["summary"] == {"converted": 1}
    assert report["postprocessing"] == "none"
    # No accidental overwrite of an earlier experiment, even if the source is unchanged.
    with pytest.raises(SystemExit):
        docentr.main(argv)


def test_cli_records_corrupt_images_and_continues(tmp_path, monkeypatch):
    source, output, argv = setup_cli(tmp_path, monkeypatch)
    (source / "bad.png").write_bytes(b"broken image")
    assert docentr.main(argv) == 1
    report = json.loads((output / "report.json").read_text())
    assert report["summary"] == {"converted": 1, "error": 1}
    assert (output / "fit" / "a.png").exists()


def test_cli_rejects_colliding_names_before_export(tmp_path, monkeypatch):
    source, output, argv = setup_cli(tmp_path, monkeypatch)
    Image.new("RGB", (20, 30)).save(source / "a.jpg")
    with pytest.raises(SystemExit):
        docentr.main(argv)
    assert not output.exists()


def test_restricted_checkpoint_load_failure_stops_before_export(tmp_path, monkeypatch, capsys):
    _, output, argv = setup_cli(tmp_path, monkeypatch)

    def rejected(*args):
        raise pickle.UnpicklingError("Weights only load failed")

    monkeypatch.setattr(docentr, "DocEnTr", rejected)
    assert docentr.main(argv) == 1
    assert not output.exists()
    assert "模型加载失败" in capsys.readouterr().err
