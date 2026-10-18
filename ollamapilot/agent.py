from __future__ import annotations

import json
import re
import secrets
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ollamapilot import undo
from ollamapilot.attachments import describe as describe_attachment
from ollamapilot.config import Settings
from ollamapilot.logbook import log_error, log_turn, redact
from ollamapilot.ollama_client import OllamaClient
from ollamapilot.privacy import redact_known
from ollamapilot.project_rules import project_rules
from ollamapilot.prompt import build_prompt
from ollamapilot.tools.base import validate_arguments
from ollamapilot.tools.registry import Registry, build_registry
from ollamapilot.workspace import (
    annotate_preview,
    directory_in_text,
    grant_scope,
    has_word,
    inside,
    verify_note,
    workspace_block,
)


class Cancelled(Exception):
    pass


class AgentError(RuntimeError):
    def __init__(self, message: str, **details: object) -> None:
        super().__init__(message)
        self.details = details


def _detail_args(arguments: dict) -> str:
    """Resumo dos argumentos para o log: textos longos viram o tamanho, chaves secretas somem."""
    shown: dict[str, object] = {}
    for key, value in (arguments or {}).items():
        if any(part in str(key).lower() for part in ("senha", "password", "token", "key", "secret")):
            shown[key] = "[omitido]"
        elif isinstance(value, str) and len(value) > 160:
            shown[key] = f"<{len(value)} caracteres>"
        elif isinstance(value, (list, dict)):
            text = json.dumps(value, ensure_ascii=False, default=str)
            shown[key] = text if len(text) <= 160 else f"<{type(value).__name__} com {len(value)} itens>"
        else:
            shown[key] = value
    return redact(json.dumps(shown, ensure_ascii=False, default=str))


def asks_to_write(question: str) -> bool:
    folded = question.lower()
    return bool(_PATHS.search(question)) and any(verb in folded for verb in _WRITE_VERBS)


def dodges_write(answer: str) -> bool:
    folded = answer.lower()
    if any(
        phrase in folded
        for phrase in (
            "não posso",
            "nao posso",
            "não consigo",
            "nao consigo",
            "não tenho acesso",
            "nao tenho acesso",
            "não tenho permissão",
            "nao tenho permissao",
            "restriç",
            "restric",
        )
    ):
        return True
    if '"name"' in folded and '"arguments"' in folded:
        return True
    if folded.lstrip().startswith("{") and "arguments" in folded:
        return True
    return "```" in answer or "<!doctype" in folded or "<html" in folded


def _decision_scope(decision: Any) -> str:
    if decision is True or decision == "once":
        return "once"
    if decision == "chat":
        return "chat"
    return "deny"


_PATHS = re.compile(r"[A-Za-z]:\\(?:[^\\/:*?\"<>|\r\n]+\\)*[^\\/:*?\"<>|\r\n]+")
_WRITE_VERBS = ("crie", "criar", "salve", "salvar", "grave", "gravar", "escreva", "escrever", "gere ", "gerar ")
_WRITE_NUDGE = (
    "O OllamaPilot grava neste computador. Chame gravar_arquivo para cada arquivo, "
    "com o caminho absoluto completo e o campo conteudo. Não recuse por segurança "
    "e não cole o JSON nem o código na resposta."
)
_WRITE_GAVE_UP = (
    "O modelo não chamou a gravação dos arquivos. Use Nova conversa ou escolha um modelo que execute ferramentas."
)
_CANCELLED = {
    "cancelado": True,
    "erro": "A ação foi interrompida antes de terminar. Não repita sem um pedido novo.",
}
_FINAL_NOW = "As ferramentas necessárias desta etapa já foram chamadas. Responda agora em linguagem natural, sem nova ferramenta."
_STALLED = (
    "As últimas chamadas não avançaram: foram repetidas, recusadas ou falharam. Não chame ferramentas. "
    "Diga o que concluiu, o que não avançou, a causa que está no resultado da ferramenta e o próximo passo. "
    "É proibido inventar causa que o resultado não trouxe, inclusive espaços no caminho. "
    "Só chame de falha o que falhou."
)
_ON_ERROR = "Explique a causa ao usuário e proponha um próximo passo seguro. Não repita a mesma chamada sem corrigir."
_SUMMARY_HEAD = "[Resumo automático do histórico anterior"
# As notas novas levam "interno": True. Os prefixos reconhecem as notas de sessões gravadas antes disso.
_INTERNAL_PREFIXES = (
    "RESULTADO CONFIÁVEL",
    "Contexto anterior compactado",
    _SUMMARY_HEAD,
    _FINAL_NOW,
    _WRITE_NUDGE,
)
_OUTGOING_KEYS = ("role", "content", "images", "audios", "tool_calls", "tool_name", "name")
_ATTACHMENTS_MARKER = "CONTEÚDO DOS ANEXOS:"
MAX_CALLS_PER_ROUND = 12
IMAGE_TOKENS = 800
STALL_LIMIT = 3


def planned_calls(reply: dict, registry) -> tuple[list[dict], bool]:
    raw = reply.get("tool_calls") or []
    if isinstance(raw, list) and raw:
        planned = []
        for call in raw:
            if not isinstance(call, dict):
                planned.append({"name": "(sem nome)", "arguments": {}, "known": False})
                continue
            function = call.get("function") if isinstance(call.get("function"), dict) else call
            name = str(function.get("name") or "").strip()
            known = bool(name) and registry.has(name)
            arguments, problem = _parse_arguments(function.get("arguments"))
            item = {"name": name or "(sem nome)", "arguments": arguments if known else {}, "known": known}
            if known and problem:
                item["invalid"] = problem
            planned.append(item)
        return planned, False
    text_calls = _calls_in_text(str(reply.get("content") or ""), registry)
    return [{"name": call["name"], "arguments": call["arguments"], "known": True} for call in text_calls], True


