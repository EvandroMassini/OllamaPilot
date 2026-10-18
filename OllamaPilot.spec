# Pacote de pasta: OllamaPilot.exe ao lado de _internal.
from pathlib import Path

root = Path(SPECPATH)
try:
    import PySide6

    _qt_lang = Path(PySide6.__file__).parent / "translations"
    _qt_lang_data = [
        (str(_qt_lang / name), "PySide6/translations")
        for name in ("qtbase_pt_BR.qm", "qt_pt_BR.qm")
        if (_qt_lang / name).is_file()
    ]
except ImportError:
    _qt_lang_data = []
try:
    import pypdfium2_raw

    _pdfium = Path(pypdfium2_raw.__file__).parent
    _pdfium_bins = [(str(_pdfium / "pdfium.dll"), "pypdfium2_raw")]
    _pdfium_data = [(str(_pdfium / "version.json"), "pypdfium2_raw")]
except ImportError:
    _pdfium_bins = []
    _pdfium_data = []
a = Analysis(
    [str(root / "desktop_entry.py")],
    pathex=[str(root)],
    binaries=_pdfium_bins,
    datas=[
        (str(root / "ollamapilot" / "ghidra_scripts"), "ghidra_scripts"),
        (str(root / "assets" / "ollamapilot.ico"), "assets"),
        *_pdfium_data,
        *_qt_lang_data,
    ],
    hiddenimports=[
        "pefile",
        "psutil",
        "pywinauto",
        "pywinauto.controls.uiawrapper",
        "comtypes",
        "psycopg",
        "fpdf",
        "docx",
        "openpyxl",
        "pypdf",
        "pypdfium2",
        "pypdfium2_raw",
        "ollamapilot.tools.continuity",
        "ollamapilot.tools.gittools",
        "ollamapilot.memory",
        "ollamapilot.workspace",
        "win32cred",
        "win32crypt",
        "ollamapilot.tools.artifacts",
        "ollamapilot.tools.binaryinfo",
        "ollamapilot.tools.database",
        "ollamapilot.tools.desktop",
        "ollamapilot.tools.filesystem",
        "ollamapilot.tools.ghidra_tool",
        "ollamapilot.tools.knowledge",
        "ollamapilot.tools.patching",
        "ollamapilot.tools.planning",
        "ollamapilot.tools.programs",
        "ollamapilot.tools.project",
        "ollamapilot.tools.webtools",
        "ollamapilot.tools.search",
        "ollamapilot.tools.sessions",
        "ollamapilot.tools.shell",
        "ollamapilot.sessions",
        "ollamapilot.process_run",
        "ollamapilot.project_rules",
        "PIL.ImageGrab",
        "ollamapilot.project_index",
        "ollamapilot.secrets",
        "ollamapilot.privacy",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "PySide6.QtWebEngine",
        "PySide6.QtWebEngineCore",
        "PySide6.QtWebEngineWidgets",
        "PySide6.QtPdf",
        "PySide6.QtQml",
        "PySide6.QtQuick",
        "PySide6.Qt3DCore",
        "PySide6.QtMultimedia",
        "PySide6.QtCharts",
        "PySide6.QtBluetooth",
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
    name="OllamaPilot",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    icon=str(root / "assets" / "ollamapilot.ico"),
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="OllamaPilot")
