"""Extract candidate strokes using texture separation and geometric filters.

The result is a candidate mask, not semantic identification of a character.
Disconnected strokes remain subject to area and optional proximity filtering.
"""

from dataclasses import asdict, dataclass
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageOps

from .texture import TEXTURE_PROFILES, smooth_texture, suppress_isolated_regions


PROFILES = {
    "gentle": (0.012, 0.001),
    "balanced": (0.020, 0.003),
    "strong": (0.027, 0.005),
}


@dataclass(frozen=True)
class Options:
    profile: str = "balanced"
    foreground: str = "light"
    threshold: int | None = None
    stroke_radius: int | None = None
    min_area: int | None = None
    median_size: int = 3
    restore_steps: int = 0
    method: str = "texture"
    texture_strength: float | None = None
    texture_scale: float = 3.0
    keep_isolated: bool = False

    def __post_init__(self):
        if self.method not in {"texture", "morphology"}:
            raise ValueError("处理方法必须为 texture 或 morphology")
        if self.profile not in PROFILES:
            raise ValueError("未知去噪强度")
        if self.foreground not in {"light", "dark"}:
            raise ValueError("字的颜色必须为 light 或 dark")
        if self.threshold is not None and not 0 <= self.threshold <= 255:
            raise ValueError("阈值必须在 0–255 之间")
        if self.stroke_radius is not None and not 0 <= self.stroke_radius <= 100:
            raise ValueError("笔画半径必须在 0–100 之间")
        if self.min_area is not None and self.min_area < 0:
            raise ValueError("最小区域面积不能为负数")
        if self.median_size not in {1, 3, 5, 7}:
            raise ValueError("中值滤波尺寸必须为 1、3、5 或 7")
        if not 0 <= self.restore_steps <= 10:
            raise ValueError("边缘恢复步数必须在 0–10 之间")
        if self.texture_strength is not None and (
            not np.isfinite(self.texture_strength) or not 0 <= self.texture_strength <= 0.05
        ):
            raise ValueError("纹理平滑强度必须在 0–0.05 之间")
        if not np.isfinite(self.texture_scale) or not 0.5 <= self.texture_scale <= 12:
            raise ValueError("纹理尺度必须在 0.5–12 像素之间")

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Result:
    original: Image.Image
    candidate: np.ndarray
    mask: np.ndarray
    metrics: dict


def load_image(path: Path, foreground: str) -> Image.Image:
    """Respect EXIF rotation and composite alpha onto the background color."""
    with Image.open(path) as source:
        oriented = ImageOps.exif_transpose(source)
        rgba = oriented.convert("RGBA")
        background = "black" if foreground == "light" else "white"
        canvas = Image.new("RGBA", rgba.size, background)
        return Image.alpha_composite(canvas, rgba).convert("RGB")


def disk(radius: int) -> np.ndarray:
    return cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * radius + 1,) * 2)


def filter_components(mask: np.ndarray, min_area: int) -> tuple[np.ndarray, int]:
    count, labels, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    keep = stats[:, cv2.CC_STAT_AREA] >= max(1, min_area)
    keep[0] = False
    return (keep[labels].astype(np.uint8) * 255, int(np.count_nonzero(keep)))


