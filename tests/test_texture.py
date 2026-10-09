import numpy as np
from PIL import Image, ImageDraw
import pytest

from hanzi_mask.processing import Options, process_image
from hanzi_mask.texture import smooth_texture, suppress_isolated_regions


def test_texture_fluctuations_are_suppressed_while_main_edge_survives():
    y, x = np.indices((64, 80))
    background = 45 + 25 * ((x + y) % 2)
    foreground = 200 + 25 * ((x + y) % 2)
    source = np.where(x < 40, background, foreground).astype(np.uint8)
    result = smooth_texture(source, 0.012)
    assert result[:, :30].std() < source[:, :30].std() * 0.1
    assert result[:, 50:].std() < source[:, 50:].std() * 0.1
    assert result[:, 45].mean() - result[:, 35].mean() > 120
    assert result.min() >= source.min()
    assert result.max() <= source.max()


def test_smoothing_treats_both_axes_consistently():
    source = np.random.default_rng(13).integers(10, 240, size=(24, 32), dtype=np.uint8)
    direct = smooth_texture(source, 0.008)
    transposed = smooth_texture(source.T, 0.008).T
    assert np.max(np.abs(direct.astype(int) - transposed.astype(int))) <= 1


@pytest.mark.parametrize("shape", [(1, 1), (1, 20), (20, 1), (2, 3)])
def test_tiny_images_are_supported(shape):
    source = np.arange(np.prod(shape), dtype=np.uint8).reshape(shape)
    result = smooth_texture(source, 0.012)
    assert result.shape == source.shape
    assert result.dtype == np.uint8


def test_downsampled_working_image_returns_to_source_size():
    source = np.zeros((61, 97), dtype=np.uint8)
    source[12:50, 30:70] = 220
    result = smooth_texture(source, 0.006, max_side=32)
    assert result.shape == source.shape
    assert result[30, 50] > 180
    assert result[30, 2] < 30


def test_constant_image_and_zero_strength_are_unchanged():
    source = np.full((25, 35), 127, dtype=np.uint8)
    assert np.array_equal(smooth_texture(source, 0.02), source)
    source[10:15, 12:20] = 230
    assert np.array_equal(smooth_texture(source, 0), source)


def test_nearby_dot_is_kept_and_remote_small_speck_is_removed():
    mask = np.zeros((120, 120), dtype=np.uint8)
    mask[20:100, 55:70] = 255
    mask[35:45, 75:81] = 255
    mask[5:11, 5:15] = 255
    result, removed = suppress_isolated_regions(mask, gap=8)
    assert result[50, 60] == 255
    assert result[39, 78] == 255
    assert result[8, 8] == 0
    assert removed == 1


def test_large_disconnected_parts_are_all_kept():
    mask = np.zeros((100, 150), dtype=np.uint8)
    mask[20:80, 15:30] = 255
    mask[15:90, 110:125] = 255
    result, removed = suppress_isolated_regions(mask, gap=3)
    assert np.array_equal(result, mask)
    assert removed == 0


@pytest.mark.parametrize("kwargs", [
    {"texture_strength": float("nan")}, {"texture_strength": -0.01},
    {"texture_strength": 0.06}, {"texture_scale": float("inf")},
    {"texture_scale": 0}, {"method": "unknown"},
])
def test_invalid_texture_options(kwargs):
    with pytest.raises(ValueError):
        Options(**kwargs)


@pytest.mark.parametrize("method", ["texture", "morphology"])
def test_explicit_threshold_and_method_are_reported(tmp_path, method):
    path = tmp_path / "source.png"
    source = Image.new("L", (64, 64), 20)
    ImageDraw.Draw(source).rectangle((20, 10, 35, 50), fill=230)
    source.save(path)
    result = process_image(path, Options(method=method, threshold=150))
    assert result.metrics["threshold"] == 150
    assert result.metrics["method"] == method
    assert result.mask[30, 27] == 255
    assert result.mask[30, 5] == 0


def test_manual_mask_bypasses_texture_processing(tmp_path, monkeypatch):
    def unexpected_call(*args, **kwargs):
        pytest.fail("提供人工掩膜时不应进行自动纹理处理")

    monkeypatch.setattr("hanzi_mask.processing.smooth_texture", unexpected_call)
    path = tmp_path / "source.png"
    source = Image.new("L", (32, 32), 0)
    ImageDraw.Draw(source).rectangle((10, 10, 20, 20), fill=255)
    source.save(path)
    result = process_image(path, Options(), path)
    assert np.array_equal(result.mask, np.asarray(source))
    assert result.metrics["method"] == "manual_mask"
    assert result.metrics["texture_strength"] is None


def test_color_gate_rejects_monochrome_uniform_and_conflicting_hues():
    from hanzi_mask.texture import colored_foreground_signal
    image = np.full((100, 100, 3), 80, dtype=np.uint8)
    assert colored_foreground_signal(image)[0] is None
    image[:] = (180, 40, 40)
    assert colored_foreground_signal(image)[0] is None
    image[:] = 80
    image[15:85, 30:48] = (180, 40, 40)
    image[15:85, 52:70] = (40, 70, 230)
    assert colored_foreground_signal(image)[0] is None


def test_color_hue_wraparound_preserves_both_sides_of_red():
    from hanzi_mask.texture import colored_foreground_signal
    image = np.full((100, 100, 3), 80, dtype=np.uint8)
    image[15:85, 30:48] = (180, 40, 45)
    image[15:85, 48:66] = (180, 45, 40)
    signal, evidence = colored_foreground_signal(image)
    assert signal is not None
    assert signal[30, 40] > 200 and signal[30, 55] > 200
    assert signal[5, 5] == 0
    assert evidence["hue_coherence"] == 1.0
