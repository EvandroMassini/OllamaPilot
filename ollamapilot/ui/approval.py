from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
)


class ApprovalCard(QFrame):
    decided = Signal(str)
    interrupted = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("approval")
        self.hide()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        self.title = QLabel("Autorização")
        self.title.setStyleSheet("font-weight: 700; font-size: 15px;")
        self.summary = QLabel("")
        self.summary.setWordWrap(True)
        self.summary.setObjectName("muted")
        self.detail = QPlainTextEdit()
        self.detail.setReadOnly(True)
        self.detail.setFixedHeight(140)
        self.note = QLabel(
            "Uma vez libera só esta ação. Neste chat a autorização fica salva nesta conversa, até você começar outra."
        )
        self.note.setWordWrap(True)
        self.note.setObjectName("muted")
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        deny = QPushButton("Recusar")
        deny.setObjectName("ghost")
        stop = QPushButton("Interromper")
        stop.setObjectName("ghost")
        once = QPushButton("Autorizar uma vez")
        once.setObjectName("primary")
        chat = QPushButton("Autorizar neste chat")
        chat.setObjectName("chatGrant")
        self.chat_button = chat
        deny.clicked.connect(lambda: self._choose("deny"))
        once.clicked.connect(lambda: self._choose("once"))
        chat.clicked.connect(lambda: self._choose("chat"))
        stop.clicked.connect(self._interrupt)
        buttons.addWidget(deny)
        buttons.addWidget(stop)
        buttons.addWidget(once)
        buttons.addWidget(chat)
        layout.addWidget(self.title)
        layout.addWidget(self.summary)
        layout.addWidget(self.detail)
        layout.addWidget(self.note)
        layout.addLayout(buttons)

    def show_preview(self, preview: dict) -> None:
        self.title.setText(str(preview.get("title") or "Autorização"))
        self.summary.setText(str(preview.get("summary") or ""))
        self.detail.setPlainText(str(preview.get("detail") or ""))
        scope = str(preview.get("escopo") or "").strip()
        if scope:
            self.note.setText(
                f"Uma vez libera só esta ação. Neste chat libera, sem perguntar de novo, {scope} até você começar outra conversa."
            )
            self.chat_button.setToolTip(f"Libera {scope} pelo resto desta conversa.")
        self.show()

    def _choose(self, scope: str) -> None:
        self.hide()
        self.decided.emit(scope)

    def _interrupt(self) -> None:
        self.hide()
        self.interrupted.emit()
