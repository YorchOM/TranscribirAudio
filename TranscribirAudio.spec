# -*- mode: python ; coding: utf-8 -*-
# La aplicación de ventana (app.py), sin consola. El modelo de Whisper NO va
# dentro: se descarga a modelos/, junto al .exe. ctranslate2, onnxruntime y av
# traen sus propios hooks.
from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs

a = Analysis(
    ["app.py"],
    binaries=collect_dynamic_libs("ctranslate2"),
    datas=collect_data_files("faster_whisper")  # modelo del VAD (silero, .onnx)
    + [("assets/icono.ico", ".")],               # icono de la ventana
    excludes=["torch", "matplotlib", "IPython", "pytest"],
)
pyz = PYZ(a.pure, a.zipped_data)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="TranscribirAudio",
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
    icon="assets/icono.ico",  # icono del .exe (se regenera con assets/hacer_icono.py)
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="TranscribirAudio",
)
