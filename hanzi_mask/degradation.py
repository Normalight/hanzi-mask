"""Procedural rubbing-like corruption with an unchanged clean glyph target."""

import cv2
import numpy as np


def field(rng, size, cells):
    values = rng.normal(0, 1, (cells, cells)).astype(np.float32)
    values = cv2.resize(values, (size, size), interpolation=cv2.INTER_CUBIC)
    return values / max(float(values.std()), .01)


def complex_rubbing(gray, target, rng):
    size = target.shape[0]
    fine = field(rng, size, max(8, size // 3))
    medium = field(rng, size, max(6, size // 12))
    coarse = field(rng, size, 5)
    texture = .55 * fine + .3 * medium + .15 * coarse
    bg, fg = rng.uniform(.06, .26), rng.uniform(.65, .97)
    illumination = field(rng, size, 3) * rng.uniform(.01, .1)
    background = bg + rng.uniform(.03, .12) * texture + illumination
    stroke = fg + field(rng, size, 16) * rng.uniform(.02, .1)
    synthetic = background * (1 - target) + stroke * target
    mix = rng.uniform(0, .35)
    noisy = synthetic * (1 - mix) + gray * mix
    # Thresholded multiscale fields produce ragged patches rather than round dots.
    coverage = rng.uniform(.06, .32)
    cutoff = np.quantile(texture, 1 - coverage)
    speckle = np.clip((texture - cutoff) * rng.uniform(2, 6), 0, 1)
    # Directional rubbing grain, varying in length and density.
    streak = rng.normal(0, 1, (size, size)).astype(np.float32)
    vertical = rng.random() < .5
    streak = cv2.GaussianBlur(streak, (0, 0), .6 if vertical else 9, sigmaY=9 if vertical else .6)
    streak /= max(float(streak.std()), .001)
    streak = np.clip((streak - rng.uniform(1, 2.5)) * .5, 0, 1)
    noise_alpha = np.maximum(speckle, streak * rng.uniform(.3, .9))
    # Thick edge stains are deliberately unrelated to the foreground mask.
    if rng.random() < .45:
        band = np.zeros_like(target)
        width = int(rng.integers(3, max(4, size // 8)))
        band[:, :width] = np.clip(field(rng, size, 10)[:, :width] + .2, 0, 1)
        band = np.rot90(band, int(rng.integers(4))).copy()
        noise_alpha = np.maximum(noise_alpha, band)
    noisy = noisy * (1 - noise_alpha) + stroke * noise_alpha
    cracks = np.zeros_like(target)
    for _ in range(int(rng.integers(0, 6))):
        pts = rng.integers(0, size, (int(rng.integers(2, 5)), 2)).astype(np.int32)
        cv2.polylines(cracks, [pts], False, float(rng.uniform(.2, .7)), int(rng.integers(1, 3)))
    pits = np.clip((-fine - rng.uniform(1.4, 2.2)) * .3, 0, .6)
    damage = np.maximum(cracks, pits)
    noisy = noisy * (1 - damage) + background * damage
    noisy += rng.normal(0, rng.uniform(.01, .05), noisy.shape).astype(np.float32)
    if rng.random() < .4:
        noisy = cv2.GaussianBlur(noisy, (3, 3), float(rng.uniform(.3, .7)))
    return (np.clip(noisy, 0, 1) ** rng.uniform(.8, 1.2)).astype(np.float32)
