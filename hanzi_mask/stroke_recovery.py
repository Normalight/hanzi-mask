"""Constrained reconstruction of unevenly bright texture stroke candidates.

The existing bright structure threshold supplies seeds. A lower structure
threshold and local contrast supply an allowed domain; only seed-connected
regions can contribute additions. This precedes the existing width, area and
proximity filters, so its additions are not a claim of final stroke recovery.
"""

import cv2
import numpy as np


MAX_RECOVERY_PIXELS = 4_000_000
_MINIMUM_LOCAL_CONTRAST = 4.0
_LOCAL_CONTRAST_RANGE_RATIO = 0.08
_CANDIDATE_THRESHOLD_RANGE_RATIO = 0.08
_MINIMUM_REGION_SEED_PIXELS = 3
_MAXIMUM_CANVAS_FRACTION = 0.60
_MAXIMUM_BORDER_FRACTION = 0.35
_MAXIMUM_REGION_GROWTH = 8.0
_CANDIDATE_SUPPORT_GROWTH = 4.0
_MINIMUM_CANDIDATE_COVERAGE = 0.60


def _binary_mask(mask: np.ndarray) -> bool:
    # Keep validation temporaries bounded when an entire page was imported.
    for start in range(0, mask.size, 1_000_000):
        block = mask.flat[start:start + 1_000_000]
        if not np.all((block == 0) | (block == 255)):
            return False
    return True


