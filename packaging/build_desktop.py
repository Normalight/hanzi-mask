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

ROOT = Path(__file__).resolve().parents[1]
TARGETS = {'windows-x64': ('win32', {'amd64', 'x86_64'}),
           'macos-arm64': ('darwin', {'arm64', 'aarch64'})}


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
        subprocess.run(['ditto', '-c', '-k', '--sequesterRsrc', '--keepParent',
                        str(bundle), str(archive)], check=True)
    else:
        shutil.make_archive(str(archive.with_suffix('')), 'zip',
                            root_dir=bundle.parent, base_dir=bundle.name)
    archive.with_suffix('.zip.sha256').write_text(
        f'{sha256(archive.read_bytes()).hexdigest()}  {archive.name}\n', encoding='utf-8')
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
