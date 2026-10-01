"""Train a small glyph-specific segmentation experiment from explicit mask pairs."""

import argparse
import json
from pathlib import Path
import random
import time

import cv2
import numpy as np
from PIL import Image
import torch

from .cli import image_bytes, write_output
from .docentr import preview_tile
from .glyph_unet import GlyphUNet, segmentation_loss
from .processing import Options, load_image, process_image
from .supervised_data import GlyphPairs, check_checkpoint_sources, digest, letterbox, normalize, read_manifest


def predict(model, image, size, device):
    array = normalize(np.asarray(image.convert("L")))
    padded, (x, y, w, h) = letterbox(array, size)
    with torch.inference_mode():
        scores = model(torch.from_numpy(padded[None, None]).to(device)).sigmoid()[0, 0].cpu().numpy()
    return cv2.resize(scores[y:y+h, x:x+w], image.size, interpolation=cv2.INTER_LINEAR)


def validate(model, batches, device):
    model.eval()
    intersections = predicted = actual = 0
    with torch.inference_mode():
        for images, masks in batches:
            p = model(torch.from_numpy(images).to(device)).sigmoid().cpu().numpy() > .5
            gt = masks > .5
            intersections += int(np.count_nonzero(p & gt))
            predicted += int(np.count_nonzero(p))
            actual += int(np.count_nonzero(gt))
    return 2 * intersections / max(1, predicted + actual)


