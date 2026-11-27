# -*- mode: python ; coding: utf-8 -*-
# Script PyInstaller Spec pour Windows (Kōdo POS)
import os
from PyInstaller.utils.hooks import collect_data_files, collect_submodules, collect_all

datas = [
    ('logo.png', '.'),
    ('logo_ticket.png', '.'),
    ('instagram_block.png', '.'),
    ('kodo_pos.db', '.'),
    ('plan_permissions.json', '.'),
    ('dist', 'dist'),
]
binaries = []
hiddenimports = [
    *collect_submodules('kodo_core'),
    'server_pos',
    'patch_loader',
    'kodo_base',
    'kodo_ed25519',
    'database_manager',
    'audit_trail',
    'backup_manager',
    'ticket_printer',
    'pdf_generator',
    'license_manager',
    'shopify_sync',
    'firebase_sync',
    'export_manager',
    'openpyxl',
    'openpyxl.cell._writer',
    *collect_submodules('PIL'),
    *collect_submodules('reportlab'),
    *collect_submodules('cryptography'),
]

datas += collect_data_files('openpyxl')
datas += collect_data_files('PIL')

for _pkg in ('qrcode', 'barcode', 'certifi'):
    try:
        _ret = collect_all(_pkg)
        datas += _ret[0]
        binaries += _ret[1]
        hiddenimports += _ret[2]
    except Exception:
        pass

a = Analysis(
    ['launch_app.py'],
    pathex=['.'],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'numpy', 'pandas', 'scipy', 'matplotlib',
        'pytest', 'unittest', 'test', 'tkinter.test', 'curses', 'IPython', 'jupyter',
    ],
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
    upx=False,
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
