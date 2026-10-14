"""Validate, build and smoke-test an independent desktop archive."""
import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import tomllib
import zipfile
import unicodedata

ROOT = Path(__file__).resolve().parents[1]
TARGETS = {'windows-x64': ('win32', {'amd64', 'x86_64'}),
           'macos-arm64': ('darwin', {'arm64', 'aarch64'})}


def validate_mac_archive_size(bundle):
    # ditto can silently wrap 32-bit sizes instead of producing valid Zip64.
    entries = list(bundle.rglob('*'))
    sizes = [path.stat().st_size for path in entries
             if path.is_file() and not path.is_symlink()]
    if len(entries) + 1 >= 65535 or sum(sizes) >= 2**32:
        raise RuntimeError('Mac 应用超过传统 ZIP 容量，ditto 不能安全生成 Zip64；需调整打包方式')


def verify_archive(archive, bundle):
    with zipfile.ZipFile(archive) as contents:
        entries = {}
        for entry in contents.infolist():
            name = entry.filename
            # ditto writes UTF-8 names without consistently setting the UTF-8 bit.
            if not entry.flag_bits & 0x800:
                try:
                    name = name.encode('cp437').decode('utf-8')
                except (UnicodeEncodeError, UnicodeDecodeError):
                    pass
            entries[unicodedata.normalize('NFC', name)] = entry
        for path in bundle.rglob('*'):
            if path.is_file() and not path.is_symlink():
                name = unicodedata.normalize('NFC', path.relative_to(bundle.parent).as_posix())
                if name not in entries or entries[name].file_size != path.stat().st_size:
                    raise RuntimeError(f'ZIP 文件大小不匹配：{name}')
        bad_file = contents.testzip()
        if bad_file is not None:
            raise RuntimeError(f'ZIP 完整性检查失败：{bad_file}')


def file_sha256(path):
    digest = sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def validate(target):
    expected_os, architectures = TARGETS[target]
    if sys.platform != expected_os or platform.machine().lower() not in architectures:
        raise RuntimeError(f'{target} 必须在对应架构的原生系统构建')
    if sys.version_info[:2] != (3, 11):
        raise RuntimeError('构建使用 Python 3.11')
    manifest = json.loads((ROOT / 'vendor/manifest.json').read_text(encoding='utf-8'))
    for entry in manifest['wheels']:
        wheel = ROOT / 'vendor' / entry['filename']
        if sha256(wheel.read_bytes()).hexdigest() != entry['sha256']:
            raise RuntimeError(f'共享依赖校验失败：{wheel.name}')
    return tomllib.loads((ROOT / 'pyproject.toml').read_text(encoding='utf-8'))['project']['version']


def build(target):
    version = validate(target)
    environment = dict(os.environ, PACKAGE_VERSION=f'v{version}', QT_QPA_PLATFORM='offscreen')
    environment.setdefault('HANZI_PROTECT', '1')
    subprocess.run([sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean',
                    'packaging/desktop.spec'], cwd=ROOT, env=environment, check=True)
    if target == 'macos-arm64':
        bundle = ROOT / 'dist/字形换底.app'
        # PyInstaller signs binaries ad hoc. Sign the complete app after assembly.
        subprocess.run(['codesign', '--force', '--deep', '--sign', '-', str(bundle)], check=True)
        subprocess.run(['codesign', '--verify', '--deep', '--strict', str(bundle)], check=True)
        executable = bundle / 'Contents/MacOS/hanzi-mask'
    else:
        bundle = ROOT / 'dist/hanzi-mask'
        executable = bundle / 'hanzi-mask.exe'
    with tempfile.TemporaryDirectory(prefix='hanzi-mask-frozen-test-') as directory:
        subprocess.run([str(executable), '--smoke-test'], cwd=directory,
                       env=environment, check=True, timeout=120)
    release = ROOT / 'dist/release'
    release.mkdir(exist_ok=True)
    archive = release / f'字形换底-v{version}-{target}.zip'
    if target == 'macos-arm64':
        validate_mac_archive_size(bundle)
        subprocess.run(['ditto', '-c', '-k', '--sequesterRsrc', '--keepParent',
                        str(bundle), str(archive)], check=True)
    else:
        shutil.make_archive(str(archive.with_suffix('')), 'zip',
                            root_dir=bundle.parent, base_dir=bundle.name)
    verify_archive(archive, bundle)
    archive.with_suffix('.zip.sha256').write_text(
        f'{file_sha256(archive)}  {archive.name}\n', encoding='utf-8')
    print(archive)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', choices=TARGETS, required=True)
    parser.add_argument('--check', action='store_true', help='仅校验，不开始构建')
    args = parser.parse_args()
    if args.check:
        print(f'{args.target}: v{validate(args.target)} 配置校验通过')
    else:
        build(args.target)


if __name__ == '__main__':
    main()
