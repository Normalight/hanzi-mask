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


def test_default_cli_covers_all_curated_examples_with_desktop_options(tmp_path, monkeypatch):
    from tools import prepare_client_examples as examples

    workspace = tmp_path / 'workspace'
    sources = []
    grouped = workspace / 'output/字形图片高置信归并-测试/归类结果/按字'
    for character, name, _ in examples.GROUPED_EXAMPLES:
        sources.append(grouped / character / name)
    for _, name, _ in examples.RUBBING_EXAMPLES:
        sources.append(workspace / '10-1dbe' / name)
    special = {
        '55-9f82_crop_4.png', '高靈府院君申叔舟神道碑-55-9f82_crop_4.png',
        '55-9f82_crop_124.png', '高靈府院君申叔舟神道碑-55-9f82_crop_124.png',
    }
    sources.extend(workspace / '字形图片/1' / name for name in sorted(special))
    for source in sources:
        source.parent.mkdir(parents=True, exist_ok=True)
        rubbing(source)
    original_bytes = {source: source.read_bytes() for source in sources}
    # Extra fallback crops must not push the final curated cases out of the default slice.
    rubbing(workspace / '10-1dbe/extra.png')
    monkeypatch.setattr(examples, 'PROJECT_ROOT', workspace / 'hanzi-mask')
    output = tmp_path / 'examples'

    assert examples.main(['-o', str(output)]) == 0

    report = json.loads((output / 'manifest.json').read_text())
    assert report['count'] == 16
    selected = {record['source'] for record in report['images']}
    assert selected == {str(source) for source in sources}
    assert special <= {record['source'].rsplit('/', 1)[-1] for record in report['images']}
    assert report['parameters']['profile'] == 'balanced'
    for name in ('keep_isolated', 'edge_refine', 'stroke_recovery', 'tip_recovery'):
        assert report['parameters'][name] is True
    for source, before in original_bytes.items():
        assert source.read_bytes() == before