def export(model, manifest, output, size, device):
    rows, records, pages = [], [], []

    def flush():
        if not rows:
            return
        page = Image.new("RGB", (rows[0].width, 250 * len(rows)))
        for i, row in enumerate(rows):
            page.paste(row, (0, 250 * i))
        name = f"preview-{len(pages) + 1:02d}.png"
        write_output(output / name, image_bytes(page, "PNG"))
        pages.append(name)
        rows.clear()

    for split in ("train", "test"):
        for item in manifest[split]:
            path = Path(item["image"])
            original = load_image(path, "light")
            scores = predict(model, original, size, device)
            mask = (scores > .5).astype(np.uint8) * 255
            name = path.with_suffix(".png").name
            write_output(output / split / name, image_bytes(Image.fromarray(mask), "PNG"))
            write_output(output / (split + "-scores") / name,
                         image_bytes(Image.fromarray(np.rint(scores * 255).astype(np.uint8)), "PNG"))
            texture = process_image(path, Options()).mask
            _, otsu = cv2.threshold(np.asarray(original.convert("L")), 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
            images = [("SOURCE " + split.upper(), original), ("OTSU", Image.fromarray(otsu)),
                      ("TEXTURE", Image.fromarray(texture)), ("TRAINED U-NET", Image.fromarray(mask))]
            rows.append(preview_tile(path.name.split("_")[0], images))
            if len(rows) == 6:
                flush()
            records.append({"image": str(path), "split": split, "foreground_fraction": float(np.mean(mask > 0)),
                            "size": list(original.size), "source_unchanged": digest(path) == item["sha256"]})
        flush()
    return records, pages


def main(argv=None):
    parser = argparse.ArgumentParser(description="用干净字形掩膜训练小型 U-Net，独立复杂图仅作推理。")
    parser.add_argument("manifest", type=Path)
    parser.add_argument("-o", "--output", required=True, type=Path)
    parser.add_argument("--steps", type=int, default=1000)
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--size", type=int, default=192)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--device", choices=["auto", "cpu", "mps", "cuda"], default="auto")
    parser.add_argument("--checkpoint", type=Path, help="只加载已训练模型并导出，不继续训练")
    args = parser.parse_args(argv)
    if args.size < 64 or args.size > 512 or args.size % 16 or args.steps < 1 or args.batch_size < 1:
        parser.error("size 须为 64–512 的 16 倍数，steps 和 batch-size 须大于零")
    try:
        manifest = read_manifest(args.manifest)
    except (OSError, ValueError, KeyError) as error:
        parser.error(str(error))
    output = args.output.expanduser().resolve()
    if output.exists() or output.is_symlink():
        parser.error("请指定不存在的新输出目录")
    for split in ("train", "test"):
        for item in manifest[split]:
            if Path(item["image"]).is_relative_to(output):
                parser.error("输出目录不能包含原图")
    device = args.device
    if device == "auto":
        device = "cuda" if torch.cuda.is_available() else ("mps" if torch.backends.mps.is_available() else "cpu")
    torch.manual_seed(args.seed)
    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.set_num_threads(4)
    cv2.setNumThreads(1)
    model = GlyphUNet().to(device)
    if args.checkpoint:
        state = torch.load(args.checkpoint, map_location="cpu", weights_only=True)
        try:
            check_checkpoint_sources(state["training_sources"], manifest)
        except (KeyError, ValueError) as error:
            parser.error(str(error))
    output.mkdir(parents=True, exist_ok=False)
    started = time.perf_counter()
    history = []
    best_dice = -1
    best_step = 0
    checkpoint = output / "best.pt"
    if args.checkpoint:
        args.size = state["size"]
        model.load_state_dict(state["model"], strict=True)
        best_dice, best_step = state["synthetic_validation_dice"], state["step"]
    else:
        # Only training entries are passed here. Synthetic validation uses fresh
        # noise seeds on the SAME clean glyphs, not an independent real dataset.
        pairs = GlyphPairs(manifest["train"], args.size)
        rng, val_rng = np.random.default_rng(args.seed), np.random.default_rng(args.seed + 100000)
        batches = [pairs.batch(val_rng, args.batch_size) for _ in range(4)]
        optimizer = torch.optim.AdamW(model.parameters(), lr=1e-3, weight_decay=1e-4)
        scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, args.steps, eta_min=1e-5)
        loss_total = 0
        print(f"训练 {len(manifest['train'])} 张，真实测试 {len(manifest['test'])} 张；设备 {device}", flush=True)
        # Persist examples so the synthetic training task is inspectable.
        examples = []
        for i in range(min(4, args.batch_size)):
            examples.append(preview_tile(f"synthetic validation {i}", [
                ("NOISY INPUT", Image.fromarray((batches[0][0][i, 0] * 255).astype(np.uint8))),
                ("TARGET", Image.fromarray((batches[0][1][i, 0] * 255).astype(np.uint8)))]))
        page = Image.new("RGB", (440, 1000))
        for i, row in enumerate(examples):
            page.paste(row, (0, i * 250))
        write_output(output / "synthetic-examples.png", image_bytes(page, "PNG"))
        for step in range(1, args.steps + 1):
            model.train()
            images, masks = pairs.batch(rng, args.batch_size)
            optimizer.zero_grad(set_to_none=True)
            loss = segmentation_loss(model(torch.from_numpy(images).to(device)), torch.from_numpy(masks).to(device))
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1)
            optimizer.step()
            scheduler.step()
            loss_total += float(loss.detach().cpu())
            if step % 100 == 0 or step == args.steps:
                dice = validate(model, batches, device)
                record = {"step": step, "loss": loss_total / (step % 100 or 100),
                          "synthetic_validation_dice": dice, "seconds": round(time.perf_counter() - started, 2)}
                history.append(record)
                loss_total = 0
                if dice > best_dice:
                    best_dice, best_step = dice, step
                    torch.save({"model": {k: v.cpu() for k, v in model.state_dict().items()},
                                "size": args.size, "step": step, "synthetic_validation_dice": dice,
                                "training_sources": manifest["train"]}, checkpoint)
                write_output(output / "history.json", json.dumps(history, indent=2).encode(), overwrite=True)
                print(json.dumps(record), flush=True)
        state = torch.load(checkpoint, map_location="cpu", weights_only=True)
        model.load_state_dict(state["model"], strict=True)
    model.eval()
    records, pages = export(model, manifest, output, args.size, device)
    report = {"manifest": manifest, "model": "GlyphUNet width=16", "device": device,
              "torch": torch.__version__, "seed": args.seed, "size": args.size,
              "best_step": best_step, "synthetic_validation_dice": best_dice,
              "validation_note": "相同训练字形的新合成噪声，不代表独立真实图准确率",
              "test_ground_truth": None, "threshold": .5, "postprocessing": "none",
              "elapsed_seconds": round(time.perf_counter() - started, 2),
              "checkpoint": str(args.checkpoint.resolve() if args.checkpoint else checkpoint),
              "checkpoint_sha256": digest(args.checkpoint or checkpoint), "previews": pages, "results": records}
    write_output(output / "report.json", json.dumps(report, ensure_ascii=False, indent=2).encode())
    print(f"完成：{output}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
