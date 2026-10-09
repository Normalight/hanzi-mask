"""Preserve short, source-supported tips connected to accepted texture strokes.

This final stage only adds pixels. Whole raw-bright extensions are inspected
before imposing a reach limit, so a long thin branch cannot be silently cut
short and then accepted as a small tip. Shape and digital-topology guards are
conservative evidence, not semantic recognition of a calligraphic stroke.
"""

import cv2
import numpy as np

from .edge_refinement import _RING, _binary_mask, _simple_pattern, _topology


MAX_TIP_PIXELS = 4_000_000
MAX_TIP_PROPOSALS = 100_000
MAX_TIP_REGIONS = 256
_MINIMUM_LOCAL_CONTRAST = 12.0
_MINIMUM_SOURCE_POSITION = 0.60
_MAXIMUM_TOTAL_ADDED_FRACTION = 0.06
_KERNEL = np.ones((3, 3), dtype=np.uint8)


def _shape_evidence(
    proposed: np.ndarray, core: np.ndarray, foreground: np.ndarray,
    distance: np.ndarray, source_width: np.ndarray, reach: int,
) -> tuple[bool, bool]:
    """Find axial continuation or a widening root tapering toward a thin end."""
    contact = (cv2.dilate(proposed.astype(np.uint8), _KERNEL) > 0) & foreground
    contact_points = np.argwhere(contact).astype(np.float64)
    if not contact_points.size:
        return False, False
    attachment = contact_points.mean(axis=0)
    core_points = np.argwhere(core).astype(np.float64)
    outward = core_points.mean(axis=0) - attachment
    length = float(np.linalg.norm(outward))
    axial = False
    if length >= 1.0:
        direction = outward / length
        body_points = np.argwhere(foreground).astype(np.float64)
        # Restrict orientation to the attached body near this root; a distant
        # radical must not dictate the direction of a local terminal stroke.
        body_points = body_points[np.sum((body_points - attachment) ** 2, axis=1) <= reach ** 2]
        if len(body_points) >= 3:
            centered = body_points - body_points.mean(axis=0)
            covariance = centered.T @ centered / len(body_points)
            values, vectors = np.linalg.eigh(covariance)
            alignment = abs(float(vectors[:, -1] @ direction))
            body_forward = float(((body_points - attachment) @ direction).max())
            core_forward = float(((core_points - attachment) @ direction).max())
            axial = (values[-1] >= 0.5 and values[-1] >= 2.0 * max(values[0], 0.25)
                     and alignment >= 0.75 and body_forward <= 2.0 and core_forward >= 1.0)

    # A rounded dot can have an authentic triangular tip despite having no
    # stable body axis. Distance along the full proposed extension distinguishes
    # its broad attachment from its narrower distal source foreground.
    along = distance[proposed]
    widths = source_width[proposed]
    span = float(along.max() - along.min())
    tapered = False
    if span >= 1.0 and len(contact_points) >= 2:
        near = widths[along <= along.min() + 0.35 * span]
        far = widths[along >= along.max() - 0.35 * span]
        if near.size and far.size:
            root_width = float(np.percentile(near, 75))
            end_width = float(np.percentile(far, 75))
            tapered = (root_width + 1e-6 >= end_width + 0.4
                       and end_width <= 0.8 * root_width + 1e-6
                       and end_width <= 2.0)
    return axial, tapered


