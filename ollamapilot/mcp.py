"""Cliente MCP por stdio, só para servidores declarados em storage/mcp.json.

Nada é descoberto sozinho: um servidor só inicia quando o MCP está ligado em
Configurações e o servidor tem "ativo": true. Cada ferramenta vira uma Tool do
registry e passa pela mesma validação, autorização e trilha das demais.
"""

from __future__ import annotations

import atexit
import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable
from typing import Any

from ollamapilot import paths
from ollamapilot.logbook import log_error, redact
from ollamapilot.process_run import CREATE_NO_WINDOW, kill_tree
from ollamapilot.tools.base import Tool

PROTOCOL_VERSION = "2025-06-18"
POLICIES = ("perguntar", "liberada", "bloqueada")
MAX_SERVERS = 8
MAX_TOOLS_PER_SERVER = 32
MAX_TOOLS_TOTAL = 48
MAX_SCHEMA_CHARS = 6000
MAX_DESCRIPTION = 500
MAX_RESULT_CHARS = 20_000
MAX_LINE_BYTES = 8 * 1024 * 1024
INIT_TIMEOUT = 20
_NAME = re.compile(r"^[A-Za-z0-9_-]{1,24}$")
# Só o necessário para um processo do Windows funcionar; chaves e tokens do
# ambiente do OllamaPilot não passam para o servidor.
_ENV_KEYS = (
    "SYSTEMROOT", "WINDIR", "COMSPEC", "PATH", "PATHEXT", "TEMP", "TMP", "USERPROFILE",
    "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)",
    "HOMEDRIVE", "HOMEPATH", "USERNAME", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "OS",
)

TEMPLATE = {
    "versao": 1,
    "servidores": [
        {
            "nome": "exemplo",
            "ativo": False,
            "comando": "C:\\caminho\\do\\servidor.exe",
            "argumentos": [],
            "pasta": "",
            "ambiente": {},
            "timeout_segundos": 60,
            "permitir_nuvem": False,
            "padrao": "perguntar",
            "ferramentas": {},
        }
    ],
}


class McpError(RuntimeError):
    pass


class McpCancelled(McpError):
    pass


def config_path():
    return paths.STORAGE / "mcp.json"


def create_template() -> bool:
    """Cria o mcp.json de exemplo, com o servidor desligado. Não sobrescreve um existente."""
    path = config_path()
    if path.exists():
        return False
    paths.write_text_atomic(path, json.dumps(TEMPLATE, ensure_ascii=False, indent=2))
    return True


def load_config() -> tuple[list[dict], list[str]]:
    """Lê e valida o mcp.json. Um servidor com erro fica de fora e o motivo aparece no status."""
    path = config_path()
    if not path.is_file():
        return [], []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        return [], [f"mcp.json ilegível: {error}"]
    servers = raw.get("servidores") if isinstance(raw, dict) else None
    if not isinstance(servers, list):
        return [], ["mcp.json precisa de uma lista em \"servidores\"."]
    specs: list[dict] = []
    errors: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(servers):
        spec, problem = _validate_spec(item, index)
        if problem:
            errors.append(problem)
            continue
        if spec["nome"] in seen:
            errors.append(f"servidor '{spec['nome']}' aparece duas vezes.")
            continue
        seen.add(spec["nome"])
        specs.append(spec)
    if len(specs) > MAX_SERVERS:
        errors.append(f"só os {MAX_SERVERS} primeiros servidores são usados.")
        specs = specs[:MAX_SERVERS]
    return specs, errors


