"""Paired glyph augmentation. Held-out photographs never enter this generator."""

import hashlib
import json
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

from .processing import load_image


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def read_manifest(path):
    path = Path(path).resolve()
    manifest = json.loads(path.read_text())
    if not manifest.get("train") or not manifest.get("test"):
        raise ValueError("manifest 必须有非空 train 和 test 列表")
    hashes = set()
    train_groups = {item["group"] for item in manifest["train"] if item.get("group")}
    if any(item.get("group") in train_groups for item in manifest["test"]):
        raise ValueError("同一来源组跨越训练与测试，可能存在缩放副本或同页泄漏")
    for split in ("train", "test"):
        output_names = set()
        for item in manifest[split]:
            image_path = (path.parent / item["image"]).resolve()
            name = image_path.with_suffix(".png").name.casefold()
            if name in output_names:
                raise ValueError("同一划分内存在 PNG 输出名称冲突")
            output_names.add(name)
            image_hash = digest(image_path)
            if image_hash in hashes:
                raise ValueError("训练/测试含重复原图内容，或同一划分内有重复样本")
            hashes.add(image_hash)
            item.update(image=str(image_path), sha256=image_hash)
            original = load_image(image_path, "light")
            if split == "train":
                mask_path = (path.parent / item["mask"]).resolve()
                with Image.open(mask_path) as mask:
                    arr = np.asarray(mask.convert("L"))
                    if mask.size != original.size or not set(np.unique(arr)) <= {0, 255}:
                        raise ValueError("训练掩膜须与原图同尺寸且像素仅为 0/255")
                    if arr.min() == arr.max():
                        raise ValueError("训练掩膜必须同时包含字形和背景")
                item.update(mask=str(mask_path), mask_sha256=digest(mask_path))
    return manifest


def letterbox(array, size, nearest=False):
    h, w = array.shape[:2]
    scale = size / max(h, w)
    rw, rh = max(1, round(w * scale)), max(1, round(h * scale))
    interpolation = cv2.INTER_NEAREST if nearest else cv2.INTER_LINEAR
    resized = cv2.resize(array, (rw, rh), interpolation=interpolation)
    x, y = (size - rw) // 2, (size - rh) // 2
    canvas = np.zeros((size, size), dtype=array.dtype)
    canvas[y:y + rh, x:x + rw] = resized
    return canvas, (x, y, rw, rh)


def normalize(gray):
    gray = gray.astype(np.float32)
    lo, hi = np.percentile(gray, [2, 98])
    return np.clip((gray - lo) / max(hi - lo, 1), 0, 1).astype(np.float32)


def check_checkpoint_sources(training_sources, manifest):
    """A new manifest must not relabel a previously trained image as held out."""
    known = {item["sha256"] for item in training_sources}
    groups = {item["group"] for item in training_sources if item.get("group")}
    for item in manifest["test"]:
        if item["sha256"] in known or item.get("group") in groups:
            raise ValueError("测试样本已出现在该权重的训练来源中")
    actual = {(item["sha256"], item["mask_sha256"]) for item in training_sources}
    declared = {(item["sha256"], item["mask_sha256"]) for item in manifest["train"]}
    if actual != declared:
        raise ValueError("清单训练原图/掩膜与权重记录不一致")


class GlyphPairs:
    def __init__(self, training, size=192):
        self.size = size
        self.templates = []
        for item in training:
            original = load_image(Path(item["image"]), "light")
            gray, _ = letterbox(normalize(np.asarray(original.convert("L"))), size)
            with Image.open(item["mask"]) as mask:
                target, _ = letterbox(np.asarray(mask.convert("L")), size, nearest=True)
            self.templates.append((gray, target.astype(np.float32) / 255))

    def sample(self, rng):
        s = self.size
        gray, target = self.templates[int(rng.integers(len(self.templates)))]
        # Shared affine and smooth displacement preserve input/label alignment.
        matrix = cv2.getRotationMatrix2D((s / 2, s / 2), float(rng.uniform(-12, 12)), float(rng.uniform(.75, 1.05)))
        matrix[0, :2] *= rng.uniform(.85, 1.12)
        matrix[:, 2] += rng.uniform(-.06, .06, 2) * s
        gray = cv2.warpAffine(gray, matrix, (s, s))
        target = cv2.warpAffine(target, matrix, (s, s), flags=cv2.INTER_NEAREST)
        yy, xx = np.mgrid[:s, :s].astype(np.float32)
        dx = cv2.resize(rng.normal(0, 1.5, (5, 5)).astype(np.float32), (s, s))
        dy = cv2.resize(rng.normal(0, 1.5, (5, 5)).astype(np.float32), (s, s))
        gray = cv2.remap(gray, xx + dx, yy + dy, cv2.INTER_LINEAR)
        target = cv2.remap(target, xx + dx, yy + dy, cv2.INTER_NEAREST)
        # Mix real clean-image texture with synthetic strokes/backgrounds.
        bg, fg = rng.uniform(.05, .3), rng.uniform(.65, .98)
        base = bg + (fg - bg) * target
        mix = rng.uniform(0, .7)
        noisy = mix * gray + (1 - mix) * base
        fine = rng.normal(0, rng.uniform(.01, .1), (s, s)).astype(np.float32)
        coarse = cv2.resize(rng.normal(0, .1, (12, 12)).astype(np.float32), (s, s))
        noisy += fine + coarse
        # Same-brightness blobs include attached and isolated noise, not only dust.
        spots = np.zeros((s, s), dtype=np.float32)
        for _ in range(int(rng.integers(30, 180))):
            center = tuple(int(v) for v in rng.integers(0, s, 2))
            radius = int(rng.choice([1, 2, 3, 4, 6, 9], p=[.25, .25, .2, .15, .1, .05]))
            cv2.ellipse(spots, center, (radius, max(1, int(radius * rng.uniform(.4, 1.5)))),
                        float(rng.uniform(0, 180)), 0, 360, float(rng.uniform(.5, 1)), -1)
        spots = cv2.GaussianBlur(spots, (3, 3), .5)
        noisy = noisy * (1 - spots) + fg * spots
        # Cracks and eroded flecks partially occlude true strokes.
        damage = np.zeros((s, s), dtype=np.float32)
        for _ in range(int(rng.integers(0, 8))):
            start = tuple(int(v) for v in rng.integers(0, s, 2))
            end = tuple(int(v) for v in rng.integers(0, s, 2))
            cv2.line(damage, start, end, float(rng.uniform(.3, .8)), int(rng.integers(1, 3)))
        noisy = noisy * (1 - damage) + bg * damage
        if rng.random() < .4:
            noisy = cv2.GaussianBlur(noisy, (3, 3), float(rng.uniform(.3, .8)))
        noisy = np.clip(noisy, 0, 1) ** rng.uniform(.7, 1.5)
        return noisy.astype(np.float32)[None], target.astype(np.float32)[None]

    def batch(self, rng, count):
        pairs = [self.sample(rng) for _ in range(count)]
        return np.stack([p[0] for p in pairs]), np.stack([p[1] for p in pairs])