def preserve_tips(mask: np.ndarray, raw_signal: np.ndarray, *,
                  stroke_radius: int) -> tuple[np.ndarray, dict]:
    """Add bounded, attached fine tips from the unmedianed foreground signal.

    The caller runs this after geometry and edge refinement. There must be no
    later opening, area filter or edge pass trimming these accepted additions.
    raw_signal is bright-foreground luminance or the original color-difference
    channel. Output always remains a fresh original-size 0/255 uint8 mask.
    Resource or evidence failures preserve every original foreground pixel.
    """
    if (not isinstance(mask, np.ndarray) or mask.ndim != 2 or mask.dtype != np.uint8
            or not mask.size or not _binary_mask(mask)):
        raise ValueError("笔尖保留需要非空的二维 0/255 uint8 掩膜")
    if (not isinstance(raw_signal, np.ndarray) or raw_signal.shape != mask.shape
            or raw_signal.dtype != np.uint8):
        raise ValueError("笔尖原图信号必须是同尺寸的 uint8 灰度图")
    if isinstance(stroke_radius, bool) or not isinstance(stroke_radius, int) or not 0 <= stroke_radius <= 100:
        raise ValueError("笔画半径必须是 0–100 之间的整数")
    reach = max(2, min(8, 2 * stroke_radius + 1))
    model_window = 2 * reach + 3
    maximum_region_area = min(64, max(12, 4 * (stroke_radius + 1) ** 2))
    maximum_half_width = max(1.5, min(4.0, stroke_radius + 1.0))
    metrics = {
        "tip_recovery_enabled": True,
        "tip_recovery_applied": False,
        "tip_added_pixels": 0,
        "tip_recovery_reason": "unchanged_no_supported_tips",
        "tip_maximum_image_pixels": MAX_TIP_PIXELS,
        "tip_maximum_proposals": MAX_TIP_PROPOSALS,
        "tip_maximum_regions": MAX_TIP_REGIONS,
        "tip_resource_limit": None,
        "tip_reach_pixels": reach,
        "tip_maximum_growth_rounds": reach,
        "tip_core_minimum_distance_pixels": 1.0,
        "tip_canvas_edge_policy": "reject_touching_source_core_or_proposal",
        "tip_local_window_pixels": model_window,
        "tip_minimum_local_contrast_levels": _MINIMUM_LOCAL_CONTRAST,
        "tip_minimum_source_position": _MINIMUM_SOURCE_POSITION,
        "tip_maximum_region_area_pixels": maximum_region_area,
        "tip_maximum_core_half_width_pixels": maximum_half_width,
        "tip_maximum_total_added_fraction": _MAXIMUM_TOTAL_ADDED_FRACTION,
        "tip_raw_threshold": None,
        "tip_raw_extension_pixels": 0,
        "tip_core_regions": 0,
        "tip_proposed_regions": 0,
        "tip_proposed_pixels": 0,
        "tip_axial_proposed_regions": 0,
        "tip_tapered_proposed_regions": 0,
        "tip_rejected_regions": {},
        "tip_rejected_topology_pixels": 0,
        "tip_topology_preserved": True,
        "tip_actual_maximum_distance_pixels": 0.0,
    }

    def unchanged(reason: str):
        metrics["tip_recovery_reason"] = reason
        return mask.copy(), metrics

    # No image-sized float or label arrays are created before this budget.
    if mask.size > MAX_TIP_PIXELS:
        metrics["tip_resource_limit"] = "image_pixels"
        return unchanged("unchanged_resource_limit")
    foreground_pixels = int(np.count_nonzero(mask))
    if not foreground_pixels or foreground_pixels == mask.size or min(mask.shape) < 3:
        return unchanged("unchanged_no_accepted_boundary")
    maximum_added = max(8, round(foreground_pixels * _MAXIMUM_TOTAL_ADDED_FRACTION))
    metrics["tip_maximum_total_added_pixels"] = maximum_added
    foreground = mask > 0
    raw_threshold, _ = cv2.threshold(raw_signal, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)
    metrics["tip_raw_threshold"] = float(raw_threshold)
    raw_bright = raw_signal > raw_threshold
    extension = raw_bright & ~foreground
    extension_pixels = int(np.count_nonzero(extension))
    metrics["tip_raw_extension_pixels"] = extension_pixels
    # The complete source-bright extent is kept here, including remote pixels
    # without local body anchors. Cropping it to a near-body evidence window
    # would incorrectly turn a long branch into a short acceptable proposal.
    if extension_pixels > MAX_TIP_PROPOSALS:
        metrics["tip_resource_limit"] = "raw_extension_pixels"
        return unchanged("unchanged_resource_limit")
    if not extension_pixels:
        return unchanged("unchanged_source_aligned")
    distance = cv2.distanceTransform((~foreground).astype(np.uint8), cv2.DIST_L2, 5)
    core = extension & (distance > 1.0)
    if not np.any(core):
        return unchanged("unchanged_only_one_pixel_edge")
    count, labels, stats, _ = cv2.connectedComponentsWithStats(core.astype(np.uint8), connectivity=8)
    metrics["tip_core_regions"] = count - 1
    furthest = np.zeros(count, dtype=np.float32)
    nearest = np.full(count, np.inf, dtype=np.float32)
    np.maximum.at(furthest, labels[core], distance[core])
    np.minimum.at(nearest, labels[core], distance[core])
    possible = (nearest <= reach) & (furthest <= reach)
    possible &= stats[:, cv2.CC_STAT_AREA] <= maximum_region_area
    possible &= np.maximum(stats[:, cv2.CC_STAT_WIDTH], stats[:, cv2.CC_STAT_HEIGHT]) <= 2 * reach + 1
    touches_canvas = ((stats[:, cv2.CC_STAT_LEFT] == 0) | (stats[:, cv2.CC_STAT_TOP] == 0)
                      | (stats[:, cv2.CC_STAT_LEFT] + stats[:, cv2.CC_STAT_WIDTH] == mask.shape[1])
                      | (stats[:, cv2.CC_STAT_TOP] + stats[:, cv2.CC_STAT_HEIGHT] == mask.shape[0]))
    possible &= ~touches_canvas
    possible[0] = False
    rejected = {}
    near_region = nearest <= reach
    near_region[0] = False
    for reason, condition in (
        ("complete_extent_exceeds_reach", furthest > reach),
        ("large_source_core", stats[:, cv2.CC_STAT_AREA] > maximum_region_area),
        ("long_source_core", np.maximum(stats[:, cv2.CC_STAT_WIDTH], stats[:, cv2.CC_STAT_HEIGHT]) > 2 * reach + 1),
        ("source_extent_touches_canvas", touches_canvas),
    ):
        amount = int(np.count_nonzero(near_region & condition))
        if amount:
            rejected[reason] = amount
    metrics["tip_rejected_regions"] = rejected
    region_ids = np.flatnonzero(possible)
    if len(region_ids) > MAX_TIP_REGIONS:
        metrics["tip_resource_limit"] = "candidate_regions"
        return unchanged("unchanged_resource_limit")
    if not region_ids.size:
        return unchanged("unchanged_complete_extent_guard")

    background_radius = max(3, 3 * stroke_radius)
    metrics["tip_background_radius_pixels"] = background_radius
    metrics["tip_background_window_pixels"] = 2 * background_radius + 1
    background = cv2.morphologyEx(
        raw_signal, cv2.MORPH_OPEN,
        cv2.getStructuringElement(cv2.MORPH_RECT, (2 * background_radius + 1,) * 2),
        borderType=cv2.BORDER_REFLECT_101,
    )
    source_contrast = cv2.subtract(raw_signal, background)
    del background
    signal = raw_signal.astype(np.float32)
    foreground_anchor = foreground.astype(np.float32)
    background_anchor = (~foreground & ~raw_bright).astype(np.float32)

    def box(values):
        return cv2.boxFilter(values, cv2.CV_32F, (model_window, model_window),
                             normalize=False, borderType=cv2.BORDER_REFLECT_101)

    foreground_count, background_count = box(foreground_anchor), box(background_anchor)
    foreground_mean = box(signal * foreground_anchor) / np.maximum(foreground_count, 1.0)
    background_mean = box(signal * background_anchor) / np.maximum(background_count, 1.0)
    background_square = box(signal * signal * background_anchor) / np.maximum(background_count, 1.0)
    del foreground_anchor, background_anchor
    background_std = np.sqrt(np.maximum(background_square - background_mean ** 2, 0.0))
    del background_square
    local_contrast = foreground_mean - background_mean
    del foreground_mean
    contrast_floor = np.maximum(_MINIMUM_LOCAL_CONTRAST, 1.5 * background_std)
    del background_std
    source_position = (signal - background_mean) / np.maximum(local_contrast, 1.0)
    evidence = (extension & (foreground_count >= 2) & (background_count >= 2)
                & (local_contrast >= contrast_floor)
                & (source_contrast >= contrast_floor)
                & (signal - background_mean >= contrast_floor)
                & (source_position >= _MINIMUM_SOURCE_POSITION))
    del signal, foreground_count, background_count, background_mean
    del local_contrast, contrast_floor, source_contrast
    source_width = cv2.distanceTransform(
        np.pad(raw_bright.astype(np.uint8), 1), cv2.DIST_L2, 5
    )[1:-1, 1:-1]
    _, owners = cv2.connectedComponents(mask, connectivity=8)
    proposals = np.zeros(mask.shape, dtype=bool)

    def reject(reason):
        rejected[reason] = rejected.get(reason, 0) + 1

    height, width = mask.shape
    for region in region_ids:
        x, y, w, h, _ = stats[region]
        # Include the core's one-pixel bridge and its accepted body contact.
        padding = reach + 2
        top, bottom = max(0, int(y) - padding), min(height, int(y + h) + padding)
        left, right = max(0, int(x) - padding), min(width, int(x + w) + padding)
        crop = np.s_[top:bottom, left:right]
        local_core = labels[crop] == region
        local_foreground = foreground[crop]
        bridge = ((cv2.dilate(local_core.astype(np.uint8), _KERNEL) > 0)
                  & extension[crop] & (distance[crop] <= 1.0))
        proposed = local_core | bridge
        proposed_pixels = int(np.count_nonzero(proposed))
        if ((top == 0 and np.any(proposed[0, :]))
                or (bottom == height and np.any(proposed[-1, :]))
                or (left == 0 and np.any(proposed[:, 0]))
                or (right == width and np.any(proposed[:, -1]))):
            reject("tip_region_touches_canvas")
            continue
        if proposed_pixels > maximum_region_area:
            reject("large_tip_region")
            continue
        if not np.all(evidence[crop][proposed]):
            reject("insufficient_source_contrast")
            continue
        if float(source_width[crop][local_core].max()) > maximum_half_width:
            reject("broad_source_region")
            continue
        neighbours = cv2.dilate(proposed.astype(np.uint8), _KERNEL) > 0
        attached_owners = np.unique(owners[crop][neighbours & local_foreground])
        if len(attached_owners) != 1:
            reject("no_unique_accepted_owner")
            continue
        owner_foreground = local_foreground & (owners[crop] == attached_owners[0])
        # Every proposed pixel must be reachable as a complete region within
        # the same bounded number of 8-neighbour steps. An overlong source
        # region is refused as a whole, rather than truncated to these rounds.
        grown = owner_foreground.astype(np.uint8)
        allowed = owner_foreground | proposed
        for _ in range(reach):
            grown = (cv2.dilate(grown, _KERNEL) > 0) & allowed
            grown = grown.astype(np.uint8)
        if not np.all(grown[proposed]):
            reject("complete_geodesic_extent_exceeds_reach")
            continue
        axial, tapered = _shape_evidence(
            proposed, local_core, owner_foreground, distance[crop], source_width[crop], reach
        )
        if not axial and not tapered:
            reject("no_terminal_or_taper_evidence")
            continue
        proposals[crop] |= proposed
        metrics["tip_proposed_regions"] += 1
        metrics["tip_axial_proposed_regions"] += int(axial)
        metrics["tip_tapered_proposed_regions"] += int(tapered)

    proposed_pixels = int(np.count_nonzero(proposals))
    metrics["tip_proposed_pixels"] = proposed_pixels
    if not proposed_pixels:
        return unchanged("unchanged_source_or_shape_guard")
    if proposed_pixels > MAX_TIP_PROPOSALS or proposed_pixels > maximum_added:
        metrics["tip_resource_limit"] = "proposal_pixels" if proposed_pixels > MAX_TIP_PROPOSALS else None
        return unchanged("unchanged_resource_limit" if proposed_pixels > MAX_TIP_PROPOSALS else "unchanged_total_growth_guard")
    coordinates = np.argwhere(proposals)
    # Near-body points go first, then stronger source evidence. A simple-point
    # addition cannot start a new component or bridge two accepted branches.
    # Topology can reject individual points; a short proposal is not guaranteed
    # to be fully restored even after passing the whole-region evidence guards.
    order = np.lexsort((-source_position[proposals], distance[proposals]))
    del labels, owners, source_width, source_position, evidence, extension, raw_bright, core
    current = np.pad(foreground.astype(np.uint8), 1)
    rejected_pixels = 0
    for index in order:
        row, column = coordinates[index]
        cy, cx = int(row) + 1, int(column) + 1
        pattern = 0
        for bit, (dy, dx) in enumerate(_RING):
            if current[cy + dy, cx + dx]:
                pattern |= 1 << bit
        if _simple_pattern(pattern):
            current[cy, cx] = 1
        else:
            rejected_pixels += 1
    metrics["tip_rejected_topology_pixels"] = rejected_pixels
    if rejected_pixels == proposed_pixels:
        return unchanged("unchanged_topology_guard")
    recovered = current[1:-1, 1:-1] * np.uint8(255)
    if _topology(recovered) != _topology(mask):
        return unchanged("unchanged_topology_guard")
    added_locations = recovered > mask
    added = int(np.count_nonzero(added_locations))
    maximum_distance = float(distance[added_locations].max()) if added else 0.0
    metrics.update(tip_recovery_applied=bool(added), tip_added_pixels=added,
                   tip_actual_maximum_distance_pixels=maximum_distance,
                   tip_recovery_reason="source_supported_short_tip" if added else "unchanged_topology_guard")
    return recovered, metrics
