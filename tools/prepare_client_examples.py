"""Copy read-only rubbing crops and prepare texture outputs for desktop demos.

The expected images reproduce the current texture settings; they are candidate
glyphs for visual review, not manually verified segmentation labels.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import shutil
import sys

from PIL import Image


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from hanzi_mask.cli import SUPPORTED, natural_key  # noqa: E402
from hanzi_mask.preview import comparison_tile, preview_page  # noqa: E402
from hanzi_mask.processing import Options, process_image  # noqa: E402


# Paths are relative to a locally discovered grouped-character dataset, never a
# particular user's home directory. Missing crops are skipped on other machines.
GROUPED_EXAMPLES = (
    ("安", "10-23e8_10-23e8_crop_109_000645_5015e0ab0e.png", "小尺寸拓片"),
    ("安", "17-44ad_17-44ad_crop_43_006720_aeb6589ce3.png", "粗纹理与大尺寸"),
    ("安", "4-c265_4-c265_crop_90_010365_a2492ba8a0.png", "细笔画与留白"),
    ("天", "59-375c_59-375c_crop_41_017364_6dddb9b352.png", "疏朗笔画"),
    ("國", "1_55-9f82_crop_91_000211_db8f44dfe0.png", "围合结构"),
    ("德", "59-375c_59-375c_crop_93_017421_2647879aa8.png", "密集结构"),
    ("水", "69-fe23_69-fe23_crop_108_019172_1ba233daef.png", "背景碎斑"),
    ("愛", "37-f499_37-f499_crop_20_009859_234d39374b.png", "密集结构与侵蚀"),
)
RUBBING_EXAMPLES = (
    ("育", "10-1dbe_crop_103.png", "石面细纹"),
    ("時", "10-1dbe_crop_104.png", "粗笔画与杂斑"),
    ("之", "10-1dbe_crop_107.png", "曲线与回锋"),
    ("日", "10-1dbe_crop_111.png", "字内留白"),
)


@dataclass(frozen=True)
class Example:
    source: Path
    character: str
    note: str = "外部示例"


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def discover_default(workspace: Path) -> list[Example]:
    """Use curated local examples, with the original test folder as a fallback."""
    selected: list[Example] = []
    grouped_roots = sorted(
        (workspace / "output").glob("字形图片高置信归并-*/归类结果/按字"),
        reverse=True,
    )
    for character, name, note in GROUPED_EXAMPLES:
        path = next((root / character / name for root in grouped_roots
                     if (root / character / name).is_file()), None)
        if path is not None:
            selected.append(Example(path, character, note))
    rubbing = workspace / "10-1dbe"
    for character, name, note in RUBBING_EXAMPLES:
        path = rubbing / name
        if path.is_file():
            selected.append(Example(path, character, note))
    painted = workspace / '字形图片' / '1' / '55-9f82_crop_4.png'
    if painted.is_file():
        selected.append(Example(painted, '眞', '红色字与中性石面，颜色前景检测'))
    used = {item.source.resolve() for item in selected}
    if rubbing.is_dir():
        for path in sorted(rubbing.iterdir(), key=natural_key):
            if path.is_file() and path.suffix.lower() in SUPPORTED and path.resolve() not in used:
                selected.append(Example(path, "拓片", "本地拓片补充示例"))
    return selected


def discover_external(source: Path, count: int) -> list[Example]:
    """Sample across a directory's ordered crops, retaining glyph subfolders."""
    if not source.is_dir():
        raise ValueError("外部输入路径必须是图片文件夹")
    files = sorted((p for p in source.rglob("*")
                    if p.is_file() and p.suffix.lower() in SUPPORTED), key=natural_key)
    if len(files) > count:
        # Equally spaced indices cover both ends rather than taking only the first
        # glyph folder; round is deterministic and count <= len(files).
        indices = [round(i * (len(files) - 1) / (count - 1)) for i in range(count)] if count > 1 else [0]
        files = [files[i] for i in indices]
    return [Example(p, p.parent.name if p.parent != source else "拓片") for p in files]


