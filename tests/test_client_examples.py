"""Example preparation preserves inputs and protects existing destinations."""

import json

import numpy as np
from PIL import Image, ImageDraw
import pytest

from hanzi_mask.processing import Options
from tools.prepare_client_examples import Example, discover_external, prepare


def rubbing(path):
    image = Image.new("RGB", (72, 64), "#151515")
    draw = ImageDraw.Draw(image)
    draw.rectangle((18, 10, 27, 51), fill="#eeeeee")
    draw.rectangle((18, 42, 57, 51), fill="#eeeeee")
    image.save(path)


def test_examples_keep_source_bytes_and_export_binary_at_original_size(tmp_path):
    source = tmp_path / "source.png"
    rubbing(source)
    original = source.read_bytes()
    output = tmp_path / "demo"

    report = prepare([Example(source, "安", "示例")], output, Options())

    record = report["images"][0]
    assert source.read_bytes() == original
    assert (output / record["input"]).read_bytes() == original
    assert record["source_sha256"] == record["copied_sha256"]
    with Image.open(output / record["expected"]) as image:
        assert image.size == (72, 64)
        assert set(np.unique(np.asarray(image))) == {0, 255}
    assert report["parameters"]["method"] == "texture"
    assert json.loads((output / "manifest.json").read_text())["count"] == 1
    assert record["needs_visual_review"] is True


def test_existing_destination_remains_untouched(tmp_path):
    source = tmp_path / "source.png"
    rubbing(source)
    output = tmp_path / "demo"
    output.mkdir()
    marker = output / "keep.txt"
    marker.write_text("保留")

    with pytest.raises(ValueError, match="输出目录已存在"):
        prepare([Example(source, "安")], output, Options())

    assert list(output.iterdir()) == [marker]
    assert marker.read_text() == "保留"


def test_png_name_collision_rejected_before_copy(tmp_path):
    first, second = tmp_path / "crop.png", tmp_path / "crop.jpg"
    rubbing(first)
    rubbing(second)
    output = tmp_path / "demo"

    with pytest.raises(ValueError, match="文件名冲突"):
        prepare([Example(first, "安"), Example(second, "安")], output, Options())

    assert not output.exists()


def test_external_sampling_covers_glyph_directories(tmp_path):
    for character in ("A", "B", "C"):
        folder = tmp_path / character
        folder.mkdir()
        for index in range(3):
            rubbing(folder / f"crop{index}.png")

    examples = discover_external(tmp_path, 3)

    assert {item.character for item in examples} == {"A", "B", "C"}
    assert len(examples) == 3
