from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

import httpx

from ollamapilot.config import Settings


class OllamaError(RuntimeError):
    pass


class _RetryWithoutUsage(Exception):
    """O servidor recusou stream_options; a mesma chamada vai de novo sem ele."""


def _image_kind(data: str) -> str:
    for prefix, kind in (("iVBOR", "png"), ("/9j/", "jpeg"), ("R0lG", "gif"), ("UklG", "webp")):
        if data.startswith(prefix):
            return kind
    return "png"


def to_openai_messages(messages: list[dict]) -> list[dict]:
    """Converte a conversa no formato do Ollama para o da OpenAI, com ids ligando cada chamada ao resultado."""
    output: list[dict] = []
    pending: list[str] = []
    for message in messages:
        role = message.get("role")
        content = str(message.get("content") or "")
        if role == "assistant":
            item: dict[str, Any] = {"role": "assistant", "content": content}
            calls = message.get("tool_calls") or []
            if calls:
                converted = []
                pending = []
                for index, call in enumerate(calls):
                    function = call.get("function") if isinstance(call.get("function"), dict) else call
                    ident = str(call.get("id") or f"call_{len(output)}_{index}")
                    arguments = function.get("arguments")
                    if not isinstance(arguments, str):
                        arguments = json.dumps(arguments or {}, ensure_ascii=False)
                    converted.append(
                        {"id": ident, "type": "function", "function": {"name": str(function.get("name") or ""), "arguments": arguments}}
                    )
                    pending.append(ident)
                item["tool_calls"] = converted
            output.append(item)
        elif role == "tool":
            if pending:
                output.append({"role": "tool", "tool_call_id": pending.pop(0), "content": content})
            else:
                name = message.get("tool_name") or message.get("name") or "ferramenta"
                output.append({"role": "user", "content": f"Resultado de {name}:\n{content}"})
        elif role == "user" and message.get("images"):
            parts: list[dict] = [{"type": "text", "text": content}]
            for image in message["images"]:
                parts.append({"type": "image_url", "image_url": {"url": f"data:image/{_image_kind(image)};base64,{image}"}})
            output.append({"role": "user", "content": parts})
        else:
            output.append({"role": role or "user", "content": content})
    return output


CLOUD_ROOT = "https://ollama.com"


def describe_balance(payload: dict, model: str, remote: bool) -> str:
    """Traduz o saldo da conta. A API não separa crédito por modelo nem lista a cota gratuita."""
    name = model.strip() or "nenhum"
    if remote:
        lines = [
            f"Modelo selecionado: {name}, online.",
            "O saldo abaixo é da conta. A API não reserva um crédito só para este modelo.",
        ]
    else:
        lines = [
            f"Modelo selecionado: {name}, neste computador.",
            "Este modelo não gasta o crédito da nuvem.",
        ]
    included = payload.get("included") if isinstance(payload.get("included"), dict) else {}
    purchased = payload.get("purchased") if isinstance(payload.get("purchased"), dict) else {}
    if "balance_usd" in included and "allowance_usd" in included:
        balance = float(included["balance_usd"])
        allowance = float(included["allowance_usd"])
        used = 0.0 if allowance <= 0 else max(0.0, (allowance - balance) / allowance * 100)
        lines.append(f"Crédito incluído: US$ {_money(balance)} de US$ {_money(allowance)}.")
        lines.append(f"Usado: {_percent(used)}.")
        period = included.get("period") if isinstance(included.get("period"), dict) else {}
        if period.get("until"):
            lines.append(f"Recarrega em {_remaining(str(period['until']))}.")
    else:
        for label, key in (("Sessão", "session"), ("Semana", "weekly")):
            block = included.get(key)
            if isinstance(block, dict) and "remaining_percent" in block:
                lines.append(f"{label}: resta {_percent(float(block['remaining_percent']))}.")
                if block.get("resets_at"):
                    lines.append(f"Recarrega em {_remaining(str(block['resets_at']))}.")
    if not any(line.startswith(("Crédito incluído", "Sessão", "Semana")) for line in lines):
        lines.append("A resposta não trouxe o crédito incluído.")
    if "balance_usd" in purchased:
        lines.append(f"Crédito comprado: US$ {_money(float(purchased['balance_usd']))}.")
    lines.append(
        "A API não informa quais modelos aceitam o crédito gratuito. Essa lista só aparece na página da conta, no site do Ollama."
    )
    return "\n".join(lines)


