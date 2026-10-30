"""Conservative, source-guided refinement of already accepted texture edges.

Only a fixed one-pixel band may change. Strong local source evidence is needed
for every proposal, and digital simple-point rules preserve 8-connected stroke
components and 4-connected background holes. This cannot recover a lost stroke
or determine whether an accepted component is actually part of a character.
"""

from functools import lru_cache

import cv2
import numpy as np

from .cancellation import check_cancelled

BAND_RADIUS_PIXELS = 1.0
MAX_REFINEMENT_PIXELS = 4_000_000
MAX_REFINEMENT_PROPOSALS = 100_000
_GUIDE_WINDOW = 7
_RING = ((-1, -1), (-1, 0), (-1, 1), (0, -1),
         (0, 1), (1, -1), (1, 0), (1, 1))
_CARDINAL = frozenset((1, 3, 4, 6))


def _groups(indices: set[int], connectivity: int) -> list[set[int]]:
    """Connected groups of a 3-by-3 neighborhood with the centre excluded."""
    remaining = set(indices)
    groups = []
    while remaining:
        first = remaining.pop()
        group, pending = {first}, [first]
        while pending:
            current = pending.pop()
            cy, cx = _RING[current]
            neighbours = []
            for other in remaining:
                oy, ox = _RING[other]
                distance = abs(cy - oy) + abs(cx - ox)
                adjacent = distance == 1 if connectivity == 4 else max(abs(cy - oy), abs(cx - ox)) == 1
                if adjacent:
                    neighbours.append(other)
            for other in neighbours:
                remaining.remove(other)
                group.add(other)
                pending.append(other)
        groups.append(group)
    return groups


@lru_cache(maxsize=256)
def _simple_pattern(pattern: int) -> bool:
    foreground = {index for index in range(8) if pattern & (1 << index)}
    if len(_groups(foreground, 8)) != 1:
        return False
    background = set(range(8)) - foreground
    # Only background groups touching the centre through a cardinal edge
    # can be joined or split when that centre pixel changes.
    adjacent_background = [group for group in _groups(background, 4) if group & _CARDINAL]
    return len(adjacent_background) == 1


def _topology(mask: np.ndarray) -> tuple[int, int]:
    foreground_components = int(cv2.connectedComponents(mask, connectivity=8)[0] - 1)
    padded = np.pad(mask, 1, mode="constant")
    background = (padded == 0).astype(np.uint8)
    # Padding supplies one known outside background component.
    holes = int(cv2.connectedComponents(background, connectivity=4)[0] - 2)
    return foreground_components, max(0, holes)


def _binary_mask(mask: np.ndarray) -> bool:
    # Bound validation temporaries even when a caller imports an entire page.
    for start in range(0, mask.size, 1_000_000):
        block = mask.flat[start:start + 1_000_000]
        if not np.all((block == 0) | (block == 255)):
            return False
    return True