def process_image(path: Path, options: Options, manual_mask: Path | None = None) -> Result:
    original = load_image(path, options.foreground)
    gray = np.asarray(original.convert("L"))
    if options.median_size > 1:
        gray = cv2.medianBlur(gray, options.median_size)

    # Make the foreground bright internally, regardless of input polarity.
    foreground_gray = gray if options.foreground == "light" else 255 - gray
    threshold_type = cv2.THRESH_BINARY
    if options.threshold is None:
        threshold_type |= cv2.THRESH_OTSU
    candidate_threshold, candidate = cv2.threshold(
        foreground_gray, options.threshold or 0, 255, threshold_type
    )
    low_contrast = int(gray.max()) - int(gray.min()) < 3
    if low_contrast:
        candidate[:] = 0

    radius_ratio, area_ratio = PROFILES[options.profile]
    strength, threshold_bias, texture_radius_ratio = TEXTURE_PROFILES[options.profile]
    if options.method == "texture":
        radius_ratio = texture_radius_ratio
    if options.texture_strength is not None:
        strength = options.texture_strength
    radius = options.stroke_radius
    if radius is None:
        radius = max(1, round(min(gray.shape) * radius_ratio))
    min_area = options.min_area
    if min_area is None:
        min_area = max(1, round(gray.size * area_ratio))

    threshold = candidate_threshold
    isolated_removed = 0
    if manual_mask is not None:
        with Image.open(manual_mask) as source:
            # External masks use white=character and black=background.
            supplied = ImageOps.exif_transpose(source)
            if supplied.size != original.size:
                raise ValueError(
                    f"人工掩膜尺寸 {supplied.size} 与原图 {original.size} 不一致"
                )
            if "A" in supplied.getbands() or "transparency" in supplied.info:
                rgba = supplied.convert("RGBA")
                supplied = Image.alpha_composite(
                    Image.new("RGBA", rgba.size, "black"), rgba
                )
            mask = (np.asarray(supplied.convert("L")) > 127).astype(np.uint8) * 255
        _, components = filter_components(mask, 0)
    else:
        mask = candidate.copy()
        if options.method == "texture" and not low_contrast:
            processed = smooth_texture(foreground_gray, strength, options.texture_scale)
            if options.threshold is None:
                threshold, _ = cv2.threshold(
                    processed, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU
                )
                # Prefer the bright stroke interiors over intermediate texture.
                # Clamp the percentile so sparse, bright strokes are never inverted.
                high = max(threshold, float(np.percentile(processed, 95)))
                threshold += threshold_bias * (high - threshold)
            mask = (processed > threshold).astype(np.uint8) * 255
        reconstruction_limit = mask.copy()
        if radius:
            mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, disk(radius))
        mask, _ = filter_components(mask, min_area)
        for _ in range(options.restore_steps):
            mask = cv2.bitwise_and(cv2.dilate(mask, disk(1)), reconstruction_limit)
        if options.method == "texture" and not options.keep_isolated:
            mask, isolated_removed = suppress_isolated_regions(mask, min(gray.shape) * 0.08)
        mask, components = filter_components(mask, min_area)

    foreground_pixels = int(np.count_nonzero(mask))
    candidate_pixels = int(np.count_nonzero(candidate))
    removed_pixels = int(np.count_nonzero((candidate > 0) & (mask == 0)))
    added_pixels = int(np.count_nonzero((candidate == 0) & (mask > 0)))
    removed_fraction = removed_pixels / candidate_pixels if candidate_pixels else 0.0
    reasons = []
    if low_contrast:
        reasons.append("图片亮度近乎一致，无法从亮度分出笔画")
    if not foreground_pixels:
        reasons.append("结果没有白色区域，检查字的明暗方向或降低去噪强度")
    if components >= 8:
        reasons.append("存在较多独立区域，请检查残留碎斑和断裂笔画")
    if removed_fraction >= 0.55 and manual_mask is None:
        reasons.append("删除比例较高，请检查细笔画是否被删除")
    if foreground_pixels / gray.size >= 0.6:
        reasons.append("白色区域占比较高，请检查前景方向和背景粘连")
    if isolated_removed:
        reasons.append("已筛除远离主体的小区域，请复核独立点画；可用 --keep-isolated 保留")
    metrics = {
        "width": original.width,
        "height": original.height,
        "threshold": float(threshold),
        "candidate_threshold": float(candidate_threshold),
        "texture_strength": strength if options.method == "texture" and manual_mask is None else None,
        "texture_scale": options.texture_scale if options.method == "texture" and manual_mask is None else None,
        "isolated_regions_removed": isolated_removed,
        "low_contrast": low_contrast,
        "stroke_radius": radius,
        "min_area": min_area,
        "candidate_pixels": candidate_pixels,
        "foreground_pixels": foreground_pixels,
        "removed_pixels": removed_pixels,
        "added_pixels": added_pixels,
        "removed_fraction": round(removed_fraction, 4),
        "components": components,
        "method": "manual_mask" if manual_mask is not None else options.method,
        "manual_mask": str(manual_mask) if manual_mask is not None else None,
        "needs_visual_review": True,
        "review_reasons": reasons,
    }
    return Result(original, candidate, mask, metrics)
