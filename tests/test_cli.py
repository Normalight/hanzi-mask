import hashlib
import json

import numpy as np
from PIL import Image, ImageDraw
import pytest

from hanzi_mask.cli import main


def character(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("L", (64, 64), 0)
    ImageDraw.Draw(image).rectangle((20, 10, 30, 50), fill=255)
    image.save(path)


def latest_report(output):
    return json.loads(sorted(output.glob("report-*.json"))[-1].read_text())


def test_batch_preserves_sources_and_existing_outputs(tmp_path):
    source, output = tmp_path / "原图", tmp_path / "结果"
    character(source / "字_10.png")
    character(source / "字_2.png")
    before = {p.name: hashlib.sha256(p.read_bytes()).digest() for p in source.iterdir()}
    assert main([str(source), "-o", str(output), "--preview"]) == 0
    report = latest_report(output)
    assert report["summary"] == {"converted": 2, "skipped": 0, "error": 0}
    assert report["images"][0]["source"] == "字_2.png"
    assert len(report["previews"]) == 1
    for name, digest in before.items():
        assert hashlib.sha256((source / name).read_bytes()).digest() == digest
        with Image.open(output / name) as image:
            assert image.size == (64, 64)
            assert set(np.unique(np.asarray(image))) <= {0, 255}
    existing = output / "字_2.png"
    existing.write_bytes(b"preserve this existing file")
    assert main([str(source), "-o", str(output)]) == 0
    assert existing.read_bytes() == b"preserve this existing file"
    assert latest_report(output)["summary"]["skipped"] == 2
    assert main([str(source), "-o", str(output), "--overwrite"]) == 0
    with Image.open(existing) as image:
        assert image.size == (64, 64)


def test_recursive_discovery_excludes_output_and_mask_directories(tmp_path):
    source = tmp_path / "input"
    output, masks = source / "output", source / "masks"
    character(source / "sub" / "字.png")
    character(output / "old.png")
    character(masks / "sub" / "字.png")
    assert main([str(source), "-o", str(output), "--recursive", "--mask-dir", str(masks)]) == 0
    report = latest_report(output)
    assert len(report["images"]) == 1
    assert report["images"][0]["output"] == "sub/字.png"
    assert report["images"][0]["method"] == "manual_mask"


def test_corrupt_image_is_reported_and_other_images_continue(tmp_path):
    source, output = tmp_path / "input", tmp_path / "output"
    character(source / "good.png")
    (source / "bad.png").write_bytes(b"not a PNG")
    assert main([str(source), "-o", str(output)]) == 1
    assert latest_report(output)["summary"] == {"converted": 1, "skipped": 0, "error": 1}
    assert (output / "good.png").exists()


def test_limit_and_single_file_input(tmp_path):
    source, output = tmp_path / "input", tmp_path / "output"
    character(source / "a.png")
    character(source / "b.png")
    assert main([str(source), "-o", str(output), "--limit", "1"]) == 0
    assert len(latest_report(output)["images"]) == 1
    second = tmp_path / "second"
    assert main([str(source / "b.png"), "-o", str(second)]) == 0
    assert (second / "b.png").exists()


@pytest.mark.parametrize("kind", ["same", "parent"])
def test_source_directory_cannot_be_overwritten(tmp_path, kind):
    source = tmp_path / "input"
    character(source / "a.png")
    output = source if kind == "same" else tmp_path
    with pytest.raises(SystemExit) as error:
        main([str(source), "-o", str(output), "--overwrite"])
    assert error.value.code == 2


def test_png_name_collisions_fail_before_export(tmp_path):
    source, output = tmp_path / "input", tmp_path / "output"
    character(source / "字.png")
    character(source / "字.jpg")
    with pytest.raises(SystemExit) as error:
        main([str(source), "-o", str(output)])
    assert error.value.code == 2
    assert not output.exists()


def test_symlinked_output_subdirectory_cannot_modify_source(tmp_path):
    source, output = tmp_path / "input", tmp_path / "output"
    character(source / "sub" / "a.png")
    output.mkdir()
    try:
        (output / "sub").symlink_to(source / "sub", target_is_directory=True)
    except OSError:
        pytest.skip("当前平台不允许创建目录符号链接")
    before = (source / "sub" / "a.png").read_bytes()
    assert main([str(source), "-o", str(output), "--recursive", "--overwrite"]) == 1
    assert (source / "sub" / "a.png").read_bytes() == before


def test_invalid_cli_option_is_a_usage_error(tmp_path):
    source = tmp_path / "a.png"
    character(source)
    with pytest.raises(SystemExit) as error:
        main([str(source), "-o", str(tmp_path / "output"), "--stroke-radius", "-1"])
    assert error.value.code == 2


@pytest.mark.parametrize("method", ["texture", "morphology"])
def test_method_parameters_reach_the_report(tmp_path, method):
    source, output = tmp_path / "a.png", tmp_path / "output"
    character(source)
    assert main([
        str(source), "-o", str(output), "--method", method,
        "--texture-strength", "0.006", "--texture-scale", "2",
        "--keep-isolated",
    ]) == 0
    report = latest_report(output)
    assert report["parameters"]["method"] == method
    assert report["parameters"]["keep_isolated"] is True
    assert report["images"][0]["method"] == method
    if method == "texture":
        assert report["images"][0]["texture_strength"] == 0.006
        assert report["images"][0]["texture_scale"] == 2


def test_report_name_collision_retries_with_unique_suffix(tmp_path, monkeypatch):
    import hanzi_mask.cli as cli

    source, output = tmp_path / "原图", tmp_path / "结果"
    character(source / "字_2.png")
    # A coarse or frozen clock can repeat time_ns() between rapid runs; the
    # exclusive publication must retry with a fresh unique report name.
    monkeypatch.setattr(cli.time, "time_ns", lambda: 1791984308369105500)
    assert main([str(source), "-o", str(output)]) == 0
    assert main([str(source), "-o", str(output), "--overwrite"]) == 0
    reports = sorted(path.name for path in output.glob("report-*.json"))
    assert len(reports) == 2 and reports[0] != reports[1]
