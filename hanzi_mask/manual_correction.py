"""Persist a user's binary-mask correction without changing earlier results.

Each correction is an immutable cache revision. The desktop can undo by keeping
an earlier ProcessedImage; ordinary export and backup replacement APIs accept
both the original and corrected records without special handling.
"""

from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import shutil
import tempfile

import cv2
import numpy as np
from PIL import Image

from .batch_service import ProcessedImage, load_result


def _digest(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def apply_correction(item: ProcessedImage, mask: np.ndarray,
                     cache_dir: Path | str) -> ProcessedImage:
    """Save one binary mask revision after verifying source bytes and dimensions.

    mask must be uint8, original-size, and contain only 0/255. No texture solver
    runs here. Existing results, caller arrays, candidate masks and source images
    remain unchanged. A new record is returned only after the cache is complete.
    """
    if not isinstance(mask, np.ndarray) or mask.ndim != 2 or mask.dtype != np.uint8:
        raise ValueError("人工矫正掩膜必须是二维 uint8 图")
    if not mask.size or not np.isin(mask, [0, 255]).all():
        raise ValueError("人工矫正掩膜只能包含 0 和 255")
    corrected = mask.copy()
    result = load_result(item, item.options.foreground if item.options is not None else "light")
    expected = (result.original.height, result.original.width)
    if corrected.shape != expected:
        raise ValueError(f"人工矫正掩膜尺寸必须与原图一致：{expected}")
    previous = np.asarray(result.mask)
    candidate = np.asarray(result.candidate)
    if previous.shape != expected or candidate.shape != expected:
        raise ValueError("原处理结果尺寸不正确，请重新处理图片")

    metrics = deepcopy(item.metrics or result.metrics)
    if item.options is not None:
        metrics["parameters"] = item.options.to_dict()
    pixels = int(np.count_nonzero(corrected))
    candidate_pixels = int(np.count_nonzero(candidate))
    removed = int(np.count_nonzero((candidate > 0) & (corrected == 0)))
    added = int(np.count_nonzero((candidate == 0) & (corrected > 0)))
    correction_removed = int(np.count_nonzero((previous > 0) & (corrected == 0)))
    correction_added = int(np.count_nonzero((previous == 0) & (corrected > 0)))
    components = int(cv2.connectedComponents(corrected, connectivity=8)[0] - 1)
    removed_fraction = removed / candidate_pixels if candidate_pixels else 0.0
    step = int(metrics.get("correction_steps", 0)) + 1
    history = list(metrics.get("correction_history", []))
    history.append({"step": step, "removed_pixels": correction_removed,
                    "added_pixels": correction_added, "foreground_pixels": pixels})
    metrics.update(
        width=result.original.width, height=result.original.height,
        candidate_pixels=candidate_pixels, foreground_pixels=pixels,
        removed_pixels=removed, added_pixels=added,
        removed_fraction=round(removed_fraction, 4), components=components,
        manual_correction=True, correction_steps=step,
        correction_removed_pixels=correction_removed,
        correction_added_pixels=correction_added,
        correction_total_removed_pixels=int(metrics.get("correction_total_removed_pixels", 0)) + correction_removed,
        correction_total_added_pixels=int(metrics.get("correction_total_added_pixels", 0)) + correction_added,
        correction_history=history, needs_visual_review=True,
    )
    dynamic_reasons = ("结果没有白色区域", "存在较多独立区域", "删除比例较高", "白色区域占比较高",
                       "已人工矫正候选字形")
    reasons = [reason for reason in metrics.get("review_reasons", [])
               if not reason.startswith(dynamic_reasons)]
    if not pixels:
        reasons.append("结果没有白色区域，请检查是否误删了文字主体")
    if components >= 8:
        reasons.append("存在较多独立区域，请检查残留碎斑和断裂笔画")
    if removed_fraction >= 0.55:
        reasons.append("删除比例较高，请复核人工清理是否误删细笔画")
    if pixels / corrected.size >= 0.6:
        reasons.append("白色区域占比较高，请检查背景粘连")
    reasons.append("已人工矫正候选字形，请复核笔画与清理区域")
    metrics["review_reasons"] = reasons

    cache = Path(cache_dir).expanduser().resolve()
    if item.source.path.is_relative_to(cache):
        raise ValueError("矫正缓存目录不能是原图所在目录或其上级目录")
    cache.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix="correction-", dir=cache))
    try:
        mask_path = directory / "mask.png"
        Image.fromarray(corrected).save(mask_path, format="PNG")
        candidate_path = item.candidate_path
        if item.result is not None or candidate_path is None:
            candidate_path = directory / "candidate.png"
            Image.fromarray(candidate).save(candidate_path, format="PNG")
        (directory / "metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2),
                                                encoding="utf-8")
        if _digest(item.source.path) != item.source_sha256:
            raise ValueError("人工矫正过程中原图已变化，请重新处理图片")
    except Exception:
        shutil.rmtree(directory)
        raise
    return ProcessedImage(item.source, None, item.source_sha256,
                          mask_path, candidate_path, metrics, options=item.options)
