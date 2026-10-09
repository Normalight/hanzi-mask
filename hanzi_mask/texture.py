"""Structure/texture separation inspired by relative total variation (RTV).

Reference: Xu et al., ACM TOG 31(6), 2012, DOI 10.1145/2366145.2366158.
This grayscale adaptation uses reweighted quadratic regularization, a direct
sparse solver and reflected boundary filtering. It does not use model weights.
"""

import cv2
import numpy as np
from scipy.sparse import diags
from scipy.sparse.linalg import spsolve


TEXTURE_PROFILES = {
    # regularization strength, threshold bias, opening radius / short side
    "gentle": (0.006, 0.25, 0.010),
    "balanced": (0.012, 0.40, 0.015),
    "strong": (0.020, 0.50, 0.020),
}


def smooth_texture(
    gray: np.ndarray,
    strength: float,
    scale: float = 3.0,
    iterations: int = 4,
    max_side: int = 512,
) -> np.ndarray:
    """Return a uint8 structure image at the original resolution.

    Texture has strong local gradients but weak coherent directional change.
    Reweighting makes diffusion stronger there and weaker across stable edges.
    Limiting the working resolution bounds sparse factorization memory usage.
    """
    if gray.ndim != 2 or gray.dtype != np.uint8 or not gray.size:
        raise ValueError("纹理分离需要非空的 uint8 灰度图")
    if not np.isfinite(strength) or not 0 <= strength <= 0.05:
        raise ValueError("纹理平滑强度必须在 0–0.05 之间")
    if not np.isfinite(scale) or not 0.5 <= scale <= 12:
        raise ValueError("纹理尺度必须在 0.5–12 像素之间")
    if iterations < 1 or max_side < 2:
        raise ValueError("迭代次数至少为 1，工作边长至少为 2")
    if strength == 0 or min(gray.shape) < 2 or int(gray.max()) == int(gray.min()):
        return gray.copy()

    height, width = gray.shape
    ratio = min(1.0, max_side / max(height, width))
    working = gray
    if ratio < 1:
        working = cv2.resize(
            gray,
            (max(2, round(width * ratio)), max(2, round(height * ratio))),
            interpolation=cv2.INTER_AREA,
        )
    original = working.astype(np.float64) / 255.0
    structure = original.copy()
    h, w = structure.shape
    sigma = max(0.5, scale * ratio)
    for _ in range(iterations):
        dx = np.diff(structure, axis=1, append=structure[:, -1:])
        dy = np.diff(structure, axis=0, append=structure[-1:, :])
        local_weight = 1.0 / np.maximum(np.hypot(dx, dy), 0.01)
        coarse = cv2.GaussianBlur(
            structure, (0, 0), sigma, borderType=cv2.BORDER_REFLECT
        )
        coherent_x = np.diff(coarse, axis=1, append=coarse[:, -1:])
        coherent_y = np.diff(coarse, axis=0, append=coarse[-1:, :])
        horizontal = strength * local_weight / np.maximum(np.abs(coherent_x), 0.001)
        vertical = strength * local_weight / np.maximum(np.abs(coherent_y), 0.001)
        # No periodic link between a row's last pixel and the next row's first.
        horizontal[:, -1] = 0
        vertical[-1, :] = 0
        diagonal = 1.0 + horizontal + vertical
        diagonal[:, 1:] += horizontal[:, :-1]
        diagonal[1:, :] += vertical[:-1, :]
        horizontal_edges = -horizontal.ravel()[:-1]
        vertical_edges = -vertical.ravel()[:-w]
        system = diags(
            [diagonal.ravel(), horizontal_edges, horizontal_edges,
             vertical_edges, vertical_edges],
            [0, 1, -1, w, -w], shape=(h * w, h * w), format="csc",
        )
        structure = spsolve(system, original.ravel()).reshape(h, w)
        if not np.all(np.isfinite(structure)):
            raise ValueError("纹理分离求解失败")
        sigma = max(sigma / 2.0, 0.5)
    result = np.clip(np.rint(structure * 255), 0, 255).astype(np.uint8)
    if ratio < 1:
        result = cv2.resize(result, (width, height), interpolation=cv2.INTER_LINEAR)
    return result


