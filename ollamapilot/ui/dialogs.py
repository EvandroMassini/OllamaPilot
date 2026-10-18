from __future__ import annotations

import json
import os
from pathlib import Path

from PySide6.QtCore import QDate, QSize, Qt
from PySide6.QtGui import QTextOption
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDialog,
    QFileDialog,
    QFrame,
    QFormLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QSpinBox,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ollamapilot.config import CONTEXT_MAX, CONTEXT_MIN, CONTEXT_STEP, db_access_mode


def _context_hint() -> str:
    low = f"{CONTEXT_MIN:,}".replace(",", ".")
    high = f"{CONTEXT_MAX:,}".replace(",", ".")
    return f"Tokens que o modelo enxerga da conversa e das ferramentas. De {low} a {high}."

from ollamapilot.ollama_client import OllamaClient, OllamaError, describe_capabilities, parse_capabilities
from ollamapilot.tools.artifacts import list_artifacts
from ollamapilot.tools.knowledge import clear_index, index_files, sources
from ollamapilot.ui.workers import FunctionWorker


class SettingsDialog(QDialog):
    def __init__(self, settings, client: OllamaClient, parent=None) -> None:
        super().__init__(parent)
        self.settings = settings
        self.client = client
        self.setWindowTitle("Configurações")
        self.resize(700, 760)
        root = QVBoxLayout(self)
        title = QLabel("Configurações")
        title.setStyleSheet("font-size: 18px; font-weight: 700;")
        root.addWidget(title)
        help_text = QLabel("Cada aba reúne um grupo. Se a barra à direita aparecer, ainda há campos abaixo.")
        help_text.setObjectName("muted")
        help_text.setWordWrap(True)
        root.addWidget(help_text)

        self.url = QLineEdit(settings.ollama_url)
        self.provider = self._choice(
            [
                ("Ollama", "ollama"),
                ("Compatível com OpenAI (LM Studio, llama.cpp, vLLM, OpenRouter)", "openai"),
            ],
            getattr(settings, "provider", "ollama"),
        )
        self.openai_url = QLineEdit(getattr(settings, "openai_url", ""))
        self.openai_key = QLineEdit(getattr(settings, "openai_api_key", ""))
        self.openai_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.tool_selection = self._choice(
            [
                ("Pelo assunto do pedido (economiza contexto)", "auto"),
                ("Todas em toda rodada", "todas"),
            ],
            getattr(settings, "tool_selection", "auto"),
        )
        self.source = self._choice(
            [
                ("Somente modelos locais (já baixados)", "local"),
                ("Locais e online (ollama.com, exige API Key)", "online"),
            ],
            getattr(settings, "model_source", "local"),
        )
        self.cloud_key = QLineEdit(settings.cloud_api_key)
        self.cloud_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.source.currentIndexChanged.connect(self._source_changed)
        self.model = QLineEdit(settings.model)
        self.temperature = ValueSlider(0, 2, settings.temperature, 0.1, 1)
        self.context = ValueSlider(CONTEXT_MIN, CONTEXT_MAX, settings.num_ctx, CONTEXT_STEP)
        self.keep_alive = self._choice(
            [
                ("Descarregar ao terminar", "0"),
                ("5 minutos", "5m"),
                ("30 minutos", "30m"),
                ("1 hora", "1h"),
                ("4 horas", "4h"),
                ("24 horas", "24h"),
            ],
            settings.keep_alive,
        )
        self.think = QComboBox()
        self.think.addItems(["auto", "false", "true", "low", "medium", "high"])
        self.think.setCurrentText(settings.think or "auto")
        self.response_format = QComboBox()
        self.response_format.addItems(["text", "json", "schema"])
        self.response_format.setCurrentText(settings.response_format or "text")
        self.schema = QPlainTextEdit(settings.format_schema)
        self.schema.setFixedHeight(80)
        self.seed = QLineEdit("" if settings.seed is None else str(settings.seed))
        self.top_k = ValueSlider(0, 100, settings.top_k, 1)
        self.top_p = ValueSlider(0, 1, settings.top_p, 0.05, 2)
        self.min_p = ValueSlider(0, 1, settings.min_p, 0.05, 2)
        self.repeat = ValueSlider(0.5, 2, settings.repeat_penalty, 0.05, 2)
        self.predict = self._choice(
            [
                ("Sem limite", -1),
                ("256", 256),
                ("512", 512),
                ("1.024", 1024),
                ("2.048", 2048),
                ("4.096", 4096),
                ("8.192", 8192),
                ("16.384", 16384),
            ],
            settings.num_predict,
        )
        self.embed = QLineEdit(settings.embed_model)
        self.ghidra = QLineEdit(settings.ghidra_home)
        browse = QPushButton("Procurar")
        browse.setObjectName("ghost")
        browse.clicked.connect(self._browse_ghidra)
        ghidra_host = QWidget()
        ghidra_row = QHBoxLayout(ghidra_host)
        ghidra_row.setContentsMargins(0, 0, 0, 0)
        ghidra_row.addWidget(self.ghidra)
        ghidra_row.addWidget(browse)
        self.ghidra_timeout = ValueSlider(60, 1800, settings.ghidra_timeout, 60, suffix=" s")
        self.command_timeout = ValueSlider(5, 600, settings.command_timeout, 5, suffix=" s")
        self.web = QCheckBox("Pesquisa web pela API da Ollama")
        self.web.setChecked(settings.web_enabled)
        self.web_key = QLineEdit(settings.web_api_key)
        self.web_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.trace = QCheckBox("Registrar a trilha de cada turno")
        self.trace.setChecked(getattr(settings, "trace_enabled", True))
        self.mcp = QCheckBox("Servidores MCP locais")
        self.mcp.setChecked(getattr(settings, "mcp_enabled", False))
        mcp_button = QPushButton("Servidores…")
        mcp_button.setObjectName("ghost")
        mcp_button.clicked.connect(lambda: McpDialog(self.settings, self).exec())
        mcp_host = QWidget()
        mcp_row = QHBoxLayout(mcp_host)
        mcp_row.setContentsMargins(0, 0, 0, 0)
        mcp_row.addWidget(self.mcp)
        mcp_row.addStretch(1)
        mcp_row.addWidget(mcp_button)
        self.db = self._choice(
            [
                ("Desligado", "desligado"),
                ("Somente leitura", "leitura"),
                ("Leitura e escrita", "escrita"),
            ],
            db_access_mode(settings),
        )
        self.db_host = QLineEdit(settings.db_host)
        self.db_port = self._int(settings.db_port, 1, 65535, 1)
        self.db_name = QLineEdit(settings.db_name)
        self.db_user = QLineEdit(settings.db_user)
        self.db_password = QLineEdit(settings.db_password)
        self.db_password.setEchoMode(QLineEdit.EchoMode.Password)
        tabs = QTabWidget()
        tabs.addTab(
            self._form_page(
                [
                    ("URL do Ollama", self._hinted(self.url, "Endereço do servidor Ollama, nesta máquina ou em outra na rede.")),
                    (
                        "Modelos",
                        self._hinted(
                            self.source,
                            "Somente locais: a lista mostra só os modelos baixados neste servidor e nada vai para ollama.com. "
                            "Locais e online: a lista inclui os modelos de ollama.com; a conversa só vai para lá quando você escolher um deles.",
                        ),
                    ),
                    ("API Key", self._hinted(self.cloud_key, "Chave gerada no site do Ollama. Fica no Gerenciador de Credenciais do Windows, não no arquivo de configuração.")),
                    ("Modelo padrão", self._hinted(self.model, "Modelo usado quando a barra de cima ainda não escolheu outro.")),
                    (
                        "Provedor do chat",
                        self._hinted(
                            self.provider,
                            "Ollama usa a URL acima. Compatível com OpenAI manda o chat para a URL abaixo; "
                            "embeddings e pesquisa web continuam no Ollama.",
                        ),
                    ),
                    ("URL compatível", self._hinted(self.openai_url, "Endereço terminado em /v1, como http://127.0.0.1:1234/v1 no LM Studio.")),
                    ("Chave compatível", self._hinted(self.openai_key, "Chave do servidor compatível, quando ele exige. Fica no cofre do Windows.")),
                ]
            ),
            "Conexão",
        )
        tabs.addTab(
            self._form_page(
                [
                    ("Temperatura", self._hinted(self.temperature, "Variação da resposta. Perto de 0 fica estável. Perto de 2 varia mais.")),
                    ("Contexto", self._hinted(self.context, _context_hint())),
                    ("Manter carregado", self._hinted(self.keep_alive, "Tempo que o modelo permanece na memória do Ollama depois da resposta.")),
                    ("Raciocínio", self._hinted(self.think, "Quanto o modelo pensa antes de responder. Auto deixa o servidor decidir.")),
                    ("Formato", self._hinted(self.response_format, "Texto livre, JSON ou JSON obedecendo o schema abaixo.")),
                    ("JSON Schema", self._hinted(self.schema, "Modelo do JSON quando o formato é schema. Nos outros formatos, deixe vazio.")),
                    ("Seed", self._hinted(self.seed, "Número que repete a mesma resposta. Vazio deixa o resultado variar.")),
                    ("Top K", self._hinted(self.top_k, "Quantos tokens mais prováveis entram no sorteio. Zero usa o padrão do modelo.")),
                    ("Top P", self._hinted(self.top_p, "Recorte pela probabilidade acumulada. 1 considera o conjunto do Top K.")),
                    ("Min P", self._hinted(self.min_p, "Descarta tokens com probabilidade muito baixa.")),
                    ("Penalidade de repetição", self._hinted(self.repeat, "Reduz a chance de repetir o que já foi dito. 1,00 não altera.")),
                    ("Máximo de tokens", self._hinted(self.predict, "Tamanho máximo da resposta. Sem limite, o modelo para sozinho.")),
                ]
            ),
            "Geração",
        )
        tabs.addTab(
            self._form_page(
                [
                    ("Pasta do Ghidra", self._hinted(ghidra_host, "Pasta raiz da instalação, a que contém support\\analyzeHeadless.bat.")),
                    ("Tempo do Ghidra", self._hinted(self.ghidra_timeout, "Tempo máximo da análise de um arquivo, em segundos.")),
                    ("Tempo de comando", self._hinted(self.command_timeout, "Tempo máximo padrão de um programa iniciado pelo agente, em segundos. Cada programa do menu Programas pode ter o seu.")),
                ]
            ),
            "Programas",
        )
        tabs.addTab(
            self._form_page(
                [
                    (
                        "Ferramentas enviadas",
                        self._hinted(
                            self.tool_selection,
                            "Pelo assunto: janelas, Ghidra, web, banco, Biblioteca e projeto só vão ao modelo quando o pedido fala deles "
                            "ou quando já foram usados no chat. Arquivos e programas vão sempre.",
                        ),
                    ),
                    ("Modelo de embeddings", self._hinted(self.embed, "Modelo que indexa e busca os documentos da Biblioteca (RAG).")),
                    ("", self._hinted(self.web, "Liga a busca e a leitura de páginas pela API em ollama.com.")),
                    ("Chave da pesquisa", self._hinted(self.web_key, "Chave usada só na busca e na leitura de páginas. Também fica no cofre do Windows.")),
                    (
                        "",
                        self._hinted(
                            self.trace,
                            "Grava em storage\\logs\\turnos.jsonl o modelo, as ferramentas, as autorizações, a duração e o desfecho. "
                            "Não grava o texto da conversa, o raciocínio nem chaves.",
                        ),
                    ),
                    (
                        "",
                        self._hinted(
                            mcp_host,
                            "Liga as ferramentas dos servidores declarados em storage\\mcp.json. Desligado, nenhum servidor roda. "
                            "Cada ferramenta pede autorização, salvo se o arquivo disser o contrário.",
                        ),
                    ),
                    (
                        "PostgreSQL",
                        self._hinted(
                            self.db,
                            "Desligado esconde as ferramentas de banco. Somente leitura consulta. "
                            "Leitura e escrita também cria, altera e apaga, e cada comando pede confirmação.",
                        ),
                    ),
                    ("Host", self._hinted(self.db_host, "Endereço do PostgreSQL.")),
                    ("Porta", self._hinted(self.db_port, "Porta do PostgreSQL.")),
                    ("Banco padrão", self._hinted(self.db_name, "Usado quando o pedido não disser outro banco. Pode ficar vazio.")),
                    ("Usuário", self._hinted(self.db_user, "Usuário do PostgreSQL neste servidor. Vale para o banco padrão e para outro banco que você autorizar.")),
                    ("Senha", self._hinted(self.db_password, "Senha desse usuário. Fica no Gerenciador de Credenciais do Windows.")),
                ]
            ),
            "Recursos",
        )
        root.addWidget(tabs, 1)
        self.feedback = QLabel("")
        self.feedback.setWordWrap(True)
        self.feedback.setObjectName("muted")
        root.addWidget(self.feedback)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        close = QPushButton("Fechar")
        close.setObjectName("ghost")
        save = QPushButton("Testar e salvar")
        save.setObjectName("primary")
        close.clicked.connect(self.reject)
        save.clicked.connect(self._save)
        buttons.addWidget(close)
        buttons.addWidget(save)
        root.addLayout(buttons)

    def _source_changed(self) -> None:
        if self.source.currentData() == "online" and not self.cloud_key.text().strip():
            self.feedback.setText("Modelos online exigem a API Key: informe-a abaixo antes de salvar.")
            self.cloud_key.setFocus()
        else:
            self.feedback.setText("")

    def _browse_ghidra(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Pasta do Ghidra", self.ghidra.text())
        if folder:
            self.ghidra.setText(folder)

    def _save(self) -> None:
        url = self.url.text().strip().rstrip("/")
        if not url.startswith(("http://", "https://")):
            self.feedback.setText("A URL precisa começar com http:// ou https://.")
            return
        provider = str(self.provider.currentData() or "ollama")
        openai_url = self.openai_url.text().strip().rstrip("/")
        if provider == "openai" and not openai_url.startswith(("http://", "https://")):
            self.feedback.setText("A URL compatível com OpenAI precisa começar com http:// ou https://.")
            self.openai_url.setFocus()
            return
        source = str(self.source.currentData() or "local")
        key = self.cloud_key.text().strip()
        if source == "online" and not key:
            self.feedback.setText("Para usar modelos online, informe a API Key. Ou escolha Somente modelos locais.")
            self.cloud_key.setFocus()
            return
        self.settings.ollama_url = url
        self.settings.model_source = source
        self.settings.cloud_api_key = key
        if source == "local" and self.settings.model_remote:
            self.settings.model_remote = False
            self.settings.model = ""
        self.settings.model = self.model.text().strip()
        if provider != getattr(self.settings, "provider", "ollama"):
            self.settings.model_remote = False
        self.settings.provider = provider
        self.settings.openai_url = openai_url or "http://127.0.0.1:1234/v1"
        self.settings.openai_api_key = self.openai_key.text().strip()
        self.settings.tool_selection = str(self.tool_selection.currentData() or "auto")
        self.settings.temperature = self.temperature.value()
        self.settings.num_ctx = self.context.value()
        self.settings.keep_alive = str(self.keep_alive.currentData())
        self.settings.think = self.think.currentText()
        self.settings.response_format = self.response_format.currentText()
        self.settings.format_schema = self.schema.toPlainText().strip()
        seed = self.seed.text().strip()
        try:
            self.settings.seed = int(seed) if seed else None
        except ValueError:
            self.feedback.setText("A seed precisa ser um número inteiro, ou ficar vazia.")
            return
        self.settings.top_k = self.top_k.value()
        self.settings.top_p = self.top_p.value()
        self.settings.min_p = self.min_p.value()
        self.settings.repeat_penalty = self.repeat.value()
        self.settings.num_predict = int(self.predict.currentData())
        self.settings.embed_model = self.embed.text().strip() or "embeddinggemma"
        self.settings.ghidra_home = self.ghidra.text().strip()
        self.settings.ghidra_timeout = self.ghidra_timeout.value()
        self.settings.command_timeout = self.command_timeout.value()
        self.settings.web_enabled = self.web.isChecked()
        self.settings.web_api_key = self.web_key.text().strip()
        self.settings.trace_enabled = self.trace.isChecked()
        self.settings.mcp_enabled = self.mcp.isChecked()
        if not self.settings.mcp_enabled:
            from ollamapilot.mcp import MANAGER

            MANAGER.shutdown()
        access = str(self.db.currentData() or "desligado")
        self.settings.db_access = access
        self.settings.db_enabled = access != "desligado"
        self.settings.db_host = self.db_host.text().strip() or "127.0.0.1"
        self.settings.db_port = self.db_port.value()
        self.settings.db_name = self.db_name.text().strip()
        self.settings.db_user = self.db_user.text().strip()
        self.settings.db_password = self.db_password.text()
        self.settings.save()
        try:
            if provider == "openai":
                count = len(self.client.openai_models())
                self.feedback.setText(f"Salvo. O servidor compatível respondeu com {count} modelo(s).")
            else:
                version = self.client.version().get("version", "")
                self.feedback.setText(f"Salvo. Ollama respondeu, versão {version}.")
            self.accept()
        except OllamaError as error:
            self.feedback.setText(f"Salvo. O servidor não respondeu: {error}")
            self.accept()

    def _form_page(self, rows: list[tuple[str, QWidget]]) -> QScrollArea:
        host = QWidget()
        form = QFormLayout(host)
        form.setContentsMargins(16, 14, 16, 14)
        for label, widget in rows:
            if label:
                form.addRow(label, widget)
            else:
                form.addRow(widget)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setWidget(host)
        return scroll

    @staticmethod
    def _hinted(widget: QWidget, text: str) -> QWidget:
        host = QWidget()
        column = QVBoxLayout(host)
        column.setContentsMargins(0, 0, 0, 8)
        column.setSpacing(2)
        column.addWidget(widget)
        hint = QLabel(text)
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        column.addWidget(hint)
        return host

    @staticmethod
    def _choice(options: list[tuple], current) -> QComboBox:
        box = QComboBox()
        for label, value in options:
            box.addItem(str(label), value)
        index = box.findData(current)
        if index < 0:
            box.addItem(str(current), current)
            index = box.count() - 1
        box.setCurrentIndex(index)
        return box

    @staticmethod
    def _int(value: int, low: int, high: int, step: int) -> QSpinBox:
        box = QSpinBox()
        box.setRange(low, high)
        box.setSingleStep(step)
        box.setValue(int(value))
        return box

class ValueSlider(QWidget):
    """Barra com valor visível, presa entre um mínimo e um máximo."""

    def __init__(self, low, high, value, step, decimals: int = 0, suffix: str = "") -> None:
        super().__init__()
        self._low = float(low)
        self._step = float(step)
        self._decimals = decimals
        self._suffix = suffix
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, int(round((float(high) - self._low) / self._step)))
        self.slider.setValue(self._tick(value))
        self.label = QLabel()
        self.label.setMinimumWidth(78)
        self.label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.slider.valueChanged.connect(lambda _value: self._show())
        layout.addWidget(self.slider, 1)
        layout.addWidget(self.label)
        self._show()

    def _tick(self, value) -> int:
        tick = int(round((float(value) - self._low) / self._step))
        return max(0, min(self.slider.maximum(), tick))

    def value(self):
        raw = self._low + self.slider.value() * self._step
        if self._decimals == 0:
            return int(round(raw))
        return round(raw, self._decimals)

    def _show(self) -> None:
        shown = self.value()
        if self._decimals:
            text = f"{shown:.{self._decimals}f}"
        else:
            text = f"{int(shown):,}".replace(",", ".")
        self.label.setText(text + self._suffix)


