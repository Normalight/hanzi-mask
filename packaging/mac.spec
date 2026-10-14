# macOS-only runtime build for the native updater layout, independent of desktop.spec.
import os
from pathlib import Path
os.environ.setdefault("MAC_ARCH", "arm64")
if os.environ["MAC_ARCH"] not in {"arm64", "x86_64"}:
    raise ValueError("Mac builds require arm64 or x86_64")
root = Path(SPECPATH).parent
import sys
sys.path.insert(0, str(root))
datas = []
import runpy
protection = runpy.run_path(str(root / 'packaging/protection.py'))
code_root = protection['prepare'](root)
native_release = runpy.run_path(str(root / 'packaging/native_release.py'))
datas.extend(native_release['analysis_data'](root))
datas.append(protection['build_info'](root))
# The compiled shared package reads products.json next to its module at
# runtime; ship the registry from the installed wheel alongside it.
import hanzi_update
datas.append((str(Path(hanzi_update.__file__).parent / 'products.json'), 'hanzi_update'))
a = Analysis([str(code_root / 'packaging/entry.py')],
    pathex=[str(code_root), str(code_root / 'packaging')], datas=datas, binaries=[],
    hiddenimports=['ui.platform_integration', '_cffi_backend', 'packaging', 'packaging.version',
                   'hanzi_auth.desktop', 'hanzi_auth.policy', 'hanzi_auth.channel',
                   'hanzi_auth.credentials', 'hanzi_auth.single_instance',
                   'hanzi_update.core', 'hanzi_update.native_ui'],
    hookspath=[], runtime_hooks=[],
    excludes=['PyQt5', 'PyQt6', 'PySide2', 'tkinter', 'pytest', 'torch',
              'transformers', 'hanzi_auth.server', 'hanzi_auth.cli'], noarchive=False)
# No slimming here: the shared slimming helper prunes onnxruntime, which this
# product does not ship.
protection['apply'](a, code_root)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='runtime',
    console=False, strip=False, upx=False, target_arch=os.environ['MAC_ARCH'],
    codesign_identity=None)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='runtime')
runtime_info = native_release['runtime_bundle_info'](root)
app = BUNDLE(coll, name='Runtime.app', bundle_identifier=runtime_info['CFBundleIdentifier'],
    info_plist=runtime_info)