def strip_tool_text(content: str, registry) -> tuple[str, bool]:
    """Remove do texto visível o JSON que já foi entendido como ferramenta."""
    spans = _tool_spans(content, registry)
    if not spans:
        return content, False
    pieces: list[str] = []
    cursor = 0
    for start, end, _call in spans:
        pieces.append(content[cursor:start])
        cursor = end
    pieces.append(content[cursor:])
    text = "".join(pieces)
    text = re.sub(r"```(?:json)?[ \t]*\r?\n?\s*```", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip(), True


def repair_transcript(messages: list[dict]) -> list[dict]:
    """Garante que cada chamada de ferramenta tenha um resultado e que nenhum resultado fique solto."""
    repaired: list[dict] = []
    index = 0
    while index < len(messages):
        message = messages[index]
        if not isinstance(message, dict) or not isinstance(message.get("role"), str):
            index += 1
            continue
        if message.get("role") == "tool":
            # Resultado sem a chamada antes dele: vira nota, para o modelo não receber um par quebrado.
            name = str(message.get("tool_name") or message.get("name") or "ferramenta")
            repaired.append(
                {
                    "role": "user",
                    "content": f"RESULTADO CONFIÁVEL DA FERRAMENTA {name} (registro antigo):\n{message.get('content') or ''}",
                    "interno": True,
                }
            )
            index += 1
            continue
        repaired.append(message)
        calls = message.get("tool_calls") if message.get("role") == "assistant" else None
        if not isinstance(calls, list) or not calls:
            index += 1
            continue
        index += 1
        seen = 0
        while index < len(messages) and isinstance(messages[index], dict) and messages[index].get("role") == "tool":
            repaired.append(messages[index])
            seen += 1
            index += 1
        while seen < len(calls):
            function = calls[seen].get("function") if isinstance(calls[seen], dict) else {}
            if not isinstance(function, dict):
                function = calls[seen] if isinstance(calls[seen], dict) else {}
            name = str((function or {}).get("name") or "ferramenta")
            repaired.append(
                {
                    "role": "tool",
                    "tool_name": name,
                    "name": name,
                    "content": json.dumps(_CANCELLED, ensure_ascii=False),
                }
            )
            seen += 1
    return repaired


def is_internal(message: dict) -> bool:
    if message.get("interno"):
        return True
    if message.get("role") != "user":
        return False
    return str(message.get("content") or "").startswith(_INTERNAL_PREFIXES)


def request_index(messages: list[dict]) -> int | None:
    """Posição do último pedido escrito pelo usuário, ignorando as notas internas do OllamaPilot."""
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if message.get("role") == "user" and not is_internal(message):
            return index
    return None


def context_budget(num_ctx: int, overhead: int = 0) -> int:
    reserve = max(1024, int(num_ctx) // 8)
    return max(1024, int(num_ctx) - reserve - int(overhead))


def fit_transcript(messages: list[dict], num_ctx: int, *, overhead: int = 0, ratio: float = 1.0) -> list[dict]:
    """Reduz o histórico pelo orçamento de contexto sem separar chamada e resultado."""
    return fit_with_report(messages, num_ctx, overhead=overhead, ratio=ratio)[0]


def fit_with_report(messages: list[dict], num_ctx: int, *, overhead: int = 0, ratio: float = 1.0) -> tuple[list[dict], dict]:
    prepared = [dict(item) for item in messages]
    request = request_index(prepared)
    for index, message in enumerate(prepared):
        if index != request and not _current_capture(message, index, request):
            _shrink_old_attachment(message)
    budget = context_budget(num_ctx, overhead)
    report = {"resumidas": 0, "reduzidas": 0, "orcamento": budget}

    def cost(items: list[dict]) -> int:
        return estimate_tokens(items, ratio)

    if cost(prepared) <= budget:
        return prepared, report
    _shrink_old_tool_arguments(prepared, report)
    if cost(prepared) <= budget:
        return prepared, report
    # O pedido atual fica inteiro no tail. O que sai é o começo do histórico, em blocos:
    # uma chamada de ferramenta e o resultado dela não podem ser separados.
    if request is None:
        cut = len(prepared) - len(_blocks(prepared)[-1]) if prepared else 0
        head, tail = prepared[:cut], prepared[cut:]
    else:
        head, tail = prepared[:request], prepared[request:]
    blocks = _blocks(head)
    dropped: list[dict] = []
    while blocks:
        note = [_summary_note(dropped)] if dropped else []
        if cost(note + [item for block in blocks for item in block] + tail) <= budget:
            break
        dropped.extend(blocks.pop(0))
    kept = ([_summary_note(dropped)] if dropped else []) + [item for block in blocks for item in block]
    report["resumidas"] = len(dropped)
    if cost(kept + tail) > budget:
        tail = _reduce_tail(tail, budget - cost(kept), ratio, report)
    return kept + tail, report


def estimate_tokens(messages: list[dict], ratio: float = 1.0) -> int:
    images = sum(len(message.get("images") or []) for message in messages)
    return int((_tokens(messages) + images * IMAGE_TOKENS) * max(ratio, 0.5))


def _current_capture(message: dict, index: int, request: int | None) -> bool:
    """Captura de tela do turno atual: a imagem fica até o fim do turno."""
    return bool(message.get("captura")) and request is not None and index > request


def _shrink_old_attachment(message: dict) -> dict:
    content = str(message.get("content") or "")
    if _ATTACHMENTS_MARKER in content:
        head = content.split(_ATTACHMENTS_MARKER, 1)[0].rstrip()
        message["content"] = head + "\n\n[Anexo omitido do contexto antigo.]" if head else "[Anexo omitido do contexto antigo.]"
    message.pop("images", None)
    return message


def _shrink_old_tool_arguments(messages: list[dict], report: dict) -> None:
    """Troca textos longos dentro de chamadas já executadas pelo tamanho. O resultado continua no histórico."""
    last = max((i for i, m in enumerate(messages) if m.get("role") == "assistant" and m.get("tool_calls")), default=-1)
    for index, message in enumerate(messages):
        calls = message.get("tool_calls")
        if index == last or message.get("role") != "assistant" or not isinstance(calls, list):
            continue
        changed = False
        shrunk = []
        for call in calls:
            if not isinstance(call, dict) or not isinstance(call.get("function"), dict):
                shrunk.append(call)
                continue
            function = dict(call["function"])
            arguments = function.get("arguments")
            if isinstance(arguments, dict):
                small = {}
                for key, value in arguments.items():
                    if isinstance(value, str) and len(value) > 600:
                        small[key] = f"<{len(value)} caracteres já enviados; omitidos do contexto>"
                        changed = True
                    else:
                        small[key] = value
                function["arguments"] = small
            shrunk.append({**call, "function": function})
        if changed:
            message["tool_calls"] = shrunk
            report["reduzidas"] += 1


def _reduce_tail(tail: list[dict], room: int, ratio: float, report: dict) -> list[dict]:
    """Encolhe o turno atual sem tirar o pedido: primeiro resultados antigos, depois anexos, depois o resto."""
    tail = [dict(item) for item in tail]

    def over() -> bool:
        return estimate_tokens(tail, ratio) > room

    for index in range(1, max(1, len(tail) - 2)):
        if not over():
            return tail
        if _cut_content(tail[index], 600):
            report["reduzidas"] += 1
    if over() and tail and _ATTACHMENTS_MARKER in str(tail[0].get("content") or ""):
        head, attached = str(tail[0]["content"]).split(_ATTACHMENTS_MARKER, 1)
        spare = max(0, room - estimate_tokens(tail[1:], ratio) - estimate_tokens([{"content": head}], ratio) - 64)
        keep = max(0, int(spare * 4 / max(ratio, 0.5)))
        if keep < len(attached):
            tail[0]["content"] = (
                head + _ATTACHMENTS_MARKER + attached[:keep] + f"\n…[anexo cortado para caber no contexto: {len(attached) - keep} caracteres omitidos]"
            )
            report["reduzidas"] += 1
    for index in range(1, len(tail)):
        if not over():
            break
        if _cut_content(tail[index], 2000):
            report["reduzidas"] += 1
    return tail


def _cut_content(message: dict, limit: int) -> bool:
    if message.get("role") not in ("tool", "assistant") and not is_internal(message):
        return False
    content = str(message.get("content") or "")
    if len(content) <= limit + 80:
        return False
    message["content"] = content[:limit] + f"…[reduzido para caber no contexto: {len(content) - limit} caracteres omitidos]"
    return True


def _clean_path(match: str) -> str:
    text = match[:260]
    slash = text.rfind("\\")
    tail = text[slash + 1 :]
    extension = re.search(r"\.[A-Za-z0-9]{1,6}(?=\s|$)", tail)
    if extension:
        return (text[: slash + 1] + tail[: extension.end()]).strip()
    return (text[: slash + 1] + tail[:80]).strip()


def _summary_note(dropped: list[dict]) -> dict:
    requests: list[str] = []
    conclusions: list[str] = []
    tools: list[str] = []
    previous: list[str] = []
    paths: list[str] = []
    for message in dropped:
        role = message.get("role")
        content = str(message.get("content") or "")
        if content.startswith(_SUMMARY_HEAD) or content.startswith("Contexto anterior compactado"):
            previous.append(content.split("\n", 1)[-1][:1200])
            continue
        if role == "user" and not is_internal(message):
            requests.append(content.split(_ATTACHMENTS_MARKER, 1)[0].strip()[:200])
        elif role == "assistant":
            for call in message.get("tool_calls") or []:
                function = call.get("function") if isinstance(call, dict) else None
                if isinstance(function, dict):
                    arguments = function.get("arguments") if isinstance(function.get("arguments"), dict) else {}
                    target = arguments.get("caminho") or arguments.get("url") or arguments.get("programa") or ""
                    tools.append(f"{function.get('name')}" + (f" {str(target)[:120]}" if target else ""))
            if content.strip() and content != "Ferramentas acionadas.":
                conclusions.append(content.strip().replace("\n", " ")[:240])
        elif role == "tool" and tools:
            failed = '"erro"' in content or '"recusado"' in content
            tools[-1] += " → erro" if failed else " → ok"
        for match in _PATHS.findall(content):
            cleaned = _clean_path(match)
            if cleaned not in paths and len(paths) < 10:
                paths.append(cleaned)
    lines = [
        f"{_SUMMARY_HEAD}: {len(dropped)} mensagens antigas saíram do contexto para caber no limite do modelo. "
        "A conversa completa continua visível na janela.]"
    ]
    if previous:
        lines.append("Resumo anterior: " + " ".join(previous)[:1200])
    if requests:
        lines.append("Pedidos anteriores: " + " | ".join(requests[-6:]))
    if tools:
        lines.append("Ferramentas usadas: " + "; ".join(tools[-12:]))
    if conclusions:
        lines.append("Respostas anteriores: " + " | ".join(conclusions[-4:]))
    if paths:
        lines.append("Caminhos citados: " + ", ".join(paths))
    return {"role": "user", "content": "\n".join(lines)[:3500], "interno": True}


def _blocks(messages: list[dict]) -> list[list[dict]]:
    blocks: list[list[dict]] = []
    for message in messages:
        if message.get("role") == "tool" and blocks:
            blocks[-1].append(message)
        else:
            blocks.append([message])
    return blocks


def _tokens(messages: list[dict]) -> int:
    total = 0
    for message in messages:
        total += len(str(message.get("content") or "")) // 4
        if message.get("tool_calls"):
            total += len(json.dumps(message["tool_calls"], ensure_ascii=False, default=str)) // 4
        total += 4
    return total


def _outgoing(message: dict) -> dict:
    """Cópia enviada ao modelo. A transcrição em memória não é alterada."""
    clean = {key: message[key] for key in _OUTGOING_KEYS if key in message}
    clean["content"] = redact_known(str(clean.get("content") or ""))
    if "tool_calls" in clean:
        clean["tool_calls"] = redact_known(clean["tool_calls"])
    return clean


class Agent:
    """Conduz uma mensagem: o modelo pede ferramentas, o OllamaPilot executa e devolve o resultado."""

    def __init__(self, settings: Settings, client: OllamaClient) -> None:
        self.settings = settings
        self.client = client
        self.transcript: list[dict] = []
        self.chat_grants: set[str] = set()
        self.refused: set[str] = set()
        self.approved_plan: set[str] = set()
        self.plan_id = ""
        self._batch_skip: set[str] = set()
        self._batch_denied: set[str] = set()
        self.used_groups: set[str] = set()
        self.workspace: Path | None = None
        self.tasks: list[dict] = []
        self._images: list[str] = []
        self._image_note = ""
        self._audios: list[str] = []
        self._audio_note = ""
        self._turn_successes: list[str] = []
        self._code_paths: list[Path] = []
        self._verify_noted = False
        self._tasks_noted = False
        self._question = ""
        self.last_turn_id = ""
        self.last_trace: dict = {}
        self._ratio = 1.0
        self._overhead = 2500
        self._estimate = 0
        self._caps: dict = {}

    def reset(self) -> None:
        from ollamapilot.sessions import MANAGER

        MANAGER.stop_all()
        self.transcript = []
        self.chat_grants = set()
        self.refused = set()
        self.approved_plan = set()
        self.plan_id = ""
        self._batch_skip = set()
        self._batch_denied = set()
        self.used_groups = set()
        self.workspace = None
        self.tasks = []
        self._images = []
        self._image_note = ""
        self._audios = []
        self._audio_note = ""
        self._turn_successes = []
        self._code_paths = []
        self._verify_noted = False
        self._tasks_noted = False
        self._question = ""
        self.last_turn_id = ""
        self._ratio = 1.0

    def turn(
        self,
        text: str,
        attachments: list[dict],
        on_event: Callable[[str, Any], None],
        approve: Callable[[dict], Any],
        cancel: Callable[[], bool],
    ) -> str:
        question = text.strip()
        if not question:
            raise ValueError("Digite uma mensagem.")
        if len(question) > 20000:
            raise ValueError("A mensagem passa de 20.000 caracteres.")
        if not self.settings.model.strip():
            raise ValueError("Escolha um modelo. Se a lista estiver vazia, confira o servidor em Configurações.")
        started = time.perf_counter()
        trace: dict[str, Any] = {
            "modelo": self.settings.model,
            "nuvem": bool(self.settings.model_remote),
            "num_ctx": int(self.settings.num_ctx),
            "think": self.settings.think,
            "passos": [],
            "rodadas": 0,
            "chamadas_modelo": 0,
            "intervencoes": 0,
            "redundantes": 0,
            "erros_ferramenta": 0,
            "contexto_reduzido": 0,
            "prompt_eval_count": 0,
            "eval_count": 0,
        }

        def emit(kind: str, data: Any) -> None:
            if kind == "trace" and isinstance(data, dict):
                step = {key: data.get(key) for key in ("tool", "status", "ms", "autorizacao") if data.get(key) is not None}
                if data.get("status") == "error":
                    step["erro"] = str(data.get("resumo") or "")[:200]
                trace["passos"].append(step)
            on_event(kind, data)

        outcome = "erro"
        error_text = ""
        try:
            answer = self._turn(question, attachments, emit, approve, cancel, trace)
            outcome = trace.get("desfecho") or "respondido"
            return answer
        except Cancelled:
            outcome = "cancelado"
            raise
        except Exception as error:
            error_text = str(error)
            raise
        finally:
            undo.CURRENT_TURN = ""
            self._images = []
            self._image_note = ""
            for message in self.transcript:
                if message.get("captura"):
                    message.pop("images", None)
            self.transcript = repair_transcript(self.transcript)
            trace["desfecho"] = outcome
            if error_text:
                trace["erro"] = error_text[:300]
            trace["duracao_ms"] = int((time.perf_counter() - started) * 1000)
            self.last_trace = trace
            if getattr(self.settings, "trace_enabled", True):
                log_turn(trace)

    def _turn(self, question, attachments, on_event, approve, cancel, trace) -> str:
        self._caps = self._capabilities()
        tools_ok = not (self._caps.get("conhecido") and not self._caps.get("ferramentas"))
        vision_ok = not (self._caps.get("conhecido") and not self._caps.get("visao"))
        trace["ferramentas_ativas"] = tools_ok
        content = self._with_attachments(question, attachments)
        images = [item["base64"] for item in attachments if item.get("base64")]
        if images and not vision_ok:
            names = ", ".join(str(item.get("name")) for item in attachments if item.get("base64"))
            content += f"\n\n[Imagens não enviadas: {names}. O modelo {self.settings.model} não aceita imagem.]"
            on_event("trace", {"tool": "anexos", "status": "denied", "ms": 0, "resumo": f"Imagem não enviada: o modelo não tem visão ({names})"})
            images = []
        audios = [item["audio"] for item in attachments if item.get("audio")]
        audio_ok = bool(self._caps.get("conhecido") and self._caps.get("audio"))
        if audios and not audio_ok:
            names = ", ".join(str(item.get("name")) for item in attachments if item.get("audio"))
            content += (
                f"\n\n[Áudio não enviado: {names}. O modelo {self.settings.model} não declara a capacidade de áudio, "
                "então não há transcrição.]"
            )
            audios = []
        if not tools_ok:
            on_event(
                "trace",
                {"tool": "modelo", "status": "denied", "ms": 0, "resumo": "O modelo não declara ferramentas; a resposta será só texto."},
            )
        turn_id = secrets.token_hex(4)
        undo.CURRENT_TURN = turn_id
        self.last_turn_id = turn_id
        user: dict[str, Any] = {"role": "user", "content": content, "turno": turn_id}
        if images:
            user["images"] = images
        if audios:
            user["audios"] = audios
        self.transcript = repair_transcript(self.transcript)
        self.transcript.append(user)
        self._question = question
        self._adopt_workspace()
        self._turn_successes = []
        self._code_paths = []
        self._verify_noted = False
        self._tasks_noted = False
        executed: dict[str, dict] = {}
        rounds = 0
        nudges = 0
        stalled = 0
        limit = max(1, int(self.settings.max_tool_rounds))
        while rounds < limit:
            if cancel():
                raise Cancelled()
            on_event("status", "Consultando o modelo…")
            registry = build_registry(self.settings, self.client, cancel, self, self._groups()) if tools_ok else Registry([])
            reply = self._chat(registry, on_event, cancel, trace)
            if cancel() or reply.get("cancelled"):
                raise Cancelled()
            calls, legacy = planned_calls(reply, registry)
            if not calls:
                answer = (reply.get("content") or "").strip()
                if not answer and rounds == 0:
                    raise AgentError(
                        "O modelo não produziu uma resposta.",
                        modelo=self.settings.model,
                        num_ctx=self.settings.num_ctx,
                        mensagens=len(self.transcript),
                        conteudo=len(reply.get("content") or ""),
                        raciocinio=len(reply.get("thinking") or ""),
                        ferramentas=len(reply.get("tool_calls") or []),
                        metricas=reply.get("metrics") or {},
                    )
                if not answer:
                    answer = self._force_answer(registry, on_event, cancel, trace, _FINAL_NOW)
                    self._trim(on_event, trace)
                    return answer
                if tools_ok and asks_to_write(question) and not _any_success(executed) and dodges_write(answer):
                    on_event("content_reset", "")
                    if nudges < 2:
                        nudges += 1
                        self.transcript.append({"role": "user", "content": _WRITE_NUDGE, "interno": True})
                        rounds += 1
                        continue
                    answer = _WRITE_GAVE_UP
                if self._note_pending_tasks():
                    rounds += 1
                    continue
                self.transcript.append({"role": "assistant", "content": answer})
                on_event("content_reset", "")
                on_event("content", answer)
                on_event("metrics", reply.get("metrics") or {})
                self._trim(on_event, trace)
                return answer
            raw = str(reply.get("content") or "")
            visible, hid_calls = strip_tool_text(raw, registry)
            on_event("content_reset", "")
            stored = visible if hid_calls else (visible or raw)
            assistant: dict[str, Any] = {"role": "assistant", "content": stored or "Ferramentas acionadas."}
            if not legacy and reply.get("tool_calls"):
                assistant["tool_calls"] = reply["tool_calls"]
            self.transcript.append(assistant)
            done = 0
            statuses: list[str] = []
            try:
                self._prepare_batch(calls, registry, approve, on_event, cancel, trace)
                for index, call in enumerate(calls):
                    if cancel():
                        raise Cancelled()
                    if index >= MAX_CALLS_PER_ROUND:
                        status, result = "ignored", {
                            "erro": f"Limite de {MAX_CALLS_PER_ROUND} ferramentas por resposta. Peça o restante na próxima rodada."
                        }
                    else:
                        status, result = self._handle(call, registry, approve, on_event, cancel, executed, trace)
                    if status == "cancel":
                        raise Cancelled()
                    if status in ("error", "invalid", "unknown"):
                        result = {**(result or {}), "orientacao": _ON_ERROR}
                    self._append_result(call["name"], result or {}, legacy)
                    done += 1
                    statuses.append(status)
            except BaseException:
                self._abandon(calls, done, legacy)
                raise
            if self._images:
                note = self._image_note or "[Captura de tela devolvida por capturar_tela. É um resultado, não um pedido novo.]"
                self.transcript.append(
                    {
                        "role": "user",
                        "content": note,
                        "images": list(self._images),
                        "interno": True,
                        "captura": True,
                    }
                )
                self._images = []
                self._image_note = ""
            if self._audios:
                note = self._audio_note or "[Áudio devolvido pela ferramenta, no campo audios.]"
                self.transcript.append(
                    {"role": "user", "content": note, "audios": list(self._audios), "interno": True}
                )
                self._audios = []
                self._audio_note = ""
            self._note_code_changes()
            rounds += 1
            trace["rodadas"] = rounds
            # Sucesso zera a estagnação. Repetir a mesma chamada conta em dobro,
            # para o turno encerrar antes de gastar todas as rodadas no mesmo erro.
            if any(status == "success" for status in statuses):
                stalled = 0
            elif statuses and all(status in ("duplicate", "repeat_denied") for status in statuses):
                stalled += 2
            else:
                stalled += 1
            if stalled >= STALL_LIMIT:
                trace["desfecho"] = "sem_progresso"
                closing = self._force_answer(registry, on_event, cancel, trace, self._stall_note())
                self._trim(on_event, trace)
                return closing
        trace["desfecho"] = "limite_rodadas"
        reason = (
            f"Limite de {limit} rodadas de ferramentas atingido. Não chame ferramentas. "
            "Resuma para o usuário o que foi feito, o que falta e o próximo passo."
        )
        closing = self._force_answer(build_registry(self.settings, self.client, cancel, self), on_event, cancel, trace, reason)
        self._trim(on_event, trace)
        return closing

    def _handle(self, call, registry, approve, on_event, cancel, executed, trace) -> tuple[str, dict | None]:
        name = call["name"]
        if not call["known"]:
            available = ", ".join(registry.names()) or "nenhuma"
            on_event("trace", {"tool": name, "status": "error", "ms": 0, "resumo": "Ferramenta inexistente"})
            trace["erros_ferramenta"] += 1
            return "unknown", {"erro": f"A ferramenta {name} não existe. Disponíveis: {available}."}
        if call.get("invalid"):
            on_event("trace", {"tool": name, "status": "error", "ms": 0, "resumo": call["invalid"]})
            trace["erros_ferramenta"] += 1
            return "invalid", {"erro": f"{call['invalid']} Envie os argumentos como um objeto JSON."}
        tool = registry.get(name)
        self.used_groups.add(getattr(tool, "group", "arquivos"))
        arguments, problems = validate_arguments(tool.parameters, call["arguments"])
        signature = name + ":" + json.dumps(arguments, ensure_ascii=False, sort_keys=True, default=str)
        if signature in self.refused:
            on_event("trace", {"tool": name, "status": "denied", "ms": 0, "resumo": "Já recusado", "autorizacao": "recusada antes"})
            trace["redundantes"] += 1
            return "repeat_denied", {"recusado": True, "erro": "O usuário já recusou esta ação nesta conversa. Não repita."}
        previous = None if getattr(tool, "repeatable", False) else executed.get(signature)
        if previous is not None:
            trace["redundantes"] += 1
            on_event("trace", {"tool": name, "status": "denied", "ms": 0, "resumo": "Chamada repetida"})
            if previous["status"] == "success":
                return "duplicate", {"repetida": True, "erro": "Esta ação já foi feita nesta mensagem. Use o resultado anterior."}
            return "duplicate", {
                "repetida": True,
                "erro": f"Esta mesma chamada já falhou nesta mensagem: {previous['resumo']} Não repita; corrija os argumentos ou explique ao usuário.",
            }
        if problems:
            message = f"Argumentos inválidos para {name}: " + "; ".join(problems) + "."
            executed[signature] = {"status": "invalid", "resumo": message, "sensivel": tool.sensitive}
            on_event("trace", {"tool": name, "status": "error", "ms": 0, "resumo": message})
            trace["erros_ferramenta"] += 1
            properties = tool.parameters.get("properties") or {}
            return "invalid", {
                "erro": message,
                "obrigatorios": list(tool.parameters.get("required") or []),
                "parametros": list(properties),
            }
        if signature in self._batch_denied:
            status, result, authorization = (
                "denied",
                {"recusado": True, "erro": "O usuário recusou o lote de alterações. Não repita estas ações."},
                "recusada",
            )
            on_event("trace", {"tool": name, "status": "denied", "ms": 0, "resumo": "Lote recusado", "autorizacao": "recusada"})
        else:
            status, result, authorization = self._execute(
                name, tool, arguments, approve, on_event, cancel, trace, signature in self._batch_skip
            )
        if status == "cancel":
            return "cancel", None
        if isinstance(result, dict) and "_imagem" in result:
            image = result.pop("_imagem")
            note = result.pop("_imagem_nota", "")
            if status == "success":
                if isinstance(image, str) and image:
                    self._images.append(image)
                elif isinstance(image, list):
                    self._images.extend(item for item in image if isinstance(item, str) and item)
                if isinstance(note, str) and note.strip():
                    self._image_note = (self._image_note + " " + note.strip()).strip()
        if isinstance(result, dict) and "_audio" in result:
            audio = result.pop("_audio")
            note = result.pop("_audio_nota", "")
            if status == "success" and isinstance(audio, str) and audio:
                self._audios.append(audio)
                if isinstance(note, str) and note.strip():
                    self._audio_note = (self._audio_note + " " + note.strip()).strip()
        summary = _summary(result or {})
        executed[signature] = {"status": status, "resumo": summary, "sensivel": tool.sensitive}
        if status == "success":
            self._turn_successes.append(_success_label(name, arguments, result or {}))
            changed = _changed_code(self, name, arguments)
            if changed is not None:
                self._code_paths.append(changed)
        if status == "denied":
            self.refused.add(signature)
        if status == "success" and tool.sensitive:
            # Depois de uma alteração, ler de novo o mesmo arquivo ou janela é um passo novo.
            for key in [key for key, value in executed.items() if not value["sensivel"]]:
                executed.pop(key, None)
        if status == "error":
            trace["erros_ferramenta"] += 1
        return status, result

    def _groups(self) -> set[str] | None:
        """Grupos de ferramentas desta rodada: os citados nos dois últimos pedidos e os já usados na conversa."""
        if str(getattr(self.settings, "tool_selection", "auto")) == "todas":
            return None
        texts = []
        for message in reversed(self.transcript):
            if message.get("role") == "user" and not is_internal(message):
                texts.append(str(message.get("content") or "").split(_ATTACHMENTS_MARKER, 1)[0])
                if len(texts) == 2:
                    break
        return groups_for(" ".join(texts)) | self.used_groups

    def _rules_text(self) -> str:
        for message in reversed(self.transcript):
            if message.get("role") == "user" and not is_internal(message):
                return str(message.get("content") or "").split(_ATTACHMENTS_MARKER, 1)[0]
        return ""

    def plan_required(self) -> bool:
        """Dois ou mais caminhos absolutos no último pedido bloqueiam gravação até propor_plano.

        A contagem não distingue leitura de escrita: citar o executável e o arquivo de saída já exige o plano.
        """
        for message in reversed(self.transcript):
            if message.get("role") != "user" or message.get("interno"):
                continue
            found = set()
            for match in _ABS_PATH.finditer(str(message.get("content") or "")):
                found.add(match.group(0).rstrip("\\.,);").lower())
            return len(found) >= 2
        return False

    def _prepare_batch(self, calls, registry, approve, on_event, cancel, trace) -> None:
        """Uma resposta com duas ou mais alterações pede um cartão só."""
        self._batch_skip = set()
        self._batch_denied = set()
        batch = []
        for item in _unique_mutations(calls, registry):
            if item["signature"] in self.refused:
                continue
            try:
                item["preview"] = item["tool"].preview(item["arguments"])
            except Exception:
                # Uma prévia que falha deixa a ação de fora: ela segue sozinha e devolve o erro ao modelo.
                continue
            batch.append(item)
        if len(batch) < 2:
            return
        sections = []
        room = 7600 // len(batch)
        for number, item in enumerate(batch, 1):
            detail = str(item["preview"].get("detail") or item["preview"].get("summary") or "")
            if len(detail) > room:
                detail = detail[:room] + "\n…"
            sections.append(f"{number}. {item['preview'].get('title') or item['name']}\n{detail}")
        preview = {
            "title": "Várias alterações",
            "summary": f"{len(batch)} ações: " + ", ".join(Path(path).name for item in batch for path in item["paths"])[:200],
            "detail": (
                "Se autorizar, estas ações seguem sem um cartão para cada uma.\n\n" + "\n\n".join(sections)
            ),
            "escopo": "as alterações listadas nesta resposta",
            "ferramenta": "lote",
        }
        batch_paths = []
        for item in batch:
            try:
                batch_paths.extend(item["tool"].touched_paths(item["arguments"]))
            except Exception:
                continue
        if self.workspace is None:
            preview["raiz"] = ""
            preview["dentro"] = None
            where = "Pasta de trabalho: (nenhuma nesta conversa)."
        else:
            inside_all = all(inside(self.workspace, path) for path in batch_paths) if batch_paths else True
            preview["raiz"] = str(self.workspace)
            preview["dentro"] = inside_all
            place = "dentro da pasta de trabalho" if inside_all else "fora da pasta de trabalho"
            where = f"Pasta de trabalho: {self.workspace}\nAlvo: {place}."
        preview["detail"] = where + "\n\n" + str(preview.get("detail") or "")
        on_event("status", "Aguardando autorização…")
        trace["intervencoes"] += 1
        decision = _decision_scope(approve(preview))
        if cancel():
            raise Cancelled()
        if decision == "deny":
            self._batch_denied = {item["signature"] for item in batch}
            return
        self._batch_skip = {item["signature"] for item in batch}
        if decision == "chat":
            for item in batch:
                self.chat_grants.add(grant_scope(self, item["tool"], item["arguments"]))
        self.approved_plan.update(path.lower() for item in batch for path in item["paths"])
        if not self.plan_id:
            self.plan_id = secrets.token_hex(4)

    def _execute(self, name, tool, arguments, approve, on_event, cancel, trace, skip_approval: bool = False) -> tuple[str, dict | None, str]:
        blocked = _mutation_blocked(self, tool, arguments)
        if blocked:
            on_event("trace", {"tool": name, "status": "error", "ms": 0, "resumo": blocked[:180]})
            return "error", {"erro": blocked}, "plano"
        missing_root = workspace_block(self, tool, arguments)
        if missing_root:
            on_event("trace", {"tool": name, "status": "error", "ms": 0, "resumo": missing_root[:180]})
            return "error", {"erro": missing_root}, "pasta"
        try:
            needs = tool.needs_approval(arguments)
        except Exception as error:
            on_event("trace", {"tool": name, "status": "error", "ms": 0, "resumo": str(error)})
            return "error", {"erro": str(error)}, "não avaliada"
        scope_key = grant_scope(self, tool, arguments)
        covered = bool(needs and scope_key in self.chat_grants)
        authorization = "não precisa" if not needs else ("neste chat" if covered else ("lote" if skip_approval else ""))
        if needs and not covered and not skip_approval:
            try:
                preview = tool.preview(arguments)
            except Exception as error:
                on_event("trace", {"tool": name, "status": "error", "ms": 0, "resumo": str(error)})
                return "error", {"erro": str(error)}, "prévia falhou"
            detail = str(preview.get("detail") or "")
            if len(detail) > 8000:
                preview["detail"] = detail[:8000] + "\n…"
            preview = annotate_preview(self, preview, tool, arguments)
            preview["ferramenta"] = name
            preview["escopo"] = getattr(tool, "scope_label", "") or _scope_label(scope_key.split(":", 1)[0])
            on_event("status", "Aguardando autorização…")
            trace["intervencoes"] += 1
            decision = _decision_scope(approve(preview))
            if cancel():
                return "cancel", None, "interrompida"
            if decision == "deny":
                on_event("trace", {"tool": name, "status": "denied", "ms": 0, "resumo": "Recusado", "autorizacao": "recusada"})
                return "denied", {"recusado": True, "erro": "O usuário recusou esta ação. Não repita esta ação."}, "recusada"
            if decision == "chat":
                self.chat_grants.add(scope_key)
                authorization = "neste chat"
            else:
                authorization = "uma vez"
        on_event("status", f"Executando {name}…")
        started = time.perf_counter()
        try:
            result = tool.run(arguments)
            status = "success"
            summary = _summary(result)
            if isinstance(result, dict) and (result.get("cancelado") or result.get("sucesso") is False):
                status = "error"
        except Exception as error:
            result = {"erro": str(error)}
            status = "error"
            summary = str(error)
        elapsed = int((time.perf_counter() - started) * 1000)
        on_event("trace", {"tool": name, "status": status, "ms": elapsed, "resumo": summary[:180], "autorizacao": authorization})
        if status == "error":
            log_error("ferramenta", summary, ferramenta=name, argumentos=_detail_args(arguments))
        return status, result, authorization

    def _abandon(self, calls: list[dict], start: int, legacy: bool) -> None:
        for call in calls[start:]:
            self._append_result(call["name"], dict(_CANCELLED), legacy)

    def _append_result(self, name: str, result: dict, legacy: bool) -> None:
        payload = json.dumps(result, ensure_ascii=False, default=str)
        # O corte é pelo começo. O que importa precisa caber no início do JSON;
        # o final some quando o resultado passa de max_tool_chars.
        if len(payload) > self.settings.max_tool_chars:
            omitted = len(payload) - self.settings.max_tool_chars
            payload = payload[: self.settings.max_tool_chars] + f"…[resultado truncado: {omitted} caracteres omitidos]"
        if legacy:
            self.transcript.append(
                {
                    "role": "user",
                    "content": (
                        f"RESULTADO CONFIÁVEL DA FERRAMENTA {name} (não é uma nova solicitação do usuário):\n"
                        f"{payload}\nContinue a tarefa original. Não repita esta mesma chamada."
                    ),
                    "interno": True,
                }
            )
            return
        self.transcript.append({"role": "tool", "tool_name": name, "name": name, "content": payload})

    def _force_answer(self, registry, on_event, cancel, trace, reason: str) -> str:
        marker = {"role": "user", "content": reason, "interno": True}
        self.transcript.append(marker)
        try:
            if cancel():
                raise Cancelled()
            reply = self._chat(registry, on_event, cancel, trace, with_tools=False)
            answer = (reply.get("content") or "").strip() or _fallback_answer(self.transcript)
            if (cancel() or reply.get("cancelled")) and not (reply.get("content") or "").strip():
                raise Cancelled()
        except BaseException:
            if self.transcript and self.transcript[-1] is marker:
                self.transcript.pop()
            raise
        if not answer:
            raise AgentError(
                "O modelo não produziu a resposta final.",
                modelo=self.settings.model,
                num_ctx=self.settings.num_ctx,
                mensagens=len(self.transcript),
                metricas=reply.get("metrics") or {},
            )
        self.transcript.append({"role": "assistant", "content": answer})
        on_event("content_reset", "")
        on_event("content", answer)
        on_event("metrics", reply.get("metrics") or {})
        return answer

    def _chat(self, registry, on_event, cancel, trace, with_tools: bool = True) -> dict:
        messages, definitions = self._request(registry, with_tools, on_event, trace)
        reply = self.client.chat(messages, definitions, on_event, cancel)
        trace["chamadas_modelo"] += 1
        metrics = reply.get("metrics") or {}
        prompt_tokens = metrics.get("prompt_eval_count")
        if isinstance(prompt_tokens, int) and prompt_tokens > 0:
            trace["prompt_eval_count"] = prompt_tokens
            if self._estimate > 0:
                observed = prompt_tokens / self._estimate
                # Só corrige para cima: com cache de prompt o servidor pode contar menos do que enviou.
                if observed > self._ratio:
                    self._ratio = min(2.5, observed)
        if isinstance(metrics.get("eval_count"), int):
            trace["eval_count"] += metrics["eval_count"]
        return reply

    def _request(self, registry, with_tools: bool = True, on_event=None, trace=None) -> tuple[list[dict], list[dict]]:
        names = registry.names() if with_tools else []
        hints = registry.hints() if with_tools else []
        rules = project_rules(self._rules_text()) if with_tools else ""
        shown = set(names)
        hidden = [name for name in registry.all_names() if name not in shown] if with_tools and hasattr(registry, "all_names") else []
        root = str(self.workspace) if self.workspace else ""
        prompt = redact_known(build_prompt(self.settings, names, hints, rules, hidden, workspace=root, tasks=self.tasks))
        definitions = registry.definitions() if with_tools else []
        overhead = _tokens([{"content": prompt}]) + len(json.dumps(definitions, ensure_ascii=False)) // 4
        self._overhead = overhead
        fitted, report = fit_with_report(self._visible(), self._context_limit(), overhead=overhead, ratio=self._ratio)
        if (report["resumidas"] or report["reduzidas"]) and trace is not None:
            trace["contexto_reduzido"] = max(trace["contexto_reduzido"], report["resumidas"] + report["reduzidas"])
            if on_event is not None:
                on_event("status", _fit_message(report))
        self._estimate = int((overhead + estimate_tokens(fitted)) * 1.0)
        return [{"role": "system", "content": prompt}, *[_outgoing(item) for item in fitted]], definitions

    def _context_limit(self) -> int:
        from ollamapilot.config import effective_num_ctx

        return effective_num_ctx(self.settings, self._caps, persist=True)

    def _adopt_workspace(self) -> None:
        if self.workspace:
            return
        for message in self.transcript:
            if message.get("role") != "user" or is_internal(message):
                continue
            found = directory_in_text(str(message.get("content") or "").split(_ATTACHMENTS_MARKER, 1)[0])
            if found is not None:
                self.workspace = found
                return

    def _note_code_changes(self) -> None:
        if self._verify_noted or not self._code_paths or self.workspace is None:
            return
        note = verify_note(self._question, self.workspace, self._code_paths[-1])
        self.transcript.append({"role": "user", "content": note, "interno": True})
        self._verify_noted = True
        self._code_paths = []

    def _note_pending_tasks(self) -> bool:
        if self._tasks_noted:
            return False
        pending = [item for item in self.tasks if isinstance(item, dict) and item.get("estado") != "feito" and item.get("texto")]
        if not pending:
            return False
        lines = ["Ainda há tarefas pendentes nesta conversa:"]
        lines.extend(f"- {item['texto']} (pendente)" for item in pending)
        lines.append("Marque cada item ao concluir. Se ainda houver rodada de ferramenta, execute o próximo pendente antes de encerrar.")
        self.transcript.append({"role": "user", "content": "\n".join(lines), "interno": True})
        self._tasks_noted = True
        return True

    def _stall_note(self) -> str:
        lines = [_STALLED, "Nesta mensagem, o que terminou com sucesso:"]
        if self._turn_successes:
            lines.extend(f"- {item}" for item in self._turn_successes)
        else:
            lines.append("- nenhuma ferramenta terminou com sucesso")
        lines.append("Chamada repetida não apaga esse sucesso.")
        pending = [item.get("texto") for item in self.tasks if isinstance(item, dict) and item.get("estado") != "feito" and item.get("texto")]
        if pending and not self._tasks_noted:
            lines.append("Tarefas pendentes: " + "; ".join(str(item) for item in pending))
            self._tasks_noted = True
        return "\n".join(lines)

    def _capabilities(self) -> dict:
        probe = getattr(self.client, "capabilities", None)
        if not callable(probe):
            return {}
        try:
            caps = probe()
        except Exception:
            return {}
        return caps if isinstance(caps, dict) else {}

    def _visible(self) -> list[dict]:
        messages = [dict(item) for item in self.transcript]
        current = request_index(messages)
        for index, message in enumerate(messages):
            if index != current and not _current_capture(message, index, current):
                message.pop("images", None)
                _shrink_old_attachment(message)
        return messages

    def _trim(self, on_event=None, trace=None) -> None:
        fitted, report = fit_with_report(
            repair_transcript(self.transcript), self._context_limit(), overhead=self._overhead, ratio=self._ratio
        )
        self.transcript = fitted
        if report["resumidas"] and on_event is not None:
            on_event(
                "trace",
                {"tool": "contexto", "status": "success", "ms": 0, "resumo": _fit_message(report)},
            )

    @staticmethod
    def _with_attachments(question: str, attachments: list[dict]) -> str:
        blocks = []
        images = []
        for item in attachments:
            if item.get("text"):
                blocks.append(f"ARQUIVO: {item.get('name')} ({describe_attachment(item)})\n{item['text']}")
            elif item.get("base64"):
                images.append(str(item.get("name")))
        text = question
        if images:
            text += "\n\nIMAGENS ANEXADAS: " + ", ".join(images)
        if not blocks:
            return text
        return text + f"\n\n{_ATTACHMENTS_MARKER}\n" + "\n\n---\n\n".join(blocks)


def _any_success(executed: dict[str, dict]) -> bool:
    return any(value.get("status") == "success" for value in executed.values())


def _success_label(name: str, arguments: dict, result: dict) -> str:
    target = ""
    if name == "executar_programa":
        args = arguments.get("argumentos") or []
        extra = " ".join(str(item) for item in args) if isinstance(args, list) else ""
        target = (str(arguments.get("programa") or "") + " " + extra).strip()
    else:
        for key in ("caminho", "destino", "script", "url", "programa", "comando"):
            if arguments.get(key):
                target = str(arguments.get(key))
                break
        if not target and isinstance(result, dict) and result.get("caminho"):
            target = str(result["caminho"])
    return f"{name} {target}".strip() + ": sucesso"


def _changed_code(agent: Agent, name: str, arguments: dict) -> Path | None:
    if name not in {"gravar_arquivo", "editar_arquivo", "aplicar_patch"}:
        return None
    if agent.workspace is None:
        return None
    raw = arguments.get("caminho")
    if not raw:
        return None
    try:
        from ollamapilot.tools.paths import resolve_path

        path = resolve_path(raw)
    except ValueError:
        return None
    from ollamapilot.project_index import is_code_file

    if not is_code_file(path) or not inside(agent.workspace, path):
        return None
    return path


_ABS_PATH = re.compile(r"[A-Za-z]:\\[^\s\"'<>|?*\n]+")

_GROUP_WORDS = {
    "janelas": (
        "janela", "tela", "clique", "clicar", "clica", "botão", "botao", "calculadora", "bloco de notas", "notepad",
        "interface", "digite", "digitar", "captura", "screenshot", "aplicativo", "abra o", "abrir o",
    ),
    "reverso": (
        "ghidra", "binário", "binario", "executável", "executavel", ".exe", ".dll", "engenharia reversa",
        "descompilar", "descompilação", "sala limpa", "desmontar", "assembly",
    ),
    "web": (
        "http", "https", "www.", "site", "página", "pagina", "url", "internet", "web",
        "pesquisar", "pesquisa", "pesquise", "api", "endpoint", "requisição", "requisicao",
    ),
    "banco": ("banco de dados", "banco", "sql", "tabela", "postgres", "postgresql", "select"),
    "biblioteca": ("biblioteca", "documento", "rag", "pdf", "docx", "xlsx", "planilha", "download", "relatório", "relatorio"),
    "projeto": (
        "projeto", "repositório", "repositorio", "codebase", "índice", "indice", "indexar", "indexe",
        "mapa do", "símbolo", "simbolo", "função", "funcao",
    ),
    "terminal": (
        "powershell", "terminal", "console", "prompt de comando", "cmd", "shell", "pipe", "cmdlet", "sessão", "sessao",
        "interativo", "interativa", "repl", "stdin", "servidor", "npm", "pip", "git", "teste", "compilar", "compile", "build",
    ),
}


def groups_for(text: str) -> set[str]:
    # has_word exige a palavra inteira: "git" no meio de outro termo não liga o grupo terminal.
    return {group for group, words in _GROUP_WORDS.items() if any(has_word(text, word) for word in words)}


def _mutation_blocked(agent: Agent, tool, arguments: dict) -> str | None:
    if not getattr(tool, "mutates", False) or not agent.plan_required():
        return None
    if not agent.approved_plan:
        return (
            "Este pedido altera mais de um arquivo. Chame propor_plano com o objetivo e a lista de caminhos "
            "antes de gravar, editar, aplicar patch ou excluir."
        )
    try:
        paths = [str(path).lower() for path in tool.touched_paths(arguments)]
    except Exception:
        paths = []
    outside = [path for path in paths if path not in agent.approved_plan]
    if outside:
        return (
            "Estes caminhos não estão no plano aprovado: "
            + ", ".join(outside)
            + ". Atualize o plano com propor_plano."
        )
    return None


def _unique_mutations(calls, registry) -> list[dict]:
    seen = set()
    items = []
    for call in calls:
        if not call.get("known") or call.get("invalid"):
            continue
        try:
            tool = registry.get(call["name"])
        except Exception:
            continue
        if not getattr(tool, "mutates", False):
            continue
        arguments, problems = validate_arguments(tool.parameters, call.get("arguments") or {})
        if problems:
            continue
        signature = call["name"] + ":" + json.dumps(arguments, ensure_ascii=False, sort_keys=True, default=str)
        if signature in seen:
            continue
        seen.add(signature)
        try:
            paths = [str(path) for path in tool.touched_paths(arguments)]
        except Exception:
            paths = []
        items.append({"name": call["name"], "signature": signature, "paths": paths, "tool": tool, "arguments": arguments})
    return items


def _scope_label(scope_key: str) -> str:
    labels = {
        "arquivos": "gravar, editar, criar pasta e desfazer arquivos",
        "biblioteca": "gerar arquivos na biblioteca interna",
        "executar_programa": "executar qualquer programa",
        "acionar_janela": "agir sobre qualquer janela",
        "excluir": "excluir arquivos e pastas",
        "ghidra_analisar": "analisar arquivos com o Ghidra",
    }
    return labels.get(scope_key, scope_key)


def _fit_message(report: dict) -> str:
    parts = []
    if report.get("resumidas"):
        parts.append(f"{report['resumidas']} mensagens antigas resumidas")
    if report.get("reduzidas"):
        parts.append(f"{report['reduzidas']} trechos reduzidos")
    return "Contexto ajustado ao limite do modelo: " + ", ".join(parts) + "."


def extract_calls(reply: dict, registry) -> tuple[list[dict], bool]:
    native = []
    for call in reply.get("tool_calls") or []:
        function = call.get("function") or call
        name = str(function.get("name") or "")
        arguments = _arguments(function.get("arguments"))
        if registry.has(name):
            native.append({"name": name, "arguments": arguments})
    if native:
        return native, False
    return _calls_in_text(str(reply.get("content") or ""), registry), True


def _arguments(value) -> dict:
    return _parse_arguments(value)[0]


def _parse_arguments(value) -> tuple[dict, str | None]:
    if value is None:
        return {}, None
    if isinstance(value, dict):
        return value, None
    if isinstance(value, str):
        if not value.strip():
            return {}, None
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}, "Os argumentos não são um JSON válido."
        if isinstance(parsed, dict):
            return parsed, None
        return {}, "Os argumentos precisam ser um objeto JSON, não uma lista ou valor solto."
    return {}, "Os argumentos precisam ser um objeto JSON."


def _calls_in_text(content: str, registry) -> list[dict]:
    return [call for _start, _end, call in _tool_spans(content, registry)]


def _tool_spans(content: str, registry) -> list[tuple[int, int, dict]]:
    spans = []
    length = len(content)
    start = 0
    while start < length:
        if content[start] != "{":
            start += 1
            continue
        depth = 0
        in_string = False
        escape = False
        for end in range(start, length):
            char = content[end]
            if in_string:
                if escape:
                    escape = False
                elif char == "\\":
                    escape = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
                continue
            if char == "{":
                depth += 1
            if char != "}":
                continue
            depth -= 1
            if depth != 0:
                continue
            try:
                parsed = json.loads(content[start : end + 1])
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, dict) and registry.has(str(parsed.get("name") or "")) and "arguments" in parsed:
                arguments = parsed.get("arguments")
                spans.append(
                    (
                        start,
                        end + 1,
                        {
                            "name": str(parsed["name"]),
                            "arguments": arguments if isinstance(arguments, dict) else {},
                        },
                    )
                )
                start = end
            elif isinstance(parsed, dict):
                start = end
            break
        # Um "{" sem par ou um trecho que não é JSON não encerra a busca: a chamada pode vir dentro ou depois.
        start += 1
    return spans


def _fallback_answer(transcript: list[dict]) -> str:
    for message in reversed(transcript):
        content = str(message.get("content") or "").strip()
        legacy = message.get("role") == "user" and content.startswith("RESULTADO CONFIÁVEL")
        if message.get("role") != "tool" and not legacy:
            continue
        name = str(message.get("tool_name") or message.get("name") or "ferramenta")
        if not content:
            continue
        if len(content) > 2000:
            content = content[:2000] + "…"
        return f"A ferramenta {name} terminou, e o modelo não escreveu a frase final. Resultado:\n{content}"
    return ""


def _summary(result: dict) -> str:
    if not isinstance(result, dict):
        return str(result)
    if result.get("erro"):
        return str(result["erro"])
    if result.get("recusado"):
        return "Recusado"
    for key in ("caminho", "mensagem", "programa", "janela", "url"):
        if result.get(key):
            return str(result[key])
    return "Concluído"