def _validate_spec(item: object, index: int) -> tuple[dict, str]:
    if not isinstance(item, dict):
        return {}, f"servidor {index + 1}: precisa ser um objeto."
    name = item.get("nome")
    if not isinstance(name, str) or not _NAME.match(name):
        return {}, f"servidor {index + 1}: nome precisa ter de 1 a 24 letras, números, _ ou -."
    if "url" in item:
        return {}, f"servidor '{name}': só o transporte stdio é aceito; remova \"url\"."
    command = item.get("comando")
    if not isinstance(command, str) or not command.strip():
        return {}, f"servidor '{name}': falta \"comando\"."
    arguments = item.get("argumentos", [])
    if not isinstance(arguments, list) or not all(isinstance(arg, str) for arg in arguments):
        return {}, f"servidor '{name}': \"argumentos\" precisa ser uma lista de textos."
    environment = item.get("ambiente", {})
    if not isinstance(environment, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in environment.items()
    ):
        return {}, f"servidor '{name}': \"ambiente\" precisa ser um objeto de textos."
    folder = item.get("pasta", "")
    if not isinstance(folder, str):
        return {}, f"servidor '{name}': \"pasta\" precisa ser texto."
    default = item.get("padrao", "perguntar")
    if default not in POLICIES:
        return {}, f"servidor '{name}': \"padrao\" deve ser perguntar, liberada ou bloqueada."
    tools = item.get("ferramentas", {})
    if not isinstance(tools, dict) or not all(value in POLICIES for value in tools.values()):
        return {}, f"servidor '{name}': cada valor em \"ferramentas\" deve ser perguntar, liberada ou bloqueada."
    timeout = item.get("timeout_segundos", 60)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        timeout = 60
    spec = {
        "nome": name,
        "ativo": item.get("ativo") is True,
        "comando": command.strip(),
        "argumentos": list(arguments),
        "pasta": folder.strip(),
        "ambiente": dict(environment),
        "timeout_segundos": int(min(max(timeout, 5), 600)),
        "permitir_nuvem": item.get("permitir_nuvem") is True,
        "padrao": default,
        "ferramentas": dict(tools),
    }
    spec["assinatura"] = hashlib.sha256(
        json.dumps({key: spec[key] for key in ("comando", "argumentos", "pasta", "ambiente")}, sort_keys=True).encode()
    ).hexdigest()[:16]
    return spec, ""


def server_environment(extra: dict[str, str]) -> dict[str, str]:
    env = {key: os.environ[key] for key in _ENV_KEYS if key in os.environ}
    env.update(_resolve_secrets(extra))
    return env


_SECRET_REF = re.compile(r"^\$\{cofre:([A-Za-z0-9_-]{1,40})\}$")


def _resolve_secrets(extra: dict[str, str]) -> dict[str, str]:
    """${cofre:nome} lê o Gerenciador de Credenciais só na hora de iniciar o processo."""
    from ollamapilot.secrets import current

    resolved = {}
    store = current()
    for key, value in extra.items():
        match = _SECRET_REF.match(str(value).strip())
        if not match:
            resolved[key] = value
            continue
        secret = store.get(match.group(1))
        if not secret:
            raise McpError(f"O segredo '{match.group(1)}' não está no cofre do Windows.")
        resolved[key] = secret
    return resolved


def resolve_command(command: str, env: dict[str, str]) -> str:
    if os.path.isabs(command):
        if not os.path.isfile(command):
            raise McpError(f"O comando não existe: {command}")
        return command
    found = shutil.which(command, path=env.get("PATH"))
    if not found:
        raise McpError(f"O comando não foi encontrado no PATH: {command}")
    return found


