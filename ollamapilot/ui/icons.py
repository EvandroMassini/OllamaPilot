from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap


def sidebar_icon(name: str) -> QIcon:
    painters = {
        "plus": _plus,
        "chat": _chat,
        "history": _history,
        "library": _library,
        "programs": _programs,
        "files": _files,
        "export": _export,
        "models": _models,
        "settings": _settings,
        "undo": _undo,
    }
    return _icon(painters.get(name, _chat))


def paperclip_icon() -> QIcon:
    return _icon(_paperclip)


def _icon(draw) -> QIcon:
    pixmap = QPixmap(18, 18)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(QColor("#c5cedd"))
    pen.setWidthF(1.5)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    draw(painter)
    painter.end()
    return QIcon(pixmap)


def _plus(painter: QPainter) -> None:
    painter.drawLine(9, 4, 9, 14)
    painter.drawLine(4, 9, 14, 9)


def _chat(painter: QPainter) -> None:
    painter.drawRoundedRect(3, 3, 12, 9, 2, 2)
    painter.drawLine(6, 12, 5, 15)
    painter.drawLine(5, 15, 9, 12)


def _history(painter: QPainter) -> None:
    painter.drawEllipse(3, 3, 12, 12)
    painter.drawLine(9, 6, 9, 9)
    painter.drawLine(9, 9, 12, 11)


def _library(painter: QPainter) -> None:
    painter.drawLine(4, 3, 4, 15)
    painter.drawLine(9, 3, 9, 15)
    painter.drawLine(14, 3, 14, 15)
    painter.drawLine(4, 3, 9, 5)
    painter.drawLine(9, 5, 14, 3)


def _programs(painter: QPainter) -> None:
    painter.drawRoundedRect(3, 4, 12, 10, 1, 1)
    painter.drawLine(6, 7, 12, 7)
    painter.drawLine(6, 10, 10, 10)


def _files(painter: QPainter) -> None:
    painter.drawRect(5, 2, 8, 14)
    painter.drawLine(7, 6, 11, 6)
    painter.drawLine(7, 9, 11, 9)
    painter.drawLine(7, 12, 10, 12)


def _export(painter: QPainter) -> None:
    painter.drawLine(9, 3, 9, 12)
    painter.drawLine(5, 9, 9, 13)
    painter.drawLine(13, 9, 9, 13)
    painter.drawLine(4, 15, 14, 15)


def _models(painter: QPainter) -> None:
    painter.drawLine(9, 3, 14, 6)
    painter.drawLine(14, 6, 14, 12)
    painter.drawLine(14, 12, 9, 15)
    painter.drawLine(9, 15, 4, 12)
    painter.drawLine(4, 12, 4, 6)
    painter.drawLine(4, 6, 9, 3)


def _settings(painter: QPainter) -> None:
    painter.drawEllipse(6, 6, 6, 6)
    painter.drawLine(9, 2, 9, 4)
    painter.drawLine(9, 14, 9, 16)
    painter.drawLine(2, 9, 4, 9)
    painter.drawLine(14, 9, 16, 9)
    painter.drawLine(4, 4, 5, 5)
    painter.drawLine(13, 13, 14, 14)
    painter.drawLine(14, 4, 13, 5)
    painter.drawLine(5, 13, 4, 14)


def _undo(painter: QPainter) -> None:
    painter.drawArc(4, 5, 10, 10, 90 * 16, -270 * 16)
    painter.drawLine(9, 5, 12, 2)
    painter.drawLine(9, 5, 12, 8)


def _paperclip(painter: QPainter) -> None:
    painter.drawLine(12, 6, 12, 13)
    painter.drawArc(6, 11, 6, 5, 0, -180 * 16)
    painter.drawLine(6, 13, 6, 6)
    painter.drawArc(6, 3, 5, 6, 0, 180 * 16)
    painter.drawLine(11, 6, 11, 12)
