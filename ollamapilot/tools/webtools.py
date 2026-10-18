from __future__ import annotations

import html
import ipaddress
import re
import socket
from urllib.parse import urlparse

import httpx

from ollamapilot.config import Settings
from ollamapilot.ollama_client import OllamaClient
from ollamapilot.tools.base import Tool, schema, text_prop, tool


class CloudTool(Tool):
    group = "web"

    @classmethod
    def available(cls, ctx) -> bool:
        settings = ctx.settings
        return bool(getattr(settings, "web_enabled", False) and str(getattr(settings, "web_api_key", "") or "").strip())


@tool
class WebSearch(CloudTool):
    name = "pesquisar_web"
    order = 260
    description = (
        "Pesquisa na web pela API da Ollama (a consulta sai desta máquina para ollama.com). Use para fatos recentes ou externos. "
        "Retorno: {fonte, resultados: [{titulo, url, trecho}]}, até 5. Cite só títulos e URLs devolvidos."
    )
    parameters = schema({"consulta": text_prop("Texto da pesquisa, curto e específico.")}, ["consulta"])

    def __init__(self, client: OllamaClient) -> None:
        self.client = client

    def run(self, arguments: dict) -> dict:
        query = str(arguments.get("consulta") or "").strip()
        if not query:
            raise ValueError("Informe a consulta.")
        data = self.client.web_search(query, 5)
        results = data.get("results") if isinstance(data, dict) else None
        if not isinstance(results, list):
            return {"resultados": [], "bruto": str(data)[:2000]}
        items = []
        for item in results[:5]:
            if not isinstance(item, dict):
                continue
            items.append(
                {
                    "titulo": item.get("title") or "",
                    "url": item.get("url") or "",
                    "trecho": str(item.get("content") or "")[:1200],
                }
            )
        return {"fonte": "ollama.com/api/web_search", "resultados": items}


@tool
class WebFetch(CloudTool):
    name = "ler_pagina_web"
    order = 270
    description = (
        "Lê uma página pública pela API da Ollama (a URL vai para ollama.com). Para endereço desta máquina ou da rede local, use ler_site. "
        "Retorno: {url, titulo, conteudo (até 16.000 caracteres), truncated}."
    )
    parameters = schema({"url": text_prop("URL http ou https pública.")}, ["url"])

    def __init__(self, client: OllamaClient) -> None:
        self.client = client

    def run(self, arguments: dict) -> dict:
        url = _public_url(str(arguments.get("url") or ""))
        data = self.client.web_fetch(url)
        if not isinstance(data, dict):
            text = str(data)
            return {"url": url, "conteudo": text[:8000], "truncated": len(text) > 8000}
        content = str(data.get("content") or "")
        return {"url": url, "titulo": data.get("title") or "", "conteudo": content[:16000], "truncated": len(content) > 16000}


