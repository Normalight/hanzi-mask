import numpy as np
from PIL import Image
import pytest

torch = pytest.importorskip("torch")

from hanzi_mask.glyph_unet import GlyphUNet, segmentation_loss
from hanzi_mask.supervised import predict


def test_model_has_finite_gradients_and_preserves_spatial_size():
    torch.set_num_threads(2)
    torch.manual_seed(3)
    model = GlyphUNet(width=4)
    images = torch.rand(2, 1, 64, 80)
    targets = (images > .6).float()
    logits = model(images)
    assert logits.shape == targets.shape
    loss = segmentation_loss(logits, targets)
    loss.backward()
    assert torch.isfinite(loss)
    assert all(p.grad is not None and torch.isfinite(p.grad).all() for p in model.parameters())


def test_predict_crops_padding_and_restores_original_size():
    class IdentityLogits(torch.nn.Module):
        def forward(self, images):
            return (images - .5) * 50

    source = np.zeros((40, 90), dtype=np.uint8)
    source[8:32, 20:70] = 255
    scores = predict(IdentityLogits(), Image.fromarray(source), 96, "cpu")
    assert scores.shape == source.shape
    expected = source > 0
    actual = scores > .5
    assert np.mean(expected == actual) > .99