class QuietLabel(QLabel):
    """Texto que quebra a linha e não alarga a janela."""

    def __init__(self, text: str = "", parent=None) -> None:
        super().__init__(text, parent)
        self.setWordWrap(True)
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

    def sizeHint(self) -> QSize:
        return QSize(0, super().sizeHint().height())

    def minimumSizeHint(self) -> QSize:
        return QSize(0, super().minimumSizeHint().height())


class StatusLine(QPlainTextEdit):
    """Mensagem de operação. Uma URL longa rola para baixo, sem esticar a janela."""

    _max_height = 112

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("statusLine")
        self.setReadOnly(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.setWordWrapMode(QTextOption.WrapMode.WrapAnywhere)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.setMinimumWidth(0)
        self.setFixedHeight(0)
        self.document().setDocumentMargin(0)

    def setText(self, text: str) -> None:
        self.setPlainText(text or "")
        self._fit()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self.toPlainText().strip():
            self._fit()

    def _fit(self) -> None:
        if not self.toPlainText().strip():
            if self.height() != 0:
                self.setFixedHeight(0)
            return
        width = self.viewport().width()
        if width < 40:
            width = 640
        self.document().setTextWidth(width)
        height = min(max(int(self.document().size().height()) + 6, 22), self._max_height)
        if height != self.height():
            self.setFixedHeight(height)

    def sizeHint(self) -> QSize:
        return QSize(0, self.height())

    def minimumSizeHint(self) -> QSize:
        return QSize(0, 0)


class ModelsDialog(QDialog):
    def __init__(self, settings, client: OllamaClient, on_models, parent=None) -> None:
        super().__init__(parent)
        self.settings = settings
        self.client = client
        self.on_models = on_models
        self._job = None
        self.setWindowTitle("Modelos")
        self.resize(680, 640)
        layout = QVBoxLayout(self)
        title = QLabel("Gerenciar modelos")
        title.setStyleSheet("font-size: 18px; font-weight: 700;")
        self.version = QuietLabel("")
        self.version.setObjectName("muted")
        layout.addWidget(title)
        layout.addWidget(self.version)
        pull_row = QHBoxLayout()
        self.pull_name = QLineEdit()
        self.pull_name.setPlaceholderText("Ex.: qwen3.5:9b")
        pull = QPushButton("Instalar")
        pull.setObjectName("primary")
        pull.clicked.connect(self._pull)
        pull_row.addWidget(self.pull_name)
        pull_row.addWidget(pull)
        layout.addLayout(pull_row)
        self.pull_status = StatusLine()
        layout.addWidget(self.pull_status)
        self.models = QListWidget()
        layout.addWidget(self.models, 1)
        actions = QHBoxLayout()
        for label, slot in (
            ("Detalhes", self._details),
            ("Carregar", self._load),
            ("Liberar", self._unload),
            ("Copiar", self._copy),
            ("Excluir", self._delete),
        ):
            button = QPushButton(label)
            button.setObjectName("ghost")
            button.clicked.connect(slot)
            actions.addWidget(button)
        layout.addLayout(actions)
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setFixedHeight(140)
        layout.addWidget(self.details)
        create_name = QLineEdit()
        create_name.setPlaceholderText("Novo nome")
        create_from = QLineEdit()
        create_from.setPlaceholderText("Modelo-base")
        self.create_name = create_name
        self.create_from = create_from
        self.create_system = QPlainTextEdit()
        self.create_system.setPlaceholderText("Instrução de sistema, opcional")
        self.create_system.setFixedHeight(70)
        create = QPushButton("Criar modelo personalizado")
        create.setObjectName("primary")
        create.clicked.connect(self._create)
        row = QHBoxLayout()
        row.addWidget(create_name)
        row.addWidget(create_from)
        layout.addLayout(row)
        layout.addWidget(self.create_system)
        layout.addWidget(create)
        self._reload()

    def selected(self) -> str:
        item = self.models.currentItem()
        if item is None:
            return ""
        return str(item.data(Qt.ItemDataRole.UserRole) or "")

    def _reload(self) -> None:
        self._run(lambda _progress: self.client.available_models(), self._show_models)

    def _show_models(self, payload) -> None:
        version = str((payload.get("version") or {}).get("version") or "")
        remote = payload.get("remote") or []
        notes = []
        if version:
            notes.append("Ollama " + version)
        if remote:
            notes.append(f"{len(remote)} online")
        if payload.get("local_error") and not payload.get("models"):
            notes.append("servidor local sem resposta")
        if payload.get("remote_error"):
            notes.append(str(payload["remote_error"]))
        self.version.setText(" · ".join(notes))
        self.models.clear()
        for model in payload.get("models") or []:
            name = model.get("name") or ""
            if not name:
                continue
            size = (model.get("size") or 0) / 1_000_000_000
            self.models.addItem(f"{name}    {size:.1f} GB")
            item = self.models.item(self.models.count() - 1)
            item.setData(Qt.ItemDataRole.UserRole, name)
            item.setData(Qt.ItemDataRole.UserRole + 1, False)
        for model in remote:
            name = model.get("name") or ""
            if not name:
                continue
            self.models.addItem(f"{name}    online")
            item = self.models.item(self.models.count() - 1)
            item.setData(Qt.ItemDataRole.UserRole, name)
            item.setData(Qt.ItemDataRole.UserRole + 1, True)
        self.on_models(payload)

    def _remote_selected(self) -> bool:
        item = self.models.currentItem()
        if item is None:
            return False
        return bool(item.data(Qt.ItemDataRole.UserRole + 1))

    def _pull(self) -> None:
        name = self.pull_name.text().strip()
        if not name:
            return

        def work(progress):
            self.client.pull(name, progress)
            return name

        self._run(work, lambda _name: self._reload(), self.pull_status.setText)

    def _details(self) -> None:
        name = self.selected()
        if not name:
            return
        if self._remote_selected():
            self.details.setPlainText("Modelo online. A conversa vai para ollama.com com a API Key cadastrada. Ele não fica instalado neste computador.")
            return

        def work(_progress):
            return self.client.show(name)

        def show(data):
            text = data.get("modelfile") if isinstance(data, dict) else ""
            if not text:
                text = json.dumps(data, ensure_ascii=False, indent=2)[:5000]
            caps = describe_capabilities(parse_capabilities(data if isinstance(data, dict) else {}))
            self.details.setPlainText(f"Capacidades: {caps}\n\n{str(text)[:5000]}")

        self._run(work, show)

    def _load(self) -> None:
        name = self.selected()
        if not name:
            return
        if self._remote_selected():
            self.pull_status.setText("Modelo online. A consulta usa a API Key e não carrega o arquivo nesta máquina.")
            return
        self._run(lambda _progress: self.client.load_model(name, self.settings.keep_alive) or name, lambda _value: self.pull_status.setText(f"{name} carregado."))

    def _unload(self) -> None:
        name = self.selected()
        if not name:
            return
        if self._remote_selected():
            self.pull_status.setText("Modelo online. Não há arquivo local para liberar.")
            return
        self._run(lambda _progress: self.client.load_model(name, 0) or name, lambda _value: self.pull_status.setText(f"{name} liberado."))

    def _copy(self) -> None:
        source = self.selected()
        if not source:
            return
        if self._remote_selected():
            self.pull_status.setText("Modelo online. A cópia vale para os modelos instalados neste computador.")
            return
        from PySide6.QtWidgets import QInputDialog

        destination, ok = QInputDialog.getText(self, "Copiar modelo", "Novo nome:")
        if not ok or not destination.strip():
            return
        self._run(lambda _progress: self.client.copy(source, destination.strip()) or destination, lambda _value: self._reload())

    def _delete(self) -> None:
        name = self.selected()
        if not name:
            return
        if self._remote_selected():
            self.pull_status.setText("Modelo online. Ele não está instalado neste computador.")
            return
        answer = QMessageBox.question(self, "Excluir modelo", f"Excluir {name} do servidor Ollama?")
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._run(lambda _progress: self.client.delete(name) or name, lambda _value: self._reload())

    def _create(self) -> None:
        name = self.create_name.text().strip()
        base = self.create_from.text().strip()
        if not name or not base:
            self.pull_status.setText("Informe o nome novo e o modelo-base.")
            return
        system = self.create_system.toPlainText()

        def work(progress):
            self.client.create_model(name, base, system, progress)
            return name

        self._run(work, lambda _name: self._reload(), self.pull_status.setText)

    def _run(self, function, done, progress=None) -> None:
        if self._job and self._job.isRunning():
            self.pull_status.setText("Aguarde a operação atual.")
            return
        job = FunctionWorker(function)
        job.succeeded.connect(done)
        job.failed.connect(self.pull_status.setText)
        if progress is not None:
            job.progress.connect(progress)
        self._job = job
        job.start()


class KnowledgeDialog(QDialog):
    def __init__(self, settings, client: OllamaClient, parent=None) -> None:
        super().__init__(parent)
        self.settings = settings
        self.client = client
        self._job = None
        self.setWindowTitle("Biblioteca (RAG)")
        self.resize(620, 480)
        layout = QVBoxLayout(self)
        title = QLabel("Biblioteca (RAG)")
        title.setStyleSheet("font-size: 18px; font-weight: 700;")
        note = QLabel(
            "Documentos usados na busca por recuperação. O agente consulta os trechos indexados, não uma pasta fixa. "
            "Aceita texto comum, PDF, DOCX e XLSX. Um arquivo ilegível é ignorado e os outros continuam."
        )
        note.setWordWrap(True)
        note.setObjectName("muted")
        layout.addWidget(title)
        layout.addWidget(note)
        button = QPushButton("Adicionar documentos")
        button.setObjectName("primary")
        button.clicked.connect(self._index)
        clear = QPushButton("Limpar biblioteca")
        clear.setObjectName("ghost")
        clear.clicked.connect(self._clear)
        row = QHBoxLayout()
        row.addWidget(button)
        row.addWidget(clear)
        layout.addLayout(row)
        self.status = QLabel("")
        self.status.setObjectName("muted")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.files = QListWidget()
        layout.addWidget(self.files, 1)
        self._fill()

    def _fill(self) -> None:
        self.files.clear()
        for name in sources():
            self.files.addItem(name)

    def _index(self) -> None:
        chosen, _ = QFileDialog.getOpenFileNames(
            self,
            "Documentos para a Biblioteca (RAG)",
            "",
            "Documentos (*.txt *.md *.pdf *.docx *.xlsx *.xlsm *.csv *.json *.html *.xml *.py *.log);;Todos (*.*)",
        )
        if not chosen:
            return
        paths = [Path(item) for item in chosen]

        def work(progress):
            return index_files(paths, self.settings, self.client, progress)

        job = FunctionWorker(work)
        job.progress.connect(self.status.setText)
        job.succeeded.connect(lambda count: self._indexed(count))
        job.failed.connect(self.status.setText)
        self._job = job
        job.start()

    def _indexed(self, result) -> None:
        if isinstance(result, dict):
            text = f"{result.get('trechos', 0)} trechos indexados."
            skipped = result.get("ignorados") or []
            if skipped:
                text += " Ignorados: " + " · ".join(str(item) for item in skipped[:5])
        else:
            text = f"{result} trechos indexados."
        self.status.setText(text)
        self._fill()

    def _clear(self) -> None:
        clear_index()
        self._fill()
        self.status.setText("Biblioteca limpa.")


class ArtifactsDialog(QDialog):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Arquivos gerados")
        self.resize(560, 420)
        layout = QVBoxLayout(self)
        title = QLabel("Arquivos gerados")
        title.setStyleSheet("font-size: 18px; font-weight: 700;")
        note = QLabel("Downloads desta biblioteca expiram em 24 horas. Projeto gravado num caminho da conversa não aparece aqui.")
        note.setWordWrap(True)
        note.setObjectName("muted")
        layout.addWidget(title)
        layout.addWidget(note)
        self.files = QListWidget()
        layout.addWidget(self.files, 1)
        row = QHBoxLayout()
        open_file = QPushButton("Abrir")
        open_file.setObjectName("primary")
        show = QPushButton("Mostrar no Explorer")
        show.setObjectName("ghost")
        open_file.clicked.connect(self._open)
        show.clicked.connect(self._reveal)
        row.addWidget(open_file)
        row.addWidget(show)
        layout.addLayout(row)
        self._rows = list_artifacts()
        for item in self._rows:
            self.files.addItem(item.get("nome") or "")

    def _current(self) -> str:
        row = self.files.currentRow()
        if row < 0 or row >= len(self._rows):
            return ""
        return str(self._rows[row].get("caminho") or "")

    def _open(self) -> None:
        path = self._current()
        if path:
            os.startfile(path)

    def _reveal(self) -> None:
        path = self._current()
        if path:
            __import__("subprocess").Popen(["explorer", "/select,", path])


class ProgramsDialog(QDialog):
    def __init__(self, settings, parent=None) -> None:
        super().__init__(parent)
        self.settings = settings
        self._editing = -1
        self.setWindowTitle("Programas")
        self.resize(680, 520)
        layout = QVBoxLayout(self)
        title = QLabel("Programas e ferramentas")
        title.setStyleSheet("font-size: 18px; font-weight: 700;")
        note = QLabel(
            "Cadastre os programas que o agente pode usar, com o caminho e os tempos de comando e de análise. "
            "O Ghidra continua em Configurações, na aba Programas: a pasta e o tempo de análise dele não se repetem aqui."
        )
        note.setWordWrap(True)
        note.setObjectName("muted")
        layout.addWidget(title)
        layout.addWidget(note)
        self.files = QListWidget()
        self.files.currentRowChanged.connect(self._select)
        layout.addWidget(self.files, 1)
        form = QFormLayout()
        self.name = QLineEdit()
        self.path = QLineEdit()
        browse = QPushButton("Procurar")
        browse.setObjectName("ghost")
        browse.clicked.connect(self._browse)
        path_host = QWidget()
        path_row = QHBoxLayout(path_host)
        path_row.setContentsMargins(0, 0, 0, 0)
        path_row.addWidget(self.path)
        path_row.addWidget(browse)
        self.command_time = QSpinBox()
        self.command_time.setRange(1, 3600)
        self.command_time.setSuffix(" s")
        self.command_time.setValue(int(settings.command_timeout))
        self.analysis_time = QSpinBox()
        self.analysis_time.setRange(30, 7200)
        self.analysis_time.setSuffix(" s")
        self.analysis_time.setValue(900)
        form.addRow("Nome", self.name)
        form.addRow("Caminho", path_host)
        form.addRow("Tempo de comando", self.command_time)
        form.addRow("Tempo de análise", self.analysis_time)
        layout.addLayout(form)
        self.result = QLabel("")
        self.result.setWordWrap(True)
        self.result.setObjectName("muted")
        layout.addWidget(self.result)
        row = QHBoxLayout()
        add = QPushButton("Adicionar")
        add.setObjectName("primary")
        update = QPushButton("Atualizar")
        update.setObjectName("ghost")
        remove = QPushButton("Remover")
        remove.setObjectName("ghost")
        add.clicked.connect(self._add)
        update.clicked.connect(self._update)
        remove.clicked.connect(self._remove)
        row.addWidget(add)
        row.addWidget(update)
        row.addWidget(remove)
        row.addStretch(1)
        layout.addLayout(row)
        self._fill()

    def _fill(self) -> None:
        self.files.clear()
        for item in self.settings.programs or []:
            self.files.addItem(
                f"{item['nome']}  ·  comando {item['tempo_comando']} s  ·  análise {item['tempo_analise']} s"
            )

    def _select(self, row: int) -> None:
        programs = self.settings.programs or []
        if row < 0 or row >= len(programs):
            self._editing = -1
            return
        item = programs[row]
        self._editing = row
        self.name.setText(item["nome"])
        self.path.setText(item["caminho"])
        self.command_time.setValue(int(item["tempo_comando"]))
        self.analysis_time.setValue(int(item["tempo_analise"]))

    def _browse(self) -> None:
        chosen, _ = QFileDialog.getOpenFileName(self, "Programa", self.path.text())
        if chosen:
            self.path.setText(chosen)

    def _entry(self) -> dict | None:
        name = self.name.text().strip()
        path = self.path.text().strip()
        if not name or not path:
            self.result.setText("Informe o nome e o caminho do programa.")
            return None
        return {
            "nome": name,
            "caminho": path,
            "tempo_comando": self.command_time.value(),
            "tempo_analise": self.analysis_time.value(),
        }

    def _store(self, programs: list) -> None:
        from ollamapilot.config import normalize_programs

        self.settings.programs = normalize_programs(programs)
        self.settings.save()
        self._fill()

    def _add(self) -> None:
        entry = self._entry()
        if entry is None:
            return
        programs = list(self.settings.programs or [])
        programs.append(entry)
        self._store(programs)
        self.result.setText(f"{entry['nome']} foi adicionado.")
        self.name.clear()
        self.path.clear()
        self._editing = -1

    def _update(self) -> None:
        entry = self._entry()
        if entry is None or self._editing < 0:
            if self._editing < 0:
                self.result.setText("Selecione um programa da lista para atualizar.")
            return
        programs = list(self.settings.programs or [])
        programs[self._editing] = entry
        self._store(programs)
        self.result.setText(f"{entry['nome']} foi atualizado.")

    def _remove(self) -> None:
        row = self.files.currentRow()
        programs = list(self.settings.programs or [])
        if row < 0 or row >= len(programs):
            self.result.setText("Selecione um programa para remover.")
            return
        name = programs[row]["nome"]
        del programs[row]
        self._editing = -1
        self._store(programs)
        self.result.setText(f"{name} foi removido.")


class HistoryDialog(QDialog):
    """Lista das conversas salvas: abrir, excluir as selecionadas ou excluir por período."""

    PERIODS = [
        ("Mais antigas que 7 dias", 7),
        ("Mais antigas que 30 dias", 30),
        ("Mais antigas que 90 dias", 90),
        ("Mais antigas que 1 ano", 365),
        ("Entre as datas…", "intervalo"),
        ("Todas as conversas", "todas"),
    ]

    def __init__(self, window) -> None:
        super().__init__(window)
        from ollamapilot import history

        self.history = history
        self.main = window
        self.setWindowTitle("Conversas anteriores")
        self.resize(760, 560)
        layout = QVBoxLayout(self)
        title = QLabel("Conversas anteriores")
        title.setStyleSheet("font-size: 18px; font-weight: 700;")
        note = QLabel(
            "Cada conversa fica em storage\\conversas, com o texto e os anexos de texto. "
            "Ao reabrir uma conversa, as autorizações \"neste chat\" não voltam: a próxima ação pede o cartão de novo. "
            "Ctrl ou Shift selecionam várias."
        )
        note.setWordWrap(True)
        note.setObjectName("muted")
        layout.addWidget(title)
        layout.addWidget(note)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Filtrar pelo título…")
        self.search.textChanged.connect(self._fill)
        layout.addWidget(self.search)
        self.table = QTreeWidget()
        self.table.setHeaderLabels(["Título", "Última mensagem", "Pedidos", "Tamanho"])
        self.table.setRootIsDecorated(False)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.itemDoubleClicked.connect(lambda _item, _column: self._open())
        self.table.itemSelectionChanged.connect(self._selection_changed)
        layout.addWidget(self.table, 1)
        actions = QHBoxLayout()
        self.open_button = QPushButton("Abrir")
        self.open_button.setObjectName("primary")
        self.open_button.clicked.connect(self._open)
        self.rename_button = QPushButton("Mudar o título")
        self.rename_button.setObjectName("ghost")
        self.rename_button.clicked.connect(self._rename)
        self.pin_button = QPushButton("Fixar")
        self.pin_button.setObjectName("ghost")
        self.pin_button.clicked.connect(self._toggle_pin)
        self.delete_button = QPushButton("Excluir selecionadas")
        self.delete_button.setObjectName("ghost")
        self.delete_button.clicked.connect(self._delete_selected)
        actions.addWidget(self.open_button)
        actions.addWidget(self.rename_button)
        actions.addWidget(self.pin_button)
        actions.addWidget(self.delete_button)
        actions.addStretch(1)
        layout.addLayout(actions)
        period_box = QHBoxLayout()
        self.period = QComboBox()
        for label, value in self.PERIODS:
            self.period.addItem(label, value)
        self.period.currentIndexChanged.connect(self._period_changed)
        today = QDate.currentDate()
        self.start = QDateEdit(today.addDays(-30))
        self.end = QDateEdit(today)
        for edit in (self.start, self.end):
            edit.setCalendarPopup(True)
            edit.setDisplayFormat("dd/MM/yyyy")
        self.period_delete = QPushButton("Excluir do período")
        self.period_delete.setObjectName("ghost")
        self.period_delete.clicked.connect(self._delete_period)
        period_box.addWidget(QLabel("Período"))
        period_box.addWidget(self.period, 1)
        period_box.addWidget(QLabel("de"))
        period_box.addWidget(self.start)
        period_box.addWidget(QLabel("até"))
        period_box.addWidget(self.end)
        period_box.addWidget(self.period_delete)
        layout.addLayout(period_box)
        self.feedback = QLabel("")
        self.feedback.setObjectName("muted")
        self.feedback.setWordWrap(True)
        layout.addWidget(self.feedback)
        self._rows: list[dict] = []
        self._reload()
        self._period_changed()

    def _reload(self) -> None:
        self._rows = self.history.list_conversations()
        self._fill()

    def _fill(self) -> None:
        wanted = self.search.text().strip().lower()
        self.table.clear()
        current = getattr(self.main, "conversation_id", "")
        for row in self._rows:
            if wanted and wanted not in row["titulo"].lower():
                continue
            title = row["titulo"]
            if row.get("fixada"):
                title = "Fixada · " + title
            if row["id"] == current:
                title += "  (aberta)"
            item = QTreeWidgetItem([title, _when(row["atualizada"]), str(row["mensagens"]), _size(row["bytes"])])
            item.setData(0, Qt.ItemDataRole.UserRole, row["id"])
            item.setData(1, Qt.ItemDataRole.UserRole, row["atualizada"])
            item.setToolTip(0, f"Criada em {_when(row['criada'])}")
            self.table.addTopLevelItem(item)
        self.table.resizeColumnToContents(1)
        self.table.setColumnWidth(0, 400)
        shown = self.table.topLevelItemCount()
        self.feedback.setText(f"{shown} de {len(self._rows)} conversas · {_size(sum(r['bytes'] for r in self._rows))} no disco.")
        self._selection_changed()

    def _selected_ids(self) -> list[str]:
        return [str(item.data(0, Qt.ItemDataRole.UserRole)) for item in self.table.selectedItems()]

    def _selection_changed(self) -> None:
        ids = self._selected_ids()
        count = len(ids)
        rows = [row for row in self._rows if row["id"] in ids]
        self.open_button.setEnabled(count == 1)
        self.rename_button.setEnabled(count == 1 and bool(rows) and rows[0].get("legivel", True))
        pinned = [row for row in rows if row.get("fixada")]
        self.pin_button.setEnabled(count > 0 and all(row.get("legivel", True) for row in rows))
        self.pin_button.setText("Desafixar" if rows and len(pinned) == len(rows) else "Fixar")
        self.delete_button.setEnabled(count > 0)
        self.delete_button.setText(f"Excluir selecionadas ({count})" if count else "Excluir selecionadas")

    def _period_changed(self) -> None:
        between = self.period.currentData() == "intervalo"
        self.start.setEnabled(between)
        self.end.setEnabled(between)

    def _rename(self) -> None:
        ids = self._selected_ids()
        if len(ids) != 1:
            return
        row = next((item for item in self._rows if item["id"] == ids[0]), None)
        if row is None or not row.get("legivel", True):
            return
        text, accepted = QInputDialog.getText(self, "Mudar o título", "Título da conversa:", text=row["titulo"])
        if not accepted:
            return
        try:
            title = self.history.rename(ids[0], text)
        except (OSError, ValueError) as error:
            self.feedback.setText(str(error))
            return
        self._reload()
        self.feedback.setText(f"Título alterado para {title}.")

    def _toggle_pin(self) -> None:
        ids = self._selected_ids()
        rows = [row for row in self._rows if row["id"] in ids and row.get("legivel", True)]
        if not rows:
            return
        pinned = all(row.get("fixada") for row in rows)
        for row in rows:
            try:
                self.history.set_pinned(row["id"], not pinned)
            except (OSError, ValueError) as error:
                self.feedback.setText(str(error))
                return
        self._reload()
        self.feedback.setText("Conversa desafixada." if pinned else "Conversa fixada. Ela fica no topo, em ordem alfabética.")

    def _open(self) -> None:
        ids = self._selected_ids()
        if len(ids) == 1 and self.main.open_conversation(ids[0]):
            self.accept()

    def _delete_selected(self) -> None:
        self._confirm_and_delete(self._selected_ids())

    def _delete_period(self) -> None:
        from datetime import datetime, timedelta

        choice = self.period.currentData()
        if choice == "todas":
            ids = [row["id"] for row in self._rows]
        elif choice == "intervalo":
            first, last = self.start.date(), self.end.date()
            if first > last:
                self.feedback.setText("A data inicial vem depois da final.")
                return
            start = datetime(first.year(), first.month(), first.day())
            end = datetime(last.year(), last.month(), last.day()) + timedelta(days=1)
            ids = self.history.in_period(self._rows, start, end)
        else:
            ids = self.history.in_period(self._rows, None, datetime.now() - timedelta(days=int(choice)))
        if not ids:
            self.feedback.setText("Nenhuma conversa nesse período.")
            return
        self._confirm_and_delete(ids, self.period.currentText().rstrip("…").lower())

    def _confirm_and_delete(self, ids: list[str], period: str = "") -> None:
        if not ids:
            return
        current = getattr(self.main, "conversation_id", "")
        if current in ids and self.main.worker and self.main.worker.isRunning():
            self.feedback.setText("A conversa aberta está gerando uma resposta. Interrompa antes de excluí-la.")
            return
        titles = [row["titulo"] for row in self._rows if row["id"] in ids][:5]
        text = f"Excluir {len(ids)} conversa(s)" + (f" ({period})" if period else "") + "? Não dá para desfazer.\n\n"
        text += "\n".join(f"• {title}" for title in titles)
        if len(ids) > len(titles):
            text += f"\n… e mais {len(ids) - len(titles)}"
        if current in ids:
            text += "\n\nA conversa aberta está entre elas; a janela começa uma nova."
        answer = QMessageBox.question(self, "Excluir conversas", text)
        if answer != QMessageBox.StandardButton.Yes:
            return
        removed = self.history.delete(ids)
        self.main.conversations_deleted(ids)
        self._reload()
        self.feedback.setText(f"{removed} conversa(s) excluída(s). " + self.feedback.text())


def _when(stamp: str) -> str:
    from datetime import datetime

    try:
        return datetime.fromisoformat(stamp).strftime("%d/%m/%Y %H:%M")
    except ValueError:
        return stamp or "?"


def _size(count: int) -> str:
    if count >= 1024 * 1024:
        return f"{count / 1024 / 1024:.1f} MB".replace(".", ",")
    return f"{max(1, count // 1024)} KB"


class McpDialog(QDialog):
    """Mostra os servidores do mcp.json, as ferramentas e as políticas, e liga ou desliga os processos."""

    def __init__(self, settings, parent=None) -> None:
        super().__init__(parent)
        from ollamapilot import mcp

        self.mcp = mcp
        self.settings = settings
        self._job = None
        self.setWindowTitle("Servidores MCP")
        self.resize(640, 520)
        layout = QVBoxLayout(self)
        title = QLabel("Servidores MCP")
        title.setStyleSheet("font-size: 18px; font-weight: 700;")
        note = QLabel(
            "Só os servidores locais (stdio) declarados no mcp.json com \"ativo\": true são iniciados, e só com o MCP ligado em Configurações. "
            "Cada ferramenta segue a política do arquivo: perguntar (padrão) mostra o cartão, liberada dispensa, bloqueada esconde. "
            "O servidor recebe só as variáveis básicas do Windows e as do campo \"ambiente\". "
            "Com modelo online, as ferramentas ficam de fora, salvo \"permitir_nuvem\": true."
        )
        note.setWordWrap(True)
        note.setObjectName("muted")
        layout.addWidget(title)
        layout.addWidget(note)
        self.file_label = QLabel(f"Arquivo: {mcp.config_path()}")
        self.file_label.setObjectName("muted")
        self.file_label.setWordWrap(True)
        self.file_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.file_label)
        self.view = QPlainTextEdit()
        self.view.setReadOnly(True)
        layout.addWidget(self.view, 1)
        self.feedback = QLabel("")
        self.feedback.setObjectName("muted")
        self.feedback.setWordWrap(True)
        layout.addWidget(self.feedback)
        row = QHBoxLayout()
        edit = QPushButton("Abrir mcp.json")
        edit.setObjectName("ghost")
        edit.clicked.connect(self._edit)
        connect = QPushButton("Conectar agora")
        connect.setObjectName("primary")
        connect.clicked.connect(self._connect)
        stop = QPushButton("Desligar servidores")
        stop.setObjectName("ghost")
        stop.clicked.connect(self._stop)
        row.addWidget(edit)
        row.addStretch(1)
        row.addWidget(stop)
        row.addWidget(connect)
        layout.addLayout(row)
        self._show()

    def _show(self) -> None:
        status = self.mcp.MANAGER.status()
        lines = ["MCP " + ("ligado" if self.settings.mcp_enabled else "desligado em Configurações"), ""]
        if not status["servidores"]:
            lines.append("Nenhum servidor declarado.")
        for server in status["servidores"]:
            lines.append(f"{server['nome']} — {server['estado']}" + (" · permitido com modelo online" if server["nuvem"] else ""))
            lines.append(f"  comando: {server['comando']}")
            if server["erro"]:
                lines.append(f"  erro: {server['erro']}")
            for note in server["notas"]:
                lines.append(f"  aviso: {note}")
            for tool in server["ferramentas"]:
                lines.append(f"  · {tool['nome']} ({tool['politica']})")
            lines.append("")
        for error in status["erros"]:
            lines.append(f"Problema no arquivo: {error}")
        self.view.setPlainText("\n".join(lines).rstrip())

    def _edit(self) -> None:
        created = self.mcp.create_template()
        if created:
            self.feedback.setText("Criei um mcp.json de exemplo, com o servidor desligado.")
        os.startfile(str(self.mcp.config_path()))

    def _connect(self) -> None:
        if not self.settings.mcp_enabled:
            self.feedback.setText("Ligue os servidores MCP em Configurações e salve antes de conectar.")
            return
        if self._job and self._job.isRunning():
            self.feedback.setText("Aguarde a conexão atual.")
            return
        self.feedback.setText("Conectando…")

        def work(_progress):
            self.mcp.MANAGER.reconcile(self.settings, retry=True)
            return True

        job = FunctionWorker(work)
        job.succeeded.connect(lambda _ok: (self.feedback.setText("Pronto."), self._show()))
        job.failed.connect(self.feedback.setText)
        self._job = job
        job.start()

    def _stop(self) -> None:
        self.mcp.MANAGER.shutdown()
        self.feedback.setText("Servidores desligados. Voltam a iniciar na próxima mensagem, se o MCP continuar ligado.")
        self._show()
