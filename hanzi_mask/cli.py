import argparse
from collections import Counter
from datetime import datetime
from io import BytesIO
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time

from PIL import Image

from . import __version__
from .preview import comparison_tile, preview_page
from .processing import Options, PROFILES, process_image


SUPPORTED = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp"}


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(
        description="提取拓片单字候选掩膜，批量导出黑底白字 PNG。结果需要视觉复核。"
    )
    command.add_argument("input", type=Path, help="输入图片或文件夹")
    command.add_argument("-o", "--output", required=True, type=Path, help="输出文件夹")
    command.add_argument("--recursive", action="store_true", help="扫描子文件夹，保留相对路径")
    command.add_argument("--profile", choices=PROFILES, default="balanced", help="去噪强度")
    command.add_argument("--method", choices=["texture", "morphology"], default="texture", help="texture 纹理与结构分离（默认）；morphology 使用原有几何筛选")
    command.add_argument("--texture-strength", type=float, help="纹理平滑强度 0–0.05，默认按 profile 选择；仅 texture 使用")
    command.add_argument("--texture-scale", type=float, default=3.0, help="纹理尺度 0.5–12 像素，默认 3；仅 texture 使用")
    command.add_argument("--keep-isolated", action="store_true", help="保留通过面积筛选的远离主体的小区域，适用于独立点画")
    command.add_argument("--foreground", choices=["light", "dark"], default="light", help="原图字的明暗；拓片默认 light")
    command.add_argument("--threshold", type=int, help="处理后图像的分界值 0–255，默认自动估计；dark 时按反相后的亮度计算")
    command.add_argument("--stroke-radius", type=int, help="筛选笔画的圆盘半径，单位像素；0 关闭宽度筛选")
    command.add_argument("--min-area", type=int, help="保留连通区域的最小像素面积；0 关闭面积筛选")
    command.add_argument("--median-size", type=int, choices=[1, 3, 5, 7], default=3, help="中值滤波尺寸；1 关闭")
    command.add_argument("--restore-steps", type=int, default=0, help="受处理后候选限制的边缘恢复步数，0–10；可能恢复粘连碎斑")
    command.add_argument("--mask-dir", type=Path, help="人工掩膜目录：同名 PNG、同尺寸、白字黑底；有掩膜的图片直接采用掩膜")
    command.add_argument("--preview", action="store_true", help="生成原图、结果、删除/增加区域对照图")
    command.add_argument("--limit", type=int, help="只处理自然排序后的前 N 张图片")
    command.add_argument("--overwrite", action="store_true", help="允许替换输出目录中的既有结果")
    command.add_argument("--version", action="version", version=__version__)
    return command


def natural_key(path: Path):
    return [(1, int(part)) if part.isdigit() else (0, part.casefold())
            for part in re.split(r"(\d+)", path.as_posix())]


def discover_images(source: Path, output: Path, recursive: bool, mask_dir: Path | None) -> list[Path]:
    if source.is_file():
        if source.suffix.lower() not in SUPPORTED:
            raise ValueError("不支持此文件类型，请使用 PNG、JPEG、BMP、TIFF 或 WebP")
        return [source]
    candidates = source.rglob("*") if recursive else source.iterdir()
    files = []
    for path in candidates:
        if not path.is_file() or path.suffix.lower() not in SUPPORTED:
            continue
        resolved = path.resolve()
        if resolved.is_relative_to(output):
            continue
        if mask_dir is not None and resolved.is_relative_to(mask_dir):
            continue
        files.append(path)
    return sorted(files, key=natural_key)


def image_bytes(image: Image.Image, format: str) -> bytes:
    buffer = BytesIO()
    image.save(buffer, format=format)
    return buffer.getvalue()


