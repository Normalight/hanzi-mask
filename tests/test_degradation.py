import numpy as np

from hanzi_mask.degradation import complex_rubbing


def test_complex_texture_changes_background_without_changing_target():
    target = np.zeros((128, 128), dtype=np.float32)
    target[25:105, 57:71] = 1
    target[56:70, 25:105] = 1
    gray = .1 + .8 * target
    original_mask, original_gray = target.copy(), gray.copy()
    noisy = complex_rubbing(gray, target, np.random.default_rng(14))
    np.testing.assert_array_equal(target, original_mask)
    np.testing.assert_array_equal(gray, original_gray)
    assert noisy.dtype == np.float32
    assert np.isfinite(noisy).all() and 0 <= noisy.min() <= noisy.max() <= 1
    assert np.count_nonzero((noisy > .5) & (target == 0)) > 100
    np.testing.assert_array_equal(noisy, complex_rubbing(gray, target, np.random.default_rng(14)))
