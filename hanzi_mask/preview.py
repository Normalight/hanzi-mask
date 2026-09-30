from PIL import Image, ImageDraw, ImageOps
import numpy as np

from .processing import Result


def comparison_tile(name: str, result: Result) -> Image.Image:
    """Red=removed candidate; cyan=foreground added by smoothing or a mask."""
    overlay = np.asarray(result.original).copy()
    removed = (result.candidate > 0) & (result.mask == 0)
    added = (result.candidate == 0) & (result.mask > 0)
    overlay[removed] = (0.3 * overlay[removed] + 0.7 * np.array([255, 45, 45])).astype(
        np.uint8
    )
    overlay[added] = (0.3 * overlay[added] + 0.7 * np.array([20, 220, 255])).astype(
        np.uint8
    )
    canvas = Image.new("RGB", (660, 250), "#eeeeee")
    draw = ImageDraw.Draw(canvas)
    draw.text((10, 8), name, fill="black")
    images = [result.original, Image.fromarray(result.mask), Image.fromarray(overlay)]
    for index, (label, source) in enumerate(zip(["SOURCE", "MASK", "CHANGES"], images)):
        tile = ImageOps.contain(source, (210, 205), Image.Resampling.LANCZOS)
        if index == 1:
            tile = ImageOps.contain(source, (210, 205), Image.Resampling.NEAREST)
        x = index * 220
        draw.text((x + 10, 29), label, fill="black")
        canvas.paste(tile, (x + (220 - tile.width) // 2, 45 + (205 - tile.height) // 2))
    return canvas


def preview_page(tiles: list[Image.Image]) -> Image.Image:
    columns = 2 if len(tiles) > 1 else 1
    rows = (len(tiles) + columns - 1) // columns
    page = Image.new("RGB", (660 * columns, 250 * rows), "#eeeeee")
    for index, tile in enumerate(tiles):
        page.paste(tile, (index % columns * 660, index // columns * 250))
    return page
