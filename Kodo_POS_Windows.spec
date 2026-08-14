# -*- mode: python ; coding: official -*-
# Script PyInstaller Spec pour Windows (Kōdo POS)

from PyInstaller.utils.hooks import collect_data_files
from PyInstaller.utils.hooks import collect_submodules
from PyInstaller.utils.hooks import collect_all

datas = [('logo.png', '.'), ('logo_ticket.png', '.'), ('instagram_block.png', '.'), ('kodo_pos.db', '.'), ('ladresse_b.db', '.'), ('dist', 'dist')]
binaries = []
hiddenimports = ['openpyxl.cell._writer', 'views.modals', 'views.stats_view', 'firebase_sync', 'shopify_sync', 'license_manager', 'backup_manager', 'core.rollback_manager', 'core.crash_watcher', 'core.updater', 'services.update_checker', 'server_pos']
datas += collect_data_files('pandas')
datas += collect_data_files('openpyxl')
datas += collect_data_files('PIL')
hiddenimports += collect_submodules('PIL')
tmp_ret = collect_all('qrcode')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('barcode')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]

a = Analysis(
    ['launch_app.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['pandas.tests', 'openpyxl.tests', 'PIL.tests', 'matplotlib', 'scipy', 'unittest', 'test'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='Kodo_POS',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='logo.ico' if os.path.exists('logo.ico') else None,
)
