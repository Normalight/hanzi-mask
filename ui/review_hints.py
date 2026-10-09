"""Review guidance derived from measurements and available desktop actions."""

from collections.abc import Mapping
from math import isfinite


def _number(metrics: Mapping[str, object], key: str) -> float | None:
    value = metrics.get(key)
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if isfinite(number) else None


def review_hints(metrics: Mapping[str, object], *, can_undo: bool = False) -> list[str]:
    """Return severity-ordered hints without inferring semantic stroke accuracy.

    Missing measurements do not establish a problem. Undo guidance only applies
    to manual cleanup with an available history; regeneration uses the original
    image and the current strength setting.
    """
    hints = []
    pixels = _number(metrics, "foreground_pixels")
    components = _number(metrics, "components")
    removed_fraction = _number(metrics, "removed_fraction")
    width, height = _number(metrics, "width"), _number(metrics, "height")
    isolated_removed = _number(metrics, "isolated_regions_removed")
    recovered_pixels = _number(metrics, "stroke_recovery_final_surviving_added_pixels")
    tip_pixels = _number(metrics, "tip_added_pixels")
    manual = bool(metrics.get("manual_correction"))
    empty = pixels == 0
    heavy_removal = removed_fraction is not None and removed_fraction >= .55

    if empty:
        if manual and can_undo:
            hints.append('结果为空，默认不采用；可能误删字形主体，可点“撤销清理”检查。')
        elif manual:
            hints.append('结果为空，默认不采用；可点“处理当前图片”重新生成。')
        else:
            hints.append('结果为空，默认不采用；可尝试调低“强度”后“处理当前图片”复核。')

    if metrics.get("low_contrast"):
        hints.append('检测到的明暗差异很小，字形可能难以分离；请对照原图检查，无法确认时取消“采用”。')

    if heavy_removal and not empty:
        if manual and can_undo:
            hints.append('删除比例较高，可能误删细笔画；请对照原图检查，必要时点“撤销清理”。')
        elif manual:
            hints.append('删除比例较高，可能误删细笔画；可点“处理当前图片”重新生成后复核。')
        else:
            hints.append('删除比例较高，细笔画可能缺失；可尝试调低“强度”后“处理当前图片”复核。')

    if (pixels is not None and width is not None and height is not None
            and width > 0 and height > 0 and pixels / (width * height) >= .6):
        hints.append('字形区域占比偏高，可能与背景粘连；无法单独选中杂斑时，请取消“采用”。')

    if isolated_removed is not None and isolated_removed > 0:
        hints.append('已筛除远离主体的小区域，独立点画可能受影响；缺失时先取消“采用”，'
                     '可勾选“保留独立点画”后“处理当前图片”复核。')

    if (not manual and not empty
            and ((recovered_pixels is not None and recovered_pixels > 0)
                 or (tip_pixels is not None and tip_pixels > 0))):
        hints.append('本次补入部分笔画候选，请检查收笔、字内留白和背景粘连；无法单独清理时取消“采用”。')

    if components is not None and components >= 8 and not empty:
        hints.append('独立字形区域较多，请检查碎斑与断笔；确认多余的色块可选中后“清理选中色块”。')

    if metrics.get("foreground_signal") == "color" and not empty:
        hints.append('按颜色差异提取字形区域，请检查同色杂斑；确认多余后可选中并“清理选中色块”。')

    if manual and not empty and not heavy_removal:
        if can_undo:
            hints.append('已人工清理，请对照原图检查笔画；误删时可点“撤销清理”。')
        else:
            hints.append('已人工清理，请对照原图检查笔画；误删时可点“处理当前图片”重新生成。')

    return hints
