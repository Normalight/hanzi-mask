"""Original-pixel contour bounds, topology and early resource exits."""

import cv2
import numpy as np
import pytest

from hanzi_mask import edge_refinement as refinement


def topology(mask):
    components = cv2.connectedComponents(mask, connectivity=8)[0] - 1
    outside = np.pad(mask == 0, 1, constant_values=True).astype(np.uint8)
    holes = cv2.connectedComponents(outside, connectivity=4)[0] - 2
    return components, holes


def shifted_rectangle():
    mask = np.zeros((42, 49), dtype=np.uint8)
    mask[8:31, 13:33] = 255
    raw = np.full(mask.shape, 20, dtype=np.uint8)
    raw[8:31, 12:32] = 220
    return mask, raw


def test_source_guided_shift_stays_binary_and_within_one_original_pixel():
    mask, raw = shifted_rectangle()
    before_mask, before_raw = mask.copy(), raw.copy()
    result, metrics = refinement.refine_edges(mask, raw)
    assert metrics['edge_changed_pixels'] > 0
    assert metrics['edge_actual_maximum_displacement_pixels'] <= 1
    assert result.shape == mask.shape and result.dtype == np.uint8
    assert set(np.unique(result)) == {0, 255}
    assert topology(result) == topology(mask) == (1, 0)
    added, removed = (mask == 0) & (result > 0), (mask > 0) & (result == 0)
    for foreground in (True, False):
        old = (mask > 0) if foreground else (mask == 0)
        new = (result > 0) if foreground else (result == 0)
        distance_to_new = cv2.distanceTransform((~new).astype(np.uint8), cv2.DIST_L2, 5)
        distance_to_old = cv2.distanceTransform((~old).astype(np.uint8), cv2.DIST_L2, 5)
        assert distance_to_new[old].max() <= 1
        assert distance_to_old[new].max() <= 1
    assert np.count_nonzero(added | removed) == metrics['edge_changed_pixels']
    np.testing.assert_array_equal(mask, before_mask)
    np.testing.assert_array_equal(raw, before_raw)


def test_notch_repair_preserves_hole_and_separate_component():
    mask = np.zeros((50, 60), dtype=np.uint8)
    mask[10:36, 10:36] = 255
    mask[16:30, 16:30] = 0
    mask[10, 22] = 0
    mask[14:30, 46:52] = 255
    raw = np.where(mask > 0, 220, 20).astype(np.uint8)
    raw[10, 22] = 220
    result, metrics = refinement.refine_edges(mask, raw)
    assert result[10, 22] == 255
    assert metrics['edge_changed_pixels'] == 1
    assert topology(result) == topology(mask) == (2, 1)


def test_erasing_two_pixel_thin_tip_rolls_back_displacement():
    mask = np.zeros((50, 50), dtype=np.uint8)
    mask[20:32, 15:25] = 255
    mask[25, 25:27] = 255
    raw = np.full(mask.shape, 20, dtype=np.uint8)
    raw[20:32, 15:25] = 220
    raw[25, 25], raw[25, 26] = 10, 0
    result, metrics = refinement.refine_edges(mask, raw)
    assert metrics['edge_proposed_pixels'] == 2
    assert metrics['edge_refinement_reason'] == 'unchanged_displacement_guard'
    np.testing.assert_array_equal(result, mask)


def test_no_source_evidence_skips_final_topology_scan(monkeypatch):
    mask, _ = shifted_rectangle()

    def unexpected(*args, **kwargs):
        pytest.fail('an unchanged mask does not need a final full-image topology scan')

    monkeypatch.setattr(refinement, '_topology', unexpected)
    result, metrics = refinement.refine_edges(mask, np.full(mask.shape, 127, dtype=np.uint8))
    np.testing.assert_array_equal(result, mask)
    assert metrics['edge_changed_pixels'] == 0


@pytest.mark.parametrize('budget', ['image', 'proposals'])
def test_resource_limits_preserve_mask(monkeypatch, budget):
    mask, raw = shifted_rectangle()

    def unexpected(*args, **kwargs):
        pytest.fail('resource exit must precede the guarded expensive operation')

    if budget == 'image':
        monkeypatch.setattr(refinement, 'MAX_REFINEMENT_PIXELS', mask.size - 1)
        monkeypatch.setattr(refinement.cv2, 'distanceTransform', unexpected)
    else:
        monkeypatch.setattr(refinement, 'MAX_REFINEMENT_PROPOSALS', 0)
        monkeypatch.setattr(refinement, '_topology', unexpected)
    result, metrics = refinement.refine_edges(mask, raw)
    np.testing.assert_array_equal(result, mask)
    assert metrics['edge_refinement_reason'] == 'unchanged_resource_limit'
    assert metrics['edge_resource_limit'] == ('image_pixels' if budget == 'image' else 'proposal_pixels')


@pytest.mark.parametrize('invalid', ['mask_dtype', 'nonbinary', 'signal_size'])
def test_invalid_inputs_rejected(invalid):
    mask, raw = shifted_rectangle()
    if invalid == 'mask_dtype':
        mask = mask.astype(np.float32)
    elif invalid == 'nonbinary':
        mask[0, 0] = 128
    else:
        raw = raw[:-1]
    with pytest.raises(ValueError):
        refinement.refine_edges(mask, raw)
