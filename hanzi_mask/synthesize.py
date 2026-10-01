"""Export repeatable procedural image/mask pairs from training entries only."""

import argparse
import json
from pathlib import Path

import numpy as np
from PIL import Image

from .cli import image_bytes, write_output
from .docentr import preview_tile
from .supervised_data import GlyphPairs, read_manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description="从训练字形生成复杂拓片纹理及严格对齐的掩膜")
    parser.add_argument("manifest", type=Path)
    parser.add_argument("-o", "--output", required=True, type=Path)
    parser.add_argument("--count", type=int, default=1000)
    parser.add_argument("--size", type=int, default=256)
    parser.add_argument("--seed", type=int, default=123)
    args = parser.parse_args(argv)
    if args.count < 1 or not 64 <= args.size <= 512:
        parser.error("数量须大于零，size 须为 64–512")
    output = args.output.expanduser().resolve()
    if output.exists() or output.is_symlink():
        parser.error("请指定新的输出目录")
    try:
        manifest = read_manifest(args.manifest)
    except (OSError, ValueError, KeyError) as error:
        parser.error(str(error))
    pairs = GlyphPairs(manifest["train"], args.size, "complex")
    output.mkdir(parents=True, exist_ok=False)
    rng = np.random.default_rng(args.seed)
    rows = []
    for i in range(args.count):
        image, soft_mask = pairs.sample(rng)
        values = {"images": image[0], "soft-masks": soft_mask[0], "masks": (soft_mask[0] > .5).astype(np.float32)}
        for folder, array in values.items():
            write_output(output / folder / f"{i:05d}.png",
                         image_bytes(Image.fromarray(np.rint(array * 255).astype(np.uint8)), "PNG"))
        if i < 6:
            rows.append(preview_tile(f"pair {i}", [
                ("COMPLEX TEXTURE", Image.fromarray(np.rint(image[0] * 255).astype(np.uint8))),
                ("CLEAN MASK", Image.fromarray((soft_mask[0] > .5).astype(np.uint8) * 255))]))
    page = Image.new("RGB", (440, len(rows) * 250))
    for i, row in enumerate(rows):
        page.paste(row, (0, i * 250))
    write_output(output / "preview.png", image_bytes(page, "PNG"))
    report = {"count": args.count, "size": args.size, "seed": args.seed, "augmentation": "complex",
              "sources": manifest["train"], "test_images_used": False,
              "note": "纹理叠加不改变目标字形；形变同时作用于图像和掩膜，soft-masks 保留边缘覆盖度"}
    write_output(output / "dataset.json", json.dumps(report, ensure_ascii=False, indent=2).encode())
    print(f"已生成 {args.count} 对图像/掩膜：{output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
