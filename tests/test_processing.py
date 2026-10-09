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


@pytest.mark.parametrize('kind,reason', [
    ('manual', 'skipped_manual_mask'),
    ('morphology', 'skipped_non_texture'),
    ('threshold', 'skipped_explicit_threshold'),
    ('uniform', 'skipped_low_contrast'),
])
def test_recovery_helpers_bypass_inapplicable_inputs(tmp_path, monkeypatch, kind, reason):
    def unexpected(*args, **kwargs):
        pytest.fail('weak-structure and tip recovery must skip this processing mode')

    monkeypatch.setattr('hanzi_mask.processing.recover_strokes', unexpected)
    monkeypatch.setattr('hanzi_mask.processing.preserve_tips', unexpected)
    path = tmp_path / 'source.png'
    make_rubbing(path)
    if kind == 'uniform':
        Image.new('L', (40, 50), 127).save(path)
    options = Options(
        method='morphology' if kind == 'morphology' else 'texture',
        threshold=120 if kind == 'threshold' else None,
        stroke_recovery=True, tip_recovery=True, texture_strength=0,
    )
    result = process_image(path, options, path if kind == 'manual' else None)
    assert result.metrics['stroke_recovery_reason'] == reason
    assert result.metrics['tip_recovery_reason'] == reason
    assert result.metrics['stroke_recovery_final_surviving_added_pixels'] == 0
    assert result.metrics['tip_added_pixels'] == 0


def test_cli_default_keeps_desktop_recovery_features_disabled(tmp_path):
    import json
    from hanzi_mask.cli import main

    source, output = tmp_path / 'source.png', tmp_path / 'export'
    make_rubbing(source)
    assert main([str(source), '-o', str(output)]) == 0
    report = json.loads(next(output.glob('report-*.json')).read_text())
    for name in ('edge_refine', 'stroke_recovery', 'tip_recovery'):
        assert report['parameters'][name] is False
    metrics = report['images'][0]
    assert metrics['edge_refinement_enabled'] is False
    assert metrics['stroke_recovery_enabled'] is False
    assert metrics['tip_recovery_enabled'] is False


def test_edge_guide_uses_unmedianed_original_dark_foreground(tmp_path, monkeypatch):
    source = tmp_path / 'dark.png'
    raw = np.full((50, 70), 230, dtype=np.uint8)
    raw[10:42, 28:41] = 20
    raw[6, 6] = 20  # This single pixel disappears under the median filter.
    Image.fromarray(raw).save(source)
    captured = []

    def inspect(mask, guide):
        captured.append(guide.copy())
        return mask.copy(), {'edge_refinement_enabled': True, 'edge_changed_pixels': 0}

    monkeypatch.setattr('hanzi_mask.processing.refine_edges', inspect)
    result = process_image(source, Options(foreground='dark', edge_refine=True, texture_strength=0))
    np.testing.assert_array_equal(captured[0], 255 - raw)
    assert result.candidate[6, 6] == 0


def test_tip_is_last_geometry_stage_and_final_metrics_include_its_addition(tmp_path, monkeypatch):
    source = tmp_path / 'source.png'
    make_rubbing(source)
    stages = []

    def edge(mask, guide):
        stages.append('edge')
        return mask.copy(), {'edge_refinement_enabled': True, 'edge_changed_pixels': 0}

    def tip(mask, guide, *, stroke_radius):
        stages.append('tip')
        assert mask[60, 48] == 255 and mask[60, 49] == 0
        result = mask.copy()
        result[60, 49] = 255
        return result, {'tip_recovery_enabled': True, 'tip_added_pixels': 1}

    monkeypatch.setattr('hanzi_mask.processing.refine_edges', edge)
    monkeypatch.setattr('hanzi_mask.processing.preserve_tips', tip)
    result = process_image(source, Options(
        edge_refine=True, tip_recovery=True, texture_strength=0,
        stroke_radius=3, min_area=30, keep_isolated=True,
    ))
    assert stages == ['edge', 'tip']
    assert result.mask[60, 49] == 255
    assert result.metrics['tip_added_pixels'] == 1
    assert result.metrics['foreground_pixels'] == np.count_nonzero(result.mask)


def test_recovery_added_count_distinguishes_geometry_survival(tmp_path, monkeypatch):
    source = tmp_path / 'source.png'
    make_rubbing(source)

    def recovery(structure, seeds, candidate, **kwargs):
        # Isolate stage accounting from adaptive threshold selection: an accepted
        # addition attached to a tiny fragment must still undergo width pruning.
        additions = np.zeros(seeds.shape, dtype=np.uint8)
        assert seeds[4, 4] == 0
        additions[4, 4] = 255
        return seeds | additions, additions, {
            'stroke_recovery_enabled': True,
            'stroke_recovery_added_pixels_before_geometry': 1,
            'stroke_recovery_final_surviving_added_pixels': 0,
        }

    monkeypatch.setattr('hanzi_mask.processing.recover_strokes', recovery)
    result = process_image(source, Options(
        stroke_recovery=True, texture_strength=0, stroke_radius=3, min_area=30,
    ))
    assert result.mask[4, 4] == 0
    assert result.metrics['stroke_recovery_added_pixels_before_geometry'] == 1
    assert result.metrics['stroke_recovery_final_surviving_added_pixels'] == 0