class McpServer:
    """Um processo de servidor MCP e a conversa JSON-RPC com ele."""

    def __init__(self, spec: dict, spawn: Callable[[dict], Any] | None = None) -> None:
        self.spec = spec
        self.name = spec["nome"]
        self._spawn = spawn or self._spawn_process
        self.proc = None
        self.state = "desligado"
        self.error = ""
        self.tools: list[dict] = []
        self.server_info: dict = {}
        self.stale = False
        self.notes: list[str] = []
        self._pending: dict[int, dict] = {}
        self._next_id = 0
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._stderr: deque[str] = deque(maxlen=40)
        self._readers: list[threading.Thread] = []

    def _spawn_process(self, spec: dict):
        env = server_environment(spec["ambiente"])
        executable = resolve_command(spec["comando"], env)
        folder = spec["pasta"] or None
        if folder and not os.path.isdir(folder):
            raise McpError(f"A pasta do servidor não existe: {folder}")
        return subprocess.Popen(
            [executable, *spec["argumentos"]],
            cwd=folder,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0,
        )

    def start(self, cancel: Callable[[], bool] | None = None) -> None:
        self.state = "iniciando"
        self.error = ""
        self.notes = []
        try:
            self.proc = self._spawn(self.spec)
            self._readers = [
                threading.Thread(target=self._read_stdout, daemon=True),
                threading.Thread(target=self._read_stderr, daemon=True),
            ]
            for reader in self._readers:
                reader.start()
            result = self.request(
                "initialize",
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "OllamaPilot", "version": "1"},
                },
                timeout=INIT_TIMEOUT,
                cancel=cancel,
            )
            if not isinstance(result, dict) or "protocolVersion" not in result:
                raise McpError("O servidor respondeu ao initialize sem protocolVersion.")
            self.server_info = result.get("serverInfo") if isinstance(result.get("serverInfo"), dict) else {}
            if not isinstance(result.get("capabilities"), dict) or "tools" not in result["capabilities"]:
                self.notes.append("o servidor não declara ferramentas")
            self.notify("notifications/initialized")
            self.refresh_tools(cancel)
            self.state = "pronto"
        except McpError as error:
            self.state = "falhou"
            self.error = redact(str(error))[:400]
            self.stop()
            raise
        except OSError as error:
            self.state = "falhou"
            self.error = redact(f"O servidor não iniciou: {error}")[:400]
            self.stop()
            raise McpError(self.error) from error

    def refresh_tools(self, cancel: Callable[[], bool] | None = None) -> None:
        tools: list[dict] = []
        cursor = None
        for _page in range(5):
            params = {"cursor": cursor} if cursor else {}
            result = self.request("tools/list", params, timeout=INIT_TIMEOUT, cancel=cancel)
            page = result.get("tools") if isinstance(result, dict) else None
            if isinstance(page, list):
                tools.extend(item for item in page if isinstance(item, dict) and isinstance(item.get("name"), str))
            cursor = result.get("nextCursor") if isinstance(result, dict) else None
            if not cursor:
                break
        self.tools = tools
        self.stale = False

    def call_tool(self, name: str, arguments: dict, cancel: Callable[[], bool] | None = None) -> dict:
        result = self.request(
            "tools/call",
            {"name": name, "arguments": arguments},
            timeout=self.spec["timeout_segundos"],
            cancel=cancel,
        )
        if not isinstance(result, dict):
            raise McpError("O servidor devolveu um resultado fora do formato.")
        return result

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def request(self, method: str, params: dict, timeout: float, cancel: Callable[[], bool] | None = None):
        if not self.alive():
            raise McpError(self._exit_message())
        with self._lock:
            self._next_id += 1
            request_id = self._next_id
            slot = {"event": threading.Event(), "message": None}
            self._pending[request_id] = slot
        try:
            self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
            deadline = time.monotonic() + max(1.0, float(timeout))
            while not slot["event"].wait(0.1):
                if cancel is not None and cancel():
                    self._cancel_request(request_id, "Cancelado pelo usuário")
                    raise McpCancelled("Cancelado pelo usuário.")
                if time.monotonic() >= deadline:
                    self._cancel_request(request_id, "Tempo esgotado")
                    raise McpError(f"O servidor MCP '{self.name}' não respondeu em {int(timeout)} s ({method}).")
                if not self.alive():
                    raise McpError(self._exit_message())
        finally:
            with self._lock:
                self._pending.pop(request_id, None)
        message = slot["message"]
        if message is None:
            raise McpError(self._exit_message())
        if "error" in message:
            error = message["error"] if isinstance(message["error"], dict) else {}
            raise McpError(f"O servidor MCP '{self.name}' recusou {method}: {str(error.get('message') or error)[:400]}")
        return message.get("result")

    def notify(self, method: str, params: dict | None = None) -> None:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        self._send(payload)

    def _cancel_request(self, request_id: int, reason: str) -> None:
        try:
            self.notify("notifications/cancelled", {"requestId": request_id, "reason": reason})
        except McpError:
            pass

    def _send(self, payload: dict) -> None:
        data = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        with self._write_lock:
            try:
                assert self.proc is not None and self.proc.stdin is not None
                self.proc.stdin.write(data)
                self.proc.stdin.flush()
            except (OSError, ValueError, AssertionError) as error:
                raise McpError(f"Não foi possível falar com o servidor MCP '{self.name}': {error}") from error

    def _read_stdout(self) -> None:
        stream = self.proc.stdout
        while True:
            try:
                line = stream.readline(MAX_LINE_BYTES)
            except (OSError, ValueError):
                return
            if not line:
                return
            if not line.endswith(b"\n") and len(line) >= MAX_LINE_BYTES:
                self.notes.append("uma mensagem maior que 8 MB foi descartada")
                while True:
                    rest = stream.readline(MAX_LINE_BYTES)
                    if not rest or rest.endswith(b"\n"):
                        break
                continue
            try:
                message = json.loads(line.decode("utf-8", errors="replace"))
            except ValueError:
                continue
            if isinstance(message, dict):
                self._dispatch(message)

    def _dispatch(self, message: dict) -> None:
        if "method" in message:
            method = message.get("method")
            if "id" in message:
                # O OllamaPilot não oferece amostragem, raízes nem perguntas ao usuário para o servidor.
                if method == "ping":
                    reply = {"jsonrpc": "2.0", "id": message["id"], "result": {}}
                else:
                    reply = {
                        "jsonrpc": "2.0",
                        "id": message["id"],
                        "error": {"code": -32601, "message": f"O OllamaPilot não atende {method}."},
                    }
                try:
                    self._send(reply)
                except McpError:
                    pass
            elif method == "notifications/tools/list_changed":
                self.stale = True
            return
        request_id = message.get("id")
        with self._lock:
            slot = self._pending.get(request_id) if isinstance(request_id, int) else None
        if slot is not None:
            slot["message"] = message
            slot["event"].set()

    def _read_stderr(self) -> None:
        stream = getattr(self.proc, "stderr", None)
        if stream is None:
            return
        while True:
            try:
                line = stream.readline(4096)
            except (OSError, ValueError):
                return
            if not line:
                return
            self._stderr.append(redact(line.decode("utf-8", errors="replace").rstrip())[:300])

    def _exit_message(self) -> str:
        code = None if self.proc is None else self.proc.poll()
        tail = " | ".join(list(self._stderr)[-3:])
        text = f"O servidor MCP '{self.name}' não está em execução"
        if code is not None:
            text += f" (saiu com código {code})"
        return text + (f": {tail}" if tail else ".")

    def stop(self) -> None:
        proc, self.proc = self.proc, None
        if self.state != "falhou":
            self.state = "desligado"
        if proc is None:
            return
        try:
            if proc.stdin is not None:
                proc.stdin.close()
        except (OSError, ValueError):
            pass
        deadline = time.monotonic() + 2
        while proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        if proc.poll() is None:
            if isinstance(proc, subprocess.Popen):
                kill_tree(proc)
            else:
                proc.kill()
        # Fechar um pipe com a leitura ainda bloqueada nele trava o BufferedReader.
        for reader, stream in zip(self._readers, (getattr(proc, "stdout", None), getattr(proc, "stderr", None))):
            reader.join(timeout=2)
            if reader.is_alive() or stream is None:
                continue
            try:
                stream.close()
            except (OSError, ValueError):
                pass
        self._readers = []
        with self._lock:
            for slot in self._pending.values():
                slot["event"].set()


