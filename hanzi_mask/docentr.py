"""Optional DocEnTr Base/8 inference using a separately downloaded upstream repo.

Model and preprocessing reference: https://github.com/dali92002/DocEnTR
The upstream code is CC BY-NC 4.0; it is not bundled with this adapter.
"""

import argparse
from collections import Counter
from datetime import datetime
import hashlib
import importlib.util
import json
from pathlib import Path
import pickle
import sys
import time

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageOps

from .cli import discover_images, image_bytes, write_output
from .processing import load_image


SIZE = 256
MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


class DocEnTr:
    def __init__(self, repo: Path, weights: Path, device: str = "auto"):
        # Lazy imports keep the core CLI and geometry tests lightweight.
        import torch
        from vit_pytorch import ViT

        self.torch = torch
        if device == "auto":
            device = "cuda" if torch.cuda.is_available() else (
                "mps" if torch.backends.mps.is_available() else "cpu"
            )
        self.device = device
        if device == "cpu":
            torch.set_num_threads(min(4, torch.get_num_threads()))
        model_file = repo / "models" / "binae.py"
        spec = importlib.util.spec_from_file_location("docentr_upstream_binae", model_file)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        encoder = ViT(image_size=SIZE, patch_size=8, num_classes=1000,
                      dim=768, depth=6, heads=8, mlp_dim=2048)
        self.model = module.BinModel(encoder=encoder, decoder_dim=768,
                                     decoder_depth=6, decoder_heads=8)
        state = torch.load(weights, map_location="cpu", weights_only=True)
        self.model.load_state_dict(state, strict=True)
        self.model.eval().requires_grad_(False).to(device)
        self.metadata = {
            "model": "DocEnTr-Base / patch 8",
            "upstream": "https://github.com/dali92002/DocEnTR",
            "license": "CC BY-NC 4.0",
            "weights": str(weights), "weights_sha256": sha256(weights),
            "model_source_sha256": sha256(model_file),
            "device": device, "torch": torch.__version__,
        }

    def __call__(self, patch: np.ndarray) -> np.ndarray:
        """Input/output: 256×256 BGR floats, white=document background."""
        torch = self.torch
        normalized = ((patch - MEAN) / STD).transpose(2, 0, 1).copy()
        tensor = torch.from_numpy(normalized).unsqueeze(0).to(self.device)
        with torch.inference_mode():
            # Upstream requires a target to calculate an unused loss.
            _, _, prediction = self.model(tensor, tensor)
            values = prediction[0].cpu().numpy()
        # Inverse of upstream's (h w) (p1 p2 c) patch order.
        bgr = values.reshape(32, 32, 8, 8, 3).transpose(0, 2, 1, 3, 4)
        bgr = bgr.reshape(SIZE, SIZE, 3)
        return np.clip(bgr * STD + MEAN, 0, 1)


def infer_scores(original: Image.Image, predict, mode: str, foreground: str, fit_size: int = SIZE) -> np.ndarray:
    """Return uncalibrated stroke scores at original resolution, without cleanup."""
    if mode not in {"tile", "fit"} or foreground not in {"light", "dark"}:
        raise ValueError("invalid input mode or foreground")
    if not 16 <= fit_size <= SIZE:
        raise ValueError("fit_size must be between 16 and 256")
    # Official checkpoints expect OpenCV BGR, with RGB normalization constants.
    bgr = np.asarray(original.convert("RGB"))[:, :, ::-1].astype(np.float32) / 255
    if foreground == "light":
        bgr = 1 - bgr
    height, width = bgr.shape[:2]
    if mode == "fit":
        scale = fit_size / max(height, width)
        rw, rh = max(1, round(width * scale)), max(1, round(height * scale))
        resized = cv2.resize(bgr, (rw, rh), interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR)
        patch = np.ones((SIZE, SIZE, 3), dtype=np.float32)
        patch[:rh, :rw] = resized
        restored = predict(patch)[:rh, :rw]
        restored = cv2.resize(restored, (width, height), interpolation=cv2.INTER_LINEAR)
    else:
        restored = np.empty_like(bgr)
        for y in range(0, height, SIZE):
            for x in range(0, width, SIZE):
                rh, rw = min(SIZE, height - y), min(SIZE, width - x)
                patch = np.ones((SIZE, SIZE, 3), dtype=np.float32)
                patch[:rh, :rw] = bgr[y:y + rh, x:x + rw]
                restored[y:y + rh, x:x + rw] = predict(patch)[:rh, :rw]
    scores = 1 - cv2.cvtColor(np.asarray(restored, dtype=np.float32), cv2.COLOR_BGR2GRAY)
    if not np.isfinite(scores).all():
        raise ValueError("模型输出包含非有限数值")
    return np.clip(scores, 0, 1)


