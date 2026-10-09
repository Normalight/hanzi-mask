"""Short source-supported terminals without bridging or partial long branches."""

import cv2
import numpy as np
import pytest

from hanzi_mask import tip_recovery as recovery


def source_tip(taper=True, body_start=10):
    mask = np.zeros((80, 100), dtype=np.uint8)
    mask[20:27 if taper else 23, body_start:60] = 255
    raw = np.where(mask > 0, 220, 20).astype(np.uint8)
    if taper:
        for column, half_width in enumerate((3, 2, 1, 0)):
            raw[23-half_width:24+half_width, 60+column] = 220
    else:
        raw[21, 60:63] = 220
    return mask, raw


def topology(mask):
    components = cv2.connectedComponents(mask, connectivity=8)[0] - 1
    outside = np.pad(mask == 0, 1, constant_values=True).astype(np.uint8)
    holes = cv2.connectedComponents(outside, connectivity=4)[0] - 2
    return components, holes


@pytest.mark.parametrize('taper,expected', [(False, 3), (True, 16)])
def test_supported_short_tip_only_adds_and_preserves_inputs(taper, expected):
    mask, raw = source_tip(taper)
    before_mask, before_raw = mask.copy(), raw.copy()
    result, metrics = recovery.preserve_tips(mask, raw, stroke_radius=3)
    assert metrics['tip_added_pixels'] == expected
    assert metrics['tip_recovery_applied'] is True
    assert metrics['tip_actual_maximum_distance_pixels'] <= metrics['tip_reach_pixels']
    assert result.shape == mask.shape and result.dtype == np.uint8
    assert set(np.unique(result)) == {0, 255}
    assert np.all(result[mask > 0] == 255)
    assert np.all(raw[result > mask] == 220)
    assert topology(result) == topology(mask) == (1, 0)
    np.testing.assert_array_equal(mask, before_mask)
    np.testing.assert_array_equal(raw, before_raw)


def test_long_thin_branch_is_rejected_whole_instead_of_truncated():
    mask, _ = source_tip(False)
    raw = np.where(mask > 0, 220, 20).astype(np.uint8)
    raw[21, 60:83] = 220
    result, metrics = recovery.preserve_tips(mask, raw, stroke_radius=3)
    np.testing.assert_array_equal(result, mask)
    assert metrics['tip_added_pixels'] == 0
    assert metrics['tip_rejected_regions']['complete_extent_exceeds_reach'] == 1


def test_weak_source_contrast_cannot_support_a_tip():
    mask, raw = source_tip()
    raw = np.where(raw > 20, 120, 110).astype(np.uint8)
    result, metrics = recovery.preserve_tips(mask, raw, stroke_radius=3)
    np.testing.assert_array_equal(result, mask)
    assert metrics['tip_rejected_regions']['insufficient_source_contrast'] == 1


def test_source_aligned_mask_does_not_invent_extension():
    mask, _ = source_tip()
    raw = np.where(mask > 0, 220, 20).astype(np.uint8)
    result, metrics = recovery.preserve_tips(mask, raw, stroke_radius=3)
    np.testing.assert_array_equal(result, mask)
    assert metrics['tip_recovery_reason'] == 'unchanged_source_aligned'


def test_bridge_between_two_accepted_components_is_rejected():
    mask = np.zeros((60, 70), dtype=np.uint8)
    mask[20:27, 10:35] = 255
    mask[20:27, 38:50] = 255
    raw = np.where(mask > 0, 220, 20).astype(np.uint8)
    raw[23, 35:38] = 220
    result, metrics = recovery.preserve_tips(mask, raw, stroke_radius=2)
    assert metrics['tip_rejected_regions']['no_unique_accepted_owner'] == 1
    assert topology(result) == topology(mask) == (2, 0)
    np.testing.assert_array_equal(result, mask)


def test_tip_addition_preserves_inner_hole_and_separate_component():
    mask, raw = source_tip()
    mask[22:25, 15:24] = 0
    mask[45:51, 15:35] = 255
    raw[45:51, 15:35] = 220
    # Even apparent source foreground inside the hole must not fill the hole.
    result, metrics = recovery.preserve_tips(mask, raw, stroke_radius=3)
    assert metrics['tip_added_pixels'] == 16
    assert topology(result) == topology(mask) == (2, 1)


def test_total_growth_budget_rejects_supported_tips():
    mask, raw = source_tip(body_start=40)
    result, metrics = recovery.preserve_tips(mask, raw, stroke_radius=3)
    assert metrics['tip_proposed_pixels'] == 16
    assert metrics['tip_maximum_total_added_pixels'] == 8
    assert metrics['tip_recovery_reason'] == 'unchanged_total_growth_guard'
    np.testing.assert_array_equal(result, mask)


@pytest.mark.parametrize('budget', ['image', 'proposals', 'regions'])
def test_resource_limits_preserve_existing_mask(monkeypatch, budget):
    mask, raw = source_tip()

    def unexpected(*args, **kwargs):
        pytest.fail('over-budget tips must exit before further image processing')

    if budget == 'image':
        monkeypatch.setattr(recovery, 'MAX_TIP_PIXELS', mask.size - 1)
        monkeypatch.setattr(recovery.cv2, 'threshold', unexpected)
    elif budget == 'proposals':
        monkeypatch.setattr(recovery, 'MAX_TIP_PROPOSALS', 15)
        monkeypatch.setattr(recovery.cv2, 'morphologyEx', unexpected)
    else:
        monkeypatch.setattr(recovery, 'MAX_TIP_REGIONS', 0)
        monkeypatch.setattr(recovery.cv2, 'morphologyEx', unexpected)
    result, metrics = recovery.preserve_tips(mask, raw, stroke_radius=3)
    np.testing.assert_array_equal(result, mask)
    assert metrics['tip_recovery_reason'] == 'unchanged_resource_limit'
    assert metrics['tip_resource_limit'] == {
        'image': 'image_pixels', 'proposals': 'raw_extension_pixels', 'regions': 'candidate_regions',
    }[budget]


@pytest.mark.parametrize('invalid', ['nonbinary', 'radius_bool', 'signal_size'])
def test_invalid_inputs_rejected(invalid):
    mask, raw = source_tip()
    radius = 3
    if invalid == 'nonbinary':
        mask[0, 0] = 128
    elif invalid == 'radius_bool':
        radius = True
    else:
        raw = raw[:-1]
    with pytest.raises(ValueError):
        recovery.preserve_tips(mask, raw, stroke_radius=radius)
