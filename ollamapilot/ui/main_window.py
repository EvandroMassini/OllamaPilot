from __future__ import annotations

import json
import os

from PySide6.QtCore import QEvent, QSize, Qt, QTimer
from PySide6.QtGui import QIcon, QKeyEvent
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ollamapilot import history
from ollamapilot.agent import Agent, repair_transcript
from ollamapilot.attachments import describe as describe_attachment
from ollamapilot.attachments import read_attachment
from ollamapilot.logbook import log_error
from ollamapilot.ollama_client import OllamaClient, describe_balance, describe_capabilities
from ollamapilot.paths import ICON, SESSION_FILE, park_invalid, write_text_atomic
from ollamapilot.ui.approval import ApprovalCard
from ollamapilot.ui.bubbles import BusyMark, MessageBubble
from ollamapilot.ui.icons import paperclip_icon, sidebar_icon
from ollamapilot.ui.dialogs import ArtifactsDialog, HistoryDialog, KnowledgeDialog, ModelsDialog, ProgramsDialog, SettingsDialog
from ollamapilot.ui.workers import AgentWorker, FunctionWorker


class Composer(QWidget):
    def __init__(self, owner: "MainWindow") -> None:
        super().__init__()
        self.owner = owner
        from PySide6.QtWidgets import QPlainTextEdit

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 8, 18, 12)
        self.box = QFrame()
        self.box.setObjectName("composerBox")
        row = QHBoxLayout(self.box)
        row.setContentsMargins(8, 6, 8, 6)
        attach = QPushButton("Anexar")
        attach.setObjectName("ghost")
        attach.setIcon(paperclip_icon())
        attach.setIconSize(QSize(16, 16))
        attach.clicked.connect(owner.attach_files)
        self.input = QPlainTextEdit()
        self.input.setObjectName("composer")
        self.input.setPlaceholderText("Envie uma mensagem…")
        self.input.setFixedHeight(44)
        self.input.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.input.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.input.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.input.installEventFilter(owner)
        self.input.textChanged.connect(self._grow)
        self._fitting = False
        self.send = QPushButton("↑")
        self.send.setObjectName("send")
        self.send.setFixedSize(40, 40)
        self.send.clicked.connect(owner.on_send_clicked)
        row.addWidget(attach, 0, Qt.AlignmentFlag.AlignBottom)
        row.addWidget(self.input, 1)
        row.addWidget(self.send, 0, Qt.AlignmentFlag.AlignBottom)
        hint_row = QHBoxLayout()
        hint_row.setSpacing(8)
        self.busy = BusyMark()
        self.hint = QLabel("Enter envia · Shift+Enter quebra a linha · o caminho dos arquivos sai da conversa")
        self.hint.setObjectName("hint")
        self.hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint_row.addStretch(1)
        hint_row.addWidget(self.busy)
        hint_row.addWidget(self.hint)
        hint_row.addStretch(1)
        layout.addWidget(self.box)
        layout.addLayout(hint_row)

    def _grow(self) -> None:
        if self._fitting:
            return
        self._fitting = True
        try:
            metrics = self.input.fontMetrics()
            line_h = max(metrics.lineSpacing(), 18)
            document = self.input.document()
            document.setTextWidth(max(1, self.input.viewport().width() - 12))
            content = max(document.size().height(), document.blockCount() * line_h)
            needed = int(content + 28)
            height = min(180, max(48, needed))
            self.input.setFixedHeight(height)
            overflow = content > self.input.viewport().height() - 4
            self.input.setVerticalScrollBarPolicy(
                Qt.ScrollBarPolicy.ScrollBarAlwaysOn if overflow else Qt.ScrollBarPolicy.ScrollBarAlwaysOff
            )
            self.input.ensureCursorVisible()
        finally:
            self._fitting = False

    def set_running(self, running: bool) -> None:
        self.send.setText("■" if running else "↑")
        self.busy.set_active(running)
        self.hint.setText("Consultando o modelo… você pode interromper." if running else self.idle_hint())

    def show_activity(self, text: str) -> None:
        self.busy.set_active(True)
        self.hint.setText(text)

    def idle_hint(self) -> str:
        root = getattr(self.owner.agent, "workspace", None)
        folder = f" · pasta {root}" if root else " · indique a pasta de trabalho para gravar"
        if self.owner.settings.model_remote:
            return "Modelo online: a conversa e os anexos vão para ollama.com · Enter envia · Shift+Enter quebra a linha" + folder
        return "Enter envia · Shift+Enter quebra a linha" + folder