class McpManager:
    """Mantém os servidores ativos entre as rodadas e os desliga quando o MCP é desligado."""

    def __init__(self, spawn: Callable[[dict], Any] | None = None) -> None:
        self._spawn = spawn
        self._servers: dict[str, McpServer] = {}
        self._failed: dict[str, str] = {}
        self._failures: dict[str, str] = {}
        self._errors: list[str] = []
        self._lock = threading.RLock()

    def reconcile(self, settings, cancel: Callable[[], bool] | None = None, retry: bool = False) -> None:
        with self._lock:
            if not getattr(settings, "mcp_enabled", False):
                self.shutdown()
                self._errors = []
                return
            specs, self._errors = load_config()
            wanted = {spec["nome"]: spec for spec in specs if spec["ativo"]}
            for name in list(self._servers):
                server = self._servers[name]
                spec = wanted.get(name)
                if spec is None or spec["assinatura"] != server.spec["assinatura"]:
                    server.stop()
                    self._servers.pop(name)
                elif spec != server.spec:
                    server.spec = spec
            if retry:
                self._failed.clear()
            for name, spec in wanted.items():
                server = self._servers.get(name)
                if server is not None and server.alive():
                    if server.stale:
                        try:
                            server.refresh_tools(cancel)
                        except McpError as error:
                            server.notes.append(str(error)[:200])
                    continue
                # A mesma configuração que já falhou não é tentada de novo a cada mensagem.
                # Conectar agora limpa _failed antes de chegar aqui.
                if self._failed.get(name) == spec["assinatura"]:
                    continue
                if cancel is not None and cancel():
                    return
                server = McpServer(spec, self._spawn)
                try:
                    server.start(cancel)
                except McpCancelled:
                    continue
                except McpError as error:
                    self._failed[name] = spec["assinatura"]
                    self._failures[name] = server.error or str(error)
                    log_error("mcp", f"Servidor '{name}' não iniciou: {server.error or 'falha sem detalhe'}")
                    continue
                self._failures.pop(name, None)
                self._servers[name] = server

    def tools(self, settings, cancel: Callable[[], bool]) -> list[Tool]:
        try:
            self.reconcile(settings, cancel)
        except Exception as error:  # um servidor quebrado não pode derrubar o turno
            log_error("mcp", f"Falha ao preparar os servidores MCP: {redact(str(error))[:400]}")
            return []
        result: list[Tool] = []
        used: set[str] = set()
        with self._lock:
            for server in self._servers.values():
                if not server.alive():
                    continue
                if settings.model_remote and not server.spec["permitir_nuvem"]:
                    continue
                count = 0
                for info in server.tools:
                    policy = server.spec["ferramentas"].get(info["name"], server.spec["padrao"])
                    if policy == "bloqueada":
                        continue
                    if count >= MAX_TOOLS_PER_SERVER or len(result) >= MAX_TOOLS_TOTAL:
                        break
                    tool = McpTool.build(server, info, policy, cancel, used)
                    if tool is None:
                        continue
                    used.add(tool.name)
                    result.append(tool)
                    count += 1
        return result

    def status(self) -> dict:
        specs, errors = load_config()
        with self._lock:
            rows = []
            for spec in specs:
                server = self._servers.get(spec["nome"])
                if server is not None and server.alive():
                    state = "pronto"
                elif not spec["ativo"]:
                    state = "inativo no mcp.json"
                elif spec["nome"] in self._failures:
                    state = "falhou"
                else:
                    state = "desligado"
                rows.append(
                    {
                        "nome": spec["nome"],
                        "estado": state,
                        "comando": spec["comando"],
                        "ferramentas": [
                            {
                                "nome": info["name"],
                                "politica": spec["ferramentas"].get(info["name"], spec["padrao"]),
                            }
                            for info in (server.tools if server is not None else [])
                        ],
                        "erro": self._failures.get(spec["nome"], ""),
                        "notas": list(server.notes) if server is not None else [],
                        "nuvem": spec["permitir_nuvem"],
                    }
                )
            return {"arquivo": str(config_path()), "servidores": rows, "erros": errors or self._errors}

    def shutdown(self) -> None:
        with self._lock:
            for server in self._servers.values():
                server.stop()
            self._servers.clear()
            self._failed.clear()
            self._failures.clear()


