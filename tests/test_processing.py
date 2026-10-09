from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw
import pytest

from hanzi_mask.processing import Options, process_image


def make_rubbing(path: Path, dark=False):
    image = Image.new("L", (100, 100), 230 if dark else 20)
    draw = ImageDraw.Draw(image)
    foreground = 20 if dark else 230
    draw.rectangle((35, 20, 48, 80), fill=foreground)
    draw.rectangle((20, 35, 75, 48), fill=foreground)
    # A disconnected thick dot is a valid part of the synthetic character.
    draw.rectangle((68, 65, 80, 77), fill=foreground)
    # Noise has exactly the same color as the strokes.
    for point in [(5, 5), (85, 15), (10, 90), (95, 95)]:
        draw.rectangle((*point, point[0] + 2, point[1] + 2), fill=foreground)
    # A thin branch is attached to the main vertical stroke.
    draw.line((15, 65, 34, 65), fill=foreground, width=1)
    image.save(path)


@pytest.mark.parametrize("dark", [False, True])
@pytest.mark.parametrize("method", ["morphology", "texture"])
def test_same_color_noise_and_thin_attached_branch_are_suppressed(tmp_path, dark, method):
    path = tmp_path / "字.png"
    make_rubbing(path, dark)
    result = process_image(path, Options(
        foreground="dark" if dark else "light",
        stroke_radius=3, min_area=30, median_size=1,
        method=method,
    ))
    assert result.candidate[5, 5] == 255
    assert result.mask[5, 5] == 0
    assert result.candidate[65, 18] == 255
    assert result.mask[65, 18] == 0
    assert result.mask[60, 40] == 255
    assert result.mask[40, 60] == 255
    assert result.mask[70, 74] == 255
    assert result.metrics["components"] == 2
    assert set(np.unique(result.mask)) == {0, 255}
    assert not np.any((result.mask > 0) & (result.candidate == 0))


@pytest.mark.parametrize("level", [0, 120, 255])
def test_uniform_images_do_not_create_a_solid_character(tmp_path, level):
    path = tmp_path / "blank.png"
    Image.new("L", (40, 40), level).save(path)
    result = process_image(path, Options())
    assert not np.any(result.mask)
    assert result.metrics["low_contrast"]
    assert result.metrics["review_reasons"]


def test_manual_mask_preserves_explicit_strokes_and_removes_noise(tmp_path):
    source = tmp_path / "source.png"
    make_rubbing(source)
    manual = tmp_path / "manual.png"
    mask = Image.new("L", (100, 100), 0)
    ImageDraw.Draw(mask).line((10, 20, 90, 20), fill=255, width=1)
    mask.save(manual)
    result = process_image(source, Options(profile="strong"), manual)
    assert np.array_equal(result.mask, np.asarray(mask))
    assert result.metrics["method"] == "manual_mask"
    assert result.metrics["added_pixels"] > 0


def test_manual_mask_size_mismatch_is_an_error(tmp_path):
    source = tmp_path / "source.png"
    make_rubbing(source)
    mask = tmp_path / "mask.png"
    Image.new("L", (50, 50), 255).save(mask)
    with pytest.raises(ValueError, match="尺寸"):
        process_image(source, Options(), mask)


def test_transparent_pixels_do_not_turn_into_foreground(tmp_path):
    source = tmp_path / "alpha.png"
    image = Image.new("RGBA", (60, 60), (255, 255, 255, 0))
    ImageDraw.Draw(image).rectangle((20, 10, 30, 50), fill=(220, 220, 220, 255))
    image.save(source)
    result = process_image(source, Options(stroke_radius=2, min_area=10))
    assert result.mask[5, 5] == 0
    assert result.mask[20, 25] == 255
    result = process_image(source, Options(), source)
    assert result.mask[5, 5] == 0


def test_exif_orientation_is_respected(tmp_path):
    source = tmp_path / "rotated.jpg"
    image = Image.new("RGB", (40, 80), "black")
    ImageDraw.Draw(image).rectangle((10, 10, 20, 60), fill="white")
    exif = Image.Exif()
    exif[274] = 6
    image.save(source, exif=exif)
    result = process_image(source, Options())
    assert result.original.size == (80, 40)
    assert result.mask.shape == (40, 80)