def write_output(path: Path, data: bytes, overwrite: bool = False) -> None:
    """Write completely before publishing; default publication is exclusive."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".hanzi-mask-", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
        if overwrite:
            os.replace(temporary, path)
        else:
            # Hard-link publication fails if another run already created the path.
            os.link(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def run(args: argparse.Namespace, command: argparse.ArgumentParser) -> int:
    source = args.input.expanduser().resolve()
    output = args.output.expanduser().resolve()
    mask_dir = args.mask_dir.expanduser().resolve() if args.mask_dir else None
    if not source.exists() or not (source.is_dir() or source.is_file()):
        command.error("输入路径不是文件或文件夹")
    if output.exists() and not output.is_dir():
        command.error("输出路径必须是文件夹")
    source_root = source if source.is_dir() else source.parent
    if source.is_relative_to(output):
        command.error("输出目录不能与输入目录相同，也不能是输入路径的上级目录")
    if mask_dir is not None and not mask_dir.is_dir():
        command.error("人工掩膜目录不存在")
    if mask_dir is not None and (source.is_relative_to(mask_dir) or mask_dir.is_relative_to(output) or output.is_relative_to(mask_dir)):
        command.error("人工掩膜目录应独立于原图目录和输出目录")
    if args.limit is not None and args.limit <= 0:
        command.error("处理数量必须大于零")
    try:
        options = Options(
            profile=args.profile,
            foreground=args.foreground,
            threshold=args.threshold,
            stroke_radius=args.stroke_radius,
            min_area=args.min_area,
            median_size=args.median_size,
            restore_steps=args.restore_steps,
            method=args.method,
            texture_strength=args.texture_strength,
            texture_scale=args.texture_scale,
            keep_isolated=args.keep_isolated,
        )
        files = discover_images(source, output, args.recursive, mask_dir)
    except (OSError, ValueError) as error:
        command.error(str(error))
    if args.limit is not None:
        files = files[:args.limit]
    if not files:
        command.error("没有找到支持的图片")

    targets = [path.relative_to(source_root).with_suffix(".png") for path in files]
    counts = Counter(path.as_posix().casefold() for path in targets)
    conflicts = [name for name, count in counts.items() if count > 1]
    if conflicts:
        command.error("转成 PNG 后文件名冲突，请重命名源文件：" + ", ".join(conflicts))

    output.mkdir(parents=True, exist_ok=True)
    run_id = str(time.time_ns())
    records = []
    tiles = []
    pages = []

    def flush_preview():
        if not tiles:
            return
        name = f"preview-{run_id}-{len(pages) + 1:02d}.jpg"
        write_output(output / name, image_bytes(preview_page(tiles), "JPEG"))
        pages.append(name)
        tiles.clear()

    for index, (path, relative) in enumerate(zip(files, targets), start=1):
        target = output / relative
        record = {"source": path.relative_to(source_root).as_posix(), "output": relative.as_posix()}
        # Reject an existing directory symlink that would redirect a write.
        if not target.parent.resolve().is_relative_to(output):
            record.update(status="error", error="输出子目录指向输出目录之外")
            print(f"[{index}/{len(files)}] 失败 {record['source']}：{record['error']}", file=sys.stderr)
            records.append(record)
            continue
        if (target.exists() or target.is_symlink()) and not args.overwrite:
            record.update(status="skipped", reason="输出文件已存在")
            records.append(record)
            print(f"[{index}/{len(files)}] 跳过 {record['source']}（输出已存在）")
            continue
        manual_mask = mask_dir / relative if mask_dir else None
        if manual_mask is not None and not manual_mask.exists():
            manual_mask = None
        try:
            result = process_image(path, options, manual_mask)
            write_output(target, image_bytes(Image.fromarray(result.mask), "PNG"), args.overwrite)
            record.update(status="converted", **result.metrics)
        except (OSError, ValueError, Image.DecompressionBombError) as error:
            record.update(status="error", error=str(error))
            print(f"[{index}/{len(files)}] 失败 {record['source']}：{error}", file=sys.stderr)
        else:
            print(f"[{index}/{len(files)}] 完成 {record['source']} · 白色区域 {result.metrics['components']} 个")
            if args.preview:
                tiles.append(comparison_tile(record["source"], result))
                if len(tiles) == 12:
                    flush_preview()
        records.append(record)
    if args.preview:
        flush_preview()
    summary = {key: sum(item["status"] == key for item in records)
               for key in ("converted", "skipped", "error")}
    report = {
        "schema_version": 1,
        "tool_version": __version__,
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source": str(source),
        "output": str(output),
        "parameters": options.to_dict(),
        "mask_dir": str(mask_dir) if mask_dir else None,
        "summary": summary,
        "preview_legend": {"red": "相对原始明暗候选删除的区域", "cyan": "纹理平滑或人工掩膜增加的区域"},
        "previews": pages,
        "images": records,
    }
    report_path = output / f"report-{run_id}.json"
    payload = json.dumps(report, ensure_ascii=False, indent=2).encode("utf-8")
    # Windows runners can repeat a coarse time_ns() tick between rapid runs;
    # exclusive publication then collides. Retry with a fresh unique name.
    for attempt in range(100):
        try:
            write_output(report_path, payload)
            break
        except FileExistsError:
            report_path = output / f"report-{time.time_ns()}-{attempt + 1}.json"
    else:
        raise RuntimeError("无法为运行报告生成唯一文件名")
    print(f"\n完成 {summary['converted']}，跳过 {summary['skipped']}，失败 {summary['error']}")
    print(f"报告：{report_path}")
    print("结果是候选字形，请结合对照图复核笔画与残留斑点。")
    return 1 if summary["error"] else 0


def main(argv: list[str] | None = None) -> int:
    command = parser()
    args = command.parse_args(argv)
    try:
        return run(args, command)
    except OSError as error:
        print(f"文件操作失败：{error}", file=sys.stderr)
        return 1
