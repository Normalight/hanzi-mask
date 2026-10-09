from email.parser import BytesParser
from hashlib import sha256
import json
from pathlib import Path
import tomllib
from zipfile import ZipFile

from packaging.requirements import Requirement
import pytest


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_WHEELS = {
    "hanzi_update-0.1.6-py3-none-any.whl": (
        "hanzi-update", "0.1.6",
        "1589e59f37ccd155fd5ec1bf51f33477421cc61f49ba451dd692f987abdf6a44",
    ),
    "hanzi_auth-0.2.7-py3-none-any.whl": (
        "hanzi-auth", "0.2.7",
        "997b3dae0f080290e004e3731f561a813035b4e4e784b90b8dceab60d6950798",
    ),
}


def manifest():
    return json.loads((ROOT / "vendor" / "manifest.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("filename", EXPECTED_WHEELS)
def test_distributed_wheel_hash_size_and_metadata_match_manifest(filename):
    name, version, expected_hash = EXPECTED_WHEELS[filename]
    wheel_path = ROOT / "vendor" / filename
    entries = {entry["filename"]: entry for entry in manifest()["wheels"]}
    entry = entries[filename]
    assert entry["distribution"] == name and entry["version"] == version
    assert entry["sha256"] == expected_hash == sha256(wheel_path.read_bytes()).hexdigest()
    assert entry["size"] == wheel_path.stat().st_size
    with ZipFile(wheel_path) as wheel:
        metadata_path, = [path for path in wheel.namelist() if path.endswith(".dist-info/METADATA")]
        metadata = BytesParser().parsebytes(wheel.read(metadata_path))
        assert wheel.testzip() is None
    assert metadata["Name"] == name and metadata["Version"] == version
    assert metadata["Requires-Python"]
    if name == "hanzi-auth":
        update, = [Requirement(value) for value in metadata.get_all("Requires-Dist", [])
                   if Requirement(value).name == "hanzi-update"]
        assert update.marker is None and update.specifier.contains("0.1.6")


def test_vendoring_inventory_and_explicit_sync_boundary_are_documented():
    data = manifest()
    assert data["schema_version"] == 1
    assert {entry["filename"] for entry in data["wheels"]} == set(EXPECTED_WHEELS)
    assert {path.name for path in (ROOT / "vendor").glob("*.whl")} == set(EXPECTED_WHEELS)
    sync = data["synchronization"]
    assert sync["includes_hanzi_mask"] is False
    assert set(sync["current_targets"]) == {"group-by-character", "char-detection-main"}
    assert sync["tool"] == "../hanzi-auth/tools/sync_desktop.py"
    assert sync["maintenance"] and data["purpose"] and data["source"]


def test_requirements_install_local_update_before_authentication():
    requirements = [line.strip() for line in (ROOT / "requirements.txt").read_text().splitlines()
                    if line.strip() and not line.lstrip().startswith("#")]
    update = "./vendor/hanzi_update-0.1.6-py3-none-any.whl"
    auth = "./vendor/hanzi_auth-0.2.7-py3-none-any.whl"
    assert requirements.count(update) == requirements.count(auth) == 1
    assert requirements.index(update) < requirements.index(auth)
    assert {line for line in requirements if line.endswith(".whl")} == {update, auth}
    assert all((ROOT / line).is_file() for line in (update, auth))


def test_platform_extra_is_precisely_pinned_and_core_dependencies_remain_independent():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    assert set(project["optional-dependencies"]["platform"]) == {
        "hanzi-auth==0.2.7", "hanzi-update==0.1.6",
    }
    base = [Requirement(value) for value in project["dependencies"]]
    assert {requirement.name for requirement in base} == {
        "numpy", "opencv-python-headless", "Pillow", "scipy", "PySide6",
    }
    assert all(requirement.url is None for requirement in base)
    assert not {"hanzi-auth", "hanzi-update"} & {requirement.name for requirement in base}