def prepare(examples: list[Example], output: Path, options: Options) -> dict:
    """Build a new example folder without replacing sources or existing output."""
    if not examples:
        raise ValueError("未找到示例图片，请通过 --source 指定图片文件夹")
    output = output.expanduser().resolve()
    if output.exists() or output.is_symlink():
        raise ValueError("输出目录已存在，请指定新目录以保留原有示例")
    sources = [item.source.expanduser().resolve() for item in examples]
    if any(source.is_relative_to(output) for source in sources):
        raise ValueError("输出目录不能包含原始图片")
    targets = [Path(item.character) / item.source.name for item in examples]
    if any(not item.character or Path(item.character).name != item.character
           or item.character in {".", ".."} for item in examples):
        raise ValueError("字形目录名必须是单个安全文件夹名")
    names = [path.as_posix().casefold() for path in targets]
    png_names = [path.with_suffix(".png").as_posix().casefold() for path in targets]
    if len(set(names)) != len(names) or len(set(png_names)) != len(png_names):
        raise ValueError("示例文件名冲突，请重命名输入图片")
    # Verify sources before making the destination visible.
    hashes = [sha256(source) for source in sources]
    output.mkdir(parents=True)
    records, tiles = [], []
    for example, source, target, original_hash in zip(examples, sources, targets, hashes):
        copied = output / "input" / target
        expected = output / "expected" / target.with_suffix(".png")
        copied.parent.mkdir(parents=True, exist_ok=True)
        expected.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, copied)
        result = process_image(copied, options)
        Image.fromarray(result.mask).save(expected)
        records.append({
            "character": example.character,
            "note": example.note,
            "source": str(source),
            "input": copied.relative_to(output).as_posix(),
            "expected": expected.relative_to(output).as_posix(),
            "source_sha256": original_hash,
            "copied_sha256": sha256(copied),
            "expected_sha256": sha256(expected),
            **result.metrics,
        })
        # The core preview uses Pillow's bundled font. Numbered ASCII headings
        # remain legible on machines without Chinese fonts; the manifest carries
        # the matching Chinese character names and descriptions.
        title = f"Sample {len(records):02d} | {result.original.width} x {result.original.height}"
        tiles.append(comparison_tile(title, result))
    previews = []
    for start in range(0, len(tiles), 6):
        name = f"preview-{start // 6 + 1:02d}.jpg"
        preview_page(tiles[start:start + 6]).save(output / name, quality=94)
        previews.append(name)
    manifest = {
        "schema_version": 1,
        "method": "texture",
        "purpose": "客户端批量处理演示，expected 是当前参数输出，需视觉复核",
        "parameters": options.to_dict(),
        "input_directory": "input",
        "expected_directory": "expected",
        "count": len(records),
        "previews": previews,
        "images": records,
    }
    (output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="准备 texture 客户端示例，保留只读原图")
    parser.add_argument("--source", type=Path, help="外部图片目录；省略时寻找工作区本地拓片")
    parser.add_argument("-o", "--output", type=Path, default=PROJECT_ROOT / "output/client-examples",
                        help="新的输出目录，默认 output/client-examples")
    parser.add_argument("--count", type=int, default=13, help="示例数量，默认 13")
    args = parser.parse_args(argv)
    if not 1 <= args.count <= 48:
        parser.error("示例数量必须在 1–48 之间")
    try:
        examples = (discover_external(args.source.expanduser().resolve(), args.count)
                    if args.source else discover_default(PROJECT_ROOT.parent)[:args.count])
        manifest = prepare(examples, args.output, Options(method="texture"))
    except (OSError, ValueError, Image.DecompressionBombError) as error:
        parser.error(str(error))
    print(f"已准备 {manifest['count']} 张 texture 示例：{args.output.expanduser().resolve()}")
    print("原图保留在 input，候选黑底白字位于 expected；请结合 preview 复核。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