def refine_edges(mask: np.ndarray, raw_signal: np.ndarray) -> tuple[np.ndarray, dict]:
    """Refine a binary accepted mask using its unmedianed bright-foreground signal.

    raw_signal is the original luminance polarity or coherent color-difference
    channel, before median filtering and texture smoothing. Uncertain pixels,
    thin structures without reliable inner anchors and the image border are
    retained. Images over four million pixels or more than 100,000 proposals
    retain their mask, rather than resizing contours or allocating unbounded
    working arrays. The result stays original-size, uint8, and strictly 0/255.
    """
    if (not isinstance(mask, np.ndarray) or mask.ndim != 2 or mask.dtype != np.uint8
            or not mask.size or not _binary_mask(mask)):
        raise ValueError("边缘细化需要非空的二维 0/255 uint8 掩膜")
    if (not isinstance(raw_signal, np.ndarray) or raw_signal.shape != mask.shape
            or raw_signal.dtype != np.uint8):
        raise ValueError("原图边缘信号必须是同尺寸的 uint8 灰度图")
    metrics = {
        "edge_refinement_enabled": True,
        "edge_refined": False,
        "edge_changed_pixels": 0,
        "edge_added_pixels": 0,
        "edge_removed_pixels": 0,
        "edge_band_radius_pixels": BAND_RADIUS_PIXELS,
        "edge_guide_window_pixels": _GUIDE_WINDOW,
        "edge_guide_filter_size_pixels": 3,
        "edge_guide_sigma_color_levels": 12.0,
        "edge_guide_sigma_space_pixels": 0.6,
        "edge_guide_filter": "bilateral_3x3",
        "edge_minimum_local_contrast": 16.0,
        "edge_band_pixels": 0,
        "edge_supported_pixels": 0,
        "edge_proposed_pixels": 0,
        "edge_rejected_topology_pixels": 0,
        "edge_actual_maximum_displacement_pixels": 0.0,
        "edge_topology_preserved": True,
        "edge_refinement_reason": "unchanged_no_source_evidence",
        "edge_maximum_image_pixels": MAX_REFINEMENT_PIXELS,
        "edge_maximum_proposals": MAX_REFINEMENT_PROPOSALS,
        "edge_resource_limit": None,
    }
    if mask.size > MAX_REFINEMENT_PIXELS:
        metrics.update(edge_refinement_reason="unchanged_resource_limit",
                       edge_resource_limit="image_pixels")
        return mask.copy(), metrics
    original = mask.copy()
    foreground = original > 0
    if not np.any(foreground) or np.all(foreground):
        metrics["edge_refinement_reason"] = "unchanged_no_boundary"
        return original, metrics
    if min(mask.shape) < 3:
        metrics["edge_refinement_reason"] = "unchanged_image_too_small"
        return original, metrics

    padded = np.pad(foreground.astype(np.uint8), 1, mode="constant")
    inner_distance = cv2.distanceTransform(padded, cv2.DIST_L2, 5)[1:-1, 1:-1]
    outer_distance = cv2.distanceTransform(1 - padded, cv2.DIST_L2, 5)[1:-1, 1:-1]
    band = ((foreground & (inner_distance <= BAND_RADIUS_PIXELS))
            | (~foreground & (outer_distance <= BAND_RADIUS_PIXELS)))
    # There is no source evidence beyond the canvas; preserve its outermost row.
    band[[0, -1], :] = False
    band[:, [0, -1]] = False
    metrics["edge_band_pixels"] = int(np.count_nonzero(band))
    if not np.any(band):
        metrics["edge_refinement_reason"] = "unchanged_no_boundary"
        return original, metrics

    signal = raw_signal.astype(np.float32)
    # Small bilateral support stabilizes noisy samples but does not blur across
    # clear source edges or impose a rounded shape on the character.
    guide = cv2.bilateralFilter(raw_signal, 3, 12.0, 0.6,
                                borderType=cv2.BORDER_REFLECT_101).astype(np.float32)
    inner_anchor = (foreground & (inner_distance >= 2.0)).astype(np.float32)
    outer_anchor = (~foreground & (outer_distance >= 2.0)).astype(np.float32)

    def local_sum(values):
        return cv2.boxFilter(values, cv2.CV_32F, (_GUIDE_WINDOW, _GUIDE_WINDOW),
                             normalize=False, borderType=cv2.BORDER_REFLECT_101)

    inner_count, outer_count = local_sum(inner_anchor), local_sum(outer_anchor)
    foreground_mean = local_sum(signal * inner_anchor) / np.maximum(inner_count, 1.0)
    background_mean = local_sum(signal * outer_anchor) / np.maximum(outer_count, 1.0)
    background_square = local_sum(signal * signal * outer_anchor) / np.maximum(outer_count, 1.0)
    background_std = np.sqrt(np.maximum(background_square - background_mean ** 2, 0.0))
    contrast = foreground_mean - background_mean
    supported = (band & (inner_count >= 2.0) & (outer_count >= 2.0)
                 & (contrast >= np.maximum(16.0, 2.0 * background_std)))
    metrics["edge_supported_pixels"] = int(np.count_nonzero(supported))
    if not np.any(supported):
        return original, metrics
    # Release full-resolution anchor statistics before constructing scores.
    del inner_anchor, outer_anchor, inner_count, outer_count
    del foreground_mean, background_square, background_std

    denominator = np.maximum(contrast, 1.0)
    raw_position = (signal - background_mean) / denominator
    guide_position = (guide - background_mean) / denominator
    del signal, guide, background_mean, contrast, denominator
    additions = supported & ~foreground & (raw_position >= 0.65) & (guide_position >= 0.65)
    removals = supported & foreground & (raw_position <= 0.35) & (guide_position <= 0.35)
    proposals = additions | removals
    proposed_count = int(np.count_nonzero(proposals))
    metrics["edge_proposed_pixels"] = proposed_count
    if proposed_count > MAX_REFINEMENT_PROPOSALS:
        metrics.update(edge_refinement_reason="unchanged_resource_limit",
                       edge_resource_limit="proposal_pixels")
        return original, metrics
    if not proposed_count:
        metrics["edge_refinement_reason"] = "unchanged_source_aligned"
        return original, metrics
    coordinates = np.argwhere(proposals)

    # Apply the clearest evidence first. Every pixel remains inside the fixed
    # original band, so iterative updates can never grow the stroke farther out.
    point_raw, point_guide = raw_position[proposals], guide_position[proposals]
    evidence = np.where(additions[proposals], np.minimum(point_raw, point_guide) - 0.5,
                        0.5 - np.maximum(point_raw, point_guide))
    order = np.argsort(-evidence, kind="stable")
    # Sorting needs at most the bounded proposal vectors, not another complete
    # image-sized confidence map. Distances remain for the displacement guard.
    del raw_position, guide_position, band, supported, removals, proposals
    current = padded.copy()
    rejected = 0
    for step, index in enumerate(order):
        if step % 256 == 0:
            check_cancelled()
        row, column = coordinates[index]
        y, x = int(row) + 1, int(column) + 1
        pattern = 0
        for bit, (dy, dx) in enumerate(_RING):
            if current[y + dy, x + dx]:
                pattern |= 1 << bit
        if _simple_pattern(pattern):
            current[y, x] = 1 if additions[row, column] else 0
        else:
            rejected += 1
    metrics["edge_rejected_topology_pixels"] = rejected
    if rejected == proposed_count:
        metrics["edge_refinement_reason"] = "unchanged_topology_guard"
        return original, metrics
    refined = current[1:-1, 1:-1] * np.uint8(255)
    if _topology(refined) != _topology(original):
        metrics["edge_refinement_reason"] = "unchanged_topology_guard"
        return original, metrics
    added_locations = (original == 0) & (refined > 0)
    removed_locations = (original > 0) & (refined == 0)
    added = int(np.count_nonzero(added_locations))
    removed = int(np.count_nonzero(removed_locations))
    displacement = 0.0
    # The fixed band already bounds distance from added foreground to original
    # foreground, and from removed foreground to original background. Check the
    # other directions too: erasing a thin tip or filling a thin gap can move
    # their endpoints farther than one pixel while staying in the original band.
    if removed:
        new_foreground_distance = cv2.distanceTransform(1 - current, cv2.DIST_L2, 5)[1:-1, 1:-1]
        displacement = max(displacement, float(new_foreground_distance[removed_locations].max()),
                           float(inner_distance[removed_locations].max()))
    if added:
        new_background_distance = cv2.distanceTransform(current, cv2.DIST_L2, 5)[1:-1, 1:-1]
        displacement = max(displacement, float(new_background_distance[added_locations].max()),
                           float(outer_distance[added_locations].max()))
    if displacement > BAND_RADIUS_PIXELS + 1e-6:
        metrics["edge_refinement_reason"] = "unchanged_displacement_guard"
        return original, metrics
    metrics.update(edge_refined=bool(added + removed), edge_changed_pixels=added + removed,
                   edge_added_pixels=added, edge_removed_pixels=removed,
                   edge_actual_maximum_displacement_pixels=displacement,
                   edge_refinement_reason="source_guided_boundary" if added + removed else "unchanged_topology_guard")
    return refined, metrics