class McpTool(Tool):
    def __init__(self, server: McpServer, info: dict, policy: str, cancel: Callable[[], bool], name: str, parameters: dict) -> None:
        self.server = server
        self.remote_name = info["name"]
        self.info = info
        self.policy = policy
        self.cancel = cancel
        self.name = name
        self.sensitive = policy != "liberada"
        self.grant_key = name
        self.scope_label = f"usar {info['name']} do servidor MCP {server.name}"
        self.parameters = parameters
        text = " ".join(str(info.get("description") or info.get("title") or "").split())
        if len(text) > MAX_DESCRIPTION:
            text = text[: MAX_DESCRIPTION - 1] + "…"
        self.description = f"[MCP {server.name}] {text} Retorno: {{texto, estruturado, sucesso, truncated}}."

    @classmethod
    def build(cls, server: McpServer, info: dict, policy: str, cancel, used: set[str]) -> "McpTool | None":
        parameters = _clean_schema(info.get("inputSchema"))
        if parameters is None:
            server.notes.append(f"{info['name']}: schema ausente ou grande demais; ferramenta ignorada")
            return None
        base = re.sub(r"[^A-Za-z0-9_-]", "_", f"mcp_{server.name}_{info['name']}")[:60]
        name = base
        counter = 2
        while name in used:
            name = f"{base[:57]}_{counter}"
            counter += 1
        return cls(server, info, policy, cancel, name, parameters)

    def preview(self, arguments: dict) -> dict:
        hints = _hints(self.info.get("annotations"))
        body = json.dumps(arguments, ensure_ascii=False, indent=2, default=str)
        if len(body) > 4000:
            body = body[:4000] + "\n…"
        lines = [
            f"Servidor MCP: {self.server.name} ({self.server.spec['comando']})",
            f"Ferramenta: {self.remote_name}",
        ]
        if hints:
            lines.append(f"O servidor declara: {hints}. Isso não foi verificado pelo OllamaPilot.")
        lines.append(f"Tempo máximo: {self.server.spec['timeout_segundos']} s")
        lines.append("Argumentos enviados ao servidor:")
        lines.append(body)
        return {
            "title": "Ferramenta MCP",
            "summary": f"{self.server.name} · {self.remote_name}",
            "detail": "\n".join(lines),
        }

    def run(self, arguments: dict) -> dict:
        try:
            result = self.server.call_tool(self.remote_name, arguments, self.cancel)
        except McpCancelled:
            return {"cancelado": True, "erro": "Cancelado pelo usuário.", "fonte": f"mcp:{self.server.name}"}
        return convert_result(self.server.name, result)


