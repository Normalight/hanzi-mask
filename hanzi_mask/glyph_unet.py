"""Small U-Net-style segmenter, implemented here with PyTorch primitives."""

import torch
from torch import nn
from torch.nn import functional as F


def block(in_channels, out_channels):
    return nn.Sequential(
        nn.Conv2d(in_channels, out_channels, 3, padding=1, bias=False),
        nn.GroupNorm(4, out_channels), nn.SiLU(),
        nn.Conv2d(out_channels, out_channels, 3, padding=1, bias=False),
        nn.GroupNorm(4, out_channels), nn.SiLU(),
    )


class GlyphUNet(nn.Module):
    def __init__(self, width=16):
        super().__init__()
        self.encoders = nn.ModuleList([block(1, width), block(width, width * 2),
                                      block(width * 2, width * 4), block(width * 4, width * 8)])
        self.center = block(width * 8, width * 16)
        self.decoders = nn.ModuleList([block(width * 24, width * 8), block(width * 12, width * 4),
                                      block(width * 6, width * 2), block(width * 3, width)])
        self.head = nn.Conv2d(width, 1, 1)

    def forward(self, x):
        skips = []
        for encoder in self.encoders:
            x = encoder(x)
            skips.append(x)
            x = F.max_pool2d(x, 2)
        x = self.center(x)
        for decoder, skip in zip(self.decoders, reversed(skips)):
            x = F.interpolate(x, size=skip.shape[-2:], mode="bilinear", align_corners=False)
            x = decoder(torch.cat([x, skip], dim=1))
        return self.head(x)


def segmentation_loss(logits, targets):
    probs = logits.sigmoid()
    axes = (1, 2, 3)
    dice = (2 * (probs * targets).sum(axes) + 1) / (probs.sum(axes) + targets.sum(axes) + 1)
    return F.binary_cross_entropy_with_logits(logits, targets) + 1 - dice.mean()
