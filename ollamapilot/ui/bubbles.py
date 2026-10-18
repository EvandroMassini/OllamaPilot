from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QPainter, QPen, QTextOption
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QSizePolicy, QTextBrowser, QVBoxLayout, QWidget


class BusyMark(QWidget):
    """Arco que gira enquanto o agente espera o modelo, um programa ou outra resposta longa."""

    def __init__(self) -> None:
        super().__init__()
        self._angle = 0
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self.setFixedSize(16, 16)
        self.hide()

    def set_active(self, active: bool) -> None:
        if active:
            if not self._timer.isActive():
                self._timer.start(70)
            self.show()
            return
        self._timer.stop()
        self.hide()

    def _tick(self) -> None:
        self._angle = (self._angle + 28) % 360
        self.update()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.translate(8, 8)
        painter.rotate(self._angle)
        pen = QPen(QColor("#8b7cff"))
        pen.setWidth(2)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.drawArc(-5, -5, 10, 10, 30 * 16, 280 * 16)
        painter.end()


class MessageBubble(QWidget):
    def __init__(self, role: str, text: str = "") -> None:
        super().__init__()
        self.role = role
        self._markdown = text
        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 6, 0, 6)
        column = QVBoxLayout()
        column.setSpacing(4)
        if role == "assistant":
            avatar = QLabel("OP")
            avatar.setFixedSize(30, 30)
            avatar.setAlignment(Qt.AlignmentFlag.AlignCenter)
            avatar.setStyleSheet("background:#171d2a;border-radius:9px;font-weight:700;font-size:10px;")
            outer.addWidget(avatar, 0, Qt.AlignmentFlag.AlignTop)
        else:
            outer.addStretch(1)
        self.body = _message_browser()
        if role == "user":
            self.body.setStyleSheet(
                "QTextBrowser { background:#5b4ee8; color:white; border:none; border-radius:15px; }"
            )
            self.body.setPlainText(text)
        else:
            self.body.setStyleSheet(
                "QTextBrowser { background:#121722; color:#f2f5fa; border:1px solid #252d3d; border-radius:15px; }"
            )
            self.body.setMarkdown(text)
        self.body.document().contentsChanged.connect(self.fit)
        self.thinking = QTextBrowser()
        self.thinking.hide()
        self.thinking.setStyleSheet(
            "QTextBrowser { background:#0d111a; color:#929db0; border:1px solid #252d3d; border-radius:10px; padding:6px; }"
        )
        self.think_button = QPushButton("Raciocínio")
        self.think_button.setObjectName("ghost")
        self.think_button.hide()
        self.think_button.clicked.connect(self._toggle_thinking)
        self.activity_mark = BusyMark()
        self.activity_label = QLabel("")
        self.activity_label.setObjectName("muted")
        self.activity = QWidget()
        activity_row = QHBoxLayout(self.activity)
        activity_row.setContentsMargins(4, 0, 0, 0)
        activity_row.setSpacing(8)
        activity_row.addWidget(self.activity_mark)
        activity_row.addWidget(self.activity_label, 1)
        self.activity.hide()
        self.trace = QLabel("")
        self.trace.setWordWrap(True)
        self.trace.setObjectName("muted")
        self.trace.hide()
        self.metrics = QLabel("")
        self.metrics.setObjectName("muted")
        self.metrics.hide()
        copy = QPushButton("Copiar")
        copy.setObjectName("ghost")
        copy.clicked.connect(self.copy_text)
        header = QHBoxLayout()
        header.addWidget(self.think_button)
        header.addStretch(1)
        header.addWidget(copy)
        column.addLayout(header)
        column.addWidget(self.activity)
        column.addWidget(self.thinking)
        column.addWidget(self.body)
        column.addWidget(self.trace)
        column.addWidget(self.metrics)
        host = QWidget()
        host.setLayout(column)
        host.setMinimumWidth(0)
        host.setMaximumWidth(760)
        host.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        outer.addWidget(host, 8 if role == "user" else 1)
        if role == "assistant":
            outer.addStretch(0)
        self.body.setMinimumWidth(0)
        self.body.setMaximumWidth(740)

    def set_activity(self, text: str) -> None:
        if self.role != "assistant":
            return
        self.activity_label.setText(text)
        self.activity_mark.set_active(True)
        self.activity.show()

    def stop_activity(self) -> None:
        self.activity_mark.set_active(False)
        self.activity.hide()

    def clear_content(self) -> None:
        self._markdown = ""
        self.body.setPlainText("")
        self.fit()

    def append_content(self, piece: str) -> None:
        self._markdown += piece
        if self.role == "user":
            self.body.setPlainText(self._markdown)
        else:
            self.body.setMarkdown(self._markdown)
        self.fit()

    def append_thinking(self, piece: str) -> None:
        current = self.thinking.toPlainText() + piece
        self.thinking.setPlainText(current)
        self.think_button.show()
        self.thinking.setFixedHeight(min(160, 40 + current.count("\n") * 16))

    def add_trace(self, entry: dict) -> None:
        status = entry.get("status")
        color = {"success": "#41d49b", "error": "#ff6b81", "denied": "#ff6b81"}.get(status, "#929db0")
        line = f"{entry.get('tool')} · {entry.get('resumo') or status} · {entry.get('ms', 0)} ms"
        current = self.trace.text()
        self.trace.setText((current + "<br>" if current else "") + f'<span style="color:{color}">{line}</span>')
        self.trace.show()

    def set_metrics(self, metrics: dict) -> None:
        text = format_metrics(metrics)
        if not text:
            return
        self.metrics.setText(text)
        self.metrics.show()

    def copy_text(self) -> None:
        self.body.selectAll()
        self.body.copy()
        cursor = self.body.textCursor()
        cursor.clearSelection()
        self.body.setTextCursor(cursor)

    def fit(self) -> None:
        document = self.body.document()
        width = self.body.viewport().width()
        if width < 48:
            width = max(48, self.width() - 36)
        document.setTextWidth(width)
        height = int(document.size().height()) + 2
        self.body.setFixedHeight(max(44, height))

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.fit()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.fit()

    def _toggle_thinking(self) -> None:
        self.thinking.setVisible(not self.thinking.isVisible())

    def plain(self) -> str:
        return self._markdown


def _message_browser() -> QTextBrowser:
    """Quebra no limite do balão, inclusive caminhos longos, sem cortar a linha."""
    browser = QTextBrowser()
    browser.setOpenExternalLinks(True)
    browser.setFrameShape(QTextBrowser.Shape.NoFrame)
    browser.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    browser.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    browser.setLineWrapMode(QTextBrowser.LineWrapMode.WidgetWidth)
    browser.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
    browser.document().setDocumentMargin(12)
    option = QTextOption(browser.document().defaultTextOption())
    option.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
    browser.document().setDefaultTextOption(option)
    return browser


def format_metrics(metrics: dict) -> str:
    if not metrics:
        return ""
    model = metrics.get("model") or ""
    count = metrics.get("eval_count") or 0
    duration = metrics.get("eval_duration") or 0
    total = metrics.get("total_duration") or 0
    parts = [part for part in (model,) if part]
    if count:
        parts.append(f"{count} tokens")
    if duration:
        parts.append(f"{count / (duration / 1e9):.1f} tok/s" if count else "")
        parts = [part for part in parts if part]
    if total:
        parts.append(f"{total / 1e9:.1f} s")
    return " · ".join(parts)
