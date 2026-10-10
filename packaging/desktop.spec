"""Standalone protected desktop; no external model or sibling repo required."""
import os
from pathlib import Path
import runpy
import sys

root = Path(SPECPATH).parent
sys.path.insert(0, str(root))
protection = runpy.run_path(str(root / 'packaging/protection.py'))
source = protection['prepare'](root)
a = Analysis(
    [str(source / 'packaging/entry.py')],
    pathex=[str(source), str(source / 'packaging')],
    binaries=[], datas=[protection['build_info'](root)],
    hiddenimports=[
        'ui.platform_integration', '_cffi_backend',
        'hanzi_auth.desktop', 'hanzi_auth.policy', 'hanzi_auth.channel',
        'hanzi_auth.credentials', 'hanzi_auth.single_instance',
        'hanzi_update.core', 'hanzi_update.native_ui',
    ],
    hookspath=[], runtime_hooks=[],
    excludes=['PyQt5', 'PyQt6', 'PySide2', 'tkinter', 'pytest', 'torch',
              'transformers', 'hanzi_auth.server', 'hanzi_auth.cli'],
    noarchive=False,
)
protection['apply'](a, source)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='hanzi-mask',
          console=False, strip=False, upx=False,
          target_arch='arm64' if sys.platform == 'darwin' else None,
          codesign_identity=None)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='hanzi-mask')
if sys.platform == 'darwin':
    app = BUNDLE(coll, name='字形换底.app', bundle_identifier='cn.myhanzi.mask',
                 info_plist={
                     'CFBundleDisplayName': '字形换底',
                     'CFBundleShortVersionString': os.environ['PACKAGE_VERSION'].removeprefix('v'),
                     'NSHighResolutionCapable': True,
                 })
