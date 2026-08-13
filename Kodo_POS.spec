# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_data_files
from PyInstaller.utils.hooks import collect_submodules
from PyInstaller.utils.hooks import collect_all

datas = [('/Library/Frameworks/Python.framework/Versions/3.12/lib/python3.12/site-packages/customtkinter', 'customtkinter'), ('logo.png', '.'), ('logo_ticket.png', '.'), ('instagram_block.png', '.'), ('kodo_pos.db', '.'), ('ladresse_b.db', '.'), ('plan_permissions.json', '.'), ('dist', 'dist')]
binaries = []
hiddenimports = ['openpyxl.cell._writer', 'views.modals', 'views.stats_view', 'firebase_sync', 'shopify_sync', 'license_manager', 'backup_manager', 'core.rollback_manager', 'core.crash_watcher', 'server_pos']
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
    excludes=['pandas.tests', 'openpyxl.tests', 'PIL.tests', 'matplotlib', 'scipy', 'unittest', 'test', 'dateutil.tz.win', 'pytz.zoneinfo'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='Kodo_POS',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['logo.png'],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='Kodo_POS',
)
app = BUNDLE(
    coll,
    name='Kodo_POS.app',
    icon='logo.png',
    bundle_identifier='com.kodosolutions.pos',
    info_plist={
        'NSHighResolutionCapable': 'True',
        'LSBackgroundOnly': 'False',
        'CFBundleShortVersionString': '1.0.14',
        'CFBundleVersion': '1.0.14',
        'NSAppTransportSecurity': {
            'NSAllowsArbitraryLoads': True,
            'NSAllowsLocalNetworking': True,
        },
    }
)

