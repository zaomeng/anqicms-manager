# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['web_app.py'],
    pathex=['.'],
    binaries=[],
    datas=[
        ('web/static', 'web/static'),
        ('jieba_data', 'jieba_data'),
    ],
    hiddenimports=[
        'core.sitegroup_mcp_server',
        'core.self_updater',
        'core.app_info',
        'core.anqicms_mcp_client',
        'mcp_server',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
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
    name='anqicms-manager',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