@tool
class ReadSite(Tool):
    name = "ler_site"
    group = "leitura"
    order = 160
    description = (
        "Baixa uma página HTML ou texto direto desta máquina e devolve o texto sem tags. "
        "Endereço local, localhost ou IP privado só é lido depois da autorização. Lê até 2 MB; não lê arquivos binários. "
        "Retorno: {url, status, conteudo (até 16.000 caracteres), truncated}."
    )
    parameters = schema({"url": text_prop("URL http ou https completa, como http://127.0.0.1:8080/.")}, ["url"])

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def needs_approval(self, arguments: dict) -> bool:
        url = str(arguments.get("url") or "")
        return is_local_url(url) or resolves_local(url)

    def preview(self, arguments: dict) -> dict:
        url = str(arguments.get("url") or "").strip()
        return {
            "title": "Ler endereço local",
            "summary": url,
            "detail": f"O OllamaPilot vai baixar o texto de:\n{url}",
        }

    def run(self, arguments: dict) -> dict:
        url = _http_url(str(arguments.get("url") or ""))
        current = url
        try:
            with httpx.Client(timeout=20, follow_redirects=False) as client:
                for _ in range(5):
                    with client.stream("GET", current) as response:
                        if response.is_redirect:
                            target = str(response.headers.get("location") or "")
                            if not target:
                                break
                            current = str(httpx.URL(current).join(target))
                            if (is_local_url(current) or resolves_local(current)) and not is_local_url(url):
                                return {
                                    "local": True,
                                    "url": current,
                                    "mensagem": "A página redirecionou para um endereço local. Chame ler_site de novo com essa URL e aguarde a autorização.",
                                }
                            continue
                        response.raise_for_status()
                        kind = str(response.headers.get("content-type") or "").lower()
                        if kind and not any(part in kind for part in _TEXT_TYPES):
                            raise ValueError(f"O endereço devolveu {kind.split(';')[0]}, que não é texto. Nada foi lido.")
                        body, cut = _read_capped(response, MAX_SITE_BYTES)
                        encoding = response.encoding or "utf-8"
                        page = body.decode(encoding, errors="replace")
                    text = _html_to_text(page) if "html" in kind or "<html" in page[:2000].lower() else page.strip()
                    return {
                        "url": str(response.url),
                        "status": response.status_code,
                        "truncated": cut or len(text) > 16000,
                        "conteudo": text[:16000],
                    }
        except httpx.HTTPError as error:
            raise ValueError(f"Não foi possível ler a página: {error}") from error
        raise ValueError("A página fez redirecionamentos demais.")


_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD")
_SECRET_HEADER = re.compile(r"\$\{cofre:([A-Za-z0-9_-]{1,40})\}")


@tool
class HttpRequest(Tool):
    name = "requisicao_http"
    group = "leitura"
    order = 165
    repeatable = True
    sensitive = True
    grant_key = "requisicao_http"
    scope_label = "fazer requisições HTTP"
    description = (
        "Faz uma requisição HTTP (GET, POST, PUT, PATCH, DELETE, HEAD) a uma API, local ou pública, com cabeçalhos e corpo. "
        "Pede autorização. ${cofre:nome} num cabeçalho lê do cofre do Windows. "
        "Retorno: {status, tipo, corpo (até 16.000 caracteres), truncated}."
    )
    parameters = schema(
        {
            "metodo": text_prop("GET, POST, PUT, PATCH, DELETE ou HEAD."),
            "url": text_prop("URL http ou https completa."),
            "cabecalhos": {"type": "object", "description": "Opcional. Como {\"Authorization\": \"Bearer ${cofre:api}\"}."},
            "json": {"type": "object", "description": "Opcional. Corpo enviado como JSON."},
            "corpo": text_prop("Opcional. Corpo em texto, quando não for JSON."),
            "timeout_segundos": {"type": "integer", "description": "Opcional. Até 120. Padrão 30."},
        },
        ["url"],
    )

    def _request(self, arguments: dict) -> tuple[str, str, dict, object, str | None]:
        method = str(arguments.get("metodo") or "GET").strip().upper()
        if method not in _METHODS:
            raise ValueError("Método inválido. Use GET, POST, PUT, PATCH, DELETE ou HEAD.")
        url = _http_url(str(arguments.get("url") or ""))
        headers = arguments.get("cabecalhos") or {}
        if isinstance(headers, str):
            import json

            try:
                headers = json.loads(headers)
            except json.JSONDecodeError as error:
                raise ValueError("cabecalhos precisa ser um objeto JSON.") from error
        if not isinstance(headers, dict) or len(headers) > 30:
            raise ValueError("cabecalhos precisa ser um objeto com até 30 itens.")
        clean = {str(key): str(value) for key, value in headers.items() if "\n" not in f"{key}{value}"}
        body = arguments.get("corpo")
        return method, url, clean, arguments.get("json"), None if body in (None, "") else str(body)

    def preview(self, arguments: dict) -> dict:
        method, url, headers, payload, body = self._request(arguments)
        lines = [f"{method} {url}"]
        lines += [f"{key}: {value}" for key, value in headers.items()]
        if payload is not None:
            import json

            lines.append("\n" + json.dumps(payload, ensure_ascii=False, indent=2)[:3000])
        elif body:
            lines.append("\n" + body[:3000])
        return {"title": "Requisição HTTP", "summary": f"{method} {url}", "detail": "\n".join(lines)}

    def run(self, arguments: dict) -> dict:
        method, url, headers, payload, body = self._request(arguments)
        resolved = {key: _resolve_header(value) for key, value in headers.items()}
        try:
            timeout = min(max(int(arguments.get("timeout_segundos") or 30), 1), 120)
        except (TypeError, ValueError):
            timeout = 30
        try:
            with httpx.Client(timeout=timeout, follow_redirects=False) as client:
                kwargs: dict = {"headers": resolved}
                if payload is not None:
                    kwargs["json"] = payload
                elif body is not None:
                    kwargs["content"] = body.encode("utf-8")
                with client.stream(method, url, **kwargs) as response:
                    data, cut = _read_capped(response, MAX_SITE_BYTES)
                    kind = str(response.headers.get("content-type") or "")
                    status = response.status_code
                    location = response.headers.get("location")
        except httpx.HTTPError as error:
            raise ValueError(f"A requisição falhou: {error}") from error
        result: dict = {"url": url, "status": status, "tipo": kind.split(";")[0]}
        if location:
            result["redireciona_para"] = location
        if kind and not any(part in kind.lower() for part in _TEXT_TYPES):
            result["corpo"] = f"(conteúdo binário de {len(data)} bytes, não mostrado)"
            result["truncated"] = cut
            return result
        text = data.decode("utf-8", errors="replace")
        result["corpo"] = text[:16000]
        result["truncated"] = cut or len(text) > 16000
        result["mensagem"] = f"{method} {url} → {status}"
        return result


