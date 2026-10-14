"""Build safety and writable paths, without starting a compiler or bundle."""
import importlib.util
from pathlib import Path
import sys
import tomllib
import shutil
import zipfile
from hashlib import sha256

import pytest

from ui import paths

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('desktop_builder', ROOT / 'packaging/build_desktop.py')
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


def test_source_default_stays_in_project_output(monkeypatch, tmp_path):
    monkeypatch.delattr(sys, 'frozen', raising=False)
    assert paths.default_output(tmp_path, '字形换底') == tmp_path / 'output/字形换底'


def test_frozen_default_stays_outside_application(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.setattr(paths.QStandardPaths, 'writableLocation', lambda _: str(tmp_path / '文稿'))
    assert paths.default_output(tmp_path / 'App/Resources', '字形换底') == tmp_path / '文稿/字形换底'


def test_frozen_default_without_documents_uses_home(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.setattr(paths.QStandardPaths, 'writableLocation', lambda _: '')
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    assert paths.default_output(tmp_path / 'Resources', '字形换底') == tmp_path / '字形换底'


@pytest.mark.parametrize('target,system,machine', [
    ('windows-x64', 'win32', 'AMD64'), ('macos-arm64', 'darwin', 'arm64'),
])
def test_target_and_wheel_validation(target, system, machine, monkeypatch):
    monkeypatch.setattr(sys, 'platform', system)
    monkeypatch.setattr(sys, 'version_info', (3, 11, 15))
    monkeypatch.setattr(builder.platform, 'machine', lambda: machine)
    expected = tomllib.loads((ROOT / 'pyproject.toml').read_text())['project']['version']
    assert builder.validate(target) == expected


def test_wrong_architecture_stops_before_build(monkeypatch):
    monkeypatch.setattr(sys, 'platform', 'darwin')
    monkeypatch.setattr(builder.platform, 'machine', lambda: 'x86_64')
    with pytest.raises(RuntimeError, match='原生系统'):
        builder.validate('macos-arm64')


def test_tampered_vendor_stops_before_build(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, 'platform', 'darwin')
    monkeypatch.setattr(builder.platform, 'machine', lambda: 'arm64')
    monkeypatch.setattr(sys, 'version_info', (3, 11, 15))
    vendor = tmp_path / 'vendor'
    vendor.mkdir()
    (vendor / 'manifest.json').write_bytes((ROOT / 'vendor/manifest.json').read_bytes())
    for wheel in (ROOT / 'vendor').glob('*.whl'):
        (vendor / wheel.name).write_bytes(b'tampered')
    monkeypatch.setattr(builder, 'ROOT', tmp_path)
    with pytest.raises(RuntimeError, match='依赖校验失败'):
        builder.validate('macos-arm64')


def test_check_mode_does_not_build(monkeypatch):
    monkeypatch.setattr(sys, 'argv', ['builder', '--target', 'macos-arm64', '--check'])
    monkeypatch.setattr(builder, 'validate', lambda _: '0.3.0')
    monkeypatch.setattr(builder, 'build', lambda _: pytest.fail('check mode started a build'))
    builder.main()


def test_windows_archive_automatically_uses_zip64(monkeypatch, tmp_path):
    bundle = tmp_path / 'app'
    bundle.mkdir()
    (bundle / 'data.bin').write_bytes(bytes(4096))
    monkeypatch.setattr(zipfile, 'ZIP64_LIMIT', 1024)
    archive = Path(shutil.make_archive(str(tmp_path / 'windows'), 'zip',
                                      root_dir=tmp_path, base_dir='app'))
    builder.verify_archive(archive, bundle)
    with zipfile.ZipFile(archive) as contents:
        assert contents.getinfo('app/data.bin').extract_version == 45


def test_mac_rejects_large_sparse_file_without_compressing(tmp_path):
    bundle = tmp_path / 'app'
    bundle.mkdir()
    with (bundle / 'large.bin').open('wb') as stream:
        stream.truncate(2**32 + 1)
    with pytest.raises(RuntimeError, match='Zip64'):
        builder.validate_mac_archive_size(bundle)


def test_archive_rejects_truncated_entry(tmp_path):
    bundle = tmp_path / 'app'
    bundle.mkdir()
    (bundle / 'data.bin').write_bytes(b'complete data')
    archive = tmp_path / 'bad.zip'
    with zipfile.ZipFile(archive, 'w') as contents:
        contents.writestr('app/data.bin', b'x')
    with pytest.raises(RuntimeError, match='大小不匹配'):
        builder.verify_archive(archive, bundle)


def test_streamed_checksum_matches_file(tmp_path):
    path = tmp_path / 'data.bin'
    data = bytes(1024 * 1024 + 17)
    path.write_bytes(data)
    assert builder.file_sha256(path) == sha256(data).hexdigest()
