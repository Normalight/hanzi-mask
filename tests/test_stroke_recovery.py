"""Seed-connected weak structure and bounded reconstruction regressions."""

import numpy as np
import pytest

from hanzi_mask import stroke_recovery as recovery


def weak_stroke(level=110):
    structure = np.full((48, 64), 20, dtype=np.uint8)
    seeds = np.zeros(structure.shape, dtype=np.uint8)
    seeds[16:24, 12:32] = 255
    structure[seeds > 0] = 180
    structure[16:24, 32:38] = level
    structure[8:11, 46:50] = 110  # Unseeded, equally bright background fragment.
    candidate = (structure > 80).astype(np.uint8) * 255
    return structure, seeds, candidate


def recover(structure, seeds, candidate):
    return recovery.recover_strokes(
        structure, seeds, candidate, structure_threshold=100,
        seed_threshold=150, stroke_radius=1,
    )


def test_weak_connected_stroke_is_added_but_unseeded_fragment_is_not():
    structure, seeds, candidate = weak_stroke()
    before = [array.copy() for array in (structure, seeds, candidate)]
    result, additions, metrics = recover(structure, seeds, candidate)

    assert result[20, 35] == 255 and result[9, 48] == 0
    assert result.shape == seeds.shape and result.dtype == np.uint8
    assert set(np.unique(result)) == {0, 255}
    assert np.all(result[seeds > 0] == 255)
    np.testing.assert_array_equal(result, seeds | additions)
    assert metrics['stroke_recovery_added_pixels_before_geometry'] == 48
    assert metrics['stroke_recovery_final_surviving_added_pixels'] == 0
    for array, original in zip((structure, seeds, candidate), before):
        np.testing.assert_array_equal(array, original)


@pytest.mark.parametrize('candidate_support', [False, True])
def test_below_structure_threshold_needs_candidate_support(candidate_support):
    structure, seeds, candidate = weak_stroke(95)
    result, additions, metrics = recover(structure, seeds, candidate if candidate_support else seeds)
    assert result[20, 35] == (255 if candidate_support else 0)
    assert metrics['stroke_recovery_applied'] is candidate_support
    assert (additions is not None) is candidate_support
    assert metrics['stroke_recovery_candidate_floor_threshold'] < 95 < 100


@pytest.mark.parametrize('candidate_support', [False, True])
def test_large_growth_requires_candidate_coverage(candidate_support):
    structure = np.full((60, 70), 20, dtype=np.uint8)
    structure[20:25, 15:39] = 110
    seeds = np.zeros(structure.shape, dtype=np.uint8)
    seeds[20:25, 15:19] = 255
    structure[seeds > 0] = 180
    candidate = (structure > 100).astype(np.uint8) * 255 if candidate_support else seeds
    result, _, metrics = recover(structure, seeds, candidate)
    assert np.all(result[seeds > 0] == 255)
    assert metrics['stroke_recovery_applied'] is candidate_support
    if not candidate_support:
        assert metrics['stroke_recovery_rejected_region_reasons']['growth_without_candidate_support'] == 1


def test_insufficient_seed_and_excessive_growth_preserve_seeds():
    structure = np.full((40, 50), 20, dtype=np.uint8)
    structure[15:20, 15:35] = 110
    seeds = np.zeros(structure.shape, dtype=np.uint8)
    seeds[17, 15:17] = 255
    structure[seeds > 0] = 180
    result, additions, metrics = recover(structure, seeds, (structure > 100).astype(np.uint8) * 255)
    np.testing.assert_array_equal(result, seeds)
    assert additions is None
    assert metrics['stroke_recovery_rejected_region_reasons']['insufficient_seed_support'] == 1
    assert metrics['stroke_recovery_rejected_region_reasons']['excessive_growth'] == 1


def test_resource_budget_precedes_morphology_and_labels(monkeypatch):
    structure, seeds, candidate = weak_stroke()
    monkeypatch.setattr(recovery, 'MAX_RECOVERY_PIXELS', structure.size - 1)

    def unexpected(*args, **kwargs):
        pytest.fail('over-budget reconstruction must not allocate morphology or labels')

    monkeypatch.setattr(recovery.cv2, 'morphologyEx', unexpected)
    monkeypatch.setattr(recovery.cv2, 'connectedComponentsWithStats', unexpected)
    result, additions, metrics = recover(structure, seeds, candidate)
    np.testing.assert_array_equal(result, seeds)
    assert result is not seeds and additions is None
    assert metrics['stroke_recovery_resource_limit'] == 'image_pixels'


@pytest.mark.parametrize('invalid', ['dtype', 'size', 'binary'])
def test_invalid_mask_rejected(invalid):
    structure, seeds, candidate = weak_stroke()
    if invalid == 'dtype':
        seeds = seeds.astype(np.float32)
    elif invalid == 'size':
        seeds = seeds[:-1]
    else:
        seeds[0, 0] = 128
    with pytest.raises(ValueError):
        recover(structure, seeds, candidate)
