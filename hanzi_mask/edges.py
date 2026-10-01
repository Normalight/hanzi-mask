"""Separate binary mask geometry from antialiased display rendering."""

import cv2
import numpy as np


def render_edges(scores, sigma=.7, scale=4):
    """Sigma is in output pixels; no opening/closing or component removal."""
    scores = np.asarray(scores, dtype=np.float32)
    if scores.ndim != 2 or not scores.size or not np.isfinite(scores).all():
        raise ValueError("前景分数必须为非空、有限的二维数组")
    if not np.isfinite(sigma) or not 0 <= sigma <= 3 or scale not in (1, 2, 4):
        raise ValueError("平滑 sigma 须为 0–3，倍率须为 1、2、4")
    scores = np.clip(scores, 0, 1)
    smooth = cv2.GaussianBlur(scores, (0, 0), sigma, borderType=cv2.BORDER_REPLICATE) if sigma else scores
    binary = (smooth > .5).astype(np.uint8) * 255
    # Sample the score isocontour at higher resolution before area downsampling.
    height, width = scores.shape
    high = cv2.resize(smooth, (width * scale, height * scale), interpolation=cv2.INTER_LINEAR)
    high_binary = (high > .5).astype(np.uint8) * 255
    antialiased = cv2.resize(high_binary, (width, height), interpolation=cv2.INTER_AREA)
    return binary, antialiased