def suppress_isolated_regions(mask: np.ndarray, gap: float) -> tuple[np.ndarray, int]:
    """Keep substantial components and small components close to those anchors.

    Distance is measured to the actual pixels of all large parts, preserving
    disconnected radicals. A removed island may also be a genuine distant dot.
    """
    count, labels, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    if count <= 2:
        return mask.copy(), 0
    areas = stats[:, cv2.CC_STAT_AREA]
    anchors = areas >= areas[1:].max() * 0.08
    anchors[0] = False
    distance = cv2.distanceTransform((~anchors[labels]).astype(np.uint8), cv2.DIST_L2, 5)
    nearest = np.full(count, np.inf)
    np.minimum.at(nearest, labels.ravel(), distance.ravel())
    keep = anchors | (nearest <= gap)
    keep[0] = False
    removed_count = int(np.count_nonzero(~keep[1:]))
    return keep[labels].astype(np.uint8) * 255, removed_count


def colored_foreground_signal(rgb: np.ndarray) -> tuple[np.ndarray | None, dict]:
    """Find a coherent colored stroke signal against predominantly neutral stone.

    This is a color-evidence gate, not a semantic character detector. A colored
    minority must cover a substantial central extent, share one hue and sit on a
    mostly low-chroma background. RGB chroma rejects similarly bright gray
    stone; hue weighting rejects colored fragments with a different hue.
    Monochrome and heavily tinted images continue using their luminance signal.
    """
    if rgb.ndim != 3 or rgb.shape[2] != 3 or rgb.dtype != np.uint8 or not rgb.size:
        raise ValueError("颜色前景检测需要非空的 uint8 RGB 图")
    chroma = rgb.max(axis=2).astype(np.float32) - rgb.min(axis=2)
    neutral_fraction = float(np.mean(chroma <= 24))
    if neutral_fraction < 0.5 or float(chroma.max()) < 42:
        return None, {}
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    hue = hsv[:, :, 0].astype(np.float32)
    seeds = (chroma >= 42) & (hsv[:, :, 1] >= 85) & (hsv[:, :, 2] >= 40)
    colored_fraction = float(np.mean(seeds))
    if not 0.025 <= colored_fraction <= 0.45:
        return None, {}
    histogram = np.bincount(hue[seeds].astype(np.int32), weights=chroma[seeds], minlength=180)
    neighborhood = sum(np.roll(histogram, shift) for shift in range(-10, 11))
    dominant_hue = int(neighborhood.argmax())
    difference = np.abs(hue - dominant_hue)
    difference = np.minimum(difference, 180 - difference)
    consistent = seeds & (difference <= 10)
    coherence = float(chroma[consistent].sum() / chroma[seeds].sum())
    if coherence < 0.8 or np.mean(consistent) < 0.025:
        return None, {}
    # Dismiss tiny colored marks; the signal must occupy a plausible glyph area.
    _, labels, stats, _ = cv2.connectedComponentsWithStats(consistent.astype(np.uint8), connectivity=8)
    substantial = stats[:, cv2.CC_STAT_AREA] >= max(3, round(chroma.size * 0.005))
    substantial[0] = False
    rows, columns = np.nonzero(substantial[labels])
    if not rows.size or np.ptp(columns) + 1 < rgb.shape[1] * 0.3 or np.ptp(rows) + 1 < rgb.shape[0] * 0.4:
        return None, {}
    # A colored frame or border must not displace an interior neutral glyph.
    border = np.zeros(chroma.shape, dtype=bool)
    border[[0, -1], :] = True
    border[:, [0, -1]] = True
    if np.mean(consistent[border]) > 0.4:
        return None, {}
    hue_weight = np.exp(-0.5 * (difference / 12.0) ** 2)
    scale = 255.0 / max(64.0, float(np.percentile(chroma[consistent], 95)))
    signal = np.clip(np.rint(chroma * hue_weight * scale), 0, 255).astype(np.uint8)
    return signal, {
        "dominant_hue_degrees": dominant_hue * 2,
        "neutral_background_fraction": round(neutral_fraction, 4),
        "colored_seed_fraction": round(float(np.mean(consistent)), 4),
        "hue_coherence": round(coherence, 4),
    }
