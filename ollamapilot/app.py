from __future__ import annotations

import os
import sys
from pathlib import Path

from PySide6.QtCore import QLibraryInfo, QLocale, QTranslator
from PySide6.QtGui import QColor, QFont, QIcon, QPalette
from PySide6.QtWidgets import QApplication

from ollamapilot.config import Settings
from ollamapilot.paths import ICON, ensure_storage
from ollamapilot.ui.main_window import MainWindow
from ollamapilot.ui.theme import STYLESHEET


def _install_portuguese(app: QApplication) -> None:
    """Botões padrão do Qt (Sim, Não, OK, Cancelar) em português do Brasil."""
    QLocale.setDefault(QLocale(QLocale.Language.Portuguese, QLocale.Country.Brazil))
    folders = [QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)]
    if getattr(sys, "frozen", False):
        folders.append(str(Path(sys.executable).resolve().parent / "_internal" / "PySide6" / "translations"))
    app._pt_translators = []
    for folder in folders:
        for name in ("qtbase_pt_BR", "qt_pt_BR"):
            translator = QTranslator(app)
            if translator.load(name, folder):
                app.installTranslator(translator)
                app._pt_translators.append(translator)


def main() -> int:
    ensure_storage()
    app = QApplication(sys.argv)
    _install_portuguese(app)
    app.setApplicationName("OllamaPilot")
    app.setApplicationDisplayName("OllamaPilot")
    app.setStyle("Fusion")
    app.setFont(QFont("Segoe UI", 10))
    app.setStyleSheet(STYLESHEET)
    if ICON.is_file():
        app.setWindowIcon(QIcon(str(ICON)))
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor("#121722"))
    palette.setColor(QPalette.ColorRole.WindowText, QColor("#f2f5fa"))
    palette.setColor(QPalette.ColorRole.Base, QColor("#0d111a"))
    palette.setColor(QPalette.ColorRole.Text, QColor("#f2f5fa"))
    palette.setColor(QPalette.ColorRole.Button, QColor("#171d2a"))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor("#f2f5fa"))
    palette.setColor(QPalette.ColorRole.Highlight, QColor("#5b4ee8"))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
    app.setPalette(palette)
    window = MainWindow(Settings.load())
    window.show()
    if os.environ.get("OLLAMAPILOT_SELFTEST") == "1":
        from PySide6.QtCore import QTimer

        QTimer.singleShot(600, window.selftest_and_quit)
    return app.exec()