def _resolve_header(value: str) -> str:
    def replace(match) -> str:
        from ollamapilot.secrets import current

        secret = current().get(match.group(1))
        if not secret:
            raise ValueError(f"O segredo '{match.group(1)}' não está no cofre do Windows.")
        return secret

    return _SECRET_HEADER.sub(replace, value)


MAX_SITE_BYTES = 2 * 1024 * 1024
_TEXT_TYPES = ("text/", "html", "json", "xml", "javascript")


def _read_capped(response: httpx.Response, limit: int) -> tuple[bytes, bool]:
    parts: list[bytes] = []
    total = 0
    for chunk in response.iter_bytes():
        parts.append(chunk)
        total += len(chunk)
        if total > limit:
            return b"".join(parts)[:limit], True
    return b"".join(parts), False


def resolves_local(url: str) -> bool:
    """Confere o IP real do nome. Um domínio público pode apontar para a rede interna."""
    host = (urlparse(url).hostname or "").strip("[]")
    if not host:
        return False
    try:
        ipaddress.ip_address(host)
        return False
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    for info in infos:
        try:
            address = ipaddress.ip_address(info[4][0].split("%")[0])
        except ValueError:
            continue
        if address.is_private or address.is_loopback or address.is_link_local or address.is_reserved:
            return True
    return False


def is_local_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").strip("[]").lower()
    if not host or host == "localhost" or host.endswith(".local") or host.endswith(".internal"):
        return True
    if "." not in host and ":" not in host:
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_private or address.is_loopback or address.is_link_local or address.is_reserved


def _http_url(url: str) -> str:
    url = url.strip()
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Informe uma URL http ou https completa.")
    return url


def _public_url(url: str) -> str:
    url = _http_url(url)
    if is_local_url(url):
        raise ValueError("Esse endereço é local. Use ler_site.")
    return url


def _html_to_text(page: str) -> str:
    cleaned = re.sub(r"(?is)<script.*?>.*?</script>", " ", page)
    cleaned = re.sub(r"(?is)<style.*?>.*?</style>", " ", cleaned)
    cleaned = re.sub(r"(?s)<[^>]+>", " ", cleaned)
    cleaned = html.unescape(cleaned)
    cleaned = re.sub(r"[ \t]+", " ", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()