def _chat_refusal(status: int, detail: str) -> str:
    try:
        message = str(json.loads(detail).get("error") or detail)
    except (json.JSONDecodeError, AttributeError):
        message = detail
    folded = message.lower()
    if "does not support tools" in folded:
        return "Este modelo não aceita ferramentas. Escolha outro modelo para ler ou gravar arquivos."
    if "does not support thinking" in folded:
        return "Este modelo não tem raciocínio separado. Em Configurações, deixe Raciocínio em auto."
    if "does not support" in folded and ("image" in folded or "vision" in folded):
        return "Este modelo não aceita imagem. Remova o anexo ou escolha um modelo com visão."
    if status == 401 or status == 403:
        return f"O servidor recusou a autorização ({status}). Confira a API Key em Configurações."
    if status == 404 and "not found" in folded:
        return f"O modelo não foi encontrado no servidor: {message[:300]}"
    return f"Ollama recusou o chat ({status}): {message[:500]}"


def _money(value: float) -> str:
    return f"{value:.2f}".replace(".", ",")


def _percent(value: float) -> str:
    shown = f"{value:.1f}".replace(".", ",")
    return shown + "%"


def _remaining(text: str) -> str:
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    days = max(0, int((moment - datetime.now(timezone.utc)).total_seconds() // 86400))
    when = moment.astimezone().strftime("%d/%m/%Y")
    if days >= 14:
        return f"{days // 7} semanas ({when})"
    if days == 1:
        return f"1 dia ({when})"
    return f"{days} dias ({when})"


def parse_capabilities(data: dict) -> dict:
    """Lê as capacidades declaradas em /api/show. Sem a lista, nada é presumido."""
    caps = data.get("capabilities") if isinstance(data, dict) else None
    info = data.get("model_info") if isinstance(data, dict) else None
    context = None
    if isinstance(info, dict):
        for key, value in info.items():
            if str(key).endswith(".context_length") and isinstance(value, int) and value > 0:
                context = value
                break
    if not isinstance(caps, list):
        return {"conhecido": False, "contexto": context, "fonte": "/api/show sem a lista de capacidades"}
    names = {str(item).lower() for item in caps}
    return {
        "conhecido": True,
        "ferramentas": "tools" in names,
        "visao": "vision" in names,
        "raciocinio": "thinking" in names,
        "embedding": "embedding" in names,
        "audio": "audio" in names,
        "contexto": context,
        "fonte": "/api/show",
    }


def describe_capabilities(caps: dict | None, remote: bool = False) -> str:
    if not caps or not caps.get("conhecido"):
        base = "Capacidades não informadas"
        if remote:
            base += " (modelo online; os metadados não são consultados)"
        if caps and caps.get("contexto"):
            base += f" · contexto {_kilo(caps['contexto'])}"
        return base
    parts = []
    parts.append("ferramentas" if caps.get("ferramentas") else "sem ferramentas")
    parts.append("visão" if caps.get("visao") else "sem visão")
    if caps.get("raciocinio"):
        parts.append("raciocínio")
    if caps.get("audio"):
        parts.append("áudio")
    if caps.get("contexto"):
        parts.append(f"contexto {_kilo(caps['contexto'])}")
    return " · ".join(parts)


def _kilo(value: int) -> str:
    return f"{value // 1024}k" if value >= 1024 else str(value)


class OllamaClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._caps: dict[tuple, dict] = {}

    def capabilities(self, name: str | None = None, remote: bool | None = None, timeout: float = 5) -> dict:
        """Capacidades do modelo. Modelo online não é consultado, para não gastar a conta."""
        name = (name if name is not None else self.settings.model).strip()
        remote = self.settings.model_remote if remote is None else remote
        if not name:
            return {"conhecido": False, "fonte": "nenhum modelo escolhido"}
        if self.openai_mode():
            return {"conhecido": False, "fonte": "servidor compatível com OpenAI"}
        key = (bool(remote), self._base(), name)
        if key in self._caps:
            return self._caps[key]
        if remote:
            caps = {"conhecido": False, "fonte": "modelo online"}
            self._caps[key] = caps
            return caps
        try:
            data = self._post("/api/show", {"model": name}, timeout=timeout)
        except OllamaError as error:
            return {"conhecido": False, "fonte": "falha ao consultar", "erro": str(error)[:200]}
        caps = parse_capabilities(data)
        self._caps[key] = caps
        return caps

    def cached_capabilities(self) -> dict | None:
        return self._caps.get((bool(self.settings.model_remote), self._base(), self.settings.model.strip()))

    def _base(self) -> str:
        return self.settings.ollama_url.rstrip("/")

    def version(self) -> dict:
        try:
            response = httpx.get(self._base() + "/api/version", timeout=5)
            response.raise_for_status()
        except httpx.HTTPError as error:
            raise OllamaError(f"Não foi possível falar com o Ollama: {error}") from error
        return response.json()

    def tags(self) -> list[dict]:
        response = self._get("/api/tags")
        models = response.get("models") or []
        return models if isinstance(models, list) else []

    def show(self, name: str) -> dict:
        return self._post("/api/show", {"model": name}, timeout=60)

    def pull(self, name: str, on_progress: Callable[[str], None] | None = None) -> None:
        self._stream("/api/pull", {"model": name, "stream": True}, on_progress, timeout=None)

    def delete(self, name: str) -> None:
        try:
            response = httpx.request("DELETE", self._base() + "/api/delete", json={"model": name}, timeout=60)
            response.raise_for_status()
        except httpx.HTTPError as error:
            raise OllamaError(self._message(error)) from error

    def copy(self, source: str, destination: str) -> None:
        self._post("/api/copy", {"source": source, "destination": destination}, timeout=60)

    def create_model(self, name: str, base: str, system: str, on_progress: Callable[[str], None] | None = None) -> None:
        payload = {"model": name, "from": base, "stream": True}
        if system.strip():
            payload["system"] = system.strip()
        self._stream("/api/create", payload, on_progress, timeout=None)

    def load_model(self, name: str, keep_alive: str | int) -> None:
        self._post("/api/generate", {"model": name, "keep_alive": keep_alive, "prompt": ""}, timeout=120)

    def embed(self, texts: list[str]) -> list[list[float]]:
        model = self.settings.embed_model.strip() or "embeddinggemma"
        data = self._post("/api/embed", {"model": model, "input": texts}, timeout=120)
        vectors = data.get("embeddings")
        if not isinstance(vectors, list):
            raise OllamaError("O Ollama não devolveu embeddings.")
        return vectors

    def chat(
        self,
        messages: list[dict],
        tools: list[dict],
        on_event: Callable[[str, Any], None],
        cancel: Callable[[], bool],
    ) -> dict:
        if self.openai_mode():
            return self._chat_openai(messages, tools, on_event, cancel)
        caps = self.cached_capabilities() or {}
        options = self.settings.generation_options(remote=self.settings.model_remote)
        from ollamapilot.config import effective_num_ctx

        options["num_ctx"] = effective_num_ctx(self.settings, caps, persist=True)
        payload: dict[str, Any] = {
            "model": self.settings.model,
            "messages": messages,
            "stream": True,
            "options": options,
            "keep_alive": self.settings.keep_alive,
        }
        if tools:
            payload["tools"] = tools
        think = (self.settings.think or "auto").strip().lower()
        if caps.get("conhecido") and not caps.get("raciocinio") and think != "false":
            think = "auto"
        if think == "true":
            payload["think"] = True
        elif think == "false":
            payload["think"] = False
        elif think in {"low", "medium", "high"}:
            payload["think"] = think
        fmt = (self.settings.response_format or "text").strip().lower()
        if fmt == "json":
            payload["format"] = "json"
        elif fmt == "schema" and self.settings.format_schema.strip():
            try:
                payload["format"] = json.loads(self.settings.format_schema)
            except json.JSONDecodeError as error:
                raise OllamaError(f"O JSON Schema está inválido: {error}") from error

        content: list[str] = []
        thinking: list[str] = []
        tool_calls: list[dict] = []
        metrics: dict[str, Any] = {}
        model_name = self.settings.model
        url, headers = self.chat_target()
        idle = max(30, int(getattr(self.settings, "chat_idle_timeout", 600) or 600))
        stop = threading.Event()
        cancelled = False
        try:
            with httpx.Client(timeout=httpx.Timeout(10, read=idle)) as client:
                with client.stream("POST", url, json=payload, headers=headers) as response:

                    def watch() -> None:
                        nonlocal cancelled
                        while not stop.wait(0.2):
                            if cancel():
                                cancelled = True
                                response.close()
                                return

                    watcher = threading.Thread(target=watch, daemon=True)
                    watcher.start()
                    if response.status_code >= 400:
                        detail = response.read().decode("utf-8", errors="replace")
                        raise OllamaError(_chat_refusal(response.status_code, detail))
                    for line in response.iter_lines():
                        if cancel():
                            cancelled = True
                            break
                        if not line:
                            continue
                        try:
                            data = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if data.get("error"):
                            raise OllamaError(str(data["error"]))
                        message = data.get("message") or {}
                        piece = message.get("content") or ""
                        thought = message.get("thinking") or ""
                        if piece:
                            content.append(piece)
                        if thought:
                            thinking.append(thought)
                            on_event("thinking", thought)
                        if message.get("tool_calls"):
                            tool_calls.extend(message["tool_calls"])
                        if data.get("model"):
                            model_name = data["model"]
                        if data.get("done"):
                            metrics = {
                                "model": model_name,
                                "total_duration": data.get("total_duration"),
                                "eval_count": data.get("eval_count"),
                                "eval_duration": data.get("eval_duration"),
                                "prompt_eval_count": data.get("prompt_eval_count"),
                                "prompt_eval_duration": data.get("prompt_eval_duration"),
                                "done_reason": data.get("done_reason"),
                            }
        except OllamaError:
            raise
        except httpx.ReadTimeout as error:
            raise OllamaError(f"O Ollama ficou {idle} s sem enviar nada e a resposta foi abandonada.") from error
        except (httpx.HTTPError, httpx.StreamError, RuntimeError) as error:
            if not cancelled:
                raise OllamaError(f"A conexão com o Ollama caiu: {error}") from error
        finally:
            stop.set()
        return {
            "role": "assistant",
            "content": "".join(content),
            "thinking": "".join(thinking),
            "tool_calls": tool_calls,
            "metrics": metrics,
            "cancelled": cancelled,
        }

    def openai_mode(self) -> bool:
        return str(getattr(self.settings, "provider", "ollama")) == "openai"

    def _openai_base(self) -> str:
        return str(getattr(self.settings, "openai_url", "") or "").strip().rstrip("/")

    def _openai_headers(self) -> dict:
        key = str(getattr(self.settings, "openai_api_key", "") or "").strip()
        return {"Authorization": f"Bearer {key}"} if key else {}

    def openai_models(self) -> list[dict]:
        base = self._openai_base()
        if not base.startswith(("http://", "https://")):
            raise OllamaError("Informe a URL do servidor compatível com OpenAI em Configurações, como http://127.0.0.1:1234/v1.")
        try:
            response = httpx.get(base + "/models", headers=self._openai_headers(), timeout=15)
            response.raise_for_status()
        except httpx.HTTPError as error:
            raise OllamaError(f"O servidor compatível com OpenAI não respondeu: {self._message(error)}") from error
        data = response.json()
        items = data.get("data") if isinstance(data, dict) else data
        models = []
        for item in items or []:
            name = item.get("id") if isinstance(item, dict) else item
            if name:
                models.append({"name": str(name)})
        return models

    def _chat_openai(self, messages, tools, on_event, cancel) -> dict:
        base = self._openai_base()
        if not base.startswith(("http://", "https://")):
            raise OllamaError("Informe a URL do servidor compatível com OpenAI em Configurações.")
        payload: dict[str, Any] = {
            "model": self.settings.model,
            "messages": to_openai_messages(messages),
            "stream": True,
            "stream_options": {"include_usage": True},
            "temperature": float(self.settings.temperature),
            "top_p": float(self.settings.top_p),
        }
        if tools:
            payload["tools"] = tools
        if int(self.settings.num_predict) > 0:
            payload["max_tokens"] = int(self.settings.num_predict)
        if self.settings.seed is not None and str(self.settings.seed) != "":
            payload["seed"] = int(self.settings.seed)
        fmt = (self.settings.response_format or "text").strip().lower()
        if fmt == "json":
            payload["response_format"] = {"type": "json_object"}
        elif fmt == "schema" and self.settings.format_schema.strip():
            try:
                schema = json.loads(self.settings.format_schema)
            except json.JSONDecodeError as error:
                raise OllamaError(f"O JSON Schema está inválido: {error}") from error
            payload["response_format"] = {"type": "json_schema", "json_schema": {"name": "resposta", "schema": schema}}
        try:
            return self._stream_openai(base + "/chat/completions", payload, on_event, cancel)
        except _RetryWithoutUsage:
            payload.pop("stream_options", None)
            return self._stream_openai(base + "/chat/completions", payload, on_event, cancel)

    def _stream_openai(self, url: str, payload: dict, on_event, cancel) -> dict:
        content: list[str] = []
        thinking: list[str] = []
        calls: dict[int, dict] = {}
        metrics: dict[str, Any] = {"model": self.settings.model}
        idle = max(30, int(getattr(self.settings, "chat_idle_timeout", 600) or 600))
        stop = threading.Event()
        cancelled = False
        started = time.perf_counter()
        try:
            with httpx.Client(timeout=httpx.Timeout(10, read=idle)) as client:
                with client.stream("POST", url, json=payload, headers=self._openai_headers()) as response:

                    def watch() -> None:
                        nonlocal cancelled
                        while not stop.wait(0.2):
                            if cancel():
                                cancelled = True
                                response.close()
                                return

                    threading.Thread(target=watch, daemon=True).start()
                    if response.status_code >= 400:
                        detail = response.read().decode("utf-8", errors="replace")
                        if response.status_code == 400 and "stream_options" in detail and "stream_options" in payload:
                            raise _RetryWithoutUsage()
                        raise OllamaError(_chat_refusal(response.status_code, detail))
                    for line in response.iter_lines():
                        if cancel():
                            cancelled = True
                            break
                        line = line.strip()
                        if not line.startswith("data:"):
                            continue
                        body = line[5:].strip()
                        if body == "[DONE]":
                            break
                        try:
                            data = json.loads(body)
                        except json.JSONDecodeError:
                            continue
                        if data.get("error"):
                            error = data["error"]
                            raise OllamaError(str(error.get("message") if isinstance(error, dict) else error))
                        if data.get("model"):
                            metrics["model"] = data["model"]
                        usage = data.get("usage")
                        if isinstance(usage, dict):
                            metrics["prompt_eval_count"] = usage.get("prompt_tokens")
                            metrics["eval_count"] = usage.get("completion_tokens")
                        for choice in data.get("choices") or []:
                            delta = choice.get("delta") or {}
                            piece = delta.get("content")
                            if piece:
                                content.append(piece)
                            thought = delta.get("reasoning_content") or delta.get("reasoning")
                            if isinstance(thought, str) and thought:
                                thinking.append(thought)
                                on_event("thinking", thought)
                            for call in delta.get("tool_calls") or []:
                                slot = calls.setdefault(int(call.get("index") or 0), {"name": "", "arguments": ""})
                                function = call.get("function") or {}
                                if function.get("name"):
                                    slot["name"] += function["name"]
                                if function.get("arguments"):
                                    slot["arguments"] += function["arguments"]
                            if choice.get("finish_reason"):
                                metrics["done_reason"] = choice["finish_reason"]
        except (OllamaError, _RetryWithoutUsage):
            raise
        except httpx.ReadTimeout as error:
            raise OllamaError(f"O servidor ficou {idle} s sem enviar nada e a resposta foi abandonada.") from error
        except (httpx.HTTPError, httpx.StreamError, RuntimeError) as error:
            if not cancelled:
                raise OllamaError(f"A conexão com o servidor caiu: {error}") from error
        finally:
            stop.set()
        metrics["total_duration"] = int((time.perf_counter() - started) * 1e9)
        tool_calls = []
        for index in sorted(calls):
            slot = calls[index]
            raw = slot["arguments"].strip()
            try:
                arguments: Any = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                arguments = raw
            tool_calls.append({"function": {"name": slot["name"], "arguments": arguments}})
        return {
            "role": "assistant",
            "content": "".join(content),
            "thinking": "".join(thinking),
            "tool_calls": tool_calls,
            "metrics": metrics,
            "cancelled": cancelled,
        }

    def chat_target(self) -> tuple[str, dict]:
        if self.settings.model_remote:
            if self.settings.model_source != "online":
                raise OllamaError("Os modelos online estão desligados em Configurações. Escolha um modelo local.")
            key = self.settings.cloud_api_key.strip()
            if not key:
                raise OllamaError("Informe a API Key em Configurações para consultar um modelo online.")
            return CLOUD_ROOT + "/api/chat", {"Authorization": f"Bearer {key}"}
        return self._base() + "/api/chat", {}

    def cloud_tags(self) -> list[dict]:
        key = self.settings.cloud_api_key.strip()
        if not key:
            return []
        try:
            response = httpx.get(
                CLOUD_ROOT + "/api/tags",
                headers={"Authorization": f"Bearer {key}"},
                timeout=20,
            )
            response.raise_for_status()
        except httpx.HTTPError as error:
            raise OllamaError(self._message(error)) from error
        models = response.json().get("models") or []
        return models if isinstance(models, list) else []

    def cloud_balance(self) -> dict:
        key = self.settings.cloud_api_key.strip()
        if not key:
            raise OllamaError("Informe a API Key em Configurações para consultar o saldo.")
        try:
            response = httpx.get(
                CLOUD_ROOT + "/api/balance",
                headers={"Authorization": f"Bearer {key}"},
                timeout=20,
            )
            response.raise_for_status()
        except httpx.HTTPError as error:
            raise OllamaError(self._message(error)) from error
        data = response.json()
        if not isinstance(data, dict):
            raise OllamaError("A Ollama não devolveu o saldo.")
        return data

    def available_models(self) -> dict:
        self._caps.clear()
        payload: dict[str, Any] = {
            "version": {},
            "models": [],
            "remote": [],
            "local_error": "",
            "remote_error": "",
        }
        if self.openai_mode():
            try:
                payload["models"] = self.openai_models()
            except OllamaError as error:
                payload["local_error"] = str(error)
            return payload
        try:
            payload["version"] = self.version()
            payload["models"] = self.tags()
        except OllamaError as error:
            payload["local_error"] = str(error)
        if self.settings.online_models():
            try:
                payload["remote"] = self.cloud_tags()
            except OllamaError as error:
                payload["remote_error"] = str(error)
        return payload

    def web_search(self, query: str, max_results: int = 5) -> dict:
        return self._cloud("/api/web_search", {"query": query, "max_results": max_results})

    def web_fetch(self, url: str) -> dict:
        return self._cloud("/api/web_fetch", {"url": url})

    def _cloud(self, path: str, payload: dict) -> dict:
        key = self.settings.web_api_key.strip()
        if not self.settings.web_enabled or not key:
            raise OllamaError("A pesquisa web está desligada. Informe a chave em Configurações.")
        try:
            response = httpx.post(
                "https://ollama.com" + path,
                json=payload,
                headers={"Authorization": f"Bearer {key}"},
                timeout=40,
            )
            response.raise_for_status()
        except httpx.HTTPError as error:
            raise OllamaError(self._message(error)) from error
        return response.json()

    def _get(self, path: str) -> dict:
        try:
            response = httpx.get(self._base() + path, timeout=20)
            response.raise_for_status()
        except httpx.HTTPError as error:
            raise OllamaError(self._message(error)) from error
        return response.json()

    def _post(self, path: str, payload: dict, timeout: float | None) -> dict:
        try:
            response = httpx.post(self._base() + path, json=payload, timeout=timeout)
            response.raise_for_status()
        except httpx.HTTPError as error:
            raise OllamaError(self._message(error)) from error
        if not response.content:
            return {}
        return response.json()

    def _stream(self, path: str, payload: dict, on_progress: Callable[[str], None] | None, timeout: float | None) -> None:
        try:
            with httpx.Client(timeout=httpx.Timeout(10, read=timeout)) as client:
                with client.stream("POST", self._base() + path, json=payload) as response:
                    if response.status_code >= 400:
                        detail = response.read().decode("utf-8", errors="replace")
                        raise OllamaError(f"Ollama recusou a operação ({response.status_code}): {detail[:500]}")
                    for line in response.iter_lines():
                        if not line:
                            continue
                        try:
                            data = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if data.get("error"):
                            raise OllamaError(str(data["error"]))
                        if on_progress is not None:
                            status = str(data.get("status") or "")
                            total = data.get("total") or 0
                            completed = data.get("completed") or 0
                            if total:
                                on_progress(f"{status} {int(completed * 100 / total)}%")
                            elif status:
                                on_progress(status)
        except OllamaError:
            raise
        except httpx.HTTPError as error:
            raise OllamaError(self._message(error)) from error

    @staticmethod
    def _message(error: httpx.HTTPError) -> str:
        response = getattr(error, "response", None)
        if response is not None:
            try:
                body = response.json()
            except Exception:
                body = {}
            if isinstance(body, dict) and body.get("error"):
                return str(body["error"])
            text = (response.text or "")[:400]
            if text:
                return text
        return str(error)