@pytest.mark.parametrize("parameters", [
    {"threshold": -1}, {"threshold": 256}, {"stroke_radius": -1},
    {"min_area": -1}, {"median_size": 2}, {"restore_steps": 11},
    {"profile": "unknown"}, {"foreground": "unknown"},
])
def test_invalid_parameters(parameters):
    with pytest.raises(ValueError):
        Options(**parameters)


def test_bounded_edge_restoration_does_not_add_foreground(tmp_path):
    path = tmp_path / "source.png"
    make_rubbing(path)
    result = process_image(path, Options(method="morphology", restore_steps=2))
    assert not np.any((result.mask > 0) & (result.candidate == 0))


@pytest.mark.parametrize("ink", [(180, 40, 40), (40, 70, 230), (30, 140, 30)])
@pytest.mark.parametrize("foreground", ["light", "dark"])
def test_colored_strokes_on_equal_luminance_gray_stone_keep_body(tmp_path, ink, foreground):
    # Color retains an observable distinction even when luminance is identical.
    brightness = Image.new("RGB", (1, 1), ink).convert("L").getpixel((0, 0))
    image = Image.new("RGB", (100, 100), (brightness,) * 3)
    draw = ImageDraw.Draw(image)
    draw.rectangle((35, 15, 48, 82), fill=ink)
    draw.rectangle((18, 35, 80, 48), fill=ink)
    draw.rectangle((67, 64, 80, 77), fill=ink)
    draw.rectangle((4, 4, 6, 6), fill=ink)  # Unrelated same-color speck.
    draw.rectangle((88, 6, 93, 11), fill=(235, 235, 235))  # Bright gray stone.
    path = tmp_path / "painted.png"
    image.save(path)
    result = process_image(path, Options(foreground=foreground))
    assert result.metrics["foreground_signal"] == "color"
    assert result.metrics["color_evidence"]["hue_coherence"] >= .8
    assert result.mask[60, 40] == result.mask[40, 60] == result.mask[70, 74] == 255
    assert result.mask[5, 5] == result.mask[8, 90] == 0
    assert np.count_nonzero(result.mask) > 1000
    assert not result.metrics["low_contrast"]


def test_colored_background_does_not_displace_neutral_glyph(tmp_path):
    path = tmp_path / "red-stone.png"
    image = Image.new("RGB", (100, 100), (140, 25, 25))
    ImageDraw.Draw(image).rectangle((40, 15, 58, 84), fill=(235, 235, 235))
    image.save(path)
    result = process_image(path, Options())
    assert result.metrics["foreground_signal"] == "luminance"
    assert result.mask[50, 48] == 255
    assert result.mask[5, 5] == 0


def test_colored_frame_does_not_displace_neutral_glyph(tmp_path):
    path = tmp_path / "red-frame.png"
    image = Image.new("RGB", (100, 100), (30, 30, 30))
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 99, 99), outline=(190, 30, 30), width=5)
    draw.rectangle((40, 15, 58, 84), fill=(235, 235, 235))
    image.save(path)
    result = process_image(path, Options())
    assert result.metrics["foreground_signal"] == "luminance"
    assert result.mask[50, 48] == 255


def test_small_colored_mark_does_not_displace_neutral_glyph(tmp_path):
    path = tmp_path / "red-speck.png"
    image = Image.new("RGB", (100, 100), (30, 30, 30))
    draw = ImageDraw.Draw(image)
    draw.rectangle((2, 2, 22, 22), fill=(190, 30, 30))
    draw.rectangle((40, 15, 58, 84), fill=(235, 235, 235))
    image.save(path)
    result = process_image(path, Options())
    assert result.metrics["foreground_signal"] == "luminance"
    assert result.mask[50, 48] == 255


def test_manual_mask_still_ignores_color_inference(tmp_path):
    path = tmp_path / "painted.png"
    image = Image.new("RGB", (100, 100), (80, 80, 80))
    ImageDraw.Draw(image).rectangle((35, 15, 55, 85), fill=(190, 30, 30))
    image.save(path)
    mask = tmp_path / "mask.png"
    expected = Image.new("L", (100, 100), 0)
    ImageDraw.Draw(expected).rectangle((45, 20, 47, 80), fill=255)
    expected.save(mask)
    result = process_image(path, Options(), mask)
    assert result.metrics["foreground_signal"] == "luminance"
    assert np.array_equal(result.mask, np.asarray(expected))
