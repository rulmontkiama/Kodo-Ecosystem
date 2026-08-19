# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_data_files, collect_submodules, collect_all

datas = [
    ('logo.png', '.'),
    ('logo_ticket.png', '.'),
    ('instagram_block.png', '.'),
    ('kodo_pos.db', '.'),
    ('ladresse_b.db', '.'),
    ('plan_permissions.json', '.'),
    ('dist', 'dist')
]

binaries = []

hiddenimports = [
    'kodo_core',
    'kodo_core.config',
    'kodo_core.db',
    'kodo_core.db.connection',
    'kodo_core.db.migrations',
    'kodo_core.db.audit_trail',
    'kodo_core.domain',
    'kodo_core.domain.sales',
    'kodo_core.domain.sales.cart_engine',
    'kodo_core.domain.catalog',
    'kodo_core.domain.catalog.inventory_manager',
    'kodo_core.domain.customers',
    'kodo_core.domain.customers.crm',
    'kodo_core.domain.accounting',
    'kodo_core.domain.accounting.z_report',
    'kodo_core.hardware',
    'kodo_core.hardware.printer',
    'kodo_core.hardware.pdf',
    'kodo_core.sync',
    'kodo_core.sync.shopify',
    'kodo_core.sync.firebase',
    'kodo_core.sync.offline_engine',
    'kodo_core.services',
    'kodo_core.services.license',
    'kodo_core.services.updater',
    'kodo_core.services.migration',
    'kodo_core.api',
    'kodo_core.api.app',
    'kodo_core.api.routes',
    'kodo_core.api.routes.pos_routes',
    'kodo_core.api.routes.products_routes',
    'kodo_core.api.routes.clients_routes',
    'kodo_core.api.routes.stats_routes',
    'kodo_core.api.routes.backup_routes',
    'kodo_core.api.routes.system_routes',
    'server_pos',
    'database_manager',
    'audit_trail',
    'backup_manager',
    'ticket_printer',
    'pdf_generator',
    'license_manager',
    'shopify_sync',
    'firebase_sync',
    'openpyxl',
    'openpyxl.cell._writer'
]

tmp_ret = collect_all('qrcode')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]

tmp_ret = collect_all('barcode')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]

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
        'pytest', 'unittest', 'test', 'tkinter.test', 'curses', 'IPython', 'jupyter'
    ],
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
    upx=False,
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
    upx=False,
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
        'CFBundleShortVersionString': '1.0.18',
        'CFBundleVersion': '1.0.18',
        'NSAppTransportSecurity': {
            'NSAllowsArbitraryLoads': True,
            'NSAllowsLocalNetworking': True,
        },
    }
)