def preview_tile(name: str, images: list[tuple[str, Image.Image]]) -> Image.Image:
    tile = Image.new("RGB", (220 * len(images), 250), "#202020")
    draw = ImageDraw.Draw(tile)
    for i, (label, image) in enumerate(images):
        x = i * 220
        draw.text((x + 8, 6), label, fill="white")
        thumbnail = ImageOps.contain(image.convert("RGB"), (212, 204), Image.Resampling.NEAREST)
        tile.paste(thumbnail, (x + (220 - thumbnail.width) // 2, 28 + (204 - thumbnail.height) // 2))
    draw.text((8, 234), name, fill="white")
    return tile


def main(argv: list[str] | None = None) -> int:
    command = argparse.ArgumentParser(description="DocEnTr 预训练实验：原尺寸切块 / 整字缩放，黑底白字导出。")
    command.add_argument("input", type=Path)
    command.add_argument("-o", "--output", type=Path, required=True)
    command.add_argument("--repo", type=Path, default=Path("output/research/DocEnTR"))
    command.add_argument("--weights", type=Path, default=Path("models/docentr-base-2018.pt"))
    command.add_argument("--device", choices=["auto", "cpu", "mps", "cuda"], default="auto")
    command.add_argument("--mode", choices=["both", "tile", "fit"], default="both")
    command.add_argument("--foreground", choices=["light", "dark"], default="light")
    command.add_argument("--fit-size", type=int, default=SIZE, help="fit 模式整字最长边，16–256，周围补白至 256")
    command.add_argument("--threshold", type=float, default=0.5, help="前景分数阈值，0–1；分数不是校准概率")
    command.add_argument("--baseline", type=Path, help="同名传统算法结果目录，用于对照")
    command.add_argument("--limit", type=int)
    args = command.parse_args(argv)
    source, output = args.input.expanduser().resolve(), args.output.expanduser().resolve()
    repo, weights = args.repo.expanduser().resolve(), args.weights.expanduser().resolve()
    if not source.exists() or not (source.is_file() or source.is_dir()):
        command.error("输入路径不存在")
    if output.exists() or output.is_symlink():
        command.error("实验输出目录已存在，请指定新目录")
    if source.is_relative_to(output) or weights.is_relative_to(output) or repo.is_relative_to(output):
        command.error("输出目录不能包含输入或模型资源")
    if not 0 < args.threshold < 1 or (args.limit is not None and args.limit <= 0):
        command.error("阈值必须在 0–1 之间，数量必须大于零")
    if not 16 <= args.fit_size <= SIZE:
        command.error("整字缩放尺寸必须在 16–256 之间")
    if not weights.is_file() or not (repo / "models" / "binae.py").is_file():
        command.error("请先按 docs/DOCENTR.md 下载官方源码和 Base/8/2018 权重")
    try:
        files = discover_images(source, output, False, None)
    except (OSError, ValueError) as error:
        command.error(str(error))
    if args.limit:
        files = files[:args.limit]
    if not files:
        command.error("没有找到图片")
    names = [p.with_suffix(".png").name for p in files]
    if any(count > 1 for count in Counter(n.casefold() for n in names).values()):
        command.error("转换为 PNG 后文件名冲突")
    try:
        model = DocEnTr(repo, weights, args.device)
    except (ImportError, OSError, RuntimeError, ValueError, pickle.UnpicklingError) as error:
        print(f"模型加载失败：{error}", file=sys.stderr)
        return 1
    print(f"已加载 DocEnTr Base，推理设备：{model.device}", flush=True)
    output.mkdir(parents=True, exist_ok=False)
    modes = ["tile", "fit"] if args.mode == "both" else [args.mode]
    records, tiles, pages = [], [], []
    started = time.perf_counter()

    def flush():
        if not tiles:
            return
        page = Image.new("RGB", (tiles[0].width, 250 * len(tiles)), "#202020")
        for i, tile in enumerate(tiles):
            page.paste(tile, (0, 250 * i))
        name = f"preview-{len(pages) + 1:02d}.jpg"
        write_output(output / name, image_bytes(page, "JPEG"))
        pages.append(name)
        tiles.clear()

    for i, (path, name) in enumerate(zip(files, names), 1):
        record = {"source": str(path), "status": "converted", "modes": {}}
        try:
            original = load_image(path, args.foreground)
            images = [("SOURCE", original)]
            if args.baseline:
                baseline = args.baseline / name
                with Image.open(baseline) as image:
                    images.append(("TEXTURE", image.copy()))
            for mode in modes:
                tick = time.perf_counter()
                scores = infer_scores(original, model, mode, args.foreground, args.fit_size)
                mask = np.where(scores > args.threshold, 255, 0).astype(np.uint8)
                write_output(output / mode / name, image_bytes(Image.fromarray(mask), "PNG"))
                score_image = Image.fromarray(np.rint(scores * 255).astype(np.uint8))
                write_output(output / (mode + "-scores") / name, image_bytes(score_image, "PNG"))
                record["modes"][mode] = {"seconds": round(time.perf_counter() - tick, 3),
                                         "foreground_fraction": round(float(np.mean(mask > 0)), 5)}
                images.append(("DOCENTR " + mode.upper(), Image.fromarray(mask)))
            record.update(width=original.width, height=original.height, source_sha256=sha256(path))
            tiles.append(preview_tile(name, images))
            if len(tiles) == 6:
                flush()
            print(f"[{i}/{len(files)}] {name} {record['modes']}", flush=True)
        except (OSError, ValueError, RuntimeError, Image.DecompressionBombError) as error:
            record.update(status="error", error=str(error))
            print(f"[{i}/{len(files)}] 失败 {name}：{error}", file=sys.stderr, flush=True)
        records.append(record)
    flush()
    report = {"created_at": datetime.now().astimezone().isoformat(),
              "model": model.metadata, "input": str(source),
              "foreground": args.foreground, "threshold": args.threshold, "fit_size": args.fit_size,
              "modes": modes, "postprocessing": "none", "needs_visual_review": True,
              "score_note": "反归一化重建图的反相灰度，未经校准，不是字形正确率",
              "elapsed_seconds": round(time.perf_counter() - started, 2),
              "summary": dict(Counter(r["status"] for r in records)),
              "previews": pages, "images": records}
    write_output(output / "report.json", json.dumps(report, ensure_ascii=False, indent=2).encode())
    print(f"结果与对照：{output}", flush=True)
    return 1 if any(r["status"] == "error" for r in records) else 0


if __name__ == "__main__":
    raise SystemExit(main())