def recover_strokes(
    structure: np.ndarray,
    seeds: np.ndarray,
    candidate: np.ndarray,
    *,
    structure_threshold: float,
    seed_threshold: float,
    stroke_radius: int,
) -> tuple[np.ndarray, np.ndarray | None, dict]:
    """Return the reconstructed mask, its additions, and pre-geometry evidence.

    Inputs are original-size uint8 arrays; seeds and candidate are binary.
    The seed mask is never modified or reduced. The additions mask is binary,
    or None when unchanged, and lets the caller count its final surviving
    pixels after existing geometry and optional source-guided edge refinement.
    No texture solve, rescaling, unrestricted dilation or mask opening occurs
    here. Grayscale rectangular opening estimates background only.
    """
    if (not isinstance(structure, np.ndarray) or structure.ndim != 2
            or structure.dtype != np.uint8 or not structure.size):
        raise ValueError("笔画结构恢复需要非空的二维 uint8 结构图")
    for name, mask in (("亮笔画种子", seeds), ("原始候选", candidate)):
        if (not isinstance(mask, np.ndarray) or mask.shape != structure.shape
                or mask.dtype != np.uint8 or not _binary_mask(mask)):
            raise ValueError(f"{name}必须是同尺寸的 0/255 uint8 掩膜")
    if (not np.isfinite(structure_threshold) or not 0 <= structure_threshold <= 255
            or not np.isfinite(seed_threshold) or not 0 <= seed_threshold <= 255):
        raise ValueError("结构及种子阈值必须在 0–255 之间")
    if not isinstance(stroke_radius, int) or not 0 <= stroke_radius <= 100:
        raise ValueError("笔画半径必须在 0–100 之间")

    background_radius = max(3, 4 * stroke_radius)
    metrics = {
        "stroke_recovery_enabled": True,
        "stroke_recovery_applied": False,
        "stroke_recovery_reason": "unchanged_no_supported_additions",
        "stroke_recovery_maximum_image_pixels": MAX_RECOVERY_PIXELS,
        "stroke_recovery_resource_limit": None,
        "stroke_recovery_structure_threshold": float(structure_threshold),
        "stroke_recovery_seed_threshold": float(seed_threshold),
        "stroke_recovery_candidate_floor_threshold": None,
        "stroke_recovery_dynamic_range_levels": None,
        "stroke_recovery_local_contrast_levels": None,
        "stroke_recovery_minimum_local_contrast_levels": _MINIMUM_LOCAL_CONTRAST,
        "stroke_recovery_local_contrast_range_ratio": _LOCAL_CONTRAST_RANGE_RATIO,
        "stroke_recovery_candidate_threshold_range_ratio": _CANDIDATE_THRESHOLD_RANGE_RATIO,
        "stroke_recovery_background_radius_pixels": background_radius,
        "stroke_recovery_background_window_pixels": 2 * background_radius + 1,
        "stroke_recovery_background_filter": "grayscale_rectangular_opening",
        "stroke_recovery_minimum_region_seed_pixels": _MINIMUM_REGION_SEED_PIXELS,
        "stroke_recovery_maximum_canvas_fraction": _MAXIMUM_CANVAS_FRACTION,
        "stroke_recovery_maximum_border_fraction": _MAXIMUM_BORDER_FRACTION,
        "stroke_recovery_maximum_region_growth": _MAXIMUM_REGION_GROWTH,
        "stroke_recovery_candidate_support_growth": _CANDIDATE_SUPPORT_GROWTH,
        "stroke_recovery_minimum_candidate_coverage": _MINIMUM_CANDIDATE_COVERAGE,
        "stroke_recovery_seed_pixels_before_geometry": int(np.count_nonzero(seeds)),
        "stroke_recovery_allowed_pixels_before_geometry": 0,
        "stroke_recovery_candidate_supported_below_threshold_pixels_before_geometry": 0,
        "stroke_recovery_seed_connected_regions_before_geometry": 0,
        "stroke_recovery_accepted_regions_before_geometry": 0,
        "stroke_recovery_rejected_regions_before_geometry": 0,
        "stroke_recovery_rejected_region_reasons": {},
        "stroke_recovery_proposed_added_pixels_before_geometry": 0,
        "stroke_recovery_added_pixels_before_geometry": 0,
        "stroke_recovery_final_surviving_added_pixels": 0,
    }

    def unchanged(reason: str):
        metrics["stroke_recovery_reason"] = reason
        return seeds.copy(), None, metrics

    # Budget before percentile, morphology, full-image Boolean work or labels.
    if structure.size > MAX_RECOVERY_PIXELS:
        metrics["stroke_recovery_resource_limit"] = "image_pixels"
        return unchanged("unchanged_resource_limit")
    seed_pixels = metrics["stroke_recovery_seed_pixels_before_geometry"]
    if not seed_pixels:
        return unchanged("unchanged_no_bright_seeds")
    if seed_pixels / structure.size >= _MAXIMUM_CANVAS_FRACTION:
        return unchanged("unchanged_seed_canvas_fraction")
    if min(structure.shape) < 3:
        return unchanged("unchanged_image_too_small")

    low, high = np.percentile(structure, (5, 95))
    dynamic_range = float(high - low)
    local_contrast_floor = max(
        _MINIMUM_LOCAL_CONTRAST, _LOCAL_CONTRAST_RANGE_RATIO * dynamic_range
    )
    candidate_floor = max(
        0.0, structure_threshold - _CANDIDATE_THRESHOLD_RANGE_RATIO * dynamic_range
    )
    metrics.update(
        stroke_recovery_dynamic_range_levels=dynamic_range,
        stroke_recovery_local_contrast_levels=local_contrast_floor,
        stroke_recovery_candidate_floor_threshold=candidate_floor,
    )
    # A rectangle permits OpenCV's separable min/max implementation. The
    # grayscale operation is a background reference, never the output shape.
    window = 2 * background_radius + 1
    background = cv2.morphologyEx(
        structure, cv2.MORPH_OPEN,
        cv2.getStructuringElement(cv2.MORPH_RECT, (window, window)),
        borderType=cv2.BORDER_REFLECT_101,
    )
    local_contrast = cv2.subtract(structure, background)
    del background
    supported = local_contrast >= local_contrast_floor
    del local_contrast
    seed_selector = seeds > 0
    candidate_selector = candidate > 0
    below_threshold = (candidate_selector & (structure > candidate_floor)
                       & (structure <= structure_threshold) & supported)
    metrics["stroke_recovery_candidate_supported_below_threshold_pixels_before_geometry"] = int(
        np.count_nonzero(below_threshold & ~seed_selector)
    )
    allowed = (supported & (structure > structure_threshold)) | below_threshold
    # Seeds remain eligible even if they have weak local contrast. Guards only
    # decide which new pixels to admit; they must never delete the bright mask.
    allowed |= seed_selector
    metrics["stroke_recovery_allowed_pixels_before_geometry"] = int(np.count_nonzero(allowed))
    del below_threshold, supported

    count, labels, stats, _ = cv2.connectedComponentsWithStats(
        allowed.astype(np.uint8), connectivity=8
    )
    del allowed
    region_area = stats[:, cv2.CC_STAT_AREA]
    seed_area = np.bincount(labels[seed_selector], minlength=count)
    candidate_area = np.bincount(labels[candidate_selector], minlength=count)
    connected = seed_area > 0
    connected[0] = False
    added_area = region_area - seed_area
    with_additions = connected & (added_area > 0)
    metrics["stroke_recovery_seed_connected_regions_before_geometry"] = int(
        np.count_nonzero(connected)
    )
    metrics["stroke_recovery_proposed_added_pixels_before_geometry"] = int(
        added_area[with_additions].sum()
    )
    if not np.any(with_additions):
        return unchanged("unchanged_no_supported_additions")

    # Count each original image-border pixel once, without a full border map.
    border_labels = np.concatenate((labels[0, :], labels[-1, :],
                                    labels[1:-1, 0], labels[1:-1, -1]))
    border_fraction = np.bincount(border_labels, minlength=count) / border_labels.size
    growth = region_area / np.maximum(seed_area, 1)
    candidate_coverage = candidate_area / np.maximum(region_area, 1)
    guards = {
        "insufficient_seed_support": seed_area < _MINIMUM_REGION_SEED_PIXELS,
        "region_canvas_fraction": region_area / structure.size > _MAXIMUM_CANVAS_FRACTION,
        "region_border_fraction": border_fraction > _MAXIMUM_BORDER_FRACTION,
        "excessive_growth": growth > _MAXIMUM_REGION_GROWTH,
        "growth_without_candidate_support": (
            (growth > _CANDIDATE_SUPPORT_GROWTH)
            & (candidate_coverage < _MINIMUM_CANDIDATE_COVERAGE)
        ),
    }
    accept = with_additions.copy()
    # Counts may overlap: a region can fail more than one independent guard.
    rejected_reasons = {}
    for reason, rejected in guards.items():
        rejected_count = int(np.count_nonzero(with_additions & rejected))
        if rejected_count:
            rejected_reasons[reason] = rejected_count
        accept &= ~rejected
    metrics["stroke_recovery_rejected_region_reasons"] = rejected_reasons
    metrics["stroke_recovery_rejected_regions_before_geometry"] = int(
        np.count_nonzero(with_additions & ~accept)
    )
    if not np.any(accept):
        return unchanged("unchanged_region_guards")
    accepted_additions = int(added_area[accept].sum())
    if (seed_pixels + accepted_additions) / structure.size > _MAXIMUM_CANVAS_FRACTION:
        metrics["stroke_recovery_rejected_regions_before_geometry"] = int(
            np.count_nonzero(with_additions)
        )
        rejected_reasons["reconstructed_canvas_fraction"] = int(np.count_nonzero(accept))
        return unchanged("unchanged_reconstructed_canvas_fraction")

    additions = accept[labels] & ~seed_selector
    addition_mask = additions.astype(np.uint8) * 255
    reconstructed = cv2.bitwise_or(seeds, addition_mask)
    metrics.update(
        stroke_recovery_applied=True,
        stroke_recovery_reason="seed_connected_local_structure_before_geometry",
        stroke_recovery_accepted_regions_before_geometry=int(np.count_nonzero(accept)),
        stroke_recovery_added_pixels_before_geometry=accepted_additions,
    )
    return reconstructed, addition_mask, metrics