class MainWindow(QMainWindow):
    def __init__(self, settings) -> None:
        super().__init__()
        self.settings = settings
        self.client = OllamaClient(settings)
        self.agent = Agent(settings, self.client)
        self.conversation_id = history.new_id()
        self.attachments: list[dict] = []
        self.display: list[dict] = []
        self.worker: AgentWorker | None = None
        self._jobs: list[FunctionWorker] = []
        self._orphans: list[AgentWorker] = []
        self._assistant: MessageBubble | None = None
        self._stick = True
        self._route = "Sem conexão"
        self.setWindowTitle("OllamaPilot")
        self.resize(1180, 760)
        if ICON.is_file():
            self.setWindowIcon(QIcon(str(ICON)))
        self._build()
        self._load_session()
        if os.environ.get("OLLAMAPILOT_SELFTEST") != "1":
            QTimer.singleShot(200, self.refresh_connection)

    def _build(self) -> None:
        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        outer = QHBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._sidebar())
        main = QWidget()
        column = QVBoxLayout(main)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(0)
        column.addWidget(self._topbar())
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.feed_host = QWidget()
        self.feed = QVBoxLayout(self.feed_host)
        self.feed.setContentsMargins(28, 12, 28, 12)
        self.feed.setSpacing(0)
        self.welcome = self._welcome()
        self.feed.addWidget(self.welcome)
        self.feed.addStretch(1)
        self.scroll.setWidget(self.feed_host)
        self.scroll.verticalScrollBar().valueChanged.connect(self._remember_stick)
        column.addWidget(self.scroll, 1)
        self.approval = ApprovalCard()
        self.approval.decided.connect(self._decided)
        self.approval.interrupted.connect(self.stop)
        approval_wrap = QHBoxLayout()
        approval_wrap.setContentsMargins(18, 0, 18, 0)
        approval_wrap.addWidget(self.approval)
        column.addLayout(approval_wrap)
        self.chips = QLabel("")
        self.chips.setObjectName("muted")
        self.chips.setContentsMargins(22, 0, 22, 0)
        column.addWidget(self.chips)
        self.composer = Composer(self)
        column.addWidget(self.composer)
        outer.addWidget(main, 1)
        self.toast = QLabel(self)
        self.toast.hide()
        self.toast.setStyleSheet("background:#202738;border:1px solid #3b4559;border-radius:11px;padding:10px 14px;")

    def _sidebar(self) -> QWidget:
        side = QWidget()
        side.setObjectName("sidebar")
        side.setFixedWidth(260)
        layout = QVBoxLayout(side)
        layout.setContentsMargins(14, 18, 14, 14)
        brand = QHBoxLayout()
        mark = QLabel("OP")
        mark.setObjectName("brandMark")
        mark.setFixedSize(36, 36)
        mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        text = QVBoxLayout()
        name = QLabel("OllamaPilot")
        name.setObjectName("brandName")
        sub = QLabel("Agente local")
        sub.setObjectName("brandSub")
        text.addWidget(name)
        text.addWidget(sub)
        brand.addWidget(mark)
        brand.addLayout(text)
        layout.addLayout(brand)
        new_chat = QPushButton("Nova conversa")
        new_chat.setObjectName("newChat")
        new_chat.setIcon(sidebar_icon("plus"))
        new_chat.setIconSize(QSize(18, 18))
        new_chat.clicked.connect(self.new_chat)
        layout.addWidget(new_chat)
        choose = QPushButton("Pasta de trabalho")
        choose.clicked.connect(self.choose_workspace)
        layout.addWidget(choose)
        self.folder_label = QLabel("Nenhuma pasta nesta conversa")
        self.folder_label.setObjectName("muted")
        self.folder_label.setWordWrap(True)
        layout.addWidget(self.folder_label)
        items = (
            ("chat", "Conversa", None),
            ("history", "Conversas anteriores", self.open_history),
            ("library", "Biblioteca (RAG)", self.open_knowledge),
            ("programs", "Programas", self.open_programs),
            ("files", "Arquivos gerados", self.open_artifacts),
            ("export", "Exportar conversa", self.export_chat),
            ("undo", "Desfazer último turno", self.undo_last_turn),
            ("models", "Modelos", self.open_models),
            ("settings", "Configurações", self.open_settings),
        )
        for name, label, slot in items:
            button = QPushButton(label)
            button.setIcon(sidebar_icon(name))
            button.setIconSize(QSize(18, 18))
            if slot is None:
                button.setObjectName("active")
            else:
                button.clicked.connect(slot)
            layout.addWidget(button)
        layout.addStretch(1)
        card = QFrame()
        card.setObjectName("connection")
        row = QHBoxLayout(card)
        self.dot = QLabel("")
        self.dot.setObjectName("dot")
        self.dot.setFixedSize(10, 10)
        text = QVBoxLayout()
        self.status = QLabel("Verificando…")
        self.server = QLabel(self._server_text())
        self.server.setObjectName("muted")
        text.addWidget(self.status)
        text.addWidget(self.server)
        row.addWidget(self.dot)
        row.addLayout(text)
        layout.addWidget(card)
        return side

    def _topbar(self) -> QWidget:
        bar = QWidget()
        bar.setFixedHeight(64)
        bar.setStyleSheet("border-bottom: 1px solid #252d3d;")
        row = QHBoxLayout(bar)
        row.setContentsMargins(22, 0, 22, 0)
        label = QLabel("Modelo")
        label.setObjectName("muted")
        self.models = QComboBox()
        self.models.setMinimumWidth(360)
        self.models.currentIndexChanged.connect(self._model_changed)
        refresh = QPushButton("↻")
        refresh.setObjectName("icon")
        refresh.setFixedSize(36, 36)
        refresh.clicked.connect(self.refresh_connection)
        balance = QPushButton("Saldo")
        balance.setObjectName("ghost")
        balance.setFixedHeight(36)
        balance.clicked.connect(self.show_balance)
        balance.setVisible(self.settings.online_models())
        self.balance_button = balance
        settings = QPushButton("⚙")
        settings.setObjectName("icon")
        settings.setFixedSize(36, 36)
        settings.clicked.connect(self.open_settings)
        self.caps_label = QLabel("")
        self.caps_label.setObjectName("muted")
        row.addWidget(label)
        row.addWidget(self.models)
        row.addWidget(self.caps_label)
        row.addStretch(1)
        row.addWidget(refresh)
        row.addWidget(balance)
        row.addWidget(settings)
        return bar

    def _welcome(self) -> QWidget:
        host = QWidget()
        layout = QVBoxLayout(host)
        layout.addStretch(1)
        mark = QLabel("OP")
        mark.setObjectName("heroMark")
        mark.setFixedSize(58, 58)
        mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title = QLabel("Como posso ajudar?")
        title.setObjectName("heroTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        text = QLabel(
            "Converse com o modelo no Ollama. Diga o caminho na mensagem para criar pastas e arquivos. "
            "O OllamaPilot lê binários, executa programas instalados, interage com as janelas deles e usa o Ghidra para uma análise de sala limpa."
        )
        text.setWordWrap(True)
        text.setAlignment(Qt.AlignmentFlag.AlignCenter)
        text.setObjectName("muted")
        text.setFixedWidth(620)
        text.setMinimumHeight(72)
        grid_host = QWidget()
        grid_host.setFixedWidth(640)
        grid = QGridLayout(grid_host)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(10)
        prompts = (
            "Analise o executável que eu indicar, confirme o meu direito sobre ele e escreva a especificação de sala limpa.",
            "Crie a pasta e os arquivos de um programa novo no caminho absoluto que eu disser.",
            "Liste as janelas abertas nesta máquina.",
            "Leia o arquivo de código cujo caminho eu informar e explique o que ele faz.",
        )
        labels = (
            "Sala limpa com o Ghidra",
            "Criar arquivos num caminho",
            "Ver janelas abertas",
            "Ler um arquivo",
        )
        for index, (label, prompt) in enumerate(zip(labels, prompts)):
            button = QPushButton(label)
            button.setObjectName("suggest")
            button.setMinimumHeight(48)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(lambda _checked=False, text=prompt: self._use_prompt(text))
            grid.addWidget(button, index // 2, index % 2)
        layout.addWidget(mark, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(title, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(text, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addSpacing(8)
        layout.addWidget(grid_host, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addStretch(1)
        return host

    def _use_prompt(self, text: str) -> None:
        self.composer.input.setPlainText(text)
        self.composer.input.setFocus()

    def _server_text(self) -> str:
        if getattr(self.settings, "provider", "ollama") == "openai":
            return self.settings.openai_url
        return self.settings.ollama_url

    def refresh_connection(self) -> None:
        self.server.setText(self._server_text())
        self.balance_button.setVisible(self.settings.online_models())
        self._start_job(lambda _progress: self.client.available_models(), self._apply_connection, self._connection_failed)

    def _apply_connection(self, payload: dict) -> None:
        if isinstance(payload, list):
            payload = {"models": payload}
        local = payload.get("models") or []
        remote = (payload.get("remote") or []) if self.settings.online_models() else []
        if local:
            self._route = "Conectado"
        elif remote:
            self._route = "Nuvem"
        else:
            self._route = "Sem conexão"
        self._show_route()
        current_name = self.settings.model or ""
        current_remote = bool(self.settings.model_remote)
        self.models.blockSignals(True)
        self.models.clear()
        chosen = -1
        for model in local:
            name = str(model.get("name") or "")
            if not name:
                continue
            self._add_model(name, name, False)
            if not current_remote and name == current_name:
                chosen = self.models.count() - 1
        for model in remote:
            name = str(model.get("name") or "")
            if not name:
                continue
            self._add_model(f"{name}  · online", name, True)
            if current_remote and name == current_name:
                chosen = self.models.count() - 1
        if chosen < 0:
            # Sem escolha anterior válida, só um modelo local é selecionado sozinho. A nuvem exige escolha explícita.
            chosen = next(
                (i for i in range(self.models.count()) if not self.models.itemData(i, Qt.ItemDataRole.UserRole + 1)),
                -1,
            )
            if chosen < 0 and self.models.count():
                self.models.setCurrentIndex(-1)
                self.settings.model_remote = False
                self.settings.model = ""
                self.settings.save()
                self.notify("Só há modelos online. Escolha um na lista para enviar a conversa a ollama.com.")
        if chosen >= 0:
            self.models.setCurrentIndex(chosen)
            self._store_model(chosen)
        elif current_remote:
            self.settings.model_remote = False
            self.settings.save()
        self.models.blockSignals(False)
        local_error = str(payload.get("local_error") or "")
        remote_error = str(payload.get("remote_error") or "")
        if local_error and not local:
            self.notify(local_error, error=True)
        if remote_error:
            self.notify(remote_error, error=True)
        self._show_route()
        self._refresh_capabilities()

    def _refresh_capabilities(self) -> None:
        name = self.settings.model
        remote = bool(self.settings.model_remote)
        self.composer.hint.setText(self.composer.idle_hint())
        if not name:
            self.caps_label.setText("")
            return
        self.caps_label.setText("verificando capacidades…")

        def done(caps: dict) -> None:
            if self.settings.model == name and bool(self.settings.model_remote) == remote:
                self.caps_label.setText(describe_capabilities(caps, remote))
                self.caps_label.setToolTip(str(caps.get("erro") or caps.get("fonte") or ""))

        self._start_job(lambda _progress: self.client.capabilities(name, remote), done, lambda _message: self.caps_label.setText(""))

    def _add_model(self, label: str, name: str, remote: bool) -> None:
        self.models.addItem(label, name)
        self.models.setItemData(self.models.count() - 1, remote, Qt.ItemDataRole.UserRole + 1)

    def _store_model(self, index: int) -> None:
        name = self.models.itemData(index)
        remote = bool(self.models.itemData(index, Qt.ItemDataRole.UserRole + 1))
        if not name:
            return
        self.settings.model = str(name)
        self.settings.model_remote = remote
        self.settings.save()

    def show_balance(self) -> None:
        if not self.settings.cloud_api_key.strip():
            self.notify("Cadastre a API Key para consultar o saldo.", error=True)
            return
        model = str(self.models.currentData() or self.settings.model or "")
        remote = bool(self.settings.model_remote)

        def done(payload: dict) -> None:
            box = QMessageBox(self)
            box.setWindowTitle("Saldo")
            box.setIcon(QMessageBox.Icon.Information)
            box.setText(describe_balance(payload, model, remote))
            box.exec()

        self._start_job(lambda _progress: self.client.cloud_balance(), done, lambda message: self.notify(message, error=True))

    def _connection_failed(self, message: str) -> None:
        self._set_online(False, "Sem conexão")
        self.notify(message, error=True)

    def _set_online(self, online: bool, text: str) -> None:
        self.status.setText(text)
        self.dot.setProperty("online", online)
        self.dot.style().unpolish(self.dot)
        self.dot.style().polish(self.dot)

    def _model_changed(self) -> None:
        index = self.models.currentIndex()
        if index >= 0:
            self._store_model(index)
            self._show_route()
            self._refresh_capabilities()

    def _show_route(self) -> None:
        if self.settings.model_remote:
            self._set_online(True, "Nuvem")
            return
        self._set_online(self._route != "Sem conexão", self._route)

    def new_chat(self) -> None:
        if self.worker and self.worker.isRunning():
            self._abandon_worker()
            self.agent = Agent(self.settings, self.client)
        self.agent.reset()
        self.display = []
        self.attachments = []
        self._assistant = None
        self._clear_feed()
        self.welcome.show()
        self.chips.setText("")
        self.conversation_id = history.new_id()
        self._show_workspace()
        self._save_session()

    def _abandon_worker(self) -> None:
        """O turno em andamento termina sozinho no agente antigo, sem tocar na conversa nova."""
        worker = self.worker
        self.worker = None
        if worker is None:
            return
        for signal in (worker.event, worker.need_approval, worker.finished_ok, worker.failed):
            try:
                signal.disconnect()
            except (RuntimeError, TypeError):
                pass
        self.approval.hide()
        worker.cancel()
        worker.submit_decision("deny")
        self._orphans.append(worker)
        worker.finished.connect(lambda: self._orphans.remove(worker) if worker in self._orphans else None)
        self.composer.set_running(False)

    def undo_last_turn(self) -> None:
        from ollamapilot import undo

        if self.worker and self.worker.isRunning():
            self.notify("Interrompa a resposta atual antes de desfazer.", error=True)
            return
        transcript = self.agent.transcript
        index = next(
            (i for i in range(len(transcript) - 1, -1, -1) if transcript[i].get("role") == "user" and transcript[i].get("turno")),
            None,
        )
        if index is None:
            self.notify("Não há turno para desfazer nesta conversa.")
            return
        turn_id = str(transcript[index]["turno"])
        changes = undo.turn_changes(turn_id)
        text = "A conversa volta para antes do último pedido, e o texto dele volta para a caixa de mensagem."
        if changes:
            text += f" {changes} arquivo(s) alterado(s) nesse turno voltam ao estado anterior."
        else:
            text += " Nenhum arquivo foi alterado nesse turno."
        answer = QMessageBox.question(self, "Desfazer último turno", text)
        if answer != QMessageBox.StandardButton.Yes:
            return
        restored: list = []
        if changes:
            try:
                restored = undo.restore_turn(turn_id) or []
            except (OSError, ValueError) as error:
                self.notify(f"Os arquivos não voltaram: {error}", error=True)
                return
        request = str(transcript[index].get("content") or "")
        from ollamapilot.agent import _ATTACHMENTS_MARKER

        request = request.split(_ATTACHMENTS_MARKER, 1)[0].rstrip()
        self.agent.transcript = transcript[:index]
        self.agent.used_groups = set()
        last_user = next((i for i in range(len(self.display) - 1, -1, -1) if self.display[i].get("role") == "user"), None)
        if last_user is not None:
            self.display = self.display[:last_user]
        self._assistant = None
        self._clear_feed()
        self.welcome.show()
        self._render_display()
        self.composer.input.setPlainText(request)
        self._save_session()
        self.notify(f"Turno desfeito. {len(restored)} arquivo(s) restaurado(s)." if changes else "Turno desfeito.")

    def choose_workspace(self) -> None:
        from pathlib import Path

        folder = QFileDialog.getExistingDirectory(self, "Pasta de trabalho")
        if not folder:
            return
        self.agent.workspace = Path(folder)
        self._show_workspace()
        self.composer.hint.setText(self.composer.idle_hint())
        self._save_session()

    def _show_workspace(self) -> None:
        label = getattr(self, "folder_label", None)
        if label is None:
            return
        root = self.agent.workspace
        label.setText(str(root) if root else "Nenhuma pasta nesta conversa")

    def open_history(self) -> None:
        HistoryDialog(self).exec()

    def open_conversation(self, conversation_id: str) -> bool:
        if self.worker and self.worker.isRunning():
            self.notify("Interrompa a resposta atual antes de abrir outra conversa.", error=True)
            return False
        try:
            payload = history.load(conversation_id)
        except (OSError, ValueError) as error:
            self.notify(f"Não foi possível abrir a conversa: {error}", error=True)
            return False
        self.agent.reset()
        self.display = []
        self.attachments = []
        self._assistant = None
        self.chips.setText("")
        self._clear_feed()
        self.welcome.show()
        # Autorizações "neste chat" não voltam ao reabrir uma conversa antiga.
        self._apply_payload(payload, restore_grants=False)
        self.conversation_id = conversation_id
        self._save_session(to_history=False)
        return True

    def conversations_deleted(self, conversation_ids: list[str]) -> None:
        if self.conversation_id in conversation_ids:
            self.new_chat()

    def attach_files(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(self, "Anexar arquivos")
        if not paths:
            return
        from pathlib import Path

        for raw in paths:
            if len(self.attachments) >= 5:
                self.notify("São cinco anexos no máximo.", error=True)
                break
            result = read_attachment(Path(raw))
            if not result["ok"]:
                self.notify(result["erro"], error=True)
                continue
            self.attachments.append(result["anexo"])
        self._refresh_chips()

    def _refresh_chips(self) -> None:
        labels = [f"{item['name']} ({describe_attachment(item)})" for item in self.attachments]
        self.chips.setText("Anexos: " + ", ".join(labels) if labels else "")

    def on_send_clicked(self) -> None:
        if self.worker and self.worker.isRunning():
            self.stop()
            return
        self.send()

    def send(self) -> None:
        text = self.composer.input.toPlainText().strip()
        if not text:
            return
        self.composer.input.clear()
        attachments = list(self.attachments)
        self.attachments = []
        self.chips.setText("")
        shown = text
        if attachments:
            shown += "\n\nAnexos: " + ", ".join(f"{item['name']} ({describe_attachment(item)})" for item in attachments)
        self._append_bubble("user", shown)
        self.display.append({"role": "user", "content": shown})
        self._assistant = self._append_bubble("assistant", "")
        self.display.append({"role": "assistant", "content": "", "thinking": "", "trace": [], "metrics": {}})
        self.worker = AgentWorker(self.agent, text, attachments)
        self.worker.event.connect(self._on_event)
        self.worker.need_approval.connect(self.approval.show_preview)
        self.worker.finished_ok.connect(self._finished)
        self.worker.failed.connect(self._failed)
        self.composer.set_running(True)
        self._assistant.set_activity("Consultando o modelo…")
        self._started = __import__("time").time()
        self.worker.start()

    def stop(self) -> None:
        self.approval.hide()
        if self.worker and self.worker.isRunning():
            self.worker.cancel()
            self.worker.submit_decision("deny")

    def _decided(self, scope: str) -> None:
        if self.worker:
            self.worker.submit_decision(scope)

    def _on_event(self, kind: str, data) -> None:
        bubble = self._assistant
        if bubble is None:
            return
        if kind == "content_reset":
            bubble.clear_content()
        elif kind == "content":
            bubble.append_content(str(data))
        elif kind == "thinking":
            bubble.append_thinking(str(data))
        elif kind == "trace":
            bubble.add_trace(data if isinstance(data, dict) else {"tool": str(data), "status": "success"})
            if self.display:
                self.display[-1].setdefault("trace", []).append(data)
        elif kind == "status":
            self.composer.show_activity(str(data))
            bubble.set_activity(str(data))
        elif kind == "metrics":
            bubble.set_metrics(data if isinstance(data, dict) else {})
            if self.display:
                self.display[-1]["metrics"] = data
        self._follow()

    def _finished(self, answer: str) -> None:
        if self._assistant is not None:
            self._assistant.stop_activity()
        if self._assistant is not None and self.display:
            self.display[-1]["content"] = self._assistant.plain() or answer
            self.display[-1]["thinking"] = self._assistant.thinking.toPlainText()
        self.composer.set_running(False)
        self.approval.hide()
        self._show_workspace()
        self.composer.hint.setText(self.composer.idle_hint())
        self._save_session()
        self._follow()

    def _failed(self, message: str) -> None:
        if self._assistant is not None:
            self._assistant.stop_activity()
        if self._assistant is not None and not self._assistant.plain():
            self._assistant.append_content(message)
        elif self._assistant is not None and message != "Interrompido.":
            self._assistant.add_trace({"tool": "erro", "status": "error", "ms": 0, "resumo": message})
        if message == "Interrompido." and self._assistant is not None:
            self._assistant.add_trace({"tool": "turno", "status": "denied", "ms": 0, "resumo": "Interrompido"})
        if self.display and self._assistant is not None:
            self.display[-1]["content"] = self._assistant.plain()
        self.composer.set_running(False)
        self.approval.hide()
        self._save_session()
        if message != "Interrompido.":
            self.notify(message, error=True)

    def _append_bubble(self, role: str, text: str) -> MessageBubble:
        if self.welcome.isVisible():
            self.welcome.hide()
        bubble = MessageBubble(role, text)
        stretch = self.feed.takeAt(self.feed.count() - 1)
        if stretch is not None and stretch.widget():
            stretch.widget().setParent(None)
        self.feed.addWidget(bubble)
        self.feed.addStretch(1)
        self._follow()
        return bubble

    def _clear_feed(self) -> None:
        while self.feed.count():
            item = self.feed.takeAt(0)
            widget = item.widget()
            if widget is not None and widget is not self.welcome:
                widget.deleteLater()
        self.feed.addWidget(self.welcome)
        self.feed.addStretch(1)

    def _remember_stick(self) -> None:
        bar = self.scroll.verticalScrollBar()
        self._stick = bar.value() >= bar.maximum() - 48

    def _follow(self) -> None:
        if not self._stick:
            return
        QTimer.singleShot(0, lambda: self.scroll.verticalScrollBar().setValue(self.scroll.verticalScrollBar().maximum()))

    def eventFilter(self, obj, event) -> bool:
        composer = getattr(self, "composer", None)
        if composer is not None and obj is composer.input and event.type() == QEvent.Type.KeyPress:
            assert isinstance(event, QKeyEvent)
            enter = event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
            shifted = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
            if enter and not shifted:
                self.on_send_clicked()
                return True
        return super().eventFilter(obj, event)

    def open_settings(self) -> None:
        dialog = SettingsDialog(self.settings, self.client, self)
        if dialog.exec():
            self.refresh_connection()

    def open_models(self) -> None:
        dialog = ModelsDialog(self.settings, self.client, self._apply_model_list, self)
        dialog.exec()

    def _apply_model_list(self, payload) -> None:
        self._apply_connection(payload if isinstance(payload, dict) else {"models": payload})

    def open_knowledge(self) -> None:
        KnowledgeDialog(self.settings, self.client, self).exec()

    def open_artifacts(self) -> None:
        ArtifactsDialog(self).exec()

    def open_programs(self) -> None:
        ProgramsDialog(self.settings, self).exec()

    def export_chat(self) -> None:
        if not self.display:
            self.notify("A conversa ainda está vazia.")
            return
        path, _ = QFileDialog.getSaveFileName(self, "Exportar conversa", "conversa.md", "Markdown (*.md)")
        if not path:
            return
        from ollamapilot.privacy import scrub
        from ollamapilot.secrets import known_values

        secrets = known_values()
        lines = ["# Conversa do OllamaPilot", ""]
        for item in self.display:
            who = "Você" if item.get("role") == "user" else "OllamaPilot"
            lines.append(f"## {who}")
            lines.append(scrub(str(item.get("content") or ""), secrets))
            lines.append("")
        from pathlib import Path

        Path(path).write_text("\n".join(lines), encoding="utf-8")
        self.notify("Conversa exportada.")

    def notify(self, text: str, error: bool = False) -> None:
        border = "#ff6b81" if error else "#3b4559"
        self.toast.setStyleSheet(
            f"background:#202738;border:1px solid {border};border-radius:11px;padding:10px 14px;"
        )
        self.toast.setText(text[:240])
        self.toast.adjustSize()
        self.toast.move(max(12, self.width() - self.toast.width() - 24), self.height() - self.toast.height() - 28)
        self.toast.show()
        QTimer.singleShot(3600, self.toast.hide)

    def _start_job(self, function, done, failed) -> None:
        job = FunctionWorker(function)
        job.succeeded.connect(done)
        job.failed.connect(failed)
        self._jobs.append(job)
        job.start()

    def _session_payload(self) -> dict:
        transcript = []
        for message in self.agent.transcript:
            item = dict(message)
            item.pop("images", None)
            transcript.append(item)
        return {
            "versao": 2,
            "conversa_id": self.conversation_id,
            "transcript": transcript,
            "display": self.display,
            "autorizacoes": sorted(self.agent.chat_grants),
            "recusas": sorted(self.agent.refused),
            "plano": sorted(self.agent.approved_plan),
            "plano_id": self.agent.plan_id,
            "pasta": str(self.agent.workspace) if self.agent.workspace else "",
            "tarefas": self.agent.tasks,
        }

    def _save_session(self, to_history: bool = True) -> None:
        from ollamapilot.privacy import for_disk
        from ollamapilot.secrets import known_values

        payload = for_disk(self._session_payload(), known_values())
        try:
            write_text_atomic(SESSION_FILE, json.dumps(payload, ensure_ascii=False, indent=2, default=str))
            if to_history:
                history.save(self.conversation_id, payload)
        except (OSError, TypeError, ValueError) as error:
            path = log_error("sessao", f"A conversa não foi gravada: {error}")
            self.notify(f"A conversa não foi gravada; a cópia anterior foi mantida. {path or ''}".strip(), error=True)

    def _load_session(self) -> None:
        if not SESSION_FILE.is_file():
            return
        try:
            payload = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
        except OSError:
            return
        except ValueError:
            park_invalid(SESSION_FILE)
            return
        if not isinstance(payload, dict):
            park_invalid(SESSION_FILE)
            return
        self._apply_payload(payload, restore_grants=True)
        if history.valid_id(payload.get("conversa_id")):
            self.conversation_id = payload["conversa_id"]
        elif self.display:
            # Sessão anterior ao histórico: vira a primeira conversa da lista.
            self._save_session()

    def _apply_payload(self, payload: dict, restore_grants: bool) -> None:
        transcript = payload.get("transcript")
        display = payload.get("display")
        grants = payload.get("autorizacoes")
        refused = payload.get("recusas")
        if isinstance(transcript, list):
            clean = [item for item in transcript if isinstance(item, dict) and isinstance(item.get("role"), str)]
            self.agent.transcript = repair_transcript(clean)
        if restore_grants and isinstance(grants, list):
            self.agent.chat_grants = {str(item) for item in grants if isinstance(item, str) and item}
        if isinstance(refused, list):
            self.agent.refused = {str(item) for item in refused if isinstance(item, str) and item}
        plan = payload.get("plano")
        if isinstance(plan, list):
            self.agent.approved_plan = {str(item).lower() for item in plan if isinstance(item, str) and item}
        else:
            self.agent.approved_plan = set()
        self.agent.plan_id = payload["plano_id"] if isinstance(payload.get("plano_id"), str) else ""
        from pathlib import Path

        pasta = payload.get("pasta")
        self.agent.workspace = Path(pasta) if isinstance(pasta, str) and pasta.strip() else None
        tarefas = payload.get("tarefas")
        if isinstance(tarefas, list):
            self.agent.tasks = [
                {"texto": str(item.get("texto") or "")[:240], "estado": "feito" if item.get("estado") == "feito" else "pendente"}
                for item in tarefas
                if isinstance(item, dict) and str(item.get("texto") or "").strip()
            ]
        else:
            self.agent.tasks = []
        self._show_workspace()
        if not isinstance(display, list) or not display:
            return
        clean_display = []
        for item in display:
            if not isinstance(item, dict):
                continue
            role = item.get("role") if item.get("role") in ("user", "assistant") else "assistant"
            trace = item.get("trace") if isinstance(item.get("trace"), list) else []
            metrics = item.get("metrics") if isinstance(item.get("metrics"), dict) else {}
            clean_display.append(
                {
                    "role": role,
                    "content": str(item.get("content") or ""),
                    "thinking": str(item.get("thinking") or ""),
                    "trace": [entry for entry in trace if isinstance(entry, dict)],
                    "metrics": metrics,
                }
            )
        if not clean_display:
            return
        self.display = clean_display
        self._render_display()

    def _render_display(self) -> None:
        if not self.display:
            return
        self.welcome.hide()
        for item in self.display:
            bubble = self._append_bubble(item["role"], item["content"])
            if item.get("thinking"):
                bubble.append_thinking(item["thinking"])
            for entry in item.get("trace") or []:
                bubble.add_trace(entry)
            if item.get("metrics"):
                bubble.set_metrics(item["metrics"])

    def selftest_and_quit(self) -> None:
        from ollamapilot.paths import STORAGE
        from ollamapilot.ui.dialogs import ArtifactsDialog, KnowledgeDialog, McpDialog, ProgramsDialog, SettingsDialog

        self.new_chat()
        SettingsDialog(self.settings, self.client, self).close()
        HistoryDialog(self).close()
        McpDialog(self.settings, self).close()
        ProgramsDialog(self.settings, self).close()
        ArtifactsDialog(self).close()
        KnowledgeDialog(self.settings, self.client, self).close()
        self._append_bubble("user", "Mensagem de teste do OllamaPilot.")
        bubble = self._append_bubble("assistant", "")
        bubble.append_content("Resposta de teste com `código`.\n\n- lê arquivos\n- usa o Ghidra")
        bubble.add_trace({"tool": "ler_binario", "status": "success", "ms": 12, "resumo": "mapa do executável"})
        from PySide6.QtWidgets import QApplication

        QApplication.processEvents()
        self.scroll.verticalScrollBar().setValue(0)
        QApplication.processEvents()
        self.grab().save(str(STORAGE / "selftest.png"))
        import importlib

        missing = []
        for module in ("pypdf", "docx", "openpyxl", "ollamapilot.mcp"):
            try:
                importlib.import_module(module)
            except ImportError:
                missing.append(module)
        (STORAGE / "selftest-ok.txt").write_text("ok" if not missing else "faltam: " + ", ".join(missing), encoding="utf-8")
        self.close()
