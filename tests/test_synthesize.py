import json

import numpy as np
from PIL import Image
import pytest

from hanzi_mask.synthesize import main


def test_export_pairs_is_repeatable_and_keeps_test_sources_out(tmp_path):
    mask = np.zeros((90, 100), dtype=np.uint8)
    mask[10:80, 30:45] = 255
    mask[40:50, 10:90] = 255
    Image.fromarray(mask).save(tmp_path / "clean.png")
    Image.fromarray(mask).save(tmp_path / "mask.png")
    Image.fromarray(np.rot90(mask)).save(tmp_path / "test.png")
    manifest = {"train": [{"image": "clean.png", "mask": "mask.png", "group": "a"}],
                "test": [{"image": "test.png", "group": "b"}]}
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest))
    before = (tmp_path / "test.png").read_bytes()
    outputs = [tmp_path / "first", tmp_path / "second"]
    for out in outputs:
        assert main([str(path), "-o", str(out), "--count", "3", "--size", "96"]) == 0
        assert len(list((out / "images").glob("*.png"))) == 3
        info = json.loads((out / "dataset.json").read_text())
        assert info["test_images_used"] is False
        assert len(info["sources"]) == 1
        for p in (out / "masks").glob("*.png"):
            with Image.open(p) as image:
                assert image.size == (96, 96)
                assert set(np.unique(image)) <= {0, 255}
    assert (tmp_path / "test.png").read_bytes() == before
    for p in (outputs[0] / "images").glob("*.png"):
        assert p.read_bytes() == (outputs[1] / "images" / p.name).read_bytes()
    with pytest.raises(SystemExit):
        main([str(path), "-o", str(outputs[0])])
