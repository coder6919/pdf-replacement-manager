# PyInstaller spec — one-folder, windowed build. Run via packaging\build.ps1.
# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path

ROOT = Path(SPECPATH).parent

EXCLUDES = [
    # pulled in by PyMuPDF 2.x extras we do not use
    "onnxruntime", "pymupdf4llm", "pymupdf_layout", "networkx", "tabulate", "yaml",
    # unused Qt modules
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick", "PySide6.Qt3DCore",
    "PySide6.QtQuick", "PySide6.QtQml", "PySide6.QtMultimedia", "PySide6.QtCharts", "PySide6.QtDataVisualization",
    "PySide6.QtPdf", "PySide6.QtPdfWidgets", "PySide6.QtBluetooth", "PySide6.QtPositioning", "PySide6.QtSql",
    "PySide6.QtTest", "PySide6.QtDesigner", "PySide6.QtQuick3D", "PySide6.QtGraphs",
    "tkinter", "pytest", "pytestqt",
]

a = Analysis(
    [str(ROOT / "packaging" / "launcher.py")],
    pathex=[str(ROOT)],
    hiddenimports=["prm.ui.main_window", "pikepdf._core", "watchdog.observers.read_directory_changes"],
    excludes=EXCLUDES,
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="PDFReplacementManager",
    console=False,
    icon=str(ROOT / "build" / "icon.ico"),
    version=str(ROOT / "packaging" / "version_info.txt"),
)
coll = COLLECT(exe, a.binaries, a.datas, name="PDFReplacementManager")
