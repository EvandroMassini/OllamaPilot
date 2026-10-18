from __future__ import annotations

import traceback

from PySide6.QtCore import QThread, Signal

from ollamapilot.agent import Agent, Cancelled
from ollamapilot.logbook import log_error, redact


class AgentWorker(QThread):
    event = Signal(str, object)
    need_approval = Signal(object)
    finished_ok = Signal(str)
    failed = Signal(str)

    def __init__(self, agent: Agent, text: str, attachments: list[dict]) -> None:
        super().__init__()
        self.agent = agent
        self.text = text
        self.attachments = attachments
        self._cancel = False
        self._decision = "deny"
        self._decided = False

    def cancel(self) -> None:
        self._cancel = True

    def submit_decision(self, approved) -> None:
        if approved is True:
            self._decision = "once"
        elif approved is False:
            self._decision = "deny"
        else:
            self._decision = str(approved)
        self._decided = True

    def run(self) -> None:
        try:
            answer = self.agent.turn(
                self.text,
                self.attachments,
                on_event=lambda kind, data: self.event.emit(kind, data),
                approve=self._approve,
                cancel=lambda: self._cancel,
            )
            self.finished_ok.emit(answer)
        except Cancelled:
            self.failed.emit("Interrompido.")
        except Exception as error:
            details = getattr(error, "details", None)
            if not isinstance(details, dict):
                details = {}
            path = log_error(
                "turno",
                str(error),
                pedido=redact(self.text[:300]),
                anexos=[f"{item.get('name')} ({item.get('tipo')})" for item in self.attachments],
                rastreio=traceback.format_exc(),
                **details,
            )
            message = str(error)
            if path is not None:
                message = f"{message}\n\nRegistro: {path}"
            self.failed.emit(message)

    def _approve(self, preview: dict) -> str:
        self._decided = False
        self.need_approval.emit(preview)
        while not self._decided:
            if self._cancel:
                return "deny"
            self.msleep(80)
        return self._decision


class FunctionWorker(QThread):
    succeeded = Signal(object)
    failed = Signal(str)
    progress = Signal(str)

    def __init__(self, function) -> None:
        super().__init__()
        self.function = function

    def run(self) -> None:
        try:
            self.succeeded.emit(self.function(self.progress.emit))
        except Exception as error:
            path = log_error("tarefa", str(error), rastreio=traceback.format_exc())
            message = str(error)
            if path is not None:
                message = f"{message}\n\nRegistro: {path}"
            self.failed.emit(message)
