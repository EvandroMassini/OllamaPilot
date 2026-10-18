STYLESHEET = """
* { font-family: "Segoe UI"; color: #f2f5fa; }
QMainWindow, QWidget#root { background: #0a0d14; }
QWidget#sidebar { background: #0d111a; border-right: 1px solid #252d3d; }
QLabel#brandName { font-size: 15px; font-weight: 700; }
QLabel#brandSub, QLabel#muted, QLabel#hint { color: #929db0; }
QLabel#brandMark, QLabel#heroMark {
    background: #7c6cff;
    color: white;
    border-radius: 11px;
    font-weight: 800;
    font-size: 12px;
}
QLabel#heroMark { border-radius: 18px; font-size: 18px; }
QLabel#heroTitle { font-size: 28px; font-weight: 700; }
QFrame#connection {
    background: #121722;
    border: 1px solid #252d3d;
    border-radius: 12px;
}
QLabel#dot { background: #687086; border-radius: 5px; }
QLabel#dot[online="true"] { background: #41d49b; }
QLabel#dot[online="false"] { background: #ff6b81; }
QPushButton {
    background: transparent;
    border: 1px solid transparent;
    border-radius: 11px;
    padding: 8px 12px;
    text-align: left;
    color: #b9c2d2;
}
QTabWidget::pane {
    border: 1px solid #252d3d;
    background: #0a0d14;
    border-radius: 0 8px 8px 8px;
}
QTabBar::tab {
    background: #121722;
    color: #b9c2d2;
    padding: 8px 16px;
    margin-right: 4px;
    border: 1px solid #252d3d;
    border-bottom: none;
    border-top-left-radius: 8px;
    border-top-right-radius: 8px;
}
QTabBar::tab:selected { background: #1c2433; color: #f2f5fa; }
QScrollBar:vertical {
    background: #121722;
    width: 12px;
    margin: 2px;
    border-radius: 6px;
}
QScrollBar::handle:vertical {
    background: #9aa6bd;
    min-height: 36px;
    border-radius: 5px;
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
QPushButton:hover { background: #171d2a; color: #f2f5fa; }
QPushButton#newChat { border: 1px solid #252d3d; color: #f2f5fa; }
QPushButton#active { background: #171d2a; color: #f2f5fa; }
QPushButton#primary, QPushButton#send {
    background: #7c6cff;
    color: white;
    border: none;
    border-radius: 10px;
    padding: 8px 14px;
    text-align: center;
    font-weight: 600;
}
QPushButton#send { border-radius: 12px; font-size: 16px; padding: 0; }
QPushButton#primary:hover, QPushButton#send:hover { background: #5b4ee8; }
QPushButton#chatGrant {
    background: #1c2433;
    color: #d7ccff;
    border: 1px solid #7c6cff;
    border-radius: 10px;
    padding: 8px 14px;
    text-align: center;
    font-weight: 600;
}
QPushButton#chatGrant:hover { background: #2a3350; }
QPushButton#ghost { background: #171d2a; color: #f2f5fa; text-align: center; }
QPushButton#icon { padding: 6px 8px; text-align: center; color: #aab4c4; }
QPushButton#suggest {
    background: #121722;
    border: 1px solid #252d3d;
    border-radius: 13px;
    padding: 12px;
    text-align: left;
    color: #dce2ed;
}
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QPlainTextEdit, QListWidget {
    background: #121722;
    border: 1px solid #252d3d;
    border-radius: 10px;
    padding: 6px 8px;
    selection-background-color: #5b4ee8;
}
QComboBox::drop-down { border: none; width: 22px; }
QSlider::groove:horizontal { height: 6px; background: #252d3d; border-radius: 3px; }
QSlider::sub-page:horizontal { background: #5b4ee8; border-radius: 3px; }
QSlider::handle:horizontal {
    width: 16px;
    margin: -6px 0;
    background: #7c6cff;
    border-radius: 8px;
}
QPlainTextEdit#composer, QPlainTextEdit#statusLine {
    background: transparent;
    border: none;
    padding: 4px 2px;
}
QPlainTextEdit#statusLine { color: #929db0; padding: 0; }
QPlainTextEdit#composer QScrollBar:vertical {
    background: #1a2130;
    width: 8px;
    margin: 2px 1px 2px 0;
}
QPlainTextEdit#composer QScrollBar::handle:vertical {
    background: #9aa6bd;
    border-radius: 4px;
    min-height: 28px;
}
QPlainTextEdit#composer QScrollBar::add-page:vertical,
QPlainTextEdit#composer QScrollBar::sub-page:vertical { background: transparent; }
QPlainTextEdit#composer QScrollBar::add-line:vertical,
QPlainTextEdit#composer QScrollBar::sub-line:vertical { height: 0; }
QFrame#composerBox {
    background: #121722;
    border: 1px solid #252d3d;
    border-radius: 18px;
}
QFrame#approval {
    background: #121722;
    border: 1px solid #7c6cff;
    border-radius: 14px;
}
QTextBrowser { border: none; background: transparent; }
QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; border: none; }
QScrollBar:vertical { background: transparent; width: 10px; margin: 4px; }
QScrollBar::handle:vertical { background: #252d3d; border-radius: 4px; min-height: 24px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QDialog { background: #121722; }
QGroupBox { border: 1px solid #252d3d; border-radius: 10px; margin-top: 12px; padding: 8px; }
QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px; color: #929db0; }
QCheckBox { spacing: 8px; }
"""