def convert_result(server: str, result: dict) -> dict:
    parts: list[str] = []
    for item in result.get("content") or []:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind == "text":
            parts.append(str(item.get("text") or ""))
        elif kind in ("image", "audio"):
            parts.append(f"[{kind} omitido: {item.get('mimeType') or 'tipo desconhecido'}]")
        elif kind == "resource":
            resource = item.get("resource") if isinstance(item.get("resource"), dict) else {}
            if isinstance(resource.get("text"), str):
                parts.append(f"[{resource.get('uri', 'recurso')}]\n{resource['text']}")
            else:
                parts.append(f"[recurso binário omitido: {resource.get('uri', '')}]")
        elif kind == "resource_link":
            parts.append(f"[link: {item.get('uri', '')}]")
    text = "\n".join(parts)
    truncated = len(text) > MAX_RESULT_CHARS
    if truncated:
        text = text[:MAX_RESULT_CHARS]
    output: dict[str, Any] = {"fonte": f"mcp:{server}", "sucesso": not bool(result.get("isError")), "texto": text}
    structured = result.get("structuredContent")
    if structured is not None:
        encoded = json.dumps(structured, ensure_ascii=False, default=str)
        if len(encoded) <= MAX_RESULT_CHARS:
            output["estruturado"] = structured
        else:
            truncated = True
    output["truncated"] = truncated
    if result.get("isError"):
        output["erro"] = (text or "O servidor MCP informou erro sem detalhe.")[:500]
    return output


def _clean_schema(schema: object) -> dict | None:
    if schema is None:
        schema = {"type": "object", "properties": {}}
    if not isinstance(schema, dict):
        return None
    clean = {key: value for key, value in schema.items() if key != "$schema"}
    clean["type"] = "object"
    if not isinstance(clean.get("properties"), dict):
        clean["properties"] = {}
    if len(json.dumps(clean, ensure_ascii=False, default=str)) > MAX_SCHEMA_CHARS:
        return None
    return clean


def _hints(annotations: object) -> str:
    if not isinstance(annotations, dict):
        return ""
    labels = []
    if annotations.get("readOnlyHint") is True:
        labels.append("somente leitura")
    if annotations.get("destructiveHint") is True:
        labels.append("pode apagar ou sobrescrever dados")
    if annotations.get("openWorldHint") is True:
        labels.append("acessa serviços externos")
    return ", ".join(labels)


MANAGER = McpManager()
atexit.register(MANAGER.shutdown)
