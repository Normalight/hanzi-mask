import cv2
import numpy as np
import pytest

from hanzi_mask.edges import render_edges


def test_smoothing_keeps_separate_strokes_and_inner_holes():
    scores = np.zeros((80, 80), dtype=np.float32)
    cv2.circle(scores, (22, 35), 14, 1, 5)
    scores[18:58, 45:49] = 1
    scores[18:58, 53:57] = 1
    binary, aa = render_edges(scores, .7)
    assert binary[35, 22] == 0
    assert binary[20:56, 50:52].sum() == 0
    assert np.all(binary[20:56, 46:48] == 255)
    assert set(np.unique(binary)) == {0, 255}
    assert np.any((aa > 0) & (aa < 255))


@pytest.mark.parametrize("value", [0, 1])
def test_constant_scores_remain_constant(value):
    binary, aa = render_edges(np.full((10, 12), value, dtype=np.float32))
    assert np.all(binary == value * 255)
    assert np.all(aa == value * 255)


def test_zero_sigma_preserves_raw_binary_and_input_is_unchanged():
    scores = np.random.default_rng(8).random((20, 30)).astype(np.float32)
    before = scores.copy()
    binary, _ = render_edges(scores, 0)
    np.testing.assert_array_equal(binary, (scores > .5).astype(np.uint8) * 255)
    np.testing.assert_array_equal(scores, before)


@pytest.mark.parametrize("sigma", [-1, 4, float("nan")])
def test_invalid_sigma_is_rejected(sigma):
    with pytest.raises(ValueError):
        render_edges(np.zeros((10, 10)), sigma)
